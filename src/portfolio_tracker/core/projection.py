"""When the target is reached, and what that answer depends on.

A projection has exactly two moving parts, and they are not the same kind of
thing:

**Contributions** -- money you decide to add. Measured from your recent history
as a *starting suggestion*, and meant to be overwritten. Nobody's savings rate
is a natural constant, and the tool has no business treating yours as one.

**Returns** -- what the assets do. Not decided by anyone, and the only part
worth modelling statistically.

Keeping them apart is the whole design. Adding them together into one "growth
rate" -- which is what the first version of this module effectively did -- makes
a savings decision look like a market observation.

## What the return model is built from

The **price history of the instruments currently held**, combined at today's
weights. Not the portfolio's own past performance.

That distinction is the correction that produced this rewrite. A portfolio's
historical return series is contaminated for this purpose: it contains positions
since sold, the timing of past contributions, and weights that no longer
describe anything. Projecting forward from it means projecting a portfolio that
no longer exists. On the author's own data the difference was not subtle -- a
fully-closed crypto position that lost 40% and several disposed lots were still
influencing the forecast for a portfolio that holds none of them.

So the series here answers: *what would the assets I hold today have done over
this window?* Each instrument's euro return is its local price move combined
with the currency move, exactly as in ADR 0004::

    (1 + local return) x (1 + FX return) = (1 + EUR return)

and the portfolio series is those, weighted by what is held now.

## Blocks, and why they are long

Resampling single days would scatter a crash Monday next to a calm Tuesday and
destroy the clustering that makes real markets risky. Blocks keep runs intact.

The default is a **year**, because the thing a multi-year projection is exposed
to is good and bad *years* clumping, not good and bad weeks. It is configurable,
and the honest constraint is printed with the result: a two-year history divided
into one-year blocks supports about two independent observations, however many
overlapping windows can be cut from it. `effective_samples` says so.

## What this is not

Not a Monte Carlo over assumed market parameters -- the thing ADR 0003 cut and
ADR 0008 refused. Every number here is either an assumption stated outright by
the owner or a price this portfolio's holdings actually traded at. Nothing is
invented, and no figure appears without the thing it rests on printed beside it.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise

from portfolio_tracker.core import fx, goal
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

#: Beyond this the arithmetic still works and the answer stops meaning anything.
HORIZON_YEARS = 30

#: One trading year. Long enough that good and bad *years* survive resampling,
#: which is what a decade-long projection is actually exposed to. Configurable
#: because the right answer depends on how much history exists to cut it from.
BLOCK_DAYS = 252

#: Common alternatives, for callers offering a choice.
BLOCK_CHOICES: dict[str, int] = {"month": 21, "quarter": 63, "year": 252}

#: Below this there is not enough history to resample at all.
MIN_BOOTSTRAP_DAYS = 250

#: An instrument with less history than this is left out of the return model
#: rather than given a short, unrepresentative series.
MIN_INSTRUMENT_DAYS = 120

#: If the return model covers less of the portfolio than this, it is not a model
#: of the portfolio and is not offered.
MIN_COVERAGE = Decimal("0.50")

#: Blocks per history length below which the sample supports too few genuinely
#: independent draws to mean much. Overlapping windows do not fix this.
MIN_EFFECTIVE_SAMPLES = Decimal(3)

#: The window contributions are measured over, unless the caller says otherwise.
#: Recent enough to reflect what you are doing now rather than what you did two
#: years ago, long enough that one unusual month does not set the rate.
CONTRIBUTION_WINDOW_MONTHS = 6

DEFAULT_PATHS = 1000

#: Fixed so the same inputs give the same answer. A projection that moved when
#: you re-ran it would be untrustworthy for reasons unrelated to its model.
DEFAULT_SEED = 20260816


class ProjectionError(ValueError):
    """The projection cannot be computed as asked."""


# ---------------------------------------------------------------------------
# The two moving parts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contributions:
    """What you have been adding, and what the projection will assume.

    ``source`` is the point. A measured figure is a suggestion drawn from a
    stated window; a supplied one is your decision. They must never be
    presentable as the same kind of number.
    """

    monthly_eur: Decimal
    source: str  # measured | supplied
    window_months: int | None = None
    total_eur: Decimal | None = None
    measured_from: date | None = None
    measured_to: date | None = None

    @property
    def sentence(self) -> str:
        if self.source == "supplied":
            return f"EUR {self.monthly_eur:,.2f} a month, as you specified."
        if self.total_eur is None or self.measured_from is None:
            return "No contribution history to measure."
        return (
            f"You added EUR {self.total_eur:,.2f} between {self.measured_from} and "
            f"{self.measured_to} -- EUR {self.monthly_eur:,.2f} a month. Change it if "
            "that is not what you plan to keep doing."
        )


@dataclass
class ReturnModel:
    """The return series the bootstrap draws from, and what it covers."""

    holdings: list[tuple[str, Decimal]]  # import_key, weight used
    #: Share of today's portfolio whose instruments are modelled at all.
    covered_pct: Decimal
    #: Share represented on the *thinnest* day of the window. Lower than
    #: ``covered_pct`` whenever a holding was bought part-way through it.
    min_daily_coverage: Decimal = ZERO
    excluded: list[tuple[str, str]] = field(default_factory=list)
    series: list[Decimal] = field(default_factory=list)
    start: date | None = None
    end: date | None = None

    @property
    def days(self) -> int:
        return len(self.series)

    def effective_samples(self, block_days: int) -> Decimal:
        """Roughly how many independent blocks the history supports.

        Overlapping windows inflate the *count* of blocks without adding
        information; this is the figure that does not lie about it.
        """
        if block_days <= 0:
            return ZERO
        return Decimal(self.days) / Decimal(block_days)


@dataclass(frozen=True)
class Scenario:
    """The reach date under one stated annual return."""

    annual_return: Decimal
    reached_on: date | None
    months: int | None
    value_at_horizon_eur: Decimal


@dataclass
class Bootstrap:
    """What today's holdings would have done, resampled from their own prices."""

    paths: int
    block_days: int
    model: ReturnModel
    earliest_decile: date | None
    median: date | None
    latest_decile: date | None
    never_reached_pct: Decimal
    effective_samples: Decimal
    caveats: list[str] = field(default_factory=list)


@dataclass
class Projection:
    target_eur: Decimal
    current_eur: Decimal
    contributions: Contributions
    as_of: date
    horizon_years: int
    already_reached: bool
    scenarios: list[Scenario] = field(default_factory=list)
    bootstrap: Bootstrap | None = None
    caveats: list[str] = field(default_factory=list)

    @property
    def monthly_contribution_eur(self) -> Decimal:
        return self.contributions.monthly_eur


# ---------------------------------------------------------------------------
# Contributions -- measured as a suggestion, never as a constant
# ---------------------------------------------------------------------------


def measure_contributions(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    as_of: date,
    window_months: int = CONTRIBUTION_WINDOW_MONTHS,
) -> Contributions:
    """What was actually contributed over the recent window.

    A *recent* window rather than the whole history: a savings rate from two
    years ago is not evidence about next month, and averaging it in makes the
    figure harder to recognise as yours.
    """
    since = goal.add_months(as_of, -window_months)
    total = ZERO
    for row in conn.execute(
        "SELECT net_external_flow_eur FROM portfolio_daily "
        "WHERE person_id = ? AND date > ? AND date <= ?",
        (person_id, since.isoformat(), as_of.isoformat()),
    ):
        total += Decimal(str(row["net_external_flow_eur"] or 0))

    monthly = quantize_money(total / Decimal(window_months)) if window_months else ZERO
    return Contributions(
        monthly_eur=max(monthly, ZERO),
        source="measured",
        window_months=window_months,
        total_eur=quantize_money(total),
        measured_from=since,
        measured_to=as_of,
    )


# ---------------------------------------------------------------------------
# Returns -- from the prices of what is held now
# ---------------------------------------------------------------------------


def _current_weights(
    conn: sqlite3.Connection, person_id: int, as_of: date
) -> list[tuple[int, str, Decimal, Decimal]]:
    """(instrument_id, import_key, market value, weight) for today's holdings."""
    rows = conn.execute(
        "SELECT i.id, i.import_key, total(p.market_value_eur) AS v "
        "FROM position_daily p JOIN instrument i ON i.id = p.instrument_id "
        "JOIN account a ON a.id = p.account_id "
        "WHERE p.date = ? AND a.person_id = ? GROUP BY i.id HAVING v > 0",
        (as_of.isoformat(), person_id),
    ).fetchall()
    total = sum((Decimal(str(r["v"])) for r in rows), ZERO)
    if total <= ZERO:
        return []
    return [
        (int(r["id"]), str(r["import_key"]), Decimal(str(r["v"])), Decimal(str(r["v"])) / total)
        for r in rows
    ]


def _instrument_eur_returns(
    conn: sqlite3.Connection, instrument_id: int, currency: str
) -> dict[date, Decimal]:
    """Daily euro return of one unit of an instrument, from its own price history.

    The euro return is the local price move combined with the currency move --
    ADR 0004's identity, applied to a return rather than to an amount. No sum of
    money is converted here, which is why this module is not a currency
    conversion site.
    """
    prices = [
        (date.fromisoformat(r["date"]), Decimal(str(r["close"])))
        for r in conn.execute(
            "SELECT date, close FROM price WHERE instrument_id = ? ORDER BY date",
            (instrument_id,),
        )
        if r["close"] is not None
    ]
    if len(prices) < 2:
        return {}

    rates: dict[date, Decimal] = {}
    if currency != fx.BASE:
        days = [d for d, _ in prices]
        observed = fx.get_rate_series(conn, currency, days, strict=False)
        rates = {day: point.rate for day, point in observed.items()}

    out: dict[date, Decimal] = {}
    for (before, price_before), (today, price_today) in pairwise(prices):
        if price_before <= ZERO:
            continue
        local = price_today / price_before - ONE
        currency_leg = ONE
        if currency != fx.BASE:
            rate_before, rate_today = rates.get(before), rates.get(today)
            if not rate_before or not rate_today or rate_today <= ZERO:
                continue
            # Rates are quote-per-euro, so a rising rate is a weaker currency.
            currency_leg = rate_before / rate_today
        out[today] = (ONE + local) * currency_leg - ONE
    return out


def build_return_model(conn: sqlite3.Connection, *, person_id: int = 1, as_of: date) -> ReturnModel:
    """The daily euro return today's holdings would have produced, at today's weights.

    Instruments with too little price history are **excluded and named** rather
    than given a short series that would understate their volatility. The share
    of the portfolio actually represented is reported, because a return model
    covering 60% of the value is a different claim from one covering all of it.
    """
    holdings = _current_weights(conn, person_id, as_of)
    model = ReturnModel(holdings=[], covered_pct=ZERO)
    if not holdings:
        return model

    total_value = sum((value for _, _, value, _ in holdings), ZERO)
    usable: list[tuple[str, Decimal, dict[date, Decimal]]] = []

    for instrument_id, key, value, _weight in holdings:
        row = conn.execute(
            "SELECT currency FROM instrument WHERE id = ?", (instrument_id,)
        ).fetchone()
        series = _instrument_eur_returns(conn, instrument_id, str(row["currency"]))
        if len(series) < MIN_INSTRUMENT_DAYS:
            model.excluded.append(
                (key, f"only {len(series)} day(s) of price history; needs {MIN_INSTRUMENT_DAYS}")
            )
            continue
        usable.append((key, value, series))

    if not usable:
        return model

    covered = sum((value for _, value, _ in usable), ZERO)
    model.covered_pct = covered / total_value if total_value > ZERO else ZERO
    # Re-weighted among themselves: the excluded holdings are absent from the
    # model, not silently assumed to return zero.
    model.holdings = [(key, value / covered) for key, value, _ in usable]

    weights = dict(model.holdings)

    # Deliberately NOT the window where every holding has a price.
    #
    # Prices are stored from each instrument's first trade, so one fund bought
    # recently would truncate the whole model to that fund's lifetime. On the
    # example corpus that threw away half the usable history and left the
    # bootstrap unable to run at all.
    #
    # Instead each day uses the holdings that existed then, renormalised among
    # themselves, and a day representing too little of today's portfolio is
    # dropped rather than stretched. `min_daily_coverage` reports the thinnest
    # day kept, because "72% of the portfolio on the worst day" is a materially
    # weaker claim than "all of it" and must not read the same.
    by_day: dict[date, Decimal] = {}
    present: dict[date, Decimal] = {}
    for key, _, series in usable:
        weight = weights[key]
        for day, daily in series.items():
            by_day[day] = by_day.get(day, ZERO) + weight * daily
            present[day] = present.get(day, ZERO) + weight

    days = [d for d in sorted(by_day) if present[d] >= MIN_COVERAGE]
    if not days:
        return model

    model.series = [by_day[d] / present[d] for d in days]
    model.min_daily_coverage = min(present[d] for d in days)
    model.start, model.end = days[0], days[-1]
    return model


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


def block_factors(returns: list[Decimal], block_days: int = BLOCK_DAYS) -> list[Decimal]:
    """Compound each overlapping ``block_days`` window into one growth factor."""
    if block_days <= 0 or len(returns) < block_days:
        return []
    factors: list[Decimal] = []
    for start in range(len(returns) - block_days + 1):
        factor = ONE
        for daily in returns[start : start + block_days]:
            factor *= ONE + daily
        factors.append(factor)
    return factors


def _monthly_from_block(factor: Decimal, block_days: int) -> Decimal:
    """A block's growth expressed per month, so paths can step monthly.

    Contributions arrive monthly, so the simulation has to. Converting the block
    to a monthly rate keeps one draw meaning one *regime* while the cash flow
    stays on its real schedule.
    """
    if factor <= ZERO:
        return Decimal("-0.999999")
    months = Decimal(block_days) / Decimal(21)
    return (factor.ln() / months).exp() - ONE


@dataclass
class PathSet:
    """Every simulated path, and when each first met the target.

    Kept as first-hit months rather than a full value matrix, because that is
    what every question here actually asks: "by when am I confident" and "how
    much earlier does this lever get me there". ``bands`` is populated only
    when a caller wants to draw the fan, since it costs one list per month.
    """

    reached: list[int | None]
    horizon_months: int
    paths: int
    #: month -> the sorted end-of-month values across paths, for percentiles.
    bands: dict[int, list[Decimal]] = field(default_factory=dict)

    @property
    def never_reached(self) -> int:
        return sum(1 for m in self.reached if m is None)

    @property
    def hit_months(self) -> list[int]:
        return sorted(m for m in self.reached if m is not None)


def months_at_confidence(paths: PathSet, confidence: Decimal) -> int | None:
    """Earliest month by which at least ``confidence`` of paths have arrived.

    Cumulative first-hit rather than "value at month m is above target": a
    path that reaches the target and later dips below it has still reached it,
    and for somebody saving toward a purchase that is the answer they want.

    Returns None when the horizon ends before that many paths arrive -- which
    is a real answer, and must never be extrapolated into one.
    """
    if not paths.paths:
        return None
    needed = int((Decimal(paths.paths) * confidence).to_integral_value(rounding=ROUND_CEILING))
    arrived = 0
    counts: dict[int, int] = {}
    for month in paths.reached:
        if month is not None:
            counts[month] = counts.get(month, 0) + 1
    for month in range(1, paths.horizon_months + 1):
        arrived += counts.get(month, 0)
        if arrived >= needed:
            return month
    return None


def simulate_paths(
    *,
    factors: list[Decimal],
    block_days: int,
    current: Decimal,
    monthly_contribution: Decimal,
    target: Decimal,
    horizon_months: int,
    paths: int,
    seed: int,
    lump_sums: tuple[tuple[int, Decimal], ...] = (),
    contribution_growth_annual: Decimal = ZERO,
    return_delta_monthly: Decimal = ZERO,
    with_bands: bool = False,
) -> PathSet:
    """Resample the return history forward, one month at a time.

    Every path runs the full horizon even after it has arrived, so a scenario
    can be compared with the baseline at any month and the fan chart has
    something to draw. The first-hit month is recorded rather than the loop
    being cut short.

    ``return_delta_monthly`` is added to each sampled monthly return rather
    than scaling it, which preserves the volatility and the correlation
    structure the block bootstrap exists to capture. It is an assumption about
    a different world, not a property of this portfolio -- callers are expected
    to label it as such.

    The same ``seed`` reproduces the same draws, which is what makes a
    scenario comparison meaningful: the difference between two runs is then
    the lever, not the sampling.
    """
    monthly = [_monthly_from_block(f, block_days) for f in factors]
    if not monthly:
        return PathSet(reached=[], horizon_months=horizon_months, paths=0)

    lumps: dict[int, Decimal] = {}
    for month, amount in lump_sums:
        if 1 <= month <= horizon_months:
            lumps[month] = lumps.get(month, ZERO) + amount

    # Compounded monthly, so "2% a year more each year" is the same statement
    # however long the horizon is.
    growth = (
        (ONE + contribution_growth_annual) ** Decimal("0.0833333333") - ONE
        if (contribution_growth_annual)
        else ZERO
    )

    rng = random.Random(seed)
    reached: list[int | None] = []
    bands: dict[int, list[Decimal]] = (
        {m: [] for m in range(1, horizon_months + 1)} if with_bands else {}
    )

    for _ in range(paths):
        value = current
        contribution = monthly_contribution
        hit: int | None = None
        for month in range(1, horizon_months + 1):
            drawn = monthly[rng.randrange(len(monthly))] + return_delta_monthly
            value = value * (ONE + drawn) + contribution
            if month in lumps:
                value += lumps[month]
            if hit is None and value >= target:
                hit = month
            if with_bands:
                bands[month].append(value)
            if growth:
                contribution *= ONE + growth
        reached.append(hit)

    if with_bands:
        for month in bands:
            bands[month].sort()
    return PathSet(reached=reached, horizon_months=horizon_months, paths=paths, bands=bands)


def _simulate(
    *,
    factors: list[Decimal],
    block_days: int,
    current: Decimal,
    monthly_contribution: Decimal,
    target: Decimal,
    horizon_months: int,
    paths: int,
    seed: int,
) -> tuple[list[int], int]:
    """The shape `project()` has always used, over the shared simulation."""
    result = simulate_paths(
        factors=factors,
        block_days=block_days,
        current=current,
        monthly_contribution=monthly_contribution,
        target=target,
        horizon_months=horizon_months,
        paths=paths,
        seed=seed,
    )
    return result.hit_months, result.never_reached


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
    contribution_window_months: int = CONTRIBUTION_WINDOW_MONTHS,
    rates: tuple[Decimal, ...] = DEFAULT_RATES,
    horizon_years: int = HORIZON_YEARS,
    block_days: int = BLOCK_DAYS,
    paths: int = DEFAULT_PATHS,
    seed: int = DEFAULT_SEED,
    include_bootstrap: bool = True,
) -> Projection:
    """Project the goal across stated returns, and across what the holdings did."""
    state = goal.progress(conn, person_id=person_id)
    target = quantize_money(target_eur) if target_eur is not None else state.target_eur
    if target <= ZERO:
        raise ProjectionError("a goal target must be positive")
    if monthly_eur is not None and monthly_eur < ZERO:
        raise ProjectionError("a monthly contribution cannot be negative")

    contributions = (
        Contributions(monthly_eur=quantize_money(monthly_eur), source="supplied")
        if monthly_eur is not None
        else measure_contributions(
            conn,
            person_id=person_id,
            as_of=state.as_of,
            window_months=contribution_window_months,
        )
    )

    horizon_months = horizon_years * 12
    report = Projection(
        target_eur=target,
        current_eur=state.current_eur,
        contributions=contributions,
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
            monthly_contribution=contributions.monthly_eur,
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
            contributions=contributions,
            target=target,
            horizon_months=horizon_months,
            as_of=state.as_of,
            block_days=block_days,
            paths=paths,
            seed=seed,
        )

    report.caveats = _caveats(report)
    return report


def _build_bootstrap(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    current: Decimal,
    contributions: Contributions,
    target: Decimal,
    horizon_months: int,
    as_of: date,
    block_days: int,
    paths: int,
    seed: int,
) -> Bootstrap | None:
    model = build_return_model(conn, person_id=person_id, as_of=as_of)
    if model.days < MIN_BOOTSTRAP_DAYS or model.covered_pct < MIN_COVERAGE:
        return None

    factors = block_factors(model.series, block_days)
    if not factors:
        return None

    reached, never = _simulate(
        factors=factors,
        block_days=block_days,
        current=current,
        monthly_contribution=contributions.monthly_eur,
        target=target,
        horizon_months=horizon_months,
        paths=paths,
        seed=seed,
    )

    def when(fraction: str) -> date | None:
        months = _percentile(reached, Decimal(fraction))
        return goal.add_months(as_of, months) if months else None

    samples = model.effective_samples(block_days)
    caveats = [
        f"Built from the price history of the {len(model.holdings)} holding(s) you own "
        f"today, at today's weights -- not from the portfolio's own past performance, "
        "which includes positions you have since sold.",
        f"Covers {model.covered_pct:.0%} of the portfolio by value, over "
        f"{model.days} trading days ({model.start} to {model.end}). On the thinnest "
        f"day of that window {model.min_daily_coverage:.0%} of today's holdings had a "
        "price; the rest had not been bought yet.",
        "Resampling a past assumes the future is drawn from the same distribution. "
        "That assumption is the entire content of these figures.",
        "A decile is a property of the simulated paths, not a probability about your portfolio.",
    ]
    if samples < MIN_EFFECTIVE_SAMPLES:
        caveats.insert(
            0,
            f"WEAK: {model.days} days cut into {block_days}-day blocks is about "
            f"{samples:.1f} independent observations. Overlapping windows do not add "
            "information. Treat the spread below as illustrative, not measured.",
        )
    for key, why in model.excluded:
        caveats.append(f"{key} is not in the model: {why}.")

    return Bootstrap(
        paths=paths,
        block_days=block_days,
        model=model,
        earliest_decile=when("0.1"),
        median=when("0.5"),
        latest_decile=when("0.9"),
        never_reached_pct=(Decimal(never) / Decimal(paths)) if paths else ZERO,
        effective_samples=samples,
        caveats=caveats,
    )


def _caveats(report: Projection) -> list[str]:
    out = [
        "The rates in the table are ASSUMPTIONS, not predictions. The tool does not "
        "choose between them; the spread across the table is the useful part.",
        report.contributions.sentence,
        "Contributions are added at the end of each month and earn nothing that month.",
        "No tax, no fees and no inflation. These are nominal euros.",
    ]
    if report.bootstrap is None:
        out.append(
            "No return model: too little price history for the holdings, or too little "
            "of the portfolio covered. Only the stated-rate table is offered."
        )
    return out


def observed_annual_return(
    conn: sqlite3.Connection, person_id: int = 1, *, as_of: date | None = None
) -> Decimal | None:
    """What today's holdings returned over the model window, annualised.

    Context, not a rate to plan on -- and deliberately never one of the table's
    rows. It answers "which column is my history nearest?", which is a question
    about the past.
    """
    on = as_of or _latest_valuation(conn, person_id)
    if on is None:
        return None
    model = build_return_model(conn, person_id=person_id, as_of=on)
    if model.days < MIN_BOOTSTRAP_DAYS:
        return None
    factor = ONE
    for daily in model.series:
        factor *= ONE + daily
    if factor <= ZERO:
        return None
    return (factor.ln() * Decimal(252) / Decimal(model.days)).exp() - ONE


def _latest_valuation(conn: sqlite3.Connection, person_id: int) -> date | None:
    row = conn.execute(
        "SELECT max(date) AS d FROM portfolio_daily WHERE person_id = ?", (person_id,)
    ).fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def contribution_history(
    conn: sqlite3.Connection, *, person_id: int = 1, months: int = 24
) -> list[tuple[date, Decimal]]:
    """Contributions per calendar month, for showing what the suggestion rests on."""
    end = _latest_valuation(conn, person_id)
    if end is None:
        return []
    start = goal.add_months(end, -months)
    buckets: dict[date, Decimal] = {}
    for row in conn.execute(
        "SELECT date, net_external_flow_eur FROM portfolio_daily "
        "WHERE person_id = ? AND date > ? AND net_external_flow_eur <> '0' ORDER BY date",
        (person_id, start.isoformat()),
    ):
        when = date.fromisoformat(row["date"])
        key = when.replace(day=1)
        buckets[key] = buckets.get(key, ZERO) + Decimal(str(row["net_external_flow_eur"]))
    return sorted(buckets.items())


def next_month(when: date) -> date:
    """First day of the following month. Small, and used by the page."""
    return (when.replace(day=1) + timedelta(days=32)).replace(day=1)
