"""What a change would do, measured in months.

Three properties carry this module, and each is a way the answer could be
confidently wrong rather than obviously broken:

``test_the_same_lever_twice_gives_the_same_answer``
    Both runs must draw identical return blocks. On different seeds two runs
    differ by a month or two from sampling noise alone -- the same size as the
    effect being measured -- so the comparison would be measuring chance.

``test_a_projection_survives_a_day_with_no_valuation``
    `position_daily` is matched on an exact date. Anchoring to today rather
    than to the last valuation makes the whole projection vanish on a weekend,
    which is most days.

``test_a_target_beyond_the_horizon_is_none_not_a_date``
    Extrapolating past the horizon would invent the part of the answer that
    matters most.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_tracker.core import projection, scenarios
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

AS_OF = date(2026, 6, 30)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, price_source) "
        "VALUES (1, 'ZGEQ', 'World Equity', 'ETF', 'EUR', 'manual')"
    )
    # The migrations ship a default target; this replaces it.
    conn.execute(
        "INSERT INTO setting (key, value, updated_at) "
        "VALUES ('goal_target_eur', '200000', '2026-01-01T00:00:00Z') "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
    )

    # Two years of gently rising prices, so the bootstrap has blocks to cut.
    level = Decimal(100)
    start = AS_OF - timedelta(days=760)
    for offset in range(760):
        on = start + timedelta(days=offset)
        conn.execute(
            "INSERT INTO price (instrument_id, date, close, total_return_close, currency, "
            "source, fetched_at) VALUES (1, ?, ?, ?, 'EUR', 'manual', '2026-01-01T00:00:00Z')",
            (on.isoformat(), str(level), str(level)),
        )
        level *= Decimal("1.0003")

    # A portfolio valued up to AS_OF, and contributions to measure a rate from.
    for offset in range(200):
        on = AS_OF - timedelta(days=199 - offset)
        conn.execute(
            "INSERT INTO position_daily (date, account_id, instrument_id, quantity, currency, "
            "price_local, price_asof, fx_rate, fx_asof, market_value_local, market_value_eur, "
            "cost_basis_local, cost_basis_eur, cost_basis_eur_at_spot, unrealized_eur, "
            "unrealized_local_eur, unrealized_fx_eur) VALUES (?, 1, 1, '1000', 'EUR', "
            "'100', ?, '1', ?, '100000', '100000', '90000', '90000', '90000', "
            "'10000', '10000', '0')",
            (on.isoformat(), on.isoformat(), on.isoformat()),
        )
        conn.execute(
            "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
            "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
            "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
            "completeness, drawdown_pct, net_external_flow_eur) VALUES (?, 1, '100000', "
            "'100000', '90000', '1000', '100', '0', '0', '0', '100000', '0', '0', "
            "'FULL', '0', ?)",
            (on.isoformat(), "1000" if offset % 30 == 0 else "0"),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_the_same_lever_twice_gives_the_same_answer(db: sqlite3.Connection) -> None:
    """The comparison is only meaningful if the draws are identical."""
    levers = scenarios.Levers(monthly_contribution_delta_eur=Decimal(500))
    first = scenarios.run(db, levers=levers, paths=200, with_bands=False, as_of=AS_OF)
    second = scenarios.run(db, levers=levers, paths=200, with_bands=False, as_of=AS_OF)
    assert first.months_earlier == second.months_earlier
    assert first.baseline.months_to_target == second.baseline.months_to_target


def test_an_empty_lever_set_reproduces_the_baseline_exactly(db: sqlite3.Connection) -> None:
    """A scenario that changes nothing must change nothing.

    If this fails the two runs are not sharing draws, and every months-earlier
    figure the screen reports is partly sampling noise.
    """
    result = scenarios.run(db, levers=scenarios.Levers(), paths=200, with_bands=False, as_of=AS_OF)
    assert result.baseline.months_to_target == result.scenario.months_to_target
    assert all(v == 0 for v in result.months_earlier.values() if v is not None)


def test_a_projection_survives_a_day_with_no_valuation(db: sqlite3.Connection) -> None:
    """Anchored to the last valuation, not to today.

    `position_daily` is matched on an exact date, so a projection anchored to
    today produces "not enough price history" every weekend.
    """
    # No `as_of`: the run has to find the latest valuation for itself.
    result = scenarios.run(db, levers=scenarios.Levers(), paths=100, with_bands=False)
    assert result.as_of == AS_OF
    assert result.baseline.months_to_target


# ---------------------------------------------------------------------------
# Levers
# ---------------------------------------------------------------------------


def test_contributing_more_never_arrives_later(db: sqlite3.Connection) -> None:
    result = scenarios.run(
        db,
        levers=scenarios.Levers(monthly_contribution_delta_eur=Decimal(2000)),
        paths=200,
        with_bands=False,
        as_of=AS_OF,
    )
    earlier = result.months_earlier[str(scenarios.HEADLINE_CONFIDENCE)]
    assert earlier is not None and earlier >= 0


def test_a_lump_sum_brings_the_date_forward(db: sqlite3.Connection) -> None:
    result = scenarios.run(
        db,
        levers=scenarios.Levers(lump_sums=((6, Decimal(50000)),)),
        paths=200,
        with_bands=False,
        as_of=AS_OF,
    )
    earlier = result.months_earlier[str(scenarios.HEADLINE_CONFIDENCE)]
    assert earlier is not None and earlier > 0


def test_a_cheaper_fund_is_a_return_delta(db: sqlite3.Connection) -> None:
    """An ongoing charge and a return difference act identically, so they are
    one number in the simulation rather than two that could disagree."""
    cheaper = scenarios.Levers(ter_delta_bps=-50)
    equivalent = scenarios.Levers(return_delta_annual_pct=Decimal("0.005"))
    assert cheaper.return_delta_monthly == equivalent.return_delta_monthly


def test_a_return_assumption_is_labelled_as_one(db: sqlite3.Connection) -> None:
    """The most important framing on the screen.

    A contribution is a decision; a return is a wish. Presenting them alike
    invites the reader to treat them as equally available.
    """
    assumed = scenarios.run(
        db,
        levers=scenarios.Levers(return_delta_annual_pct=Decimal("0.02")),
        paths=100,
        with_bands=False,
        as_of=AS_OF,
    )
    assert assumed.kinds == ["assumption"]
    assert any("does not change the portfolio" in c for c in assumed.caveats)

    decided = scenarios.run(
        db,
        levers=scenarios.Levers(monthly_contribution_delta_eur=Decimal(300)),
        paths=100,
        with_bands=False,
        as_of=AS_OF,
    )
    assert decided.kinds == ["controllable"]
    assert not any("does not change the portfolio" in c for c in decided.caveats)


def test_efficiency_is_months_bought_per_hundred(db: sqlite3.Connection) -> None:
    result = scenarios.run(
        db,
        levers=scenarios.Levers(monthly_contribution_delta_eur=Decimal(1000)),
        paths=200,
        with_bands=False,
        as_of=AS_OF,
    )
    earlier = result.months_earlier[str(scenarios.HEADLINE_CONFIDENCE)]
    if earlier:
        assert result.efficiency_months_per_100 == pytest.approx(
            Decimal(earlier) / Decimal(10), abs=Decimal("0.01")
        )


# ---------------------------------------------------------------------------
# Refusing to extrapolate
# ---------------------------------------------------------------------------


def test_a_target_beyond_the_horizon_is_none_not_a_date(db: sqlite3.Connection) -> None:
    result = scenarios.run(
        db,
        levers=scenarios.Levers(target_amount_eur=Decimal(50_000_000), target_months=24),
        paths=100,
        with_bands=False,
        as_of=AS_OF,
    )
    assert result.baseline.months_to_target[str(scenarios.HEADLINE_CONFIDENCE)] is None
    assert result.baseline.dates[str(scenarios.HEADLINE_CONFIDENCE)] is None
    assert result.months_earlier[str(scenarios.HEADLINE_CONFIDENCE)] is None


def test_confidence_dates_never_run_backwards(db: sqlite3.Connection) -> None:
    """Being more confident cannot be quicker."""
    result = scenarios.run(db, levers=scenarios.Levers(), paths=300, with_bands=False, as_of=AS_OF)
    months = [
        result.baseline.months_to_target[str(c)]
        for c in scenarios.CONFIDENCES
        if result.baseline.months_to_target[str(c)] is not None
    ]
    assert months == sorted(months)


# ---------------------------------------------------------------------------
# The fan
# ---------------------------------------------------------------------------


def test_the_bands_are_ordered_at_every_point(db: sqlite3.Connection) -> None:
    result = scenarios.run(db, levers=scenarios.Levers(), paths=200, as_of=AS_OF)
    assert result.baseline.bands
    for band in result.baseline.bands:
        assert band["p05"] <= band["p25"] <= band["p50"] <= band["p75"] <= band["p95"]


def test_confidence_counts_paths_that_arrived(db: sqlite3.Connection) -> None:
    paths = projection.simulate_paths(
        factors=[Decimal("1.10")] * 20,
        block_days=252,
        current=Decimal(100),
        monthly_contribution=Decimal(10),
        target=Decimal(200),
        horizon_months=60,
        paths=100,
        seed=1,
    )
    half = projection.months_at_confidence(paths, Decimal("0.50"))
    almost_all = projection.months_at_confidence(paths, Decimal("0.90"))
    assert half is not None and almost_all is not None
    assert almost_all >= half
