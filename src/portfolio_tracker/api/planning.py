"""When the target is reached, and what would change that.

Two things live here. The **projection** -- a fan of outcomes resampled from
the holdings' own price history, which the app computed and never drew. And
**scenarios** -- the same simulation run again with a lever applied, reporting
the difference in months.

Presentation rules enforced here rather than left to the front end, because
they are the difference between a range and a forecast:

- no single-value future estimate is ever the headline. ``p50`` exists inside
  the bands so a fan can be drawn, and the summary carries probabilities and
  shortfalls instead.
- every response carries its assumptions, the window the history came from,
  and a disclosure string, verbatim.
- a scenario that never reaches the target inside the horizon returns null
  rather than a date past it.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from portfolio_tracker.api.deps import get_db, get_writable_db
from portfolio_tracker.core import projection, scenarios
from portfolio_tracker.db.connection import transaction

router = APIRouter(tags=["planning"])

#: Attached to every projection response, word for word.
DISCLOSURE = (
    "Based on resampled historical returns of the instruments you hold today. "
    "Historical samples understate tail risk and are drawn from a period that "
    "may not repeat. This is a range of outcomes, not a forecast."
)

#: Bands are returned at this spacing. A point per month over thirty years is
#: 360 rows to draw a curve that changes shape slowly; quarterly is the same
#: picture at a third of the payload.
BAND_EVERY_MONTHS = 3


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, Decimal) else None


class LeverRequest(BaseModel):
    """A change to try. Every field optional; omitting all of them is the baseline."""

    monthly_contribution_delta_eur: Decimal = Field(default=Decimal(0))
    lump_sums: list[tuple[int, Decimal]] = Field(
        default_factory=list, description="[months from now, amount]"
    )
    contribution_growth_pct_annual: Decimal = Field(default=Decimal(0), ge=-1, le=1)
    return_delta_annual_pct: Decimal = Field(default=Decimal(0), ge=-1, le=1)
    ter_delta_bps: int = Field(default=0, ge=-500, le=500)
    target_amount_eur: Decimal | None = Field(default=None, gt=0)
    name: str = Field(default="Scenario", max_length=60)
    pin: bool = False

    def levers(self) -> scenarios.Levers:
        return scenarios.Levers(
            monthly_contribution_delta_eur=self.monthly_contribution_delta_eur,
            lump_sums=tuple((int(m), Decimal(a)) for m, a in self.lump_sums),
            contribution_growth_pct_annual=self.contribution_growth_pct_annual,
            return_delta_annual_pct=self.return_delta_annual_pct,
            ter_delta_bps=self.ter_delta_bps,
            target_amount_eur=self.target_amount_eur,
        )


def _outcome_json(outcome: scenarios.Outcome, *, with_bands: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "monthly_contribution_eur": _num(outcome.monthly_contribution_eur),
        "target_eur": _num(outcome.target_eur),
        "horizon_months": outcome.horizon_months,
        # Keyed by confidence, because a single date would be read as the
        # answer. A median is a coin flip and the wrong headline for a purchase.
        "months_to_target": outcome.months_to_target,
        "dates": outcome.dates,
        "prob_target_met": _num(outcome.prob_target_met),
        "shortfall_p10_eur": _num(outcome.shortfall_p10_eur),
    }
    if with_bands:
        body["bands"] = [
            {k: (_num(v) if isinstance(v, Decimal) else v) for k, v in band.items()}
            for i, band in enumerate(outcome.bands)
            if i % BAND_EVERY_MONTHS == 0 or i == len(outcome.bands) - 1
        ]
    return body


def _comparison_json(result: scenarios.Comparison, *, with_bands: bool = True) -> dict[str, Any]:
    return {
        "name": result.name,
        "as_of": result.as_of.isoformat(),
        "seed": result.seed,
        "levers": result.levers.as_dict(),
        "lever_kinds": result.kinds,
        "baseline": _outcome_json(result.baseline, with_bands=with_bands),
        "scenario": _outcome_json(result.scenario, with_bands=with_bands),
        "months_earlier": result.months_earlier,
        "headline_confidence": str(scenarios.HEADLINE_CONFIDENCE),
        "efficiency_months_per_100": _num(result.efficiency_months_per_100),
        "assumption_warning": (
            scenarios.ASSUMPTION_WARNING if "assumption" in result.kinds else None
        ),
        "caveats": result.caveats,
        "disclosure": DISCLOSURE,
    }


def _run(
    conn: sqlite3.Connection,
    levers: scenarios.Levers,
    *,
    person_id: int,
    paths: int,
    years: int,
    name: str = "Scenario",
    with_bands: bool = True,
) -> scenarios.Comparison:
    try:
        return scenarios.run(
            conn,
            levers=levers,
            person_id=person_id,
            name=name,
            paths=paths,
            horizon_years=years,
            with_bands=with_bands,
        )
    except (scenarios.ScenarioError, projection.ProjectionError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/projection")
def baseline(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    paths: int = Query(400, ge=100, le=5000),
    years: int = Query(15, ge=1, le=30),
) -> dict[str, Any]:
    """The range of outcomes as things stand, with nothing changed.

    This is what the app has been computing since the projection engine landed
    and has never shown: a fan of where the portfolio could be, and the dates
    by which the target is met at three levels of confidence.
    """
    result = _run(
        conn,
        scenarios.Levers(),
        person_id=person_id,
        paths=paths,
        years=years,
        name="As things stand",
    )
    body = _comparison_json(result)
    # There is no scenario here, so returning one would invite a comparison
    # against itself.
    body.pop("scenario")
    body.pop("months_earlier")
    body.pop("efficiency_months_per_100")
    return body


@router.post("/api/projection/scenario")
def run_scenario(
    body: LeverRequest,
    conn: sqlite3.Connection = Depends(get_writable_db),
    person_id: int = Query(1, alias="person"),
    paths: int = Query(400, ge=100, le=5000),
    years: int = Query(15, ge=1, le=30),
) -> dict[str, Any]:
    """Run one change against the baseline, on identical draws."""
    levers = body.levers()
    result = _run(conn, levers, person_id=person_id, paths=paths, years=years, name=body.name)
    out = _comparison_json(result)
    if body.pin:
        with transaction(conn):
            out["id"] = scenarios.save(conn, name=body.name, levers=levers, person_id=person_id)
    return out


@router.get("/api/projection/scenarios")
def saved(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    paths: int = Query(300, ge=100, le=2000),
    years: int = Query(15, ge=1, le=30),
) -> dict[str, Any]:
    """Pinned scenarios, re-run against the portfolio as it is now.

    Only the question is stored, never the answer -- a saved outcome would be
    a page of figures that were true in March.
    """
    out: list[dict[str, Any]] = []
    for scenario_id, name, levers in scenarios.pinned(conn, person_id=person_id):
        try:
            result = scenarios.run(
                conn,
                levers=levers,
                person_id=person_id,
                name=name,
                paths=paths,
                horizon_years=years,
                with_bands=False,
            )
        except (scenarios.ScenarioError, projection.ProjectionError, LookupError):
            continue
        body = _comparison_json(result, with_bands=False)
        body["id"] = scenario_id
        out.append(body)
    return {"scenarios": out, "disclosure": DISCLOSURE}


@router.delete("/api/projection/scenarios/{scenario_id}")
def forget(
    scenario_id: int,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    with transaction(conn):
        scenarios.forget(conn, scenario_id)
    return {"deleted": scenario_id}


@router.get("/api/projection/sensitivity")
def sensitivity(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    paths: int = Query(300, ge=100, le=2000),
) -> dict[str, Any]:
    """How many months each extra hundred a month buys.

    Ranking levers by months bought per EUR 100 a month is the most
    decision-useful output this tool produces: it is the only figure that
    makes two different changes comparable.
    """
    try:
        steps = scenarios.sweep(conn, person_id=person_id, paths=paths)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "steps": steps,
        "measured_at_confidence": str(scenarios.HEADLINE_CONFIDENCE),
        "note": (
            "Each row is a separate simulation on the same draws as the baseline. "
            "Diminishing returns are real: the first hundred usually buys more "
            "months than the fifth."
        ),
        "disclosure": DISCLOSURE,
    }
