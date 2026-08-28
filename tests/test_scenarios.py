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
        if offset % 30 == 0:
            # A real ledger row, not just a number in the derived table:
            # `measure_contributions` reads what was actually saved, so that
            # one figure cannot disagree with the savings page's.
            conn.execute(
                "INSERT INTO transaction_ (account_id, type, trade_date, currency, "
                "gross_amount_local, gross_amount_eur, flow_class, source, confidence, "
                "is_new_savings) VALUES (1, 'DEPOSIT', ?, 'EUR', '1000', '1000', "
                "'EXTERNAL_IN', 'MANUAL', 'ACTUAL', 1)",
                (on.isoformat(),),
            )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_the_same_lever_twice_gives_the_same_answer(db: sqlite3.Connection) -> None:
    """The comparison is only meaningful if the draws are identical."""
    levers = scenarios.Levers(monthly_contribution_eur=Decimal(1500))
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
        levers=scenarios.Levers(monthly_contribution_eur=Decimal(3000)),
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


def test_a_stated_return_is_a_drift_away_from_the_measured_one(
    db: sqlite3.Connection,
) -> None:
    """Asking for 9% when the history averages 7% adds two points, not nine.

    The lever states a destination; the simulation still draws from real
    history. Getting this backwards would replace the bootstrap's volatility
    with a smooth 9% a year, which is the one thing markets never do.
    """
    implied = Decimal("0.07")
    same = scenarios.Levers(return_annual_pct=implied)
    assert same.return_delta_monthly(implied) == Decimal(0)

    higher = scenarios.Levers(return_annual_pct=Decimal("0.09"))
    drift = higher.return_delta_monthly(implied)
    assert drift > 0
    # Two points a year, compounded monthly -- not two points a month.
    assert drift < Decimal("0.003")

    # Blank means "use the history", which is the only setting that is evidence.
    assert scenarios.Levers().return_delta_monthly(implied) == Decimal(0)


def test_the_implied_return_is_what_the_bootstrap_already_assumes(
    db: sqlite3.Connection,
) -> None:
    """The number the form has to show before it can offer a different one."""
    context = scenarios.return_context(db, as_of=AS_OF)
    assert context.history_days > 0
    assert context.implied_annual != Decimal(0)
    # Every window reports how much data stands behind it, so a wild
    # annualised figure from three weeks can be recognised as one.
    for window in context.measured.values():
        assert window.days >= 0


def test_a_return_assumption_is_labelled_as_one(db: sqlite3.Connection) -> None:
    """The most important framing on the screen.

    A contribution is a decision; a return is a wish. Presenting them alike
    invites the reader to treat them as equally available.
    """
    assumed = scenarios.run(
        db,
        levers=scenarios.Levers(return_annual_pct=Decimal("0.15")),
        paths=100,
        with_bands=False,
        as_of=AS_OF,
    )
    assert assumed.kinds == ["assumption"]
    assert any("does not change the portfolio" in c for c in assumed.caveats)

    decided = scenarios.run(
        db,
        levers=scenarios.Levers(monthly_contribution_eur=Decimal(1300)),
        paths=100,
        with_bands=False,
        as_of=AS_OF,
    )
    assert decided.kinds == ["controllable"]
    assert not any("does not change the portfolio" in c for c in decided.caveats)


def test_efficiency_is_months_bought_per_hundred(db: sqlite3.Connection) -> None:
    result = scenarios.run(
        db,
        levers=scenarios.Levers(monthly_contribution_eur=Decimal(2000)),
        paths=200,
        with_bands=False,
        as_of=AS_OF,
    )
    earlier = result.months_earlier[str(scenarios.HEADLINE_CONFIDENCE)]
    # Derived from the two outcomes rather than assumed. The lever states a
    # rate, not a difference, so the difference is whatever it turned out to
    # be against the measured baseline -- which changes if the measuring
    # window does.
    delta = result.scenario.monthly_contribution_eur - result.baseline.monthly_contribution_eur
    assert delta > 0
    if earlier:
        assert result.efficiency_months_per_100 == pytest.approx(
            Decimal(earlier) / (delta / Decimal(100)), abs=Decimal("0.01")
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


# ---------------------------------------------------------------------------
# Choosing a savings rate knowingly
# ---------------------------------------------------------------------------


def test_one_large_transfer_does_not_become_a_monthly_savings_rate(
    db: sqlite3.Connection,
) -> None:
    """The failure that made the whole plan page wrong.

    A single EUR 60,000 transfer inside a six-month window is a EUR 10,000
    "monthly savings rate", and the projection then assumes it repeats every
    month for fifteen years. The average cannot be defended against this --
    it is what an average does -- so the median month is reported beside it,
    and the gap between the two is what tells the reader which to trust.
    """
    db.execute(
        "INSERT INTO transaction_ (account_id, type, trade_date, currency, "
        "gross_amount_local, gross_amount_eur, flow_class, source, confidence, "
        "is_new_savings) VALUES (1, 'DEPOSIT', ?, 'EUR', '60000', '60000', "
        "'EXTERNAL_IN', 'MANUAL', 'ACTUAL', 1)",
        ((AS_OF - timedelta(days=45)).isoformat(),),
    )
    db.commit()

    windows = scenarios.contribution_context(db, as_of=AS_OF)
    six = windows["6m"]
    assert six.median_monthly_eur is not None
    # The average is dragged into a different order of magnitude; the median
    # is not moved at all by one month, whatever is in it.
    assert six.monthly_eur > six.median_monthly_eur * 3
    assert six.by_month, "the months behind the figure must be inspectable"


def test_the_contribution_window_is_a_year(db: sqlite3.Connection) -> None:
    """Six months was not long enough for its own stated purpose.

    The constant's comment says the window is "long enough that one unusual
    month does not set the rate". At six months one transfer can be two
    thirds of it. A year also matches the trailing figure the savings section
    shows, so one page stops quoting two different savings rates.
    """
    assert projection.CONTRIBUTION_WINDOW_MONTHS == 12


def test_every_window_reports_both_an_average_and_a_median(
    db: sqlite3.Connection,
) -> None:
    windows = scenarios.contribution_context(db, as_of=AS_OF)
    assert set(windows) == {"6m", "12m", "24m"}
    for label, c in windows.items():
        assert c.monthly_eur >= 0, label
        assert c.median_monthly_eur is not None, label
        assert c.measured_from is not None, label
