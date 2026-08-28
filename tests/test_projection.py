"""When the target is reached, and the discipline that keeps it honest.

A projection has two moving parts and they are different kinds of thing:
contributions are a decision, returns are an observation. The tests here exist
mostly to stop them being merged back together.

The one that matters most is
``test_a_position_you_have_sold_does_not_influence_the_projection``. The first
version of this module bootstrapped the *portfolio's own* return history, which
contains positions since disposed, the timing of old contributions, and weights
that describe nothing current. It was projecting a portfolio that no longer
exists. The model is now built from the price history of what is held **today**.

``test_the_tool_never_picks_a_rate`` guards the other half: the output is a
table across stated assumptions and never a single blessed date.
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
#: The last day of the 730-day series. Positions and the portfolio series must
#: agree on it: `goal.progress` reads the newest portfolio_daily row, and the
#: return model reads positions on that same date.
AS_OF = date(2024, 1, 1) + timedelta(days=729)
DAYS = 730

#: A 13-day cycle, not 7. A 21-day block over a 7-day repeat is exactly three
#: whole cycles, so every block comes out identical and the bootstrap silently
#: collapses to one path -- a sample that looks varied and behaves flat.
WOBBLE = [
    "0.004", "-0.003", "0.002", "0.006", "-0.005", "0.001", "-0.001",
    "0.007", "-0.004", "0.003", "-0.002", "0.005", "-0.0015",
]  # fmt: skip


def _instrument(conn: sqlite3.Connection, iid: int, key: str, currency: str = "EUR") -> None:
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "teilfreistellung_pct) VALUES (?, ?, ?, 'ETF', ?, 1, '0.30')",
        (iid, key, f"{key} fund", currency),
    )


def _prices(
    conn: sqlite3.Connection, iid: int, *, days: int, pattern: list[str], start: date = START
) -> None:
    price = D(100)
    for offset in range(days):
        when = start + timedelta(days=offset)
        if offset:
            price *= D(1) + D(pattern[offset % len(pattern)])
        conn.execute(
            "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
            "VALUES (?, ?, ?, (SELECT currency FROM instrument WHERE id = ?), 'manual', "
            "'2026-01-01T00:00:00Z')",
            (iid, when.isoformat(), price.quantize(D("0.0001")), iid),
        )


def _hold(conn: sqlite3.Connection, iid: int, value: str) -> None:
    """A position on the as-of date, which is what sets today's weights."""
    conn.execute(
        """
        INSERT INTO position_daily
            (date, account_id, instrument_id, quantity, currency, price_local,
             price_asof, price_stale_days, fx_rate, fx_asof, fx_stale_days,
             market_value_local, market_value_eur, cost_basis_local, cost_basis_eur,
             cost_basis_eur_at_spot, unrealized_eur, unrealized_local_eur,
             unrealized_fx_eur, valuation_status)
        VALUES (?, 1, ?, '1', 'EUR', ?, ?, 0, '1', ?, 0, ?, ?, ?, ?, ?, '0', '0', '0', 'OK')
        """,
        (
            AS_OF.isoformat(), iid, value, AS_OF.isoformat(), AS_OF.isoformat(),
            value, value, value, value, value,
        ),
    )  # fmt: skip


def _portfolio_series(conn: sqlite3.Connection, *, contribution: str = "500") -> None:
    """The daily series `goal.progress` and the contribution window read."""
    value = D(10000)
    for offset in range(DAYS):
        when = START + timedelta(days=offset)
        flow = D(contribution) if offset and offset % 30 == 0 else D(0)
        if offset:
            value = value * D("1.0002") + flow
        conn.execute(
            "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
            "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
            "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
            "net_external_flow_eur, completeness, drawdown_pct, daily_return) "
            "VALUES (?, 1, ?, ?, '0', '100', '100', '0', '0', '0', ?, '0', '0', ?, 'FULL', "
            "'0', ?)",
            (
                when.isoformat(), value, value, value, flow,
                D("0.0002") if offset else None,
            ),
        )  # fmt: skip
        if flow:
            # The ledger row behind the flow. `measure_contributions` reads
            # what was actually saved rather than every external movement, so
            # the derived column alone is no longer enough to measure from.
            conn.execute(
                "INSERT INTO transaction_ (account_id, type, trade_date, currency, "
                "gross_amount_local, gross_amount_eur, flow_class, source, confidence, "
                "is_new_savings) VALUES (1, 'DEPOSIT', ?, 'EUR', ?, ?, 'EXTERNAL_IN', "
                "'MANUAL', 'ACTUAL', 1)",
                (when.isoformat(), str(flow), str(flow)),
            )


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Three holdings and one that was sold.

    ``ZKEEP``  held, two years of price history -- the backbone of the model
    ``ZTRY``   held, lira-denominated, so the currency leg is exercised
    ``ZNEW``   held, bought recently, too little history to model
    ``ZGONE``  **sold**: prices exist, no position. Must not reach the model.
    """
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    _instrument(conn, 1, "ZKEEP")
    _instrument(conn, 2, "ZTRY", "TRY")
    _instrument(conn, 3, "ZNEW")
    _instrument(conn, 4, "ZGONE")

    _prices(conn, 1, days=DAYS, pattern=WOBBLE)
    _prices(conn, 2, days=DAYS, pattern=WOBBLE)
    _prices(conn, 3, days=60, pattern=WOBBLE, start=AS_OF - timedelta(days=59))
    # A disaster, and irrelevant: it is not held any more.
    _prices(conn, 4, days=DAYS, pattern=["-0.02", "0.001"])

    for offset in range(DAYS):
        when = START + timedelta(days=offset)
        conn.execute(
            "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
            "VALUES (?, 'EUR', 'TRY', ?, 'ecb', '2026-01-01T00:00:00Z')",
            (when.isoformat(), D(35) + D("0.02") * offset),
        )

    _hold(conn, 1, "7000")
    _hold(conn, 2, "2000")
    _hold(conn, 3, "1000")
    _portfolio_series(conn)
    goal.set_target(conn, D(100000))
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# The correction: the model is built from what is HELD
# ---------------------------------------------------------------------------


def test_a_position_you_have_sold_does_not_influence_the_projection(
    db: sqlite3.Connection,
) -> None:
    """The reason this module was rewritten.

    ZGONE lost 2% every other day for two years and is fully disposed. A model
    built on the portfolio's own past would still carry it; one built on what is
    held cannot. Its absence is the whole correction.
    """
    model = projection.build_return_model(db, as_of=AS_OF)
    assert "ZGONE" not in {key for key, _ in model.holdings}
    assert "ZGONE" not in {key for key, _ in model.excluded}
    assert all(daily > D("-0.02") for daily in model.series), "no disaster leaked in"


def test_the_model_uses_todays_weights(db: sqlite3.Connection) -> None:
    """7,000 / 2,000 / 1,000 -- and ZNEW is dropped, so the rest renormalise."""
    model = projection.build_return_model(db, as_of=AS_OF)
    weights = dict(model.holdings)
    assert set(weights) == {"ZKEEP", "ZTRY"}
    assert weights["ZKEEP"] == pytest.approx(D(7) / D(9), abs=D("0.0001"))
    assert sum(weights.values(), D(0)) == pytest.approx(D(1), abs=D("0.0001"))


def test_a_recently_bought_holding_is_excluded_and_named(db: sqlite3.Connection) -> None:
    """Not given a short series that would understate its volatility."""
    model = projection.build_return_model(db, as_of=AS_OF)
    excluded = dict(model.excluded)
    assert "ZNEW" in excluded
    assert "price history" in excluded["ZNEW"]
    # And the portfolio it covers is reported honestly: 9,000 of 10,000.
    assert model.covered_pct == pytest.approx(D("0.9"), abs=D("0.001"))


def test_the_currency_leg_reaches_the_model(db: sqlite3.Connection) -> None:
    """A lira holding's euro return includes the lira falling.

    The rate climbs from 35 to 49 over the window, so the euro series must be
    materially worse than the local prices alone.
    """
    with_try = projection.build_return_model(db, as_of=AS_OF)
    db.execute("DELETE FROM position_daily WHERE instrument_id = 2")
    without = projection.build_return_model(db, as_of=AS_OF)

    def total(series: list[Decimal]) -> Decimal:
        factor = D(1)
        for daily in series:
            factor *= D(1) + daily
        return factor

    assert total(with_try.series) < total(without.series)


def test_the_thinnest_day_of_the_window_is_reported(db: sqlite3.Connection) -> None:
    """A window kept only because most holdings existed for it must say so."""
    model = projection.build_return_model(db, as_of=AS_OF)
    assert model.min_daily_coverage > D(0)
    assert model.min_daily_coverage <= D(1)


def test_nothing_held_means_no_model(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM position_daily")
    model = projection.build_return_model(db, as_of=AS_OF)
    assert model.series == []
    assert model.holdings == []


# ---------------------------------------------------------------------------
# Contributions: a suggestion, not a constant
# ---------------------------------------------------------------------------


def test_contributions_are_measured_over_a_recent_window(db: sqlite3.Connection) -> None:
    """Not the whole history. What you saved two years ago is not evidence
    about next month, and averaging it in makes the figure unrecognisable."""
    six = projection.measure_contributions(db, as_of=AS_OF, window_months=6)
    assert six.source == "measured"
    assert six.window_months == 6
    assert six.total_eur is not None and six.total_eur > D(0)
    assert six.monthly_eur == pytest.approx(six.total_eur / D(6), abs=D("0.01"))


def test_the_measured_figure_explains_itself_and_invites_a_change(
    db: sqlite3.Connection,
) -> None:
    measured = projection.measure_contributions(db, as_of=AS_OF, window_months=6)
    assert "You added EUR" in measured.sentence
    assert "Change it if" in measured.sentence


def test_a_supplied_contribution_is_a_different_kind_of_number(
    db: sqlite3.Connection,
) -> None:
    """Measured is a suggestion; supplied is your decision. Never the same."""
    result = projection.project(db, monthly_eur=D(800), include_bootstrap=False)
    assert result.contributions.source == "supplied"
    assert result.contributions.monthly_eur == D("800.00")
    assert "as you specified" in result.contributions.sentence


def test_a_different_window_gives_a_different_suggestion(db: sqlite3.Connection) -> None:
    short = projection.measure_contributions(db, as_of=AS_OF, window_months=3)
    long_ = projection.measure_contributions(db, as_of=AS_OF, window_months=24)
    assert short.window_months != long_.window_months
    assert short.measured_from is not None and long_.measured_from is not None
    assert short.measured_from > long_.measured_from


def test_saving_more_reaches_the_target_sooner(db: sqlite3.Connection) -> None:
    modest = projection.project(db, monthly_eur=D(500), include_bootstrap=False)
    generous = projection.project(db, monthly_eur=D(2000), include_bootstrap=False)
    slow = next(s for s in modest.scenarios if s.annual_return == D(0))
    fast = next(s for s in generous.scenarios if s.annual_return == D(0))
    assert fast.months is not None and slow.months is not None
    assert fast.months < slow.months


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_an_annual_rate_is_compounded_not_divided() -> None:
    """``r/12`` overstates the monthly rate and the error compounds."""
    monthly = projection.monthly_rate(D("0.12"))
    assert monthly < D("0.01")
    assert abs((D(1) + monthly) ** 12 - D("1.12")) < D("0.0000001")


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
    assert value == D("100.00")  # not 110


# ---------------------------------------------------------------------------
# The discipline
# ---------------------------------------------------------------------------


def test_the_tool_never_picks_a_rate(db: sqlite3.Connection) -> None:
    """A single date would be a number nobody chose wearing the authority of
    one somebody did. The spread between rows is the actual answer."""
    result = projection.project(db, include_bootstrap=False)
    assert len(result.scenarios) == len(projection.DEFAULT_RATES)
    assert not hasattr(result, "projected_date")
    assert any("ASSUMPTIONS, not predictions" in c for c in result.caveats)


def test_a_higher_assumed_return_never_reaches_the_target_later(
    db: sqlite3.Connection,
) -> None:
    result = projection.project(db, include_bootstrap=False)
    months = [s.months for s in result.scenarios if s.months is not None]
    assert months == sorted(months, reverse=True)


def test_the_observed_return_is_context_not_a_scenario(db: sqlite3.Connection) -> None:
    """What the holdings did is history, so it never becomes a row."""
    observed = projection.observed_annual_return(db, as_of=AS_OF)
    assert observed is not None
    result = projection.project(db, include_bootstrap=False)
    assert observed not in {s.annual_return for s in result.scenarios}


def test_no_tax_no_fees_no_inflation_is_said_out_loud(db: sqlite3.Connection) -> None:
    result = projection.project(db, include_bootstrap=False)
    assert any("No tax, no fees and no inflation" in c for c in result.caveats)


def test_a_target_already_reached_says_so_and_stops(db: sqlite3.Connection) -> None:
    result = projection.project(db, target_eur=D(100))
    assert result.already_reached
    assert result.scenarios == []


def test_a_negative_contribution_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(projection.ProjectionError, match="cannot be negative"):
        projection.project(db, monthly_eur=D(-100))


def test_no_target_set_is_a_lookup_error(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM setting WHERE key = ?", (goal.GOAL_KEY,))
    with pytest.raises(LookupError, match="no goal target"):
        projection.project(db)


# ---------------------------------------------------------------------------
# The bootstrap
# ---------------------------------------------------------------------------


def test_blocks_preserve_a_run_rather_than_shuffling_days() -> None:
    returns = [D("0.01")] * 25
    factors = projection.block_factors(returns, block_days=21)
    assert len(factors) == 5
    assert all(abs(f - (D("1.01") ** 21)) < D("0.0000001") for f in factors)


def test_too_short_a_history_produces_no_blocks() -> None:
    assert projection.block_factors([D("0.01")] * 5, block_days=21) == []


def test_the_block_length_is_configurable(db: sqlite3.Connection) -> None:
    """A month is too short for a multi-year projection; a year is the default,
    and the caller decides."""
    quarterly = projection.project(db, block_days=63, paths=200)
    yearly = projection.project(db, block_days=252, paths=200)
    assert quarterly.bootstrap is not None and yearly.bootstrap is not None
    assert quarterly.bootstrap.block_days == 63
    assert yearly.bootstrap.block_days == 252
    assert projection.BLOCK_DAYS == 252


def test_a_thin_sample_is_flagged_as_weak_rather_than_presented(
    db: sqlite3.Connection,
) -> None:
    """Overlapping windows inflate the block count without adding information.

    Two years cut into one-year blocks is about two observations, however many
    windows can be sliced from it, and the output has to say so.
    """
    result = projection.project(db, block_days=252, paths=200)
    assert result.bootstrap is not None
    assert result.bootstrap.effective_samples < projection.MIN_EFFECTIVE_SAMPLES
    assert any(c.startswith("WEAK:") for c in result.bootstrap.caveats)


def test_a_longer_sample_is_not_flagged(db: sqlite3.Connection) -> None:
    result = projection.project(db, block_days=21, paths=200)
    assert result.bootstrap is not None
    assert result.bootstrap.effective_samples > projection.MIN_EFFECTIVE_SAMPLES
    assert not any(c.startswith("WEAK:") for c in result.bootstrap.caveats)


def test_the_bootstrap_says_what_it_was_built_from(db: sqlite3.Connection) -> None:
    """The basis IS the claim. A decile without it is unreadable."""
    result = projection.project(db, paths=200)
    assert result.bootstrap is not None
    caveats = " ".join(result.bootstrap.caveats)
    assert "not from the portfolio's own past performance" in caveats
    assert "thinnest day" in caveats
    assert "entire content of these figures" in caveats


def test_the_deciles_are_ordered(db: sqlite3.Connection) -> None:
    result = projection.project(db, block_days=21, paths=300)
    assert result.bootstrap is not None
    b = result.bootstrap
    assert b.earliest_decile is not None and b.latest_decile is not None
    assert b.earliest_decile <= b.median <= b.latest_decile  # type: ignore[operator]


def test_the_same_inputs_give_the_same_answer(db: sqlite3.Connection) -> None:
    """A projection that moved when re-run would be untrustworthy for reasons
    unrelated to its model."""
    first = projection.project(db, paths=200)
    second = projection.project(db, paths=200)
    assert first.bootstrap is not None and second.bootstrap is not None
    assert first.bootstrap.median == second.bootstrap.median


def test_no_price_history_means_no_bootstrap_but_still_a_table(
    db: sqlite3.Connection,
) -> None:
    db.execute("DELETE FROM price")
    result = projection.project(db)
    assert result.bootstrap is None
    assert result.scenarios, "the stated-rate table still works without it"
    assert any("No return model" in c for c in result.caveats)


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
    assert body["bootstrap"]["model"]["covers_pct_of_portfolio"] is not None
    assert "not the portfolio's own past performance" in body["bootstrap"]["model"]["basis"]


def test_the_endpoint_separates_measured_from_supplied(client: TestClient) -> None:
    measured = client.get("/api/goal/projection", params={"paths": 200}).json()
    assert measured["contributions"]["source"] == "measured"
    assert measured["contributions"]["editable"] is True

    supplied = client.get("/api/goal/projection", params={"monthly": "2000", "paths": 200}).json()
    assert supplied["contributions"]["source"] == "supplied"
    assert supplied["contributions"]["monthly_eur"] == 2000.0


def test_the_endpoint_exposes_the_block_length(client: TestClient) -> None:
    body = client.get("/api/goal/projection", params={"block_days": 63, "paths": 200}).json()
    assert body["bootstrap"]["block_days"] == 63


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


def test_project_accepts_a_named_block_length(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        cli_app,
        ["goal", "project", "--block", "quarter", "--paths", "200", "--db", str(db_file)],
    )
    assert result.exit_code == 0, result.output
    assert "63-day blocks" in result.output


def test_show_still_offers_the_zero_return_answer(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(cli_app, ["goal", "show", "--db", str(db_file)])
    assert result.exit_code == 0, result.output
    assert "NO investment return" in result.output


def test_set_rejects_a_nonsense_target(db_file) -> None:  # type: ignore[no-untyped-def]
    assert runner.invoke(cli_app, ["goal", "set", "lots", "--db", str(db_file)]).exit_code == 1
    assert runner.invoke(cli_app, ["goal", "set", "0", "--db", str(db_file)]).exit_code == 1


# ---------------------------------------------------------------------------
# The page controls, which were broken
# ---------------------------------------------------------------------------


def test_the_contribution_box_can_actually_be_saved(client: TestClient) -> None:
    """It could not.

    The input carried ``step='50'`` while the measured figure is EUR 2,145.17,
    so the browser rejected the value already in the box; and the form posted to
    a query parameter the page did not read, so submitting would have changed
    nothing anyway.
    """
    import re

    page = client.get("/debug").text
    box = re.search(r"id='monthly'[^>]*", page).group(0)  # type: ignore[union-attr]
    assert "step='any'" in box, "a measured contribution is not a round number"

    changed = client.get("/debug", params={"monthly": "3000"}).text
    assert re.search(r"id='monthly'[^>]*value='3000", changed)
    assert "as you specified" in changed
    assert "reset to measured" in changed


def test_overriding_the_contribution_moves_the_dates(client: TestClient) -> None:
    """The control has to change the answer, not just the box."""
    import re

    def zero_rate_date(text: str) -> str | None:
        found = re.search(r"<tr><td>0% a year</td><td>([\d-]+)</td>", text)
        return found.group(1) if found else None

    measured = zero_rate_date(client.get("/debug").text)
    saving_more = zero_rate_date(client.get("/debug", params={"monthly": "99999"}).text)
    assert measured and saving_more
    assert saving_more < measured


def test_the_target_box_accepts_a_round_number(client: TestClient) -> None:
    """It did not.

    ``min='1' step='100'`` makes the valid values 1, 101, 201... so a target of
    250,000 was rejected by the browser with "closest valid values are 249,901
    or 250,001" and the form could never be submitted.
    """
    import re

    box = re.search(r"id='target'[^>]*", client.get("/debug").text).group(0)  # type: ignore[union-attr]
    assert "step='any'" in box
    assert "step='100'" not in box
    assert client.put("/api/goal", json={"target_eur": "250000"}).status_code == 200
