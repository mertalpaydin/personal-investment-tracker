"""Prose about figures this tool computed, and the settings behind it.

Every endpoint here answers fully with ``narrative: null`` when there is no
key, no network, or a refusal. The deterministic block is assembled first and
the model is asked afterwards, so the sentence "the tool is fully usable with
narration switched off" is true by construction rather than by intention.

``narrate`` defaults to false everywhere. Narration costs a call against a
free tier, and a page that quietly spends four of them on every refresh would
stop working within a day and look broken rather than rate-limited.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from portfolio_tracker.api.deps import get_db, get_writable_db
from portfolio_tracker.core import attribution, glossary, goal, inbox, returns, settings, tax
from portfolio_tracker.db.connection import transaction
from portfolio_tracker.llm import keys, runs, tasks
from portfolio_tracker.llm.adapter import LLMUnavailable
from portfolio_tracker.llm.providers.gemini import GeminiProvider

router = APIRouter(tags=["narrative"])


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, Decimal) else None


# A model is forbidden to compute anything, which is right -- and it means a
# raw 0.4733688157975862 comes back in the prose verbatim, because rounding it
# would be arithmetic. So figures are formatted here, where the semantics are
# known, and the model quotes them. Presentation is this layer's job anyway.


def _money(value: object) -> str | None:
    v = _num(value)
    return None if v is None else f"EUR {v:,.2f}"


def _pct(value: object, *, sign: bool = False) -> str | None:
    """A fraction as a percentage string. 0.4733… becomes '47.34%'."""
    v = _num(value)
    if v is None:
        return None
    return f"{v * 100:+.2f}%" if sign else f"{v * 100:.2f}%"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class LLMSettings(BaseModel):
    api_key: str | None = Field(default=None, description="Never returned once stored")
    model: str | None = None
    enabled: bool | None = None
    daily_cap: int | None = Field(default=None, ge=1, le=1000)
    send_amounts: bool | None = None


@router.get("/api/settings/llm")
def read_settings(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """What narration is configured to do. Never returns the key itself."""
    key = keys.get_key(conn)
    used = runs.usage(conn, daily_cap=keys.daily_cap(conn))
    return {
        "configured": bool(key),
        # Enough to tell which key is stored, not enough to use it.
        "hint": keys.hint(key),
        # `.env` beats the settings page, so the page has to say when it is
        # showing something it cannot change -- otherwise "Forget the key"
        # appears to do nothing.
        "key_source": keys.key_source(conn),
        "model_source": keys.model_source(conn),
        "verified_at": settings.get(conn, keys.VERIFIED_SETTING),
        "enabled": keys.enabled(conn),
        "model": keys.model(conn),
        "daily_cap": used.daily_cap,
        "calls_today": used.calls_today,
        "send_amounts": keys.send_amounts(conn),
        "provider": "gemini",
        "what_leaves_this_machine": (
            "When you ask for a narrative, the figures on that page are sent to "
            "Google as JSON — values, percentages and instrument names. Never "
            "your account numbers, your bank, or your name. Every request is "
            "recorded in full so you can read exactly what was sent."
        ),
        "tasks": list(tasks.TASKS),
    }


@router.put("/api/settings/llm")
def write_settings(
    body: LLMSettings,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Store the key and preferences. The key is write-only from here on."""
    try:
        with transaction(conn):
            if body.api_key:
                keys.set_key(conn, body.api_key)
            if body.model is not None:
                settings.set_value(conn, keys.MODEL_SETTING, body.model.strip())
            if body.enabled is not None:
                settings.set_value(conn, keys.ENABLED_SETTING, body.enabled)
            if body.daily_cap is not None:
                settings.set_value(conn, keys.CAP_SETTING, body.daily_cap)
            if body.send_amounts is not None:
                settings.set_value(conn, keys.SEND_AMOUNTS_SETTING, body.send_amounts)
    except keys.KeyError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    return read_settings(conn)


@router.delete("/api/settings/llm/key")
def delete_key(conn: sqlite3.Connection = Depends(get_writable_db)) -> dict[str, Any]:
    with transaction(conn):
        keys.clear_key(conn)
        settings.set_value(conn, keys.ENABLED_SETTING, False)

    still_there = keys.get_key(conn)
    return {
        "configured": bool(still_there),
        "note": (
            f"The stored key was removed and narration switched off, but {keys.ENV_KEY} "
            "in your .env file still supplies one. Remove it from there too if you "
            "want no key at all."
            if still_there
            else "Key removed and narration switched off."
        ),
    }


@router.get("/api/settings/llm/models")
def list_models(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Model names this key may actually use.

    Worth asking rather than guessing: model identifiers change, and a text
    field that lets somebody type one lets them type one that does not exist.
    """
    key = keys.get_key(conn)
    if not key:
        return {"models": [], "problem": "No API key is set."}
    try:
        available = GeminiProvider(key, model=keys.model(conn)).models()
    except LLMUnavailable as exc:
        return {"models": [], "problem": keys.redact(str(exc), key)}
    return {"models": available, "current": keys.model(conn)}


@router.get("/api/llm/runs")
def run_log(
    conn: sqlite3.Connection = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    """Every call, with exactly what was sent.

    This is what makes "the model only narrates figures this tool computed"
    checkable rather than merely stated: open a run and every number in the
    prose is either in the JSON beside it, or it was invented.
    """
    return {
        "runs": [
            {
                "id": r["id"],
                "task": r["task"],
                "subject": r["subject"],
                "created_at": r["created_at"],
                "model": r["model"],
                "status": r["status"],
                "input_json": r["input_json"],
                "output_text": r["output_text"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "latency_ms": r["latency_ms"],
                "error": r["error"],
            }
            for r in runs.history(conn, limit=limit)
        ]
    }


# ---------------------------------------------------------------------------
# The daily brief
# ---------------------------------------------------------------------------


def _movers(conn: sqlite3.Connection, person_id: int, as_of: str) -> list[dict[str, Any]]:
    """Holdings whose value moved most since the previous valuation."""
    rows = conn.execute(
        """
        SELECT i.import_key, i.name, p.market_value_eur, p.unrealized_eur,
               (SELECT q.market_value_eur FROM position_daily q
                 WHERE q.instrument_id = p.instrument_id AND q.account_id = p.account_id
                   AND q.date < p.date ORDER BY q.date DESC LIMIT 1) AS previous
        FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE p.date = ? AND a.person_id = ?
        """,
        (as_of, person_id),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        if row["previous"] is None or row["previous"] == 0:
            continue
        change = (row["market_value_eur"] - row["previous"]) / row["previous"]
        out.append(
            {
                "instrument": row["import_key"],
                "name": row["name"],
                "change_since_previous": _pct(change, sign=True),
                "value_eur": _money(row["market_value_eur"]),
                # Sorted on, then dropped: the model is given the readable
                # form, and a raw float beside it would only invite quoting.
                "_abs_change": abs(float(change)),
            }
        )
    out.sort(key=lambda m: m["_abs_change"], reverse=True)
    for mover in out:
        mover.pop("_abs_change")
    return out[:5]


def brief_payload(conn: sqlite3.Connection, person_id: int) -> dict[str, Any]:
    """Exactly what the model is told about the portfolio. Nothing else."""
    row = conn.execute(
        "SELECT * FROM portfolio_daily WHERE person_id = ? ORDER BY date DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "no portfolio history; recompute first")

    summary = returns.summarise(conn, person_id=person_id)
    split = attribution.summarise(conn, person_id=person_id)
    counts = inbox.counts(conn)

    payload: dict[str, Any] = {
        "as_of": row["date"],
        "currency": "EUR",
        "portfolio": {
            "total_value_eur": _money(row["total_value_eur"]),
            "holdings_eur": _money(row["market_value_eur"]),
            "deposits_eur": _money(row["deposits_eur"]),
            "uninvested_cash_eur": _money(row["cash_eur"]),
            "total_return": _pct(summary.twr, sign=True),
            "return_per_year": _pct(summary.twr_annualised, sign=True),
            "worst_fall": _pct(summary.max_drawdown),
            "volatility": _pct(summary.volatility),
            "days_tracked": summary.days,
        },
        "movers": _movers(conn, person_id, str(row["date"])),
        "where_the_return_came_from": (
            {
                "asset_prices": _pct(split.local, sign=True),
                "currency": _pct(split.currency, sign=True),
                "income": _pct(split.income, sign=True),
                "trading": _pct(split.unexplained, sign=True),
                "total": _pct(split.total, sign=True),
                "unit": "percentage points of the total return",
            }
            if split is not None
            else None
        ),
        "open_proposals": counts.get("PROPOSED", 0),
        "caveats": summary.caveats,
    }

    try:
        progress = goal.progress(conn, person_id=person_id)
        payload["goal"] = {
            "target_eur": _money(progress.target_eur),
            "reached": _pct(progress.progress_pct),
            "remaining_eur": _money(progress.remaining_eur),
            # A projection from contributions alone, assuming no investment
            # return whatever. Included so the model can repeat it; the
            # guardrails forbid it commenting on whether it looks achievable.
            "projected_date": (
                progress.projected_date.isoformat() if progress.projected_date else None
            ),
        }
    except Exception:
        payload["goal"] = None
    return payload


@router.get("/api/brief")
def brief(
    conn: sqlite3.Connection = Depends(get_writable_db),
    person_id: int = Query(1, alias="person"),
    narrate: bool = Query(False),
) -> dict[str, Any]:
    """The figures a brief is written from, and optionally the brief.

    Takes the writable connection only because asking for a narrative records
    the call; without ``narrate`` nothing is written.
    """
    payload = brief_payload(conn, person_id)
    body: dict[str, Any] = {**payload, "narrative": None, "narrative_status": "NOT_REQUESTED"}
    if not narrate:
        return body

    with transaction(conn):
        result = tasks.narrate(conn, task="daily_brief", payload=payload, max_sentences=6)
    return {**payload, **result.as_dict()}


# ---------------------------------------------------------------------------
# Tax, in plain English
# ---------------------------------------------------------------------------

_EXPLAIN_TERMS = (
    "sparerpauschbetrag",
    "vorabpauschale",
    "teilfreistellung",
    "kapitalertragsteuer",
    "steuerbescheinigung",
)


def explain_payload(conn: sqlite3.Connection, person_id: int, tax_year: int) -> dict[str, Any]:
    """The tax position with plain-English keys, plus the glossary behind them."""
    try:
        state = tax.allowance_state(conn, person_id=person_id, tax_year=tax_year)
    except tax.TaxDataMissing as exc:
        raise HTTPException(409, str(exc)) from exc

    excluded = tax.out_of_scope_report(conn, tax_year=tax_year, as_of=date.today())
    gross = state.gross_gains_eur
    distributions = state.distributions_eur
    advance = state.vorabpauschale_eur
    realised = gross - distributions - advance

    return {
        "tax_year": tax_year,
        "jurisdiction": "DE",
        "reader": "does not read German",
        "allowance": {
            "tax_free_allowance_eur": _money(state.statutory_cap_eur),
            "realised_gains_eur": _money(realised if realised > 0 else Decimal(0)),
            "dividends_and_interest_eur": _money(distributions),
            "advance_lump_sum_eur": _money(advance),
            "losses_offset_eur": _money(state.losses_offset_eur),
            "allowance_used_eur": _money(state.consumed_eur),
            "allowance_remaining_eur": _money(state.remaining_eur),
            "taxable_after_allowance_eur": _money(state.taxable_after_allowance_eur),
            "estimated_tax_eur": _money(state.estimated_tax_eur),
            "tax_rate": "26.375%",
        },
        "loss_carry_forward": {
            "individual_shares_eur": _money(state.carried_forward.aktien),
            "everything_else_eur": _money(state.carried_forward.sonstige),
        },
        "not_included": {
            "instruments": excluded.instruments,
            "reasons": excluded.reasons,
            "warning": None if excluded.is_empty else "The position below is incomplete.",
        },
        "glossary": glossary.glossary_for(_EXPLAIN_TERMS),
        "disclaimer": glossary.PLANNING_ONLY,
    }


@router.get("/api/tax/explain")
def explain(
    conn: sqlite3.Connection = Depends(get_writable_db),
    person_id: int = Query(1, alias="person"),
    tax_year: int = Query(..., alias="year"),
    narrate: bool = Query(False),
) -> dict[str, Any]:
    """The tax position in plain-English fields, optionally written out."""
    payload = explain_payload(conn, person_id, tax_year)
    if not narrate:
        return {**payload, "narrative": None, "narrative_status": "NOT_REQUESTED"}

    with transaction(conn):
        result = tasks.narrate(
            conn,
            task="tax_explainer",
            payload=payload,
            subject=str(tax_year),
            max_sentences=8,
        )
    return {**payload, **result.as_dict()}
