"""What difference a change would make, measured in months.

The question a projection cannot answer on its own: *if I put aside another
EUR 300 a month, when do I get there instead?* This runs the same bootstrap
twice -- once as things stand, once with a lever applied -- and reports the gap.

Four decisions shape everything here.

**The same seed, always.** Both runs draw the identical sequence of return
blocks, so the difference between them is the lever and not the sampling. Two
runs on different seeds would differ by a month or two from noise alone, which
is the same order as the effect being measured.

**75% confidence by default, not 50%.** A median date is a coin flip, and it is
the wrong headline for somebody buying a house. ``months_earlier`` is reported
at 50, 75 and 90 so the shape of the answer is visible, and the interface leads
with 75.

**What you control is separated from what you hope for.** Adding EUR 300 a
month is a decision. Assuming one percentage point more return is a wish about
the world, and a screen that lists them together invites the reader to treat
them as equally available. Every lever carries ``kind``, and a scenario
containing an assumed return carries a sentence saying that changing the
assumption changes nothing about the portfolio.

**Never past the horizon.** A scenario that does not reach the target inside
the horizon returns ``None`` and says so. Extrapolating would be inventing the
part of the answer that matters most.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from portfolio_tracker.core import cash, goal, projection
from portfolio_tracker.core.projection import Contributions
from portfolio_tracker.money import ZERO, quantize_money

ONE = Decimal(1)

#: The confidences every scenario is measured at. 0.75 is the headline.
CONFIDENCES: tuple[Decimal, ...] = (Decimal("0.50"), Decimal("0.75"), Decimal("0.90"))
HEADLINE_CONFIDENCE = Decimal("0.75")

#: Trading days a year, for turning an annual return delta into a monthly one.
MONTHS_PER_YEAR = 12

#: Levers that are decisions, as opposed to assumptions about the world.
CONTROLLABLE = (
    "monthly_contribution_eur",
    "lump_sums",
    "contribution_growth_pct_annual",
    "target_amount_eur",
    "target_months",
)
ASSUMED = ("return_annual_pct",)

ASSUMPTION_WARNING = (
    "Changing the assumed return does not change the portfolio. This shows what "
    "would follow if returns were different, not a way to make them so."
)


class ScenarioError(ValueError):
    """The scenario cannot be evaluated as asked."""


@dataclass
class Levers:
    """The changes a scenario applies. All optional; all default to no change.

    Stated as **what things would be**, not as differences from what they are.
    "Save EUR 1,500 a month" is a sentence somebody can check against their own
    intentions; "save EUR 300 a month more" asks them to remember what the tool
    measured first, and to trust that measurement. None means "leave this".
    """

    #: What you would put aside each month. None keeps the measured rate.
    monthly_contribution_eur: Decimal | None = None
    #: One-off amounts, as (months from now, amount).
    lump_sums: tuple[tuple[int, Decimal], ...] = ()
    #: Contributions rising each year, compounded.
    contribution_growth_pct_annual: Decimal = ZERO
    #: The annual return to assume. None resamples this portfolio's own price
    #: history, which is the default and the only setting that is evidence.
    return_annual_pct: Decimal | None = None
    #: Override the goal itself.
    target_amount_eur: Decimal | None = None
    #: A different horizon to measure against, in months.
    target_months: int | None = None

    @property
    def kinds(self) -> list[str]:
        """Which sorts of lever this scenario actually uses."""
        out: list[str] = []
        if (
            self.monthly_contribution_eur is not None
            or self.lump_sums
            or self.contribution_growth_pct_annual
            or self.target_amount_eur is not None
        ):
            out.append("controllable")
        if self.return_annual_pct is not None:
            out.append("assumption")
        return out

    @property
    def is_empty(self) -> bool:
        return not self.kinds

    def return_delta_monthly(self, implied_annual: Decimal) -> Decimal:
        """The monthly drift that turns the sampled returns into the chosen one.

        Added to each sampled month rather than rescaling it, which keeps the
        volatility and the sequencing the block bootstrap exists to capture.
        What changes is the average; how bumpy the ride is does not -- and a
        screen offering a higher return with a smoother ride would be
        describing something that does not exist.
        """
        if self.return_annual_pct is None:
            return ZERO
        annual = self.return_annual_pct - implied_annual
        if not annual:
            return ZERO
        return (ONE + annual) ** Decimal("0.0833333333") - ONE

    def as_dict(self) -> dict[str, object]:
        return {
            "monthly_contribution_eur": (
                str(self.monthly_contribution_eur)
                if self.monthly_contribution_eur is not None
                else None
            ),
            "lump_sums": [[m, str(a)] for m, a in self.lump_sums],
            "contribution_growth_pct_annual": str(self.contribution_growth_pct_annual),
            "return_annual_pct": (
                str(self.return_annual_pct) if self.return_annual_pct is not None else None
            ),
            "target_amount_eur": (
                str(self.target_amount_eur) if self.target_amount_eur is not None else None
            ),
            "target_months": self.target_months,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Levers:
        """Rebuild from stored JSON. Tolerant, because this is user input.

        Anything unreadable becomes "no change" rather than an exception: a
        pinned scenario written by an older version should degrade to a
        weaker answer, not stop the page from rendering.
        """

        def dec(key: str, default: Decimal = ZERO) -> Decimal:
            value = raw.get(key)
            return Decimal(str(value)) if value not in (None, "") else default

        def whole(key: str, default: int | None = None) -> int | None:
            value = raw.get(key)
            return int(value) if value not in (None, "") else default

        lumps: list[tuple[int, Decimal]] = []
        for entry in raw.get("lump_sums") or []:
            month, amount = entry
            lumps.append((int(month), Decimal(str(amount))))

        def maybe(key: str) -> Decimal | None:
            value = raw.get(key)
            return Decimal(str(value)) if value not in (None, "") else None

        return cls(
            monthly_contribution_eur=maybe("monthly_contribution_eur"),
            lump_sums=tuple(lumps),
            contribution_growth_pct_annual=dec("contribution_growth_pct_annual"),
            return_annual_pct=maybe("return_annual_pct"),
            target_amount_eur=maybe("target_amount_eur"),
            target_months=whole("target_months"),
        )


@dataclass
class Outcome:
    """What one run of the simulation produced."""

    monthly_contribution_eur: Decimal
    target_eur: Decimal
    horizon_months: int
    #: Confidence -> months until the target is met, or None inside the horizon.
    months_to_target: dict[str, int | None] = field(default_factory=dict)
    dates: dict[str, str | None] = field(default_factory=dict)
    prob_target_met: Decimal = ZERO
    #: Value percentiles at the horizon, for a fan chart.
    bands: list[dict[str, object]] = field(default_factory=list)
    shortfall_p10_eur: Decimal | None = None

    @property
    def headline_months(self) -> int | None:
        return self.months_to_target.get(str(HEADLINE_CONFIDENCE))


@dataclass
class Comparison:
    """A scenario measured against the baseline it was run beside."""

    name: str
    levers: Levers
    baseline: Outcome
    scenario: Outcome
    as_of: date
    seed: int
    #: Confidence -> months sooner (positive) or later (negative), or None.
    months_earlier: dict[str, int | None] = field(default_factory=dict)
    efficiency_months_per_100: Decimal | None = None
    kinds: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _month_dates(as_of: date, months: int | None) -> str | None:
    return goal.add_months(as_of, months).isoformat() if months is not None else None


def _outcome(
    *,
    paths: projection.PathSet,
    monthly: Decimal,
    target: Decimal,
    as_of: date,
    horizon_months: int,
    with_bands: bool,
) -> Outcome:
    out = Outcome(
        monthly_contribution_eur=monthly,
        target_eur=target,
        horizon_months=horizon_months,
    )
    for confidence in CONFIDENCES:
        months = projection.months_at_confidence(paths, confidence)
        out.months_to_target[str(confidence)] = months
        out.dates[str(confidence)] = _month_dates(as_of, months)

    arrived = paths.paths - paths.never_reached
    out.prob_target_met = (
        (Decimal(arrived) / Decimal(paths.paths)).quantize(Decimal("0.0001"))
        if paths.paths
        else ZERO
    )

    if with_bands and paths.bands:
        for month in sorted(paths.bands):
            values = paths.bands[month]
            if not values:
                continue
            out.bands.append(
                {
                    "month": month,
                    "date": goal.add_months(as_of, month).isoformat(),
                    "p05": quantize_money(_at(values, Decimal("0.05"))),
                    "p25": quantize_money(_at(values, Decimal("0.25"))),
                    "p50": quantize_money(_at(values, Decimal("0.50"))),
                    "p75": quantize_money(_at(values, Decimal("0.75"))),
                    "p95": quantize_money(_at(values, Decimal("0.95"))),
                }
            )
        final = paths.bands[max(paths.bands)]
        p10 = _at(final, Decimal("0.10"))
        # The number the plan called the useful one: not "you will have X" but
        # "in the worst tenth of outcomes you are this far short".
        out.shortfall_p10_eur = quantize_money(max(ZERO, target - p10))
    return out


def _at(sorted_values: list[Decimal], fraction: Decimal) -> Decimal:
    index = int((Decimal(len(sorted_values) - 1) * fraction).to_integral_value())
    return sorted_values[index]


def implied_annual_return(factors: list[Decimal], block_days: int) -> Decimal:
    """The annual return the resampled history averages out to.

    The bootstrap never states a return -- it draws from what actually
    happened -- but a screen offering "assume a different return" has to say
    what it is being different *from*. This is that number: the mean sampled
    month, compounded to a year.
    """
    if not factors:
        return ZERO
    monthly = [projection._monthly_from_block(f, block_days) for f in factors]
    mean = sum(monthly, ZERO) / Decimal(len(monthly))
    return ((ONE + mean) ** 12 - ONE).quantize(Decimal("0.0001"))


@dataclass(frozen=True)
class MeasuredReturn:
    """One window's actual return, both as it happened and per year."""

    period: Decimal | None
    annualised: Decimal | None
    days: int


@dataclass
class ReturnContext:
    """What this portfolio has actually done, for choosing an assumption.

    Offered because "assume a different return" is unanswerable without
    knowing what the tool is already using, and because a number picked from
    nowhere is how a projection becomes a wish.
    """

    implied_annual: Decimal
    measured: dict[str, MeasuredReturn] = field(default_factory=dict)
    history_from: date | None = None
    history_days: int = 0
    #: False while purchases exist that no recorded money paid for. Those read
    #: as return, so ``measured`` is inflated -- offering it as something to
    #: assume would launder a data gap into a projection.
    measured_reliable: bool = True


def return_context(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    as_of: date | None = None,
    block_days: int = projection.BLOCK_DAYS,
) -> ReturnContext:
    """The measured returns beside the one the projection is using."""
    from portfolio_tracker.core import returns as returns_module

    state = goal.progress(conn, person_id=person_id)
    horizon = as_of or state.as_of

    model = projection.build_return_model(conn, person_id=person_id, as_of=horizon)
    factors = projection.block_factors(model.series, block_days)

    measured: dict[str, MeasuredReturn] = {}
    for label, days in (("3m", 92), ("6m", 183), ("12m", 366)):
        summary = returns_module.summarise(
            conn, person_id=person_id, start=horizon - timedelta(days=days), end=horizon
        )
        # Both figures, because either alone misleads: three months of +4% is
        # not "+4% a year", and "+17% a year" from twelve weeks of data is not
        # something anybody actually earned.
        measured[label] = MeasuredReturn(
            period=summary.twr if summary.days >= 30 else None,
            annualised=summary.twr_annualised if summary.days >= 30 else None,
            days=summary.days,
        )

    whole = returns_module.summarise(conn, person_id=person_id)
    measured["since_start"] = MeasuredReturn(
        period=whole.twr, annualised=whole.twr_annualised, days=whole.days
    )

    return ReturnContext(
        implied_annual=implied_annual_return(factors, block_days),
        measured=measured,
        history_from=model.start,
        history_days=model.days,
        measured_reliable=not cash.compute_flows(conn).unfunded,
    )


def contribution_context(
    conn: sqlite3.Connection, *, person_id: int = 1, as_of: date | None = None
) -> dict[str, Contributions]:
    """The savings rate over several windows, so one can be chosen knowingly.

    An average over any window is one large transfer away from being a poor
    description of a habit, and there is no window length that is right for
    everybody. So the windows are shown rather than picked: six months, twelve
    months, and the median month inside each. The form defaults to the twelve
    month average because that is what the savings section on the same page
    reports, and a screen quoting two different savings rates is worse than
    either figure alone.
    """
    state = goal.progress(conn, person_id=person_id)
    horizon = as_of or state.as_of
    return {
        f"{months}m": projection.measure_contributions(
            conn, person_id=person_id, as_of=horizon, window_months=months
        )
        for months in (6, 12, 24)
    }


def run(
    conn: sqlite3.Connection,
    *,
    levers: Levers | None = None,
    person_id: int = 1,
    name: str = "Scenario",
    paths: int = projection.DEFAULT_PATHS,
    block_days: int = projection.BLOCK_DAYS,
    horizon_years: int = projection.HORIZON_YEARS,
    seed: int = projection.DEFAULT_SEED,
    monthly_eur: Decimal | None = None,
    with_bands: bool = True,
    as_of: date | None = None,
) -> Comparison:
    """Run the baseline and the scenario side by side on identical draws."""
    applied = levers or Levers()
    horizon_months = applied.target_months or horizon_years * 12

    state = goal.progress(conn, person_id=person_id)
    # The last day the portfolio was actually valued, not today. `position_daily`
    # is matched on an exact date, so anchoring to today makes the whole
    # projection vanish on a weekend or whenever prices are a day behind --
    # which is most of the time. `project()` has always used this; using
    # `date.today()` here was a bug that only showed on a stale day.
    horizon = as_of or state.as_of
    base_target = applied.target_amount_eur or state.target_eur
    current = state.current_eur

    measured = projection.measure_contributions(conn, person_id=person_id, as_of=horizon)
    base_monthly = monthly_eur if monthly_eur is not None else measured.monthly_eur

    model = projection.build_return_model(conn, person_id=person_id, as_of=horizon)
    factors = projection.block_factors(model.series, block_days)
    if not factors:
        raise ScenarioError(
            "There is not enough price history to resample. A scenario needs at "
            "least one full block of returns from the instruments you hold."
        )

    def simulate(monthly: Decimal, target: Decimal, **extra: object) -> projection.PathSet:
        return projection.simulate_paths(
            factors=factors,
            block_days=block_days,
            current=current,
            monthly_contribution=monthly,
            target=target,
            horizon_months=horizon_months,
            paths=paths,
            # The same seed for both runs. Without this the difference between
            # them would include a month or two of sampling noise, which is the
            # same size as the effect being measured.
            seed=seed,
            with_bands=with_bands,
            **extra,  # type: ignore[arg-type]
        )

    implied = implied_annual_return(factors, block_days)

    baseline_paths = simulate(base_monthly, base_target)
    # Absolute, not a difference: the form asks what you would save, and a
    # blank means "carry on as measured".
    scenario_monthly = (
        base_monthly
        if applied.monthly_contribution_eur is None
        else max(ZERO, applied.monthly_contribution_eur)
    )
    scenario_paths = simulate(
        scenario_monthly,
        base_target,
        lump_sums=applied.lump_sums,
        contribution_growth_annual=applied.contribution_growth_pct_annual,
        return_delta_monthly=applied.return_delta_monthly(implied),
    )

    baseline = _outcome(
        paths=baseline_paths,
        monthly=base_monthly,
        target=base_target,
        as_of=horizon,
        horizon_months=horizon_months,
        with_bands=with_bands,
    )
    scenario = _outcome(
        paths=scenario_paths,
        monthly=scenario_monthly,
        target=base_target,
        as_of=horizon,
        horizon_months=horizon_months,
        with_bands=with_bands,
    )

    result = Comparison(
        name=name,
        levers=applied,
        baseline=baseline,
        scenario=scenario,
        as_of=horizon,
        seed=seed,
        kinds=applied.kinds,
    )

    for confidence in CONFIDENCES:
        key = str(confidence)
        before, after = baseline.months_to_target[key], scenario.months_to_target[key]
        # None on either side means "not inside the horizon", and a difference
        # against that is not a number. Saying so beats inventing one.
        result.months_earlier[key] = (
            before - after if before is not None and after is not None else None
        )

    delta = scenario_monthly - base_monthly
    earlier = result.months_earlier.get(str(HEADLINE_CONFIDENCE))
    if delta > ZERO and earlier is not None:
        # The most decision-useful figure in the whole tool: months bought per
        # hundred euros a month, which is what makes two levers comparable.
        result.efficiency_months_per_100 = (Decimal(earlier) / (delta / Decimal(100))).quantize(
            Decimal("0.01")
        )

    result.caveats = _caveats(applied, baseline, scenario, model, block_days)
    return result


def _caveats(
    levers: Levers,
    baseline: Outcome,
    scenario: Outcome,
    model: projection.ReturnModel,
    block_days: int,
) -> list[str]:
    out = [
        "Both runs draw the same return blocks in the same order, so the "
        "difference between them is the change you made and not chance.",
        "Resampled from the price history of the instruments you hold today, at "
        "today's weights. Historical samples understate tail risk and come from "
        "a period that may not repeat. This is a range of outcomes, not a forecast.",
    ]
    if "assumption" in levers.kinds:
        out.append(ASSUMPTION_WARNING)
    if levers.return_annual_pct is not None:
        out.append(
            "A higher assumed return with unchanged volatility is not something "
            "that exists in markets. Read the earlier date as arithmetic, not as "
            "an opportunity."
        )
    if baseline.headline_months is None:
        out.append(
            "The target is not reached within the horizon in three quarters of "
            "outcomes even before the change, so there is no baseline date to "
            "compare against."
        )
    effective = model.effective_samples(block_days)
    if effective < projection.MIN_EFFECTIVE_SAMPLES:
        out.append(
            f"WEAK: the history supports only about {effective:.1f} independent "
            f"{block_days}-day blocks, so the spread of outcomes is thinner than it looks."
        )
    return out


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------


def sweep(
    conn: sqlite3.Connection,
    *,
    person_id: int = 1,
    steps: tuple[Decimal, ...] = (
        Decimal(100),
        Decimal(250),
        Decimal(500),
        Decimal(1000),
    ),
    paths: int = 400,
    as_of: date | None = None,
) -> list[dict[str, object]]:
    """How many months each extra hundred a month buys.

    Fewer paths than a single scenario on purpose: this runs several
    simulations and the answer it gives is a shape, not a date.
    """
    # The sweep still talks in extra-per-month, because that is the question
    # it answers: what does each additional hundred buy. It resolves the
    # measured rate first so the levers stay absolute.
    state = goal.progress(conn, person_id=person_id)
    base = projection.measure_contributions(
        conn, person_id=person_id, as_of=as_of or state.as_of
    ).monthly_eur

    out: list[dict[str, object]] = []
    for step in steps:
        try:
            found = run(
                conn,
                levers=Levers(monthly_contribution_eur=base + step),
                person_id=person_id,
                paths=paths,
                with_bands=False,
                as_of=as_of,
            )
        except (ScenarioError, LookupError, projection.ProjectionError):
            continue
        out.append(
            {
                "monthly_delta_eur": float(step),
                "months_earlier_75": found.months_earlier.get(str(HEADLINE_CONFIDENCE)),
                "months_earlier_50": found.months_earlier.get("0.50"),
                "efficiency_months_per_100": (
                    float(found.efficiency_months_per_100)
                    if found.efficiency_months_per_100 is not None
                    else None
                ),
                "kind": "controllable",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Pinning
# ---------------------------------------------------------------------------


def save(
    conn: sqlite3.Connection,
    *,
    name: str,
    levers: Levers,
    person_id: int = 1,
    pinned: bool = True,
) -> int:
    from datetime import UTC, datetime

    cursor = conn.execute(
        "INSERT INTO scenario (name, person_id, levers, created_at, pinned) VALUES (?, ?, ?, ?, ?)",
        (
            name,
            person_id,
            json.dumps(levers.as_dict()),
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            1 if pinned else 0,
        ),
    )
    return int(cursor.lastrowid or 0)


def pinned(conn: sqlite3.Connection, *, person_id: int = 1) -> list[tuple[int, str, Levers]]:
    """Saved scenarios, re-runnable against whatever the portfolio is now."""
    return [
        (int(r["id"]), str(r["name"]), Levers.from_dict(json.loads(r["levers"])))
        for r in conn.execute(
            "SELECT id, name, levers FROM scenario WHERE person_id = ? AND pinned = 1 "
            "ORDER BY created_at",
            (person_id,),
        )
    ]


def forget(conn: sqlite3.Connection, scenario_id: int) -> None:
    conn.execute("DELETE FROM scenario WHERE id = ?", (scenario_id,))
