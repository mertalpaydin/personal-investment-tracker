"""Unit accounting, TWR and XIRR.

The property that matters most is the first one tested: a contribution must not
create return. Everything else in this module is a consequence of getting that
right, and the two bugs it caught during development both produced
enthusiastically wrong numbers -- 877% and 1326% -- rather than obviously
broken ones.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.core import returns as R


def series(*rows: tuple[str, str, str]) -> list[tuple[date, Decimal, Decimal]]:
    return [(date.fromisoformat(d), Decimal(v), Decimal(f)) for d, v, f in rows]


# ---------------------------------------------------------------------------
# The core property
# ---------------------------------------------------------------------------


def test_a_contribution_creates_no_return() -> None:
    """Adding 10,000 to a 10,000 portfolio is not a 100% gain.

    This is the whole reason for unit accounting; a naive value-over-value
    ratio reports the owner's savings as performance.
    """
    states = R.build_units(series(("2026-01-05", "10000", "0"), ("2026-01-06", "20000", "10000")))
    assert states[-1].unit_price == states[0].unit_price
    assert R.twr(states) == Decimal(0)


def test_a_withdrawal_creates_no_loss() -> None:
    states = R.build_units(series(("2026-01-05", "10000", "0"), ("2026-01-06", "6000", "-4000")))
    assert states[-1].unit_price == states[0].unit_price


def test_growth_is_return_and_is_measured() -> None:
    states = R.build_units(series(("2026-01-05", "10000", "0"), ("2026-01-06", "11000", "0")))
    assert R.twr(states) == Decimal("0.1")


def test_growth_and_a_contribution_on_the_same_day_are_separated() -> None:
    """Value goes 10,000 -> 21,000 with 10,000 contributed: a 10% gain, not 110%."""
    states = R.build_units(series(("2026-01-05", "10000", "0"), ("2026-01-06", "21000", "10000")))
    assert R.twr(states) == Decimal("0.1")


def test_units_times_price_equals_value_every_day() -> None:
    states = R.build_units(
        series(
            ("2026-01-05", "10000", "10000"),
            ("2026-01-06", "10500", "0"),
            ("2026-01-07", "21000", "10000"),
            ("2026-01-08", "20000", "0"),
        )
    )
    assert R.check_unit_identity(states) == []


# ---------------------------------------------------------------------------
# The acceptance criterion: TWR ignores timing, XIRR does not
# ---------------------------------------------------------------------------


def _two_portfolios() -> tuple[list[R.DayState], list[R.DayState]]:
    """Identical holdings and identical returns; contributions timed differently.

    Both hold an asset that rises 10% in the first half and 10% in the second.
    One contributes everything up front; the other adds half at the midpoint.
    """
    early = R.build_units(
        series(
            ("2026-01-01", "20000", "20000"),
            ("2026-06-01", "22000", "0"),
            ("2026-12-01", "24200", "0"),
        )
    )
    late = R.build_units(
        series(
            ("2026-01-01", "10000", "10000"),
            ("2026-06-01", "21000", "10000"),
            ("2026-12-01", "23100", "0"),
        )
    )
    return early, late


def test_twr_is_identical_regardless_of_contribution_timing() -> None:
    early, late = _two_portfolios()
    assert R.twr(early) == R.twr(late)


def test_xirr_differs_with_contribution_timing() -> None:
    """The same investments, a different personal outcome. Both are true."""
    early, late = _two_portfolios()

    def money_weighted(states: list[R.DayState]) -> Decimal | None:
        flows = [(s.on, -s.external_flow_eur) for s in states if s.external_flow_eur != 0]
        flows.append((states[-1].on, states[-1].value_eur))
        return R.xirr(flows)

    assert money_weighted(early) != money_weighted(late)


# ---------------------------------------------------------------------------
# XIRR
# ---------------------------------------------------------------------------


def test_xirr_recovers_a_known_rate() -> None:
    """Invest 1,000, receive 1,100 a year later: 10%."""
    rate = R.xirr([(date(2025, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("1100"))])
    assert rate is not None
    assert abs(rate - Decimal("0.1")) < Decimal("0.001")


def test_xirr_handles_a_loss() -> None:
    rate = R.xirr([(date(2025, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("900"))])
    assert rate is not None
    assert rate < 0


def test_xirr_returns_nothing_when_no_rate_can_exist() -> None:
    """All flows one sign: there is no rate that balances them, and inventing
    one would be worse than admitting it."""
    assert (
        R.xirr([(date(2025, 1, 1), Decimal("-100")), (date(2026, 1, 1), Decimal("-100"))]) is None
    )


def test_xirr_needs_at_least_two_flows() -> None:
    assert R.xirr([(date(2025, 1, 1), Decimal("-100"))]) is None


# ---------------------------------------------------------------------------
# Other metrics
# ---------------------------------------------------------------------------


def test_annualising_refuses_very_short_windows() -> None:
    """Annualising a fortnight produces something shaped like a forecast with
    none of the substance."""
    assert R.annualise(Decimal("0.02"), 14) is None
    assert R.annualise(Decimal("0.02"), 365) is not None


def test_annualising_a_full_year_is_the_period_return() -> None:
    result = R.annualise(Decimal("0.20"), 365)
    assert result is not None
    assert abs(result - Decimal("0.20")) < Decimal("0.0001")


def test_drawdown_is_measured_on_unit_price_not_value() -> None:
    """Otherwise a withdrawal registers as a crash."""
    states = R.build_units(
        series(
            ("2026-01-05", "10000", "0"),
            ("2026-01-06", "5000", "-5000"),  # half taken out, no loss
            ("2026-01-07", "5000", "0"),
        )
    )
    assert R.max_drawdown(states) == Decimal(0)


def test_drawdown_catches_a_real_fall() -> None:
    states = R.build_units(
        series(
            ("2026-01-05", "10000", "0"),
            ("2026-01-06", "8000", "0"),
            ("2026-01-07", "9000", "0"),
        )
    )
    drawdown = R.max_drawdown(states)
    assert drawdown is not None
    assert abs(drawdown - Decimal("-0.2")) < Decimal("0.0001")


def test_volatility_needs_enough_observations() -> None:
    short = R.build_units(series(("2026-01-05", "100", "0"), ("2026-01-06", "101", "0")))
    assert R.volatility(short) is None


def test_volatility_of_a_flat_series_is_zero() -> None:
    rows = [(f"2026-01-{d:02d}", "10000", "0") for d in range(1, 29)]
    states = R.build_units(series(*rows))  # type: ignore[arg-type]
    result = R.volatility(states)
    assert result is not None
    assert result == Decimal(0)


# ---------------------------------------------------------------------------
# Inception and edge cases
# ---------------------------------------------------------------------------


def test_the_first_day_has_no_return_to_report() -> None:
    states = R.build_units(series(("2026-01-05", "10000", "10000")))
    assert states[0].daily_return is None
    assert states[0].unit_price == R.INCEPTION_UNIT_PRICE


def test_a_portfolio_emptied_and_restarted_does_not_invent_a_return() -> None:
    states = R.build_units(
        series(
            ("2026-01-05", "10000", "10000"),
            ("2026-01-06", "0", "-10000"),
            ("2026-01-07", "5000", "5000"),
        )
    )
    assert R.check_unit_identity(states) == []
    # The restart contributes no return of its own.
    assert states[-1].daily_return == Decimal(0)


def test_an_empty_series_produces_nothing() -> None:
    assert R.build_units([]) == []
    assert R.twr([]) is None
    assert R.max_drawdown([]) is None


def test_a_drop_to_zero_value_is_a_total_loss_not_an_error() -> None:
    states = R.build_units(series(("2026-01-05", "10000", "0"), ("2026-01-06", "0", "0")))
    assert R.twr(states) == Decimal(-1)


# ---------------------------------------------------------------------------
# The headline total versus the valuation basis
#
# The owner's rule: the total is holdings plus deposit balances. Uninvested
# cash from sales and distributions is excluded, because whether it is still
# held cannot be known from the source data.
#
# Returns are computed on the complete value including that cash. Without
# that, a sale would drop the value on the day it settles and restore it when
# the money is spent -- a loss followed by an identical gain.
# ---------------------------------------------------------------------------


def test_selling_does_not_look_like_a_loss() -> None:
    """Holdings fall by 5,000 and cash rises by 5,000: no return either way."""
    states = R.build_units(
        series(
            ("2026-01-05", "100000", "0"),  # all holdings
            ("2026-01-06", "100000", "0"),  # 95,000 holdings + 5,000 cash
        )
    )
    assert R.twr(states) == Decimal(0)


def test_a_distribution_is_not_a_loss() -> None:
    """A fund's price falls on its ex-date by what it paid. With the payment
    counted, the two cancel; without it, the fall is booked as a loss."""
    with_income = R.build_units(
        series(("2026-01-05", "100000", "0"), ("2026-01-06", "100000", "0"))
    )
    without_income = R.build_units(
        series(("2026-01-05", "100000", "0"), ("2026-01-06", "98000", "0"))
    )
    assert R.twr(with_income) == Decimal(0)
    assert R.twr(without_income) is not None
    assert R.twr(without_income) < Decimal(0)
