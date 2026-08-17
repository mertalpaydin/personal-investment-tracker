"""Fetch whatever is missing, then recompute. The daily job's actual work.

Until this existed, prices arrived only when somebody typed ``pt fetch-prices``.
Nothing scheduled it, nothing noticed when it had not happened, and a rebuild
would happily re-value three-day-old prices across a longer date grid and
report a fresh-looking figure. The database this was written against was two to
three days stale, and there was no place on the page that said so.

Four properties matter here, and each is a decision rather than an accident:

**It catches up rather than fetching "yesterday".** The window starts from the
last stored observation, so a machine that was off for a fortnight resumes from
where the data stops. Asking for yesterday would leave a permanent hole.

**Only one runs at a time, and the second does not queue.** Two concurrent
updates would not corrupt anything -- they would fetch the same days twice and
rebuild twice, for nothing. The loser records why it did nothing and exits.

**One dead provider is a partial success, not a failure.** Thirteen of fourteen
instruments updating is a good day. Reporting it as FAILED would train the
reader to ignore the status.

**The rebuild is one transaction.** ``rebuild.run`` normally commits per phase,
which leaves a reader mid-run seeing ``position_daily`` emptied but not yet
refilled -- a page that 404s for a few seconds every evening. Under WAL, a
single transaction means readers keep the previous snapshot until the new one
lands whole.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from portfolio_tracker.core import fx as core_fx
from portfolio_tracker.core import locking, rebuild
from portfolio_tracker.core.calendars import business_days_between
from portfolio_tracker.db import migrate as migrations
from portfolio_tracker.db.connection import connect, transaction
from portfolio_tracker.ingest import fx as ingest_fx
from portfolio_tracker.ingest import prices as ingest_prices

log = logging.getLogger(__name__)

#: How far behind the last price may fall before the app calls itself stale.
#: One business day: prices arrive after the close, so during a trading day
#: yesterday's close is the newest thing that can exist.
STALE_AFTER_BUSINESS_DAYS = 1

#: Days of rate history to fetch for a currency with none stored. Ten years,
#: because a cost basis is frozen at its trade-date rate and a holding bought
#: long ago still needs the rate from the day it was bought.
FX_BACKFILL_DAYS = 3650

#: Days of stored rate history re-requested each run. The ECB revises nothing,
#: but a run that overlapped a weekend gap would otherwise skip it forever.
FX_OVERLAP_DAYS = 7


@dataclass
class UpdateOutcome:
    """What one run did. Mirrors the `update_run` row it writes."""

    run_id: int
    status: str = "RUNNING"
    trigger: str = "MANUAL"
    fx_rows: int = 0
    price_rows: int = 0
    instruments: int = 0
    days_covered: int | None = None
    last_price_date: date | None = None
    rebuilt: bool = False
    problems: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("OK", "PARTIAL")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def newest_price(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT max(date) AS d FROM price").fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def is_stale(conn: sqlite3.Connection, *, today: date | None = None) -> bool:
    """True when the newest price is more than a business day behind."""
    newest = newest_price(conn)
    if newest is None:
        # Nothing stored at all. An empty database is not stale, it is empty,
        # and kicking off a fetch for an instrument-less database is pointless.
        return bool(conn.execute("SELECT 1 FROM instrument LIMIT 1").fetchone())
    return business_days_between(newest, today or date.today()) > STALE_AFTER_BUSINESS_DAYS


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(  # type: ignore[no-any-return]
        "SELECT * FROM update_run ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()


def history(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM update_run ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()


def _open_run(conn: sqlite3.Connection, trigger: str) -> int:
    cursor = conn.execute(
        "INSERT INTO update_run (started_at, trigger, status, host) VALUES (?, ?, 'RUNNING', ?)",
        (_now(), trigger, socket.gethostname()),
    )
    # Committed immediately: a reader polling the page has to be able to see
    # that something is in progress, and an uncommitted row is invisible.
    conn.commit()
    return int(cursor.lastrowid or 0)


def _close_run(conn: sqlite3.Connection, outcome: UpdateOutcome) -> None:
    conn.execute(
        "UPDATE update_run SET finished_at = ?, status = ?, fx_rows = ?, price_rows = ?, "
        "instruments = ?, days_covered = ?, last_price_date = ?, rebuilt = ?, "
        "problems = ?, error = ? WHERE id = ?",
        (
            _now(),
            outcome.status,
            outcome.fx_rows,
            outcome.price_rows,
            outcome.instruments,
            outcome.days_covered,
            outcome.last_price_date.isoformat() if outcome.last_price_date else None,
            1 if outcome.rebuilt else 0,
            json.dumps(outcome.problems) if outcome.problems else None,
            outcome.error,
            outcome.run_id,
        ),
    )
    conn.commit()


def _skipped(conn: sqlite3.Connection, trigger: str, why: str) -> UpdateOutcome:
    cursor = conn.execute(
        "INSERT INTO update_run (started_at, finished_at, trigger, status, error, host) "
        "VALUES (?, ?, ?, 'SKIPPED', ?, ?)",
        (_now(), _now(), trigger, why, socket.gethostname()),
    )
    conn.commit()
    return UpdateOutcome(run_id=int(cursor.lastrowid or 0), status="SKIPPED", error=why)


def _update_fx(conn: sqlite3.Connection, outcome: UpdateOutcome, *, end: date) -> None:
    """Refresh every currency the portfolio actually needs.

    A failed quote is a problem, never fatal: a missing rate makes some
    positions unvaluable, which the valuation layer already reports honestly,
    whereas aborting the run would leave the prices unfetched as well.

    Each quote resumes from its own last stored rate, for the same reason the
    price window does -- asking for "the last week" would leave a hole after
    any outage longer than a week.
    """
    for quote in core_fx.required_quotes(conn):
        try:
            outcome.fx_rows += ingest_fx.store(
                conn, ingest_fx.fetch_ecb(quote, _fx_start(conn, quote, end))
            )
        except Exception as exc:
            outcome.problems.append(f"{quote} rates: {exc}")
        try:
            outcome.fx_rows += ingest_fx.store(conn, ingest_fx.load_manual(quote))
        except Exception as exc:
            outcome.problems.append(f"manual {quote} rate file: {exc}")
    conn.commit()


def _fx_start(conn: sqlite3.Connection, quote: str, end: date) -> date:
    """Where to resume fetching rates for one currency."""
    row = conn.execute("SELECT max(date) AS d FROM fx_rate WHERE quote = ?", (quote,)).fetchone()
    if row is None or not row["d"]:
        # No history at all: reach back far enough to cover any holding this
        # portfolio could have, since a cost basis is frozen at its trade date.
        return end - timedelta(days=FX_BACKFILL_DAYS)
    stored = date.fromisoformat(row["d"])
    return min(stored - timedelta(days=FX_OVERLAP_DAYS), end)


def run_update(
    db_path: Path | str,
    *,
    trigger: str = "MANUAL",
    end: date | None = None,
    rebuild_after: bool = True,
    only: list[int] | None = None,
) -> UpdateOutcome:
    """Fetch rates and prices, then recompute. Never raises on a data problem.

    Returns an :class:`UpdateOutcome`; the same information is in the
    ``update_run`` table, which is what the page reads.
    """
    target = Path(db_path)
    horizon = end or date.today()

    with connect(target) as conn:
        try:
            with locking.exclusive(target.with_suffix(".updatelock")):
                return _run_locked(
                    conn,
                    trigger=trigger,
                    end=horizon,
                    rebuild_after=rebuild_after,
                    only=only,
                )
        except locking.LockBusy as exc:
            return _skipped(conn, trigger, str(exc))


def _run_locked(
    conn: sqlite3.Connection,
    *,
    trigger: str,
    end: date,
    rebuild_after: bool,
    only: list[int] | None,
) -> UpdateOutcome:
    # The checkout may have moved on since the scheduled task was registered.
    migrations.migrate(conn)

    before = newest_price(conn)
    outcome = UpdateOutcome(run_id=_open_run(conn, trigger), trigger=trigger)

    try:
        _update_fx(conn, outcome, end=end)

        coverage = ingest_prices.fetch_all(conn, end=end, incremental=True, only=only)
        outcome.instruments = len(coverage)
        for report in coverage:
            if report.error:
                outcome.problems.append(f"{report.import_key}: {report.error}")

        after = newest_price(conn)
        outcome.last_price_date = after
        outcome.price_rows = sum(report.written for report in coverage)
        outcome.days_covered = business_days_between(before, after) if before and after else None

        if rebuild_after and conn.execute("SELECT 1 FROM transaction_ LIMIT 1").fetchone():
            # One transaction, so readers never see a half-rebuilt derived layer.
            with transaction(conn):
                result = rebuild.run(conn, end=end, strict=False, commit=False)
            outcome.rebuilt = True
            outcome.problems.extend(result.breaks)
            outcome.problems.extend(f"oversold: {o}" for o in result.oversold)

        outcome.status = "PARTIAL" if outcome.problems else "OK"
    except BaseException as exc:
        outcome.status = "FAILED"
        outcome.error = f"{type(exc).__name__}: {exc}"
        log.exception("update failed")
    finally:
        _close_run(conn, outcome)

    return outcome


def env_db_path() -> Path:
    """Where the database is, for a process started with no arguments."""
    override = os.environ.get("PORTFOLIO_DB")
    if override:
        return Path(override)
    # jobs/ -> portfolio_tracker/ -> src/ -> repo root
    return Path(__file__).resolve().parents[3] / "data" / "portfolio.db"
