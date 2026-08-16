"""When the target is reached, and the discipline that keeps it honest.

The whole risk of this module is that it becomes a forecast. Two tests carry
that weight:

``test_the_tool_never_picks_a_rate`` asserts the output is a table across stated
assumptions and never a single date, because a single date is a number nobody
chose being presented as one somebody did.

``test_the_bootstrap_always_carries_the_window_it_resampled`` asserts the sample
window travels with every figure derived from it. Resampling a past assumes the
future is drawn from the same distribution, and that assumption is the entire
content of the answer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from portfolio_tracker.api.app import app as api_app
from portfolio_tracker.api.app import get_db, get_writable_db
from portfolio_tracker.cli import app as cli_app
from portfolio_tracker.core import goal, projection
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal
runner = CliRunner()

START = date(2024, 1, 1)


def _seed(conn: sqlite3.Connection, *, days: int, daily: str, contribution: str) -> None:
    """A portfolio that grew at a steady rate with steady contributions."""
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    value, units, price = D(10000), D(100), D(100)
    for offset in range(days):
        when = START + timedelta(days=offset)
        flow = D(contribution) if offset and offset % 30 == 0 else D(0)
        ret = D(daily) if offset else None
        if offset:
            value = value * (D(1) + D(daily)) + flow
            price = price * (D(1) + D(daily))
            units = value / price
        conn.execute(
            "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
            "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
            "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
            "net_external_flow_eur, completeness, drawdown_pct, daily_return) "
            "VALUES (?, 1, ?, ?, '0', ?, ?, '0', '0', '0', ?, '0', '0', ?, 'FULL', '0', ?)",
            (when.isoformat(), value, value, units, price, value, flow, ret),
        )
    conn.commit()


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Two years of steady growth, EUR 500 a month, target EUR 100,000.

    Deliberately flat: with one return repeated, every deterministic assertion
    below has a hand-checkable answer.
    """
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    _seed(conn, days=730, daily="0.0002", contribution="500")
    goal.set_target(conn, D(100000))
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def varied() -> Iterator[sqlite3.Connection]:
    """The same portfolio with returns that actually move.

    A bootstrap cannot produce dispersion the sample does not contain, so the
    tests about spread and seeds need a history that has some. The sequence is
    spelled out rather than drawn from `random`, because a fixture that changes
    between runs would make a failure impossible to reproduce.

    **The length is 13 and not 7 on purpose.** A 21-day block over a 7-day
    cycle is exactly three whole cycles, so every block comes out identical and
    the bootstrap silently collapses to a single path -- a fixture that looks
    varied and behaves flat. 13 is coprime with 21, so the blocks genuinely
    differ.
    """
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")

    pattern = [
        "0.012", "-0.009", "0.004", "0.018", "-0.015", "0.002", "-0.003",
        "0.021", "-0.011", "0.007", "-0.006", "0.014", "-0.002",
    ]  # fmt: skip
    value, units, price = D(10000), D(100), D(100)
    for offset in range(730):
        when = START + timedelta(days=offset)
        flow = D(500) if offset and offset % 30 == 0 else D(0)
        daily = D(pattern[offset % len(pattern)])
        ret = daily if offset else None
        if offset:
            value = value * (D(1) + daily) + flow
            price = price * (D(1) + daily)
            units = value / price
        conn.execute(
            "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
            "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
            "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
            "net_external_flow_eur, completeness, drawdown_pct, daily_return) "
            "VALUES (?, 1, ?, ?, '0', ?, ?, '0', '0', '0', ?, '0', '0', ?, 'FULL', '0', ?)",
            (when.isoformat(), value, value, units, price, value, flow, ret),
        )
    goal.set_target(conn, D(500000))
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_an_annual_rate_is_compounded_not_divided() -> None:
    """``r/12`` overstates the monthly rate and the error compounds."""
    monthly = projection.monthly_rate(D("0.12"))
    assert monthly < D("0.01")  # not 12/12
    twelve = (D(1) + monthly) ** 12
    assert abs(twelve - D("1.12")) < D("0.0000001")


def test_a_zero_rate_is_exactly_zero() -> None:
    assert projection.monthly_rate(D(0)) == D(0)


def test_a_total_wipeout_is_refused_rather_than_computed() -> None:
    with pytest.raises(projection.ProjectionError, match="wipe the portfolio out"):
        projection.monthly_rate(D(-1))


def test_contributions_land_at_the_end_of_the_month() -> None:
    """Assuming they arrive at the start compounds money not yet paid in."""
    months, value = projection.months_to_target(
        current=D(0),
        monthly_contribution=D(100),
        rate=D("0.10"),
        target=D(1000000),
        horizon_months=1,
    )
    assert months is None
    # 0 x 1.1 + 100. If the contribution earned that month it would be 110.
    assert value == D("100.00")


def test_a_target_out_of_reach_returns_no_date_rather_than_a_far_one() -> None:
    months, value = projection.months_to_target(
        current=D(1000),
        monthly_contribution=D(1),
        rate=D(0),
        target=D(1000000),
        horizon_months=12,
    )
    assert months is None
    assert value == D("1012.00")


# ---------------------------------------------------------------------------
# The discipline
# ---------------------------------------------------------------------------


def test_the_tool_never_picks_a_rate(db: sqlite3.Connection) -> None:
    """The output is a table across assumptions, never one projected date.

    A single date would be a number nobody chose, presented as one somebody
    did. The spread between the rows is the actual answer.
    """
    result = projection.project(db)
    assert len(result.scenarios) == len(projection.DEFAULT_RATES)
    assert {s.annual_return for s in result.scenarios} == set(projection.DEFAULT_RATES)
    # No field anywhere holds a single blessed projection.
    assert not hasattr(result, "projected_date")
    assert any("ASSUMPTIONS, not predictions" in c for c in result.caveats)


def test_a_higher_assumed_return_never_reaches_the_target_later(
    db: sqlite3.Connection,
) -> None:
    """Monotonicity. If this fails the compounding is wrong somewhere."""
    result = projection.project(db)
    months = [s.months for s in result.scenarios if s.months is not None]
    assert months == sorted(months, reverse=True)


def test_the_zero_rate_row_agrees_with_the_contributions_only_projection(
    db: sqlite3.Connection,
) -> None:
    """The 0% row must reproduce `core/goal.py` exactly, or one of them is wrong."""
    plain = goal.progress(db)
    result = projection.project(db)
    zero = next(s for s in result.scenarios if s.annual_return == D(0))
    assert zero.months == plain.months_at_current_rate
    assert zero.reached_on == plain.projected_date


def test_the_observed_return_is_offered_as_context_not_as_a_scenario(
    db: sqlite3.Connection,
) -> None:
    """What the portfolio did is history. It is not a rate to plan on, so it
    never becomes one of the rows."""
    observed = projection.observed_annual_return(db)
    assert observed is not None and observed > D(0)
    result = projection.project(db)
    assert observed not in {s.annual_return for s in result.scenarios}


def test_no_tax_no_fees_no_inflation_is_said_out_loud(db: sqlite3.Connection) -> None:
    result = projection.project(db)
    assert any("No tax, no fees and no inflation" in c for c in result.caveats)


# ---------------------------------------------------------------------------
# The bootstrap
# ---------------------------------------------------------------------------


def test_blocks_preserve_a_month_rather_than_shuffling_days() -> None:
    """Drawing single days would erase volatility clustering."""
    returns = [D("0.01")] * 25
    factors = projection.block_factors(returns, block_days=21)
    # 25 days of 1% gives 5 overlapping 21-day windows.
    assert len(factors) == 5
    assert all(abs(f - (D("1.01") ** 21)) < D("0.0000001") for f in factors)


def test_too_short_a_history_produces_no_blocks() -> None:
    assert projection.block_factors([D("0.01")] * 5, block_days=21) == []


def test_the_bootstrap_refuses_rather_than_resampling_a_thin_history() -> None:
    """Less history is not a worse estimate; it is a different kind of object."""
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    _seed(conn, days=100, daily="0.0002", contribution="500")
    goal.set_target(conn, D(100000))
    conn.commit()

    result = projection.project(conn)
    assert result.bootstrap is None
    assert any("Too little history to resample" in c for c in result.caveats)
    assert result.scenarios, "the stated-rate table still works without it"
    conn.close()


def test_the_bootstrap_always_carries_the_window_it_resampled(
    db: sqlite3.Connection,
) -> None:
    """The sample window IS the claim. A decile without it is unreadable."""
    result = projection.project(db, paths=200)
    assert result.bootstrap is not None
    b = result.bootstrap
    assert b.sample_days > projection.MIN_BOOTSTRAP_DAYS
    assert b.sample_start < b.sample_end
    assert any(str(b.sample_start) in c for c in b.caveats)
    assert any("assumption rather than a finding" in c for c in b.caveats)
    assert any("not a probability about your portfolio" in c for c in b.caveats)


def test_the_deciles_are_ordered(db: sqlite3.Connection) -> None:
    result = projection.project(db, paths=200)
    assert result.bootstrap is not None
    b = result.bootstrap
    assert b.earliest_decile is not None
    assert b.earliest_decile <= b.median <= b.latest_decile  # type: ignore[operator]


def test_the_same_inputs_give_the_same_answer(db: sqlite3.Connection) -> None:
    """A projection that moved when re-run would be untrustworthy for reasons
    unrelated to its model."""
    first = projection.project(db, paths=200)
    second = projection.project(db, paths=200)
    assert first.bootstrap is not None and second.bootstrap is not None
    assert first.bootstrap.median == second.bootstrap.median
    assert first.bootstrap.earliest_decile == second.bootstrap.earliest_decile


def test_a_history_with_no_dispersion_produces_none(db: sqlite3.Connection) -> None:
    """The steady fixture grew at exactly the same rate every day, so every
    block is identical and every simulated path is the same path.

    Correct, and worth pinning: a bootstrap cannot manufacture uncertainty that
    is not in the sample. A cone drawn from a flat history would be inventing
    the one thing it is supposed to measure.
    """
    result = projection.project(db, paths=200)
    assert result.bootstrap is not None
    assert result.bootstrap.earliest_decile == result.bootstrap.latest_decile


def test_a_different_seed_gives_a_different_draw(varied: sqlite3.Connection) -> None:
    """Determinism is a fixed seed, not a degenerate simulation.

    Needs a history that actually varies -- see the test above for why.
    """
    a = projection.project(varied, paths=300, seed=1)
    b = projection.project(varied, paths=300, seed=2)
    assert a.bootstrap is not None and b.bootstrap is not None
    assert a.bootstrap.earliest_decile != a.bootstrap.latest_decile, "the sample varies"
    assert (a.bootstrap.earliest_decile, a.bootstrap.median, a.bootstrap.latest_decile) != (
        b.bootstrap.earliest_decile,
        b.bootstrap.median,
        b.bootstrap.latest_decile,
    )


def test_a_volatile_history_widens_the_spread(varied: sqlite3.Connection) -> None:
    """Dispersion in, dispersion out -- and it must reach the reported deciles."""
    result = projection.project(varied, paths=300)
    assert result.bootstrap is not None
    assert result.bootstrap.earliest_decile < result.bootstrap.latest_decile  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Overrides -- a what-if with no forecasting in it
# ---------------------------------------------------------------------------


def test_saving_more_reaches_the_target_sooner(db: sqlite3.Connection) -> None:
    """Arithmetic on a number the owner supplies, not a prediction."""
    modest = projection.project(db, monthly_eur=D(500), include_bootstrap=False)
    generous = projection.project(db, monthly_eur=D(2000), include_bootstrap=False)
    slow = next(s for s in modest.scenarios if s.annual_return == D(0))
    fast = next(s for s in generous.scenarios if s.annual_return == D(0))
    assert fast.months is not None and slow.months is not None
    assert fast.months < slow.months


def test_a_supplied_contribution_is_labelled_as_supplied(db: sqlite3.Connection) -> None:
    result = projection.project(db, monthly_eur=D(800), include_bootstrap=False)
    assert any("was supplied, not measured" in c for c in result.caveats)


def test_a_target_already_reached_says_so_and_stops(db: sqlite3.Connection) -> None:
    result = projection.project(db, target_eur=D(100))
    assert result.already_reached
    assert result.scenarios == []
    assert result.caveats == ["The target has already been reached."]


def test_a_negative_contribution_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(projection.ProjectionError, match="cannot be negative"):
        projection.project(db, monthly_eur=D(-100))


def test_no_target_set_is_a_lookup_error(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM setting WHERE key = ?", (goal.GOAL_KEY,))
    with pytest.raises(LookupError, match="no goal target"):
        projection.project(db)


# ---------------------------------------------------------------------------
# HTTP and CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    api_app.dependency_overrides[get_db] = lambda: db
    api_app.dependency_overrides[get_writable_db] = lambda: db
    with TestClient(api_app) as test_client:
        yield test_client
    api_app.dependency_overrides.clear()


def test_the_endpoint_returns_a_table_and_names_its_basis(client: TestClient) -> None:
    body = client.get("/api/goal/projection", params={"paths": 200}).json()
    assert len(body["scenarios"]) == 5
    assert "chooses none of them" in body["basis"]
    assert body["observed_annual_return"] is not None
    assert body["bootstrap"]["sample"]["days"] > 250
    assert body["caveats"]


def test_the_endpoint_accepts_a_what_if_contribution(client: TestClient) -> None:
    body = client.get("/api/goal/projection", params={"monthly": "2000", "paths": 200}).json()
    assert body["monthly_contribution_eur"] == 2000.0


def test_a_missing_target_is_a_404(db: sqlite3.Connection, client: TestClient) -> None:
    db.execute("DELETE FROM setting WHERE key = ?", (goal.GOAL_KEY,))
    assert client.get("/api/goal/projection").status_code == 404


@pytest.fixture
def db_file(tmp_path, db: sqlite3.Connection):  # type: ignore[no-untyped-def]
    target = tmp_path / "p.db"
    on_disk = connect(target)
    db.backup(on_disk)
    on_disk.commit()
    on_disk.close()
    return target


def test_project_prints_the_table_and_the_disclaimer(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(cli_app, ["goal", "project", "--paths", "200", "--db", str(db_file)])
    assert result.exit_code == 0, result.output
    assert "IF the portfolio returns" in result.output
    assert "ASSUMPTIONS, not predictions" in result.output
    assert "not a rate to plan on" in result.output


def test_show_still_offers_the_zero_return_answer(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(cli_app, ["goal", "show", "--db", str(db_file)])
    assert result.exit_code == 0, result.output
    assert "NO investment return" in result.output
    assert "pt goal project" in result.output


def test_set_rejects_a_nonsense_target(db_file) -> None:  # type: ignore[no-untyped-def]
    assert runner.invoke(cli_app, ["goal", "set", "lots", "--db", str(db_file)]).exit_code == 1
    assert runner.invoke(cli_app, ["goal", "set", "0", "--db", str(db_file)]).exit_code == 1
    # `--` last, so typer reads the negative as the value rather than an option.
    assert runner.invoke(cli_app, ["goal", "set", "--db", str(db_file), "--", "-5"]).exit_code == 1
