"""Maintenance, reachable from the page instead of from a terminal.

Every endpoint here is a thin adapter over the same ``core`` or ``ingest``
function the matching CLI command already calls. That is deliberate: two
implementations of "rebuild" is how the two come to disagree, and the CLI stays
because it is how this gets tested and debugged.

The one genuinely new capability is the scheduled task. It lets an HTTP request
cause ``schtasks.exe`` to run, which is a real widening of what this surface can
do, and it is justified only because the alternative -- telling someone who does
not use a terminal to type a ``schtasks /Create /XML`` incantation -- is not a
feature at all. The mitigations are: the server binds to loopback, the
subprocess is invoked with an argv list and ``shell=False``, and the only
user-supplied value that reaches it is a time of day matched against
``^([01]\\d|2[0-3]):[0-5]\\d$`` before use. See ADR 0011.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from portfolio_tracker.api import deps
from portfolio_tracker.api.deps import get_db, get_writable_db
from portfolio_tracker.core import doctor, rebuild
from portfolio_tracker.db.connection import transaction
from portfolio_tracker.jobs import update as job
from portfolio_tracker.jobs import windows_task

router = APIRouter(tags=["maintenance"])


def _repo_root() -> Path:
    """The checkout, derived from this file rather than the cwd.

    A scheduled task has no meaningful working directory of its own, and the
    one recorded in the task definition has to be right on the machine the task
    will run on -- which is this one.
    """
    return Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    import json

    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "trigger": row["trigger"],
        "status": row["status"],
        "fx_rows": row["fx_rows"],
        "price_rows": row["price_rows"],
        "instruments": row["instruments"],
        "days_covered": row["days_covered"],
        "last_price_date": row["last_price_date"],
        "rebuilt": bool(row["rebuilt"]),
        "problems": json.loads(row["problems"]) if row["problems"] else [],
        "error": row["error"],
    }


@router.get("/api/updates")
def updates(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Whether the data is current, and what the recent runs did."""
    newest = job.newest_price(conn)
    rows = job.history(conn, limit=20)
    running = next((r for r in rows if r["status"] == "RUNNING"), None)
    last_fund_row = conn.execute("SELECT max(fetched_at) FROM stock_fundamental").fetchone()
    last_fund = str(last_fund_row[0]) if last_fund_row and last_fund_row[0] else None
    return {
        "last_price_date": newest.isoformat() if newest else None,
        "last_fundamentals_date": last_fund,
        "stale": job.is_stale(conn),
        "running": _run_row(running) if running is not None else None,
        "runs": [_run_row(r) for r in rows],
        "scheduler": windows_task.status(repo_root=_repo_root()).as_dict(),
    }


@router.post("/api/updates/run")
def run_update_now(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Start an update in the background and return immediately.

    Threaded with its own connection, because the request's connection is
    closed on the way out while a fetch is still running. The advisory lock in
    ``run_update`` makes this safe if the scheduled task fires at the same
    moment: one of them records SKIPPED and exits.
    """
    latest = job.last_run(conn)
    if latest is not None and latest["status"] == "RUNNING":
        raise HTTPException(409, "An update is already running.")

    db_path = deps.DB_PATH

    def work() -> None:
        job.run_update(db_path, trigger="MANUAL")

    threading.Thread(target=work, daemon=True, name="portfolio-update").start()
    return {
        "started": True,
        "note": "Fetching in the background. This page will show the result when it finishes.",
    }


# ---------------------------------------------------------------------------
# The scheduled task
# ---------------------------------------------------------------------------


class ScheduleRequest(BaseModel):
    time: str = Field(
        default=windows_task.DEFAULT_TIME,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="Local time of day, 24-hour",
    )


@router.get("/api/scheduler")
def scheduler() -> dict[str, Any]:
    return windows_task.status(repo_root=_repo_root()).as_dict()


@router.post("/api/scheduler")
def schedule(payload: ScheduleRequest) -> dict[str, Any]:
    """Ask Windows to run the update every day at the given time."""
    try:
        result = windows_task.register(
            repo_root=_repo_root(), db_path=deps.DB_PATH, at_time=payload.time
        )
    except windows_task.TaskError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result.as_dict()


@router.delete("/api/scheduler")
def unschedule() -> dict[str, Any]:
    try:
        return windows_task.unregister().as_dict()
    except windows_task.TaskError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/scheduler/run-now")
def scheduler_run_now() -> dict[str, Any]:
    """Trigger the registered task, which proves the registration itself works.

    Deliberately distinct from ``/api/updates/run``: that one runs the job
    in this process, which tells you nothing about whether Windows can.
    """
    try:
        windows_task.run_now()
    except windows_task.TaskError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"started": True, "note": "Windows has been asked to run the task now."}


# ---------------------------------------------------------------------------
# Recompute and check
# ---------------------------------------------------------------------------


class RebuildBody(BaseModel):
    end: date | None = None


@router.post("/api/admin/recompute")
def recompute(
    body: RebuildBody | None = None,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Throw away the derived layer and rebuild it from the ledger.

    Safe to run at any time and never destructive: lots, valuations, returns
    and tax state are pure functions of transactions, prices and rates. That
    is what makes "any bug in the derived layer is fixed by rebuilding, never
    by patching rows" operational rather than aspirational.
    """
    end = (body.end if body else None) or date.today()
    with transaction(conn):
        result = rebuild.run(conn, end=end, strict=False, commit=False)
    return {
        "as_of": end.isoformat(),
        "lots_created": result.lots_created,
        "disposals_created": result.disposals_created,
        "position_days": result.position_days,
        "cash_rows": result.cash_rows,
        "stale": result.stale,
        "oversold": result.oversold,
        "breaks": result.breaks,
        "ok": result.ok,
    }


@router.get("/api/admin/doctor")
def run_doctor(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """The integrity checks, grouped so the worst is read first."""
    report = doctor.run(conn)
    findings = [
        {
            "code": f.code,
            "severity": f.severity,
            "message": f.message,
            "detail": getattr(f, "detail", None),
        }
        for f in report.findings
    ]
    return {
        "checked_at": date.today().isoformat(),
        "errors": [f for f in findings if f["severity"] == "ERROR"],
        "warnings": [f for f in findings if f["severity"] == "WARNING"],
        "notes": [f for f in findings if f["severity"] not in ("ERROR", "WARNING")],
        "ok": not any(f["severity"] == "ERROR" for f in findings),
    }


@router.get("/api/accounts")
def accounts(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Every account, deposit terms included."""
    rows = conn.execute(
        "SELECT a.*, p.name AS person_name FROM account a "
        "JOIN person p ON p.id = a.person_id ORDER BY a.id"
    ).fetchall()
    return {
        "accounts": [
            {
                "id": r["id"],
                "name": r["name"],
                "person": r["person_name"],
                "kind": r["kind"],
                "currency": r["currency"],
                "broker": r["broker"],
                "active": bool(r["active"]),
            }
            for r in rows
        ]
    }
