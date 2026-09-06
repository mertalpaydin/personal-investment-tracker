"""The unattended daily update.

The failure this job exists to prevent has no symptom: prices stop advancing,
every figure stays plausible, and nothing says so. So the tests are mostly
about the *reporting* being right, not just the fetching.

Two of them guard mistakes that would be invisible until the evening it
mattered:

``test_the_scheduled_entry_point_never_prints``
    It runs under ``pythonw.exe``, where ``sys.stdout`` is ``None`` and
    ``print()`` raises. One stray print turns a successful update into a task
    that reports failure and a database that quietly stops advancing.

``test_a_second_update_records_that_it_stood_down``
    Two concurrent runs would not corrupt anything, they would waste a fetch.
    The one that stands down has to leave evidence, or a missing run in the
    history is indistinguishable from a run that never fired.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from portfolio_tracker.core import locking
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.ingest import prices
from portfolio_tracker.ingest.providers.base import InstrumentRef
from portfolio_tracker.jobs import daily_update, update, windows_task


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = connect(path)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, price_source) "
        "VALUES (1, 'ZGEQ', 'Global Equity', 'ETF', 'EUR', 'manual')"
    )
    conn.commit()
    conn.close()
    return path


def _add_price(path: Path, when: date) -> None:
    conn = connect(path)
    conn.execute(
        "INSERT OR IGNORE INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (1, ?, '100', 'EUR', 'manual', '2026-01-01T00:00:00Z')",
        (when.isoformat(),),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_an_empty_database_is_not_called_stale(db_path: Path) -> None:
    """Empty and behind are different states, and only one needs fixing here."""
    conn = connect(db_path)
    conn.execute("DELETE FROM instrument")
    conn.commit()
    assert update.is_stale(conn, today=date(2026, 6, 30)) is False
    conn.close()


def test_prices_from_last_week_are_stale(db_path: Path) -> None:
    _add_price(db_path, date(2026, 6, 22))
    conn = connect(db_path)
    assert update.is_stale(conn, today=date(2026, 6, 30)) is True
    conn.close()


def test_yesterdays_close_is_current(db_path: Path) -> None:
    """Prices arrive after the close, so yesterday is as new as data gets."""
    _add_price(db_path, date(2026, 6, 29))
    conn = connect(db_path)
    assert update.is_stale(conn, today=date(2026, 6, 30)) is False
    conn.close()


# ---------------------------------------------------------------------------
# The incremental window
# ---------------------------------------------------------------------------


def _ref() -> InstrumentRef:
    return InstrumentRef(
        instrument_id=1,
        import_key="ZGEQ",
        currency="EUR",
        price_source="manual",
        price_symbol=None,
        trading_calendar="XETRA",
    )


def test_an_incremental_window_resumes_from_the_last_stored_price(db_path: Path) -> None:
    """Not "yesterday" -- that would leave a permanent hole after any outage."""
    _add_price(db_path, date(2026, 6, 10))
    conn = connect(db_path)
    start = prices.window_for(conn, _ref(), end=date(2026, 6, 30), incremental=True)
    assert start == date(2026, 6, 10) - timedelta(days=prices.REFETCH_OVERLAP_DAYS)
    conn.close()


def test_an_instrument_with_no_history_gets_the_full_window(db_path: Path) -> None:
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, flow_class) VALUES (1, 1, 'BUY', '2026-03-02', 'EUR', '1', 'INVESTMENT')"
    )
    conn.commit()
    start = prices.window_for(conn, _ref(), end=date(2026, 6, 30), incremental=True)
    assert start == date(2026, 3, 2) - timedelta(days=prices.PRICE_LEAD_DAYS)
    conn.close()


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def test_a_run_is_recorded_even_when_it_fetches_nothing(db_path: Path) -> None:
    outcome = update.run_update(db_path, trigger="MANUAL", end=date(2026, 6, 30))
    assert outcome.status in ("OK", "PARTIAL")

    conn = connect(db_path)
    row = update.last_run(conn)
    assert row is not None
    assert row["trigger"] == "MANUAL"
    assert row["finished_at"] is not None
    conn.close()


def test_a_second_update_records_that_it_stood_down(db_path: Path) -> None:
    """Refuse, never queue -- and leave evidence of the refusal."""
    with locking.exclusive(db_path.with_suffix(".updatelock")):
        outcome = update.run_update(db_path, trigger="SCHEDULED", end=date(2026, 6, 30))

    assert outcome.status == "SKIPPED"
    assert "already running" in (outcome.error or "")

    conn = connect(db_path)
    assert update.last_run(conn)["status"] == "SKIPPED"
    conn.close()


def test_the_lock_refuses_a_second_holder_and_releases_on_exit(tmp_path: Path) -> None:
    lock = tmp_path / "x.updatelock"
    with locking.exclusive(lock), pytest.raises(locking.LockBusy), locking.exclusive(lock):
        pass
    # Released, so the next run is not blocked forever by the previous one.
    with locking.exclusive(lock):
        pass


# ---------------------------------------------------------------------------
# The scheduled entry point
# ---------------------------------------------------------------------------


def test_the_scheduled_entry_point_never_prints() -> None:
    """Under pythonw.exe, print() raises and takes the whole run with it.

    Parsed rather than grepped: the module's own docstring explains this rule,
    and a test that cannot tell prose about a call from the call itself is a
    test nobody can write documentation around.
    """
    import ast

    tree = ast.parse(Path(daily_update.__file__).read_text(encoding="utf-8"))
    calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert not calls, f"daily_update must not print; found calls at lines {calls}"


def test_exit_codes_distinguish_partial_from_failed() -> None:
    """Task Scheduler's "Last Result" is the only feedback visible by default."""
    assert daily_update.EXIT_CODES["OK"] == 0
    assert daily_update.EXIT_CODES["PARTIAL"] == 1
    assert daily_update.EXIT_CODES["FAILED"] == 2
    assert daily_update.EXIT_CODES["SKIPPED"] == 3


def test_a_missing_database_fails_rather_than_creating_one(tmp_path: Path) -> None:
    assert daily_update.main(["--db", str(tmp_path / "nope.db")]) == 2


# ---------------------------------------------------------------------------
# The task definition
# ---------------------------------------------------------------------------


def test_the_task_runs_python_directly_not_uv(tmp_path: Path) -> None:
    """`uv run` re-resolves the lockfile and can reach the network.

    A scheduled task built on it fails exactly when the machine is offline,
    which is one of the times a catch-up matters most.
    """
    xml = windows_task.build_xml(
        repo_root=tmp_path,
        db_path=tmp_path / "data" / "portfolio.db",
        at_time="18:30",
        start_date="2026-08-16",
    )
    assert "uv" not in xml.split("<Command>")[1].split("</Command>")[0].lower()
    assert "portfolio_tracker.jobs.daily_update" in xml


def test_the_task_catches_up_after_a_missed_run(tmp_path: Path) -> None:
    """The setting that makes a closed laptop recoverable."""
    xml = windows_task.build_xml(
        repo_root=tmp_path, db_path=tmp_path / "p.db", at_time="18:30", start_date="2026-08-16"
    )
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<Hidden>true</Hidden>" in xml
    assert f"<WorkingDirectory>{tmp_path}</WorkingDirectory>" in xml


def test_the_task_runs_at_the_time_asked_for(tmp_path: Path) -> None:
    xml = windows_task.build_xml(
        repo_root=tmp_path, db_path=tmp_path / "p.db", at_time="07:05", start_date="2026-08-16"
    )
    assert "<StartBoundary>2026-08-16T07:05:00</StartBoundary>" in xml


@pytest.mark.parametrize("bad", ["25:00", "7:5", "18:60", "", "18:30; shutdown", "../etc"])
def test_a_time_that_is_not_a_time_is_refused(tmp_path: Path, bad: str) -> None:
    """The only user-supplied value that reaches a command line."""
    with pytest.raises(windows_task.TaskError):
        windows_task.build_xml(
            repo_root=tmp_path, db_path=tmp_path / "p.db", at_time=bad, start_date="2026-08-16"
        )


def test_the_task_xml_parses(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    xml = windows_task.build_xml(
        repo_root=tmp_path, db_path=tmp_path / "p.db", at_time="18:30", start_date="2026-08-16"
    )
    ET.fromstring(xml.split("?>", 1)[1])


def test_busy_timeout_is_set_on_every_connection(tmp_path: Path) -> None:
    """Without it, the update job and an open browser tab collide instantly."""
    conn = connect(tmp_path / "p.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    conn.close()


def test_a_run_that_writes_nothing_still_reports_a_status(db_path: Path) -> None:
    outcome = update.run_update(db_path, trigger="CATCHUP", end=date(2026, 6, 30))
    conn: sqlite3.Connection = connect(db_path)
    row = conn.execute("SELECT * FROM update_run WHERE id = ?", (outcome.run_id,)).fetchone()
    assert row["status"] != "RUNNING", "a run left RUNNING is indistinguishable from a crash"
    conn.close()


# ---------------------------------------------------------------------------
# Score snapshots
# ---------------------------------------------------------------------------


def test_the_daily_run_records_a_score_snapshot_when_fundamentals_exist(db_path: Path) -> None:
    conn: sqlite3.Connection = connect(db_path)
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, price_source) "
        "VALUES (2, 'SAP', 'SAP SE', 'STOCK', 'EUR', 'manual')"
    )
    conn.execute(
        "INSERT INTO stock_fundamental (instrument_id, forward_pe, fcf_yield, pct_off_52w_high, "
        "ev_ebitda, debt_to_equity, operating_margin, fetched_at) "
        "VALUES (2, '12', '0.06', '-0.15', '9', '50', '0.2', '2026-06-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    outcome = update.run_update(db_path, trigger="SCHEDULED", end=date(2026, 6, 30))
    assert not any("score snapshot" in p for p in outcome.problems), outcome.problems
    conn = connect(db_path)
    rows = conn.execute(
        "SELECT instrument_id, calc_date, source, value_score FROM stock_score_snapshot"
    ).fetchall()
    conn.close()
    assert [(r["instrument_id"], r["calc_date"], r["source"]) for r in rows] == [
        (2, "2026-06-30", "DAILY")
    ]
    assert rows[0]["value_score"] is not None


def test_the_daily_run_writes_no_snapshot_without_fundamentals(db_path: Path) -> None:
    outcome = update.run_update(db_path, trigger="SCHEDULED", end=date(2026, 6, 30))
    assert outcome.status in ("OK", "PARTIAL")
    conn: sqlite3.Connection = connect(db_path)
    n = conn.execute("SELECT count(*) AS n FROM stock_score_snapshot").fetchone()["n"]
    conn.close()
    assert n == 0
