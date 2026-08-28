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
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from portfolio_tracker.api.deps import get_db, get_writable_db
from portfolio_tracker.core import cash, projection, rebuild, savings, scenarios
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
    """A change to try, stated as what things would be. Null means "leave it".

    Absolute rather than differential, because "save EUR 1,500 a month" is a
    sentence somebody can check against their own bank statement, and "save
    EUR 300 a month more" is only meaningful if they already trust the figure
    the tool measured.
    """

    monthly_contribution_eur: Decimal | None = Field(default=None, ge=0)
    lump_sums: list[tuple[int, Decimal]] = Field(
        default_factory=list, description="[months from now, amount]"
    )
    contribution_growth_pct_annual: Decimal = Field(default=Decimal(0), ge=-1, le=1)
    return_annual_pct: Decimal | None = Field(default=None, ge=-1, le=1)
    target_amount_eur: Decimal | None = Field(default=None, gt=0)
    name: str = Field(default="Scenario", max_length=60)
    pin: bool = False

    def levers(self) -> scenarios.Levers:
        return scenarios.Levers(
            monthly_contribution_eur=self.monthly_contribution_eur,
            lump_sums=tuple((int(m), Decimal(a)) for m, a in self.lump_sums),
            contribution_growth_pct_annual=self.contribution_growth_pct_annual,
            return_annual_pct=self.return_annual_pct,
            target_amount_eur=self.target_amount_eur,
        )


def _return_context_json(context: scenarios.ReturnContext) -> dict[str, Any]:
    """The return the projection is using, beside the ones actually earned.

    Sent with every projection because "assume a different return" is an
    unanswerable question otherwise. A reader who cannot see what the tool is
    already assuming has no way to tell an adjustment from a fantasy.
    """
    return {
        "implied_annual": _num(context.implied_annual),
        "measured": {
            label: {
                "period": _num(window.period),
                "annualised": _num(window.annualised),
                "days": window.days,
            }
            for label, window in context.measured.items()
        },
        "history_from": context.history_from.isoformat() if context.history_from else None,
        "history_days": context.history_days,
        "measured_reliable": context.measured_reliable,
    }


def _contribution_context_json(windows: dict[str, Any]) -> dict[str, Any]:
    """What was actually paid in, over each window, average and median.

    Both figures for each window, because the gap between them is the whole
    point: an average well above the median means one transfer is carrying the
    rate, and a plan built on it assumes that transfer repeats every month.
    """
    return {
        label: {
            "average_monthly_eur": _num(c.monthly_eur),
            "median_monthly_eur": _num(c.median_monthly_eur),
            "total_eur": _num(c.total_eur),
            "from": c.measured_from.isoformat() if c.measured_from else None,
            "months": [[period, _num(amount)] for period, amount in c.by_month],
        }
        for label, c in windows.items()
    }


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
    body["returns"] = _return_context_json(scenarios.return_context(conn, person_id=person_id))
    body["contributions"] = _contribution_context_json(
        scenarios.contribution_context(conn, person_id=person_id)
    )
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
    # Repeated with the answer, not only on the form: an earlier date means
    # nothing without the assumptions that produced it in the same view.
    out["returns"] = _return_context_json(scenarios.return_context(conn, person_id=person_id))
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
            # One bucket to read, two sources: what the accounts already held
            # on day one, and later statements arriving with a balance behind
            # them. Both are "money you already had" to the person reading it.
            "money_you_already_had_eur": _num(
                result.opening_transfer_eur + result.opening_value_eur
            ),
            "already_held_on_day_one_eur": _num(result.opening_value_eur),
            "brought_in_later_eur": _num(result.opening_transfer_eur),
            "market_growth_eur": _num(result.market_growth_eur),
            "adds_to_eur": _num(result.reconciles),
            # The identity closes on the basis, which includes uninvested
            # cash; the headline figure does not. Both are sent so the page
            # can show a sum that adds up and still lead with what is held.
            "accounts_hold_eur": _num(result.closing_basis_eur),
            "uninvested_cash_eur": _num(result.uninvested_cash_eur),
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
                "already_had_eur": _num(p.opening_transfer_eur),
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


@router.get("/api/savings/opening-transfers")
def opening_transfers(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Deposits treated as money the owner already had, and the ones nearby.

    Also lists the *first* capital row of every deposit account, marked or
    not, because that is the row this distinction always turns on and the
    owner is the only one who can settle it.
    """
    rows = conn.execute(
        """
        SELECT t.id, t.trade_date, t.currency, t.gross_amount_local, t.gross_amount_eur,
               t.is_opening_transfer, t.note, a.name AS account, a.kind AS account_kind,
               t.id = (SELECT min(t2.id) FROM transaction_ t2
                       WHERE t2.account_id = t.account_id AND t2.type = 'DEPOSIT')
                 AS is_first_for_account
        FROM transaction_ t JOIN account a ON a.id = t.account_id
        WHERE a.person_id = ? AND t.type = 'DEPOSIT' AND t.flow_class = 'EXTERNAL_IN'
        ORDER BY t.trade_date
        """,
        (person_id,),
    ).fetchall()

    entries = [
        {
            "txn_id": r["id"],
            "on": r["trade_date"],
            "account": r["account"],
            "account_kind": r["account_kind"],
            "amount_local": _num(r["gross_amount_local"]),
            "currency": r["currency"],
            "amount_eur": _num(r["gross_amount_eur"]),
            "already_had": bool(r["is_opening_transfer"]),
            "first_for_account": bool(r["is_first_for_account"]),
            "note": r["note"],
        }
        for r in rows
        if r["is_opening_transfer"] or r["is_first_for_account"]
    ]
    return {
        "entries": entries,
        "what_this_is": (
            "Money that was already yours, arriving here for the first time because "
            "the account joined this tool. It counts towards what you are worth. It "
            "is not counted as saving, because you set it aside before any of this "
            "was being recorded."
        ),
    }


@router.post("/api/savings/opening-transfers/{txn_id}")
def mark_opening_transfer(
    txn_id: int,
    already_had: bool = Query(True),
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Say whether a deposit was money you already had. Then recompute."""
    row = conn.execute("SELECT id FROM transaction_ WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no transaction {txn_id}")
    with transaction(conn):
        savings.set_opening_transfer(conn, txn_id=txn_id, is_opening=already_had)
        savings.build_periods(conn)
    return {"txn_id": txn_id, "already_had": already_had}


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

    from portfolio_tracker.core import rebuild

    try:
        with transaction(conn):
            savings.set_flow_class(
                conn, txn_id, flow_class=body.flow_class, savings_label=body.savings_label
            )
            result = rebuild.run(conn, end=date.today(), strict=False, commit=False)
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
               d.note, d.account_id, a.name AS account,
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
                # What else was going on around that date. Without it the only
                # question the screen can ask is "did money arrive?", which is
                # unanswerable two years later; with it the question becomes
                # "did this sale pay for it?", which is a memory.
                "money_around_it": _money_around(conn, person_id, r),
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


#: How far back to look for a sale that might have funded a purchase.
NEARBY_DAYS_BEFORE = 120

#: And a little forward, because settlement takes a few days and a sale dated
#: after a purchase can still be the money that paid for it.
NEARBY_DAYS_AFTER = 5


def _money_around(
    conn: sqlite3.Connection, person_id: int, deposit: sqlite3.Row
) -> list[dict[str, Any]]:
    """Sales and income near the purchase, and whether they were already used.

    The distinction is the whole point, and it is not obvious. `core/cash.py`
    tracks a balance per account **and currency**, so a euro sale cannot offset
    a lira purchase in its model even when the broker converted the money for
    you. On this ledger every sale is in Depot/EUR while fourteen purchases are
    in Depot/TRY -- so for those, the sales were never applied and are exactly
    the candidates worth looking at.

    ``already_counted`` says which is which. A sale marked true was spent
    before the shortfall was worked out, so it cannot explain what is left; one
    marked false could not be applied and might be the answer.
    """
    when = deposit["bought_on"] or deposit["trade_date"]
    if not when:
        return []
    on = date.fromisoformat(str(when))

    rows = conn.execute(
        """
        SELECT t.trade_date, t.type, t.currency, t.gross_amount_local, t.gross_amount_eur,
               t.account_id, a.name AS account, i.import_key, i.name AS instrument_name
        FROM transaction_ t
        JOIN account a ON a.id = t.account_id
        LEFT JOIN instrument i ON i.id = t.instrument_id
        WHERE a.person_id = ?
          AND t.type IN ('SELL', 'DIVIDEND', 'INTEREST')
          AND t.trade_date BETWEEN ? AND ?
        ORDER BY t.trade_date
        """,
        (
            person_id,
            (on - timedelta(days=NEARBY_DAYS_BEFORE)).isoformat(),
            (on + timedelta(days=NEARBY_DAYS_AFTER)).isoformat(),
        ),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        same_pot = r["account_id"] == deposit["account_id"] and r["currency"] == deposit["currency"]
        out.append(
            {
                "date": r["trade_date"],
                "type": r["type"],
                "instrument": r["import_key"],
                "name": r["instrument_name"],
                "amount_local": _num(r["gross_amount_local"]),
                "amount_eur": _num(r["gross_amount_eur"]),
                "currency": r["currency"],
                "account": r["account"],
                # Spent before the shortfall was computed, so it cannot explain
                # what is left over.
                "already_counted": bool(same_pot and str(r["trade_date"]) <= str(when)),
                "days_before": (on - date.fromisoformat(str(r["trade_date"]))).days,
            }
        )
    return out


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
        result = rebuild.run(conn, end=date.today(), strict=False, commit=False)

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

    from portfolio_tracker.core import rebuild

    row = conn.execute(
        "SELECT * FROM transaction_ WHERE id = ? AND confidence = 'RECONSTRUCTED'", (txn_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"no inferred contribution {txn_id}")

    with transaction(conn):
        _release_proposals(conn, [txn_id])
        conn.execute("DELETE FROM transaction_ WHERE id = ?", (txn_id,))
        result = rebuild.run(conn, end=date.today(), strict=False, commit=False)

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
        result = rebuild.run(conn, end=date.today(), strict=False, commit=False)

    return {"deleted": removed, "recomputed_ok": result.ok}


# ---------------------------------------------------------------------------
# What was already there
# ---------------------------------------------------------------------------


class OpeningBalance(BaseModel):
    """What an account held before this ledger begins."""

    account_id: int
    currency: str = Field(min_length=3, max_length=3)
    amount_local: Decimal = Field(ge=0)
    on_date: date
    source: str | None = Field(default=None, max_length=120)


@router.get("/api/accounts/opening-balances")
def opening_balances(
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """What is set, and the least that would explain the ledger.

    Every figure in this tool is built from a ledger starting on whatever day
    the first export happens to cover. The accounts did not start then. Money
    already sitting in a depot funded purchases the ledger cannot explain, and
    with nothing to say so the tool reports them as unaccounted for ever --
    there is no earlier data to go and fetch.

    ``suggested`` is the deepest the running balance would go with nothing
    assumed, which is exactly the least the account can have held. A floor, not
    a guess at the real figure: the account may well have held more, which is
    why it is offered to correct rather than applied on anybody's behalf.
    """
    suggestions = cash.suggest_opening_balances(conn)
    existing = conn.execute(
        """
        SELECT o.account_id, o.currency, o.amount_local, o.on_date, o.source, a.name
        FROM opening_balance o JOIN account a ON a.id = o.account_id
        ORDER BY a.name, o.currency
        """
    ).fetchall()

    return {
        "set": [
            {
                "account_id": r["account_id"],
                "account": r["name"],
                "currency": r["currency"],
                "amount_local": _num(r["amount_local"]),
                "on_date": r["on_date"],
                "source": r["source"],
            }
            for r in existing
        ],
        "suggested": [
            {
                "account_id": s.account_id,
                "account": s.account,
                "currency": s.currency,
                "amount_local": _num(s.needed),
                "on_date": s.on_date.isoformat(),
                "explains": s.unfunded_count,
                "already_set": _num(s.current),
            }
            for s in suggestions
        ],
        "unexplained_purchases": len(cash.unfunded_purchases(conn)),
        "what_this_is": (
            "Money already in an account before this tool's records begin. It is "
            "neither saving nor return — it was not paid in during the tracked "
            "period, and nothing here earned it."
        ),
    }


@router.put("/api/accounts/opening-balances")
def set_opening_balance(
    body: OpeningBalance,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Record what an account already held, then recompute."""
    try:
        with transaction(conn):
            cash.set_opening_balance(
                conn,
                account_id=body.account_id,
                currency=body.currency.upper(),
                amount=body.amount_local,
                on=body.on_date,
                source=body.source,
            )
            result = rebuild.run(conn, end=date.today(), strict=False, commit=False)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return {
        "saved": True,
        "unexplained_purchases": len(cash.unfunded_purchases(conn)),
        "recomputed_ok": result.ok,
    }


@router.delete("/api/accounts/opening-balances/{account_id}/{currency}")
def clear_opening_balance(
    account_id: int,
    currency: str,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    with transaction(conn):
        cash.clear_opening_balance(conn, account_id=account_id, currency=currency.upper())
        rebuild.run(conn, end=date.today(), strict=False, commit=False)
    return {"cleared": True, "unexplained_purchases": len(cash.unfunded_purchases(conn))}
