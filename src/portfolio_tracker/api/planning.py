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

import re
import sqlite3
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from portfolio_tracker.api.deps import get_db, get_writable_db
from portfolio_tracker.core import projection, savings, scenarios
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


# ---------------------------------------------------------------------------
# Savings
# ---------------------------------------------------------------------------


class FlowClassUpdate(BaseModel):
    """A person's decision about what a movement was."""

    flow_class: str = Field(
        pattern="^(EXTERNAL_IN|EXTERNAL_OUT|INTERNAL_TRANSFER|INVESTMENT|INCOME|COST)$"
    )
    savings_label: str | None = Field(default=None, max_length=40)


@router.get("/api/savings")
def savings_history(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Net new money by month, and the three parts of what you are worth."""
    result = savings.summarise(conn, person_id=person_id)
    return {
        "as_of": result.as_of.isoformat(),
        "this_month_eur": _num(result.this_month_eur),
        "year_to_date_eur": _num(result.year_to_date_eur),
        "trailing_12m_eur": _num(result.trailing_12m_eur),
        "average_monthly_12m_eur": _num(result.average_monthly_12m_eur),
        # The split that answers "where did the value come from". Income sits
        # outside the sum on purpose -- see core/savings.py.
        "made_of": {
            "money_you_put_in_eur": _num(result.cumulative_eur),
            "market_growth_eur": _num(result.market_growth_eur),
            "adds_to_eur": _num(result.reconciles),
            "portfolio_value_eur": _num(result.closing_value_eur),
            "gap_eur": _num(result.reconciliation_gap),
        },
        "income_received_eur": _num(result.income_received_eur),
        "months": [
            {
                "period": p.period,
                "in_eur": _num(p.gross_in_eur),
                "out_eur": _num(p.gross_out_eur),
                "net_new_money_eur": _num(p.net_new_money_eur),
                "cumulative_eur": _num(p.cumulative_net_eur),
                "income_eur": _num(p.income_received_eur),
                "market_growth_eur": _num(p.market_growth_eur),
                "value_eur": _num(p.closing_value_eur),
            }
            for p in result.periods
        ],
        "caveats": result.caveats,
        "definition": (
            "New savings is money crossing into your accounts from outside them. "
            "Moving money between your own accounts is not saving, and neither is "
            "a reinvested dividend -- that is a return being compounded."
        ),
    }


@router.get("/api/savings/unclear")
def unclear(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Withdrawals matching more than one deposit, awaiting a decision.

    Never guessed. Two identical amounts on the same day are genuinely
    ambiguous, and choosing wrongly either erases a real contribution or
    invents one -- and both errors are invisible afterwards.
    """
    out = []
    for withdrawal, matches in savings.candidates(conn, person_id=person_id):
        if len(matches) < 2:
            continue
        out.append(
            {
                "out_txn_id": withdrawal["id"],
                "date": withdrawal["trade_date"],
                "amount_eur": _num(withdrawal["gross_amount_eur"]),
                "could_pair_with": [
                    {"id": m["id"], "date": m["trade_date"], "account_id": m["account_id"]}
                    for m in matches
                ],
            }
        )
    return {"unclear": out}


@router.patch("/api/transactions/{txn_id}/flow-class")
def reclassify(
    txn_id: int,
    body: FlowClassUpdate,
    conn: sqlite3.Connection = Depends(get_writable_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Say what a movement really was. Sticky through re-imports.

    Recomputes the derived layer afterwards, because flow classification feeds
    unit accounting directly: a transfer counted as new money issues units and
    attributes the return they earn to a contribution instead of to
    performance. Changing it and not recomputing would leave the return series
    disagreeing with the ledger it came from.
    """
    from datetime import date as _date

    from portfolio_tracker.core import rebuild

    try:
        with transaction(conn):
            savings.set_flow_class(
                conn, txn_id, flow_class=body.flow_class, savings_label=body.savings_label
            )
            result = rebuild.run(conn, end=_date.today(), strict=False, commit=False)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return {
        "id": txn_id,
        "flow_class": body.flow_class,
        "savings_label": body.savings_label,
        "recomputed": {"breaks": result.breaks, "ok": result.ok},
        "note": "Recorded as your decision. Re-importing will not change it back.",
    }


# ---------------------------------------------------------------------------
# Cleaning up what an earlier version inferred
# ---------------------------------------------------------------------------


def _release_proposals(conn: sqlite3.Connection, txn_ids: list[int]) -> None:
    """Detach the proposals that produced these rows, so they can be deleted.

    `pending_entry.resulting_txn_id` points at the ledger row a confirmation
    created, and the foreign key stops the row being removed while it does.
    The proposal is an artefact of the inference being removed, so it is
    reopened as rejected with the reason recorded rather than deleted -- the
    decision trail is the one thing worth keeping from it.
    """
    if not txn_ids:
        return
    marks = ",".join("?" for _ in txn_ids)
    conn.execute(
        f"UPDATE pending_entry SET resulting_txn_id = NULL, status = 'REJECTED', "
        f"note = 'the contribution this proposed was never recorded; the inference "
        f"that produced it has been removed' WHERE resulting_txn_id IN ({marks})",
        txn_ids,
    )
    conn.execute(
        f"UPDATE pending_entry SET source_txn_id = NULL WHERE source_txn_id IN ({marks})",
        txn_ids,
    )


class KeepDeposit(BaseModel):
    """Confirming that an inferred contribution really was new money."""

    amount_eur: Decimal | None = Field(default=None, gt=0, description="Correct it if wrong")
    label: str | None = Field(default=None, max_length=40)


@router.get("/api/savings/inferred")
def inferred(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Contributions an earlier version of this tool deduced rather than read.

    The rule was: a purchase larger than the cash on record means money must
    have arrived. On this ledger that produced EUR 131,479 of savings nobody
    recorded, against EUR 23,367 that were. The money may have been a deposit
    -- or it may have come from an account this tool cannot see, which is a
    different fact with different consequences.

    The inference is gone. These rows are what it left behind, for you to keep
    or discard one at a time.
    """
    rows = conn.execute(
        """
        SELECT d.id, d.trade_date, d.currency, d.gross_amount_local, d.gross_amount_eur,
               d.note, a.name AS account,
               b.trade_date AS bought_on, b.quantity AS bought_qty,
               b.gross_amount_local AS bought_local, b.currency AS bought_currency,
               b.gross_amount_eur AS bought_eur,
               i.import_key, i.name AS instrument_name
        FROM transaction_ d
        JOIN account a ON a.id = d.account_id
        -- The proposal that produced this row still points at the purchase
        -- that triggered it, which is the only thing that makes the guess
        -- reviewable: "money must have arrived" is unanswerable, "money must
        -- have arrived to buy this" is a question somebody can settle.
        LEFT JOIN pending_entry pe ON pe.resulting_txn_id = d.id
        LEFT JOIN transaction_ b ON b.id = pe.source_txn_id
        LEFT JOIN instrument i ON i.id = b.instrument_id
        WHERE a.person_id = ? AND d.flow_class = 'EXTERNAL_IN'
          AND d.confidence = 'RECONSTRUCTED'
        ORDER BY d.trade_date
        """,
        (person_id,),
    ).fetchall()

    total = sum((Decimal(str(r["gross_amount_eur"] or 0)) for r in rows), Decimal(0))
    return {
        "deposits": [
            {
                "id": r["id"],
                "date": r["trade_date"],
                "account": r["account"],
                "amount_eur": _num(r["gross_amount_eur"]),
                # What the money was needed for. The old explanation was a
                # bare "purchase of 40015.36 against a cash balance of 0.00":
                # no separators, no currency, and no clue what was bought --
                # and on a lira fund that 40,015.36 was lira while the deposit
                # beside it was 1,114.44 euro, which reads as nonsense.
                "bought": (
                    {
                        "instrument": r["import_key"],
                        "name": r["instrument_name"],
                        "date": r["bought_on"],
                        "quantity": _num(r["bought_qty"]),
                        "amount_local": _num(r["bought_local"]),
                        "currency": r["bought_currency"],
                        "amount_eur": _num(r["bought_eur"]),
                    }
                    if r["import_key"]
                    else None
                ),
                "cash_before_eur": _cash_before(r["note"]),
            }
            for r in rows
        ],
        "count": len(rows),
        "total_eur": float(total),
        "what_to_do": (
            "Keep the ones that were money you paid in. Discard the rest — "
            "those were funded by something this tool cannot see."
        ),
    }


def _cash_before(note: str | None) -> float | None:
    """The recorded cash balance at the moment of the purchase.

    Parsed out of the note the old inference wrote, because that is the only
    place it was ever stored. Best effort: a row whose note has been edited
    simply loses this one field rather than breaking the review.
    """
    if not note:
        return None
    found = re.search(r"cash balance of ([\d.]+)", note)
    if not found:
        return None
    try:
        return float(found.group(1))
    except ValueError:
        return None


@router.post("/api/savings/inferred/{txn_id}/keep")
def keep_inferred(
    txn_id: int,
    body: KeepDeposit,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """This one really was new money. Optionally correct the amount."""
    from datetime import date as _date

    from portfolio_tracker.core import rebuild

    row = conn.execute(
        "SELECT * FROM transaction_ WHERE id = ? AND confidence = 'RECONSTRUCTED'", (txn_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"no inferred contribution {txn_id}")

    with transaction(conn):
        if body.amount_eur is not None:
            rate = Decimal(str(row["fx_rate"] or 1))
            conn.execute(
                "UPDATE transaction_ SET gross_amount_eur = ?, gross_amount_local = ? WHERE id = ?",
                (str(body.amount_eur), str(body.amount_eur * rate), txn_id),
            )
        # ACTUAL and USER: a re-import will not undo this, and no classifier
        # will second-guess it.
        conn.execute(
            "UPDATE transaction_ SET confidence = 'ACTUAL', flow_class_source = 'USER', "
            "is_new_savings = 1, savings_label = ?, note = ? WHERE id = ?",
            (body.label, "confirmed as money you paid in", txn_id),
        )
        result = rebuild.run(conn, end=_date.today(), strict=False, commit=False)

    return {"id": txn_id, "kept": True, "recomputed_ok": result.ok}


@router.delete("/api/savings/inferred/{txn_id}")
def discard_inferred(
    txn_id: int,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """This was not new money. Remove it.

    A hard delete, for the reason the ledger always hard-deletes: a flag would
    have to be honoured by lots, cash, returns, tax and attribution alike, and
    one missed filter is a quietly wrong figure rather than a visible bug.
    """
    from datetime import date as _date

    from portfolio_tracker.core import rebuild

    row = conn.execute(
        "SELECT * FROM transaction_ WHERE id = ? AND confidence = 'RECONSTRUCTED'", (txn_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"no inferred contribution {txn_id}")

    with transaction(conn):
        _release_proposals(conn, [txn_id])
        conn.execute("DELETE FROM transaction_ WHERE id = ?", (txn_id,))
        result = rebuild.run(conn, end=_date.today(), strict=False, commit=False)

    return {"id": txn_id, "deleted": True, "recomputed_ok": result.ok}


@router.delete("/api/savings/inferred")
def discard_all_inferred(
    conn: sqlite3.Connection = Depends(get_writable_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Remove every remaining inferred contribution at once.

    Offered because the honest starting point for most of these is "not mine to
    claim": keep the few you recognise, then clear the rest in one action
    rather than thirty.
    """
    from datetime import date as _date

    from portfolio_tracker.core import rebuild

    with transaction(conn):
        doomed = [
            int(r["id"])
            for r in conn.execute(
                "SELECT t.id FROM transaction_ t JOIN account a ON a.id = t.account_id "
                "WHERE a.person_id = ? AND t.confidence = 'RECONSTRUCTED' "
                "AND t.flow_class = 'EXTERNAL_IN'",
                (person_id,),
            )
        ]
        _release_proposals(conn, doomed)
        marks = ",".join("?" for _ in doomed) or "NULL"
        cursor = conn.execute(
            f"DELETE FROM transaction_ WHERE id IN ({marks})",
            doomed,
        )
        removed = cursor.rowcount
        result = rebuild.run(conn, end=_date.today(), strict=False, commit=False)

    return {"deleted": removed, "recomputed_ok": result.ok}
