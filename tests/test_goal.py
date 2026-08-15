"""The savings target and its projection.

The projection is the only forward-looking number in this system, which makes
it the only one that can be wrong about the future rather than about the past.
These tests pin the three ways it is allowed to answer: a date, no date because
there is too little history, and no date because the target is unreachably far.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_tracker.core import goal
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.commit()
    return conn


def history(
    conn: sqlite3.Connection,
    *,
    days: int,
    total: str,
    monthly_flow: str = "1000",
    start: date = date(2024, 1, 1),
) -> None:
    """A daily series with a contribution on the first of each month."""
    rows = []
    for offset in range(days + 1):
        when = start + timedelta(days=offset)
        flow = D(monthly_flow) if when.day == 1 else D(0)
        rows.append((when.isoformat(), D(total), D(total), D(0), D(100), D(100), flow))
    conn.executemany(
        "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
        "cost_basis_eur, units_outstanding, unit_price, net_external_flow_eur) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The target itself
# ---------------------------------------------------------------------------


def test_the_default_target_is_stored_not_hardcoded(db: sqlite3.Connection) -> None:
    """Migration 0006 seeds the figure its owner chose, so it is inspectable."""
    assert goal.get_target(db) == D(200000)


def test_setting_a_target_round_trips(db: sqlite3.Connection) -> None:
    goal.set_target(db, D("123456.78"))
    assert goal.get_target(db) == D("123456.78")


@pytest.mark.parametrize("bad", ["0", "-1", "-200000"])
def test_a_non_positive_target_is_refused(db: sqlite3.Connection, bad: str) -> None:
    """Zero makes progress infinite; a negative makes it meaningless. Both are typos."""
    with pytest.raises(ValueError, match="must be positive"):
        goal.set_target(db, D(bad))
    assert goal.get_target(db) == D(200000)


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


def test_progress_is_measured_against_the_headline_total(db: sqlite3.Connection) -> None:
    history(db, days=400, total="50000")
    goal.set_target(db, D(200000))
    state = goal.progress(db)
    assert state.current_eur == D("50000.00")
    assert state.remaining_eur == D("150000.00")
    assert state.progress_pct == D("0.25")
    assert any("Uninvested cash is excluded" in c for c in state.caveats)


def test_an_exceeded_target_does_not_report_negative_remaining(db: sqlite3.Connection) -> None:
    history(db, days=400, total="250000")
    state = goal.progress(db)
    assert state.remaining_eur == D("0.00")
    assert state.progress_pct == D("1.25")
    assert state.projected_date is None


# ---------------------------------------------------------------------------
# The projection: three permitted answers
# ---------------------------------------------------------------------------


def test_the_projection_counts_contributions_and_no_investment_return(
    db: sqlite3.Connection,
) -> None:
    """Deliberately pessimistic, and therefore falsifiable.

    Two years of EUR 1,000/month is EUR 24,000 over ~24 months. With EUR 76,000
    still to find, the honest answer at that rate is about 76 more months --
    and it must not be shortened by an assumed market return nobody promised.
    """
    history(db, days=730, total="24000", monthly_flow="1000")
    goal.set_target(db, D(100000))
    state = goal.progress(db)
    assert state.monthly_contribution_eur == pytest.approx(D(1000), abs=D(20))
    assert state.months_at_current_rate is not None
    assert 74 <= state.months_at_current_rate <= 78
    assert any("no investment return at all" in c for c in state.caveats)


def test_too_little_history_yields_no_date_rather_than_a_bad_one(db: sqlite3.Connection) -> None:
    """A rate measured over three weeks is a different object, not a worse estimate."""
    history(db, days=40, total="5000")
    state = goal.progress(db)
    assert state.projected_date is None
    assert state.monthly_contribution_eur is None
    assert any("too few to measure" in c for c in state.caveats)


def test_an_unreachable_target_gets_no_date_at_all(db: sqlite3.Connection) -> None:
    """A date 900 years out is not information; it is an invitation to believe."""
    history(db, days=730, total="24000", monthly_flow="1")
    goal.set_target(db, D(10_000_000))
    state = goal.progress(db)
    assert state.projected_date is None
    assert any("years away" in c for c in state.caveats)


def test_the_projected_date_is_the_month_count_applied_to_the_calendar(
    db: sqlite3.Connection,
) -> None:
    history(db, days=730, total="24000", monthly_flow="1000")
    goal.set_target(db, D(100000))
    state = goal.progress(db)
    assert state.months_at_current_rate is not None
    assert state.projected_date is not None
    expected = goal._add_months(state.as_of, state.months_at_current_rate)
    assert state.projected_date == expected


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),  # clamps into a short month
        (date(2028, 1, 31), 1, date(2028, 2, 29)),  # and knows about leap years
        (date(2026, 8, 15), 12, date(2027, 8, 15)),
        (date(2026, 12, 1), 1, date(2027, 1, 1)),  # crosses the year boundary
        (date(2026, 8, 15), 0, date(2026, 8, 15)),
    ],
)
def test_month_arithmetic_stays_on_the_calendar(start: date, months: int, expected: date) -> None:
    assert goal._add_months(start, months) == expected


def test_no_history_is_a_lookup_error_not_a_zero(db: sqlite3.Connection) -> None:
    with pytest.raises(LookupError, match="rebuild"):
        goal.progress(db)
