"""When the target is reached, and what that answer depends on.

`core/goal.py` projects on contributions alone and assumes **no investment
return at all**. That is deliberately falsifiable and deliberately pessimistic,
and for a portfolio that actually compounds it is simply wrong in a known
direction: it always names a date too late.

This module offers the missing half without becoming a forecast. Two answers,
and neither of them is a prediction:

**A sensitivity table.** The reach date at 0%, 2%, 4%, 6% and 8% a year. This
is not five forecasts -- it is the statement *"the answer depends on this, and
here is how much"*. Seeing 2034 become 2030 across the range is the useful
output, because it tells the owner which assumption is doing the work. The
assumption is supplied, visible, and never chosen by the tool.

**A block bootstrap over the portfolio's own history.** Resampling the daily
returns this portfolio actually produced, in month-long blocks so a month
behaves like a month. That *is* a model, it is labelled as one, and its
sample window is printed beside every figure it produces -- because resampling
a past assumes the future is drawn from the same distribution, and that
assumption is the whole content of the answer.

## What this deliberately is not

It is not the Monte Carlo projection the original 45-table design specified,
and ADR 0003 cut. That was a percentile cone over *assumed* market parameters.
The difference is where the numbers come from: everything here is either an
assumption the owner states outright or an observation this portfolio made.
Nothing is invented, and no figure is offered without the thing it rests on
printed next to it.

It is also not a plan. There is no tax in it, no fees, no inflation, and no
sequence-of-returns advice. A projection that quietly became financial planning
would be the failure mode.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_tracker.core import goal
from portfolio_tracker.money import ZERO, quantize_money

ONE = Decimal(1)

#: The rates the sensitivity table is computed at. Spread wide on purpose: a
#: table clustered around one plausible number reads as a forecast with error
#: bars, which is exactly the impression to avoid.
DEFAULT_RATES: tuple[Decimal, ...] = (
    Decimal("0.00"),
    Decimal("0.02"),
    Decimal("0.04"),
    Decimal("0.06"),
    Decimal("0.08"),
)

#: Beyond this the arithmetic still works and the answer stops meaning
#: anything. `goal.MAX_PROJECTION_YEARS` makes the same call for the same
#: reason.
HORIZON_YEARS = 30

#: Days per bootstrap block. One trading month, so that within-month
#: autocorrelation survives resampling -- drawing single days independently
#: would erase volatility clustering and produce a suspiciously smooth cone.
BLOCK_DAYS = 21

#: Below this there is not enough history to resample. A year of daily returns
#: is already thin for a decade-long projection; less is not a worse estimate,
#: it is a different kind of object.
MIN_BOOTSTRAP_DAYS = 250

DEFAULT_PATHS = 1000

#: Fixed so the same inputs give the same answer. A projection that moved when
#: you re-ran it would be untrustworthy for reasons unrelated to its model.
DEFAULT_SEED = 20260816


@dataclass(frozen=True)
class Scenario:
    """The reach date under one stated annual return."""

    annual_return: Decimal
    reached_on: date | None
    months: int | None
    value_at_horizon_eur: Decimal


@dataclass
class Bootstrap:
    """What this portfolio's own history would have produced, resampled."""

    paths: int
    block_days: int
    sample_days: int
    sample_start: date
    sample_end: date
    #: Reach dates at the 10th, 50th and 90th percentile of simulated paths.
    #: "Earlier than 9 in 10 simulated paths" -- not a probability about you.
    earliest_decile: date | None
    median: date | None
    latest_decile: date | None
    never_reached_pct: Decimal
    caveats: list[str] = field(default_factory=list)


@dataclass
class Projection:
    target_eur: Decimal
    current_eur: Decimal
    monthly_contribution_eur: Decimal
    as_of: date
    horizon_years: int
    already_reached: bool
    scenarios: list[Scenario] = field(default_factory=list)
    bootstrap: Bootstrap | None = None
    caveats: list[str] = field(default_factory=list)


class ProjectionError(ValueError):
    """The projection cannot be computed as asked."""


# ---------------------------------------------------------------------------
# The deterministic half
# ---------------------------------------------------------------------------


def monthly_rate(annual: Decimal) -> Decimal:
    """Compound an annual rate down to a monthly one.

    ``(1+r)^(1/12) - 1``, not ``r/12``. The naive division overstates the
    monthly rate and compounds the error over a decade of steps.
    """
    if annual <= Decimal(-1):
        raise ProjectionError(f"an annual return of {annual} would wipe the portfolio out")
    if annual == ZERO:
        return ZERO
    return ((ONE + annual).ln() / Decimal(12)).exp() - ONE


def months_to_target(
    *,
    current: Decimal,
    monthly_contribution: Decimal,
    rate: Decimal,
    target: Decimal,
    horizon_months: int,
) -> tuple[int | None, Decimal]:
    """Step month by month until the target is met, or the horizon runs out.

    Contributions land at the **end** of each month and earn nothing that
    month. Assuming they arrive at the start would compound money that has not
    been paid in yet, which flatters every figure by a few months.

    Returns ``(months, value_at_horizon)``; ``months`` is None if the target is
    never reached inside the horizon.
    """
    value = current
    reached: int | None = None
    for month in range(1, horizon_months + 1):
        value = value * (ONE + rate) + monthly_contribution
        if reached is None and value >= target:
            reached = month
    return reached, quantize_money(value)


# ---------------------------------------------------------------------------
# The bootstrap half
# ---------------------------------------------------------------------------


def _daily_returns(conn: sqlite3.Connection, person_id: int) -> list[tuple[date, Decimal]]:
    return [
        (date.fromisoformat(row["date"]), Decimal(str(row["daily_return"])))
        for row in conn.execute(
            "SELECT date, daily_return FROM portfolio_daily WHERE person_id = ? "
            "AND daily_return IS NOT NULL ORDER BY date",
            (person_id,),
        )
    ]


def block_factors(returns: list[Decimal], block_days: int = BLOCK_DAYS) -> list[Decimal]:
    """Compound each overlapping ``block_days`` window into one growth factor.

    Overlapping rather than disjoint blocks: 528 days yields ~500 usable
    windows instead of 25, and the pool being thin is the binding constraint on
    whether any of this means anything.
    """
    if len(returns) < block_days:
        return []
    factors: list[Decimal] = []
    for start in range(len(returns) - block_days + 1):
        factor = ONE
        for daily in returns[start : start + block_days]:
            factor *= ONE + daily
        factors.append(factor)
    return factors


def _bootstrap(
    *,
    factors: list[Decimal],
    current: Decimal,
    monthly_contribution: Decimal,
    target: Decimal,
    horizon_months: int,
    paths: int,
    seed: int,
) -> tuple[list[int | None], int]:
    """Simulate ``paths`` futures. Returns the month each reached the target."""
    rng = random.Random(seed)
    outcomes: list[int | None] = []
    never = 0
    for _ in range(paths):
        value = current
        reached: int | None = None
        for month in range(1, horizon_months + 1):
            value = value * factors[rng.randrange(len(factors))] + monthly_contribution
            if reached is None and value >= target:
                reached = month
                break
        if reached is None:
            never += 1
        outcomes.append(reached)
    return outcomes, never


def _percentile(sorted_months: list[int], fraction: Decimal) -> int | None:
    if not sorted_months:
        return None
    index = int((Decimal(len(sorted_months) - 1) * fraction).to_integral_value())
    return sorted_months[index]


# ---------------------------------------------------------------------------
# The public call
# ---------------------------------------------------------------------------


def project(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    target_eur: Decimal | None = None,
    monthly_eur: Decimal | None = None,
    rates: tuple[Decimal, ...] = DEFAULT_RATES,
    horizon_years: int = HORIZON_YEARS,
    paths: int = DEFAULT_PATHS,
    seed: int = DEFAULT_SEED,
    include_bootstrap: bool = True,
) -> Projection:
    """Project the goal under stated returns, and under this portfolio's own.

    ``target_eur`` and ``monthly_eur`` override the stored target and the
    measured contribution rate, which is what makes "what if I saved EUR 800 a
    month" answerable without any forecasting: it is arithmetic on a number the
    owner supplies.
    """
    state = goal.progress(conn, person_id=person_id)
    target = quantize_money(target_eur) if target_eur is not None else state.target_eur
    if target <= ZERO:
        raise ProjectionError("a goal target must be positive")

    monthly = (
        quantize_money(monthly_eur)
        if monthly_eur is not None
        else (state.monthly_contribution_eur or ZERO)
    )
    if monthly < ZERO:
        raise ProjectionError("a monthly contribution cannot be negative")

    horizon_months = horizon_years * 12
    report = Projection(
        target_eur=target,
        current_eur=state.current_eur,
        monthly_contribution_eur=monthly,
        as_of=state.as_of,
        horizon_years=horizon_years,
        already_reached=state.current_eur >= target,
    )
    if report.already_reached:
        report.caveats = ["The target has already been reached."]
        return report

    for annual in rates:
        months, final = months_to_target(
            current=state.current_eur,
            monthly_contribution=monthly,
            rate=monthly_rate(annual),
            target=target,
            horizon_months=horizon_months,
        )
        report.scenarios.append(
            Scenario(
                annual_return=annual,
                reached_on=goal.add_months(state.as_of, months) if months else None,
                months=months,
                value_at_horizon_eur=final,
            )
        )

    if include_bootstrap:
        report.bootstrap = _build_bootstrap(
            conn,
            person_id=person_id,
            current=state.current_eur,
            monthly=monthly,
            target=target,
            horizon_months=horizon_months,
            as_of=state.as_of,
            paths=paths,
            seed=seed,
        )

    report.caveats = _caveats(report, monthly_supplied=monthly_eur is not None)
    return report


def _build_bootstrap(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    current: Decimal,
    monthly: Decimal,
    target: Decimal,
    horizon_months: int,
    as_of: date,
    paths: int,
    seed: int,
) -> Bootstrap | None:
    observed = _daily_returns(conn, person_id)
    if len(observed) < MIN_BOOTSTRAP_DAYS:
        return None

    factors = block_factors([r for _, r in observed])
    if not factors:
        return None

    outcomes, never = _bootstrap(
        factors=factors,
        current=current,
        monthly_contribution=monthly,
        target=target,
        horizon_months=horizon_months,
        paths=paths,
        seed=seed,
    )
    reached = sorted(m for m in outcomes if m is not None)

    def when(fraction: str) -> date | None:
        months = _percentile(reached, Decimal(fraction))
        return goal.add_months(as_of, months) if months else None

    sample_years = Decimal(len(observed)) / Decimal(252)
    return Bootstrap(
        paths=paths,
        block_days=BLOCK_DAYS,
        sample_days=len(observed),
        sample_start=observed[0][0],
        sample_end=observed[-1][0],
        earliest_decile=when("0.1"),
        median=when("0.5"),
        latest_decile=when("0.9"),
        never_reached_pct=(Decimal(never) / Decimal(paths)) if paths else ZERO,
        caveats=[
            f"Resampled from {len(observed)} days ({sample_years:.1f} years) of THIS "
            f"portfolio's own returns, {observed[0][0]} to {observed[-1][0]}. It contains "
            "whatever happened in that window and nothing else.",
            "Resampling a past assumes the future is drawn from the same distribution. "
            "That assumption is the entire content of these figures, and it is an "
            "assumption rather than a finding.",
            "A decile is a property of the simulated paths, not a probability about your "
            "portfolio. 'Earlier than 9 in 10 simulated paths' is the whole claim.",
            f"Blocks of {BLOCK_DAYS} days keep a month behaving like a month; drawing "
            "single days would erase volatility clustering and flatter the spread.",
        ],
    )


def _caveats(report: Projection, *, monthly_supplied: bool) -> list[str]:
    out = [
        "The rates in the table are ASSUMPTIONS, not predictions. The tool does not "
        "choose between them; the spread across the table is the useful part, because "
        "it shows how much the answer rests on the assumption.",
        "Contributions are added at the end of each month and earn nothing that month.",
        "No tax, no fees and no inflation. These are nominal euros, and the target is "
        "a nominal number too.",
    ]
    if monthly_supplied:
        out.append(
            f"The monthly contribution of EUR {report.monthly_contribution_eur:,.2f} was "
            "supplied, not measured."
        )
    elif report.monthly_contribution_eur > ZERO:
        out.append(
            f"EUR {report.monthly_contribution_eur:,.2f} a month is the measured average "
            "of actual contributions -- several of which were inferred, not recorded -- "
            "and it is assumed to continue."
        )
    else:
        out.append(
            "No contribution rate could be measured, so the projection is pure "
            "compounding of what is already held."
        )
    if report.bootstrap is None:
        out.append(
            f"Too little history to resample (needs {MIN_BOOTSTRAP_DAYS} days of returns), "
            "so only the stated-rate table is offered."
        )
    return out


def annualised(factor: Decimal, days: int) -> Decimal:
    """A growth factor over ``days`` expressed as an annual rate."""
    if days <= 0 or factor <= ZERO:
        return ZERO
    return (factor.ln() * Decimal(252) / Decimal(days)).exp() - ONE


def observed_annual_return(conn: sqlite3.Connection, person_id: int = 1) -> Decimal | None:
    """What this portfolio actually returned, annualised. Context, not a rate to use.

    Printed beside the sensitivity table so the owner can see which column their
    own history sits nearest -- and, in this portfolio's case, how short the
    window producing it is.
    """
    observed = _daily_returns(conn, person_id)
    if len(observed) < MIN_BOOTSTRAP_DAYS:
        return None
    factor = ONE
    for _, daily in observed:
        factor *= ONE + daily
    return annualised(factor, len(observed))
