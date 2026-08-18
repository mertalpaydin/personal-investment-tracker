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
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_tracker.core import goal, projection
from portfolio_tracker.money import ZERO, quantize_money

ONE = Decimal(1)

#: The confidences every scenario is measured at. 0.75 is the headline.
CONFIDENCES: tuple[Decimal, ...] = (Decimal("0.50"), Decimal("0.75"), Decimal("0.90"))
HEADLINE_CONFIDENCE = Decimal("0.75")

#: Trading days a year, for turning an annual return delta into a monthly one.
MONTHS_PER_YEAR = 12

#: Levers that are decisions, as opposed to assumptions about the world.
CONTROLLABLE = (
    "monthly_contribution_delta_eur",
    "lump_sums",
    "contribution_growth_pct_annual",
    "ter_delta_bps",
    "target_amount_eur",
    "target_months",
)
ASSUMED = ("return_delta_annual_pct",)

ASSUMPTION_WARNING = (
    "Changing the assumed return does not change the portfolio. This shows what "
    "would follow if returns were different, not a way to make them so."
)


class ScenarioError(ValueError):
    """The scenario cannot be evaluated as asked."""


@dataclass
class Levers:
    """The changes a scenario applies. All optional; all default to no change."""

    #: Money added or removed each month, on top of what is measured.
    monthly_contribution_delta_eur: Decimal = ZERO
    #: One-off amounts, as (months from now, amount).
    lump_sums: tuple[tuple[int, Decimal], ...] = ()
    #: Contributions rising each year, compounded.
    contribution_growth_pct_annual: Decimal = ZERO
    #: Added to the annual return. An assumption, and labelled as one.
    return_delta_annual_pct: Decimal = ZERO
    #: A cheaper (negative) or dearer fund, in basis points a year. Applied as
    #: a return delta, because that is exactly what an ongoing charge is.
    ter_delta_bps: int = 0
    #: Override the goal itself.
    target_amount_eur: Decimal | None = None
    #: A different horizon to measure against, in months.
    target_months: int | None = None

    @property
    def kinds(self) -> list[str]:
        """Which sorts of lever this scenario actually uses."""
        out: list[str] = []
        if (
            self.monthly_contribution_delta_eur
            or self.lump_sums
            or self.contribution_growth_pct_annual
            or self.ter_delta_bps
            or self.target_amount_eur is not None
        ):
            out.append("controllable")
        if self.return_delta_annual_pct:
            out.append("assumption")
        return out

    @property
    def is_empty(self) -> bool:
        return not self.kinds

    @property
    def return_delta_monthly(self) -> Decimal:
        """Annual drift plus fee change, per month.

        A fee is a certain, continuous drag, so a 10 basis point saving is
        exactly a 0.10% annual return delta. Treating them as one number keeps
        the simulation honest about the fact that they act identically.
        """
        annual = self.return_delta_annual_pct - Decimal(self.ter_delta_bps) / Decimal(10000)
        if not annual:
            return ZERO
        # Compounded, so "+1% a year" means the same over any horizon.
        return (ONE + annual) ** Decimal("0.0833333333") - ONE

    def as_dict(self) -> dict[str, object]:
        return {
            "monthly_contribution_delta_eur": str(self.monthly_contribution_delta_eur),
            "lump_sums": [[m, str(a)] for m, a in self.lump_sums],
            "contribution_growth_pct_annual": str(self.contribution_growth_pct_annual),
            "return_delta_annual_pct": str(self.return_delta_annual_pct),
            "ter_delta_bps": self.ter_delta_bps,
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

        target = raw.get("target_amount_eur")
        return cls(
            monthly_contribution_delta_eur=dec("monthly_contribution_delta_eur"),
            lump_sums=tuple(lumps),
            contribution_growth_pct_annual=dec("contribution_growth_pct_annual"),
            return_delta_annual_pct=dec("return_delta_annual_pct"),
            ter_delta_bps=whole("ter_delta_bps", 0) or 0,
            target_amount_eur=Decimal(str(target)) if target not in (None, "") else None,
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

    baseline_paths = simulate(base_monthly, base_target)
    scenario_monthly = max(ZERO, base_monthly + applied.monthly_contribution_delta_eur)
    scenario_paths = simulate(
        scenario_monthly,
        base_target,
        lump_sums=applied.lump_sums,
        contribution_growth_annual=applied.contribution_growth_pct_annual,
        return_delta_monthly=applied.return_delta_monthly,
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

    delta = applied.monthly_contribution_delta_eur
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
    if levers.return_delta_annual_pct:
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
    out: list[dict[str, object]] = []
    for step in steps:
        try:
            found = run(
                conn,
                levers=Levers(monthly_contribution_delta_eur=step),
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
