"""JSON API and a server-rendered debug page.

Every response carries ``as_of`` and a ``data_quality`` block. A number without
its provenance is how this tool would start lying: the figures here rest on
inferred contributions, prices that are sometimes carried forward, and a source
export that records neither fees nor income. All of that is recoverable from
the response rather than buried in a docstring.

The headline ``total_value_eur`` counts holdings and deposit balances only.
Uninvested cash is reported beside it, never inside it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from portfolio_tracker import __version__
from portfolio_tracker.api.render import render_dashboard
from portfolio_tracker.core import fx, goal, inbox, rebuild, returns, tax, valuation
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect, transaction
from portfolio_tracker.money import ZERO

DB_PATH = Path(DEFAULT_DB_PATH)


class GoalUpdate(BaseModel):
    """A target amount. Decimal on the way in, so a typed figure stays exact."""

    target_eur: Decimal = Field(gt=0, description="Target portfolio value in EUR")


class ConfirmRequest(BaseModel):
    """An optional correction to an inferred amount.

    Omitting it books the inference as computed. Supplying an amount records
    both figures: the owner is often the better authority on money the source
    never recorded, but a correction that erases the guess erases the evidence
    that a guess was made.
    """

    amount_local: Decimal | None = Field(default=None, gt=0)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, description="Why this did not happen")


class RebuildRequest(BaseModel):
    end: date | None = Field(default=None, description="Last valuation date; default today")


def get_db() -> Any:
    """One connection per request, read-only, closed on the way out.

    The default, so that a route which only reports cannot write by accident.
    A handful of endpoints take ``get_writable_db`` instead, and that choice is
    visible in the signature rather than buried in the body.
    """
    conn = connect(DB_PATH, read_only=True, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def get_writable_db() -> Any:
    """The writable connection, taken by the four endpoints that need it.

    Those four are the goal target, confirming a proposal, rejecting one, and
    the rebuild. Everything else in this module reports rather than records,
    and gets ``get_db``. Keeping the two separate means write access is
    something a route asks for, not something it inherits.
    """
    conn = connect(DB_PATH, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


app = FastAPI(
    title="Portfolio Tracker",
    version=__version__,
    description=(
        "Personal investment tracking with FIFO tax lots, German capital-gains "
        "treatment and daily FX revaluation. Planning figures only -- the bank's "
        "Steuerbescheinigung is authoritative."
    ),
)


def _decimal(value: object) -> float | None:
    """JSON has no decimal type, so figures are floats *at the boundary only*.

    Everything upstream is exact; converting here rather than earlier keeps the
    imprecision where it cannot compound.
    """
    return float(value) if isinstance(value, Decimal) else None


def _latest(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM portfolio_daily WHERE person_id = ? ORDER BY date DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "no portfolio history; run `pt rebuild` first")
    return cast("sqlite3.Row", row)


def _data_quality(conn: sqlite3.Connection, person_id: int, as_of: str) -> dict[str, Any]:
    """What the reader needs in order to discount the numbers correctly."""
    counts = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM transaction_ t JOIN account a ON a.id = t.account_id
             WHERE a.person_id = ? AND t.confidence = 'RECONSTRUCTED'
               AND t.flow_class IN ('EXTERNAL_IN', 'EXTERNAL_OUT')) AS inferred_flows,
          (SELECT count(*) FROM transaction_ WHERE fees_known = 0
             AND type IN ('BUY', 'SELL')) AS trades_without_fees,
          (SELECT count(*) FROM transaction_ WHERE quantity_confidence = 'ESTIMATED')
            AS estimated_quantities,
          (SELECT count(*) FROM instrument WHERE tax_regime <> 'DE_INVESTMENT')
            AS instruments_out_of_tax_scope,
          (SELECT count(*) FROM instrument WHERE tax_regime = 'DE_INVESTMENT'
             AND teilfreistellung_pct IS NULL) AS unresolved_teilfreistellung,
          (SELECT count(*) FROM position_daily WHERE date = ? AND valuation_status <> 'OK')
            AS stale_or_missing_prices
        """,
        (person_id, as_of),
    ).fetchone()
    return {key: counts[key] for key in counts.keys()}  # noqa: SIM118


@app.get("/api/health/data")
def health(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Whether the figures can be trusted, and where they cannot."""
    row = _latest(conn, person_id)
    fx_rows = [
        {
            "quote": r["quote"],
            "last_date": r["hi"],
            "observations": r["n"],
            "source": r["source"],
        }
        for r in conn.execute(
            "SELECT quote, max(date) AS hi, count(*) AS n, source FROM fx_rate GROUP BY quote"
        )
    ]
    prices = [
        {
            "instrument": r["import_key"],
            "last_date": r["hi"],
            "observations": r["n"],
            "source": r["source"],
        }
        for r in conn.execute(
            "SELECT i.import_key, max(p.date) AS hi, count(*) AS n, p.source "
            "FROM price p JOIN instrument i ON i.id = p.instrument_id "
            "GROUP BY i.import_key ORDER BY i.import_key"
        )
    ]
    return {
        "as_of": row["date"],
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "completeness": row["completeness"],
        "data_quality": _data_quality(conn, person_id, row["date"]),
        "fx": fx_rows,
        "prices": prices,
    }


@app.get("/api/portfolio")
def portfolio(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Headline position.

    ``total_value_eur`` is holdings plus deposit balances. ``uninvested_cash_eur``
    sits beside it and is deliberately **not** included: whether that money is
    still held cannot be known from the source data.
    """
    row = _latest(conn, person_id)

    by_currency: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT i.currency, p.market_value_eur, p.market_value_local, p.fx_rate "
        "FROM position_daily p JOIN instrument i ON i.id = p.instrument_id "
        "WHERE p.date = ?",
        (row["date"],),
    ):
        entry = next((e for e in by_currency if e["currency"] == r["currency"]), None)
        if entry is None:
            entry = {
                "currency": r["currency"],
                "value_eur": ZERO,
                "value_local": ZERO,
                "fx_rate": _decimal(r["fx_rate"]),
            }
            by_currency.append(entry)
        entry["value_eur"] += r["market_value_eur"]
        entry["value_local"] += r["market_value_local"]

    total = row["total_value_eur"]
    for entry in by_currency:
        entry["value_eur"] = _decimal(entry["value_eur"])
        entry["value_local"] = _decimal(entry["value_local"])

    unrealized = conn.execute(
        "SELECT p.unrealized_eur u, p.unrealized_local_eur l, p.unrealized_fx_eur f "
        "FROM position_daily p WHERE p.date = ?",
        (row["date"],),
    ).fetchall()

    return {
        "as_of": row["date"],
        "person_id": person_id,
        "total_value_eur": _decimal(total),
        "total_basis": "holdings plus deposit balances; uninvested cash excluded",
        "holdings_eur": _decimal(row["market_value_eur"]),
        "deposits_eur": _decimal(row["deposits_eur"]),
        "uninvested_cash_eur": _decimal(row["cash_eur"]),
        # Where that cash came from. "EUR 2,548 uninvested" answers the wrong
        # question: capital waiting to be reinvested and income that may
        # already have been spent have different meanings and different fates.
        "uninvested_cash": {
            "total_eur": _decimal(row["cash_eur"]),
            "from_income_eur": _decimal(row["income_cash_eur"]),
            "from_capital_eur": _decimal(row["cash_eur"] - row["income_cash_eur"]),
            "attribution_rule": (
                "capital spent before income. A bank account is fungible, so this "
                "is a stated convention, not an observation."
            ),
        },
        # Income *earned*, whether or not it is still in cash. A reinvested
        # dividend leaves the balance without ceasing to have been received.
        "income_received_eur": _decimal(row["income_received_eur"]),
        "cost_basis_eur": _decimal(row["cost_basis_eur"]),
        "unrealized_eur": _decimal(sum((r["u"] for r in unrealized), ZERO)),
        "unrealized_local_eur": _decimal(sum((r["l"] for r in unrealized), ZERO)),
        "unrealized_fx_eur": _decimal(sum((r["f"] for r in unrealized), ZERO)),
        "realized_gain_eur": _decimal(row["realized_gain_eur"]),
        "net_contributions_eur": _decimal(returns.total_external_flows(conn, person_id)),
        "by_currency": by_currency,
        "data_quality": _data_quality(conn, person_id, row["date"]),
    }


@app.get("/api/positions")
def positions(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Every open position, with its gain split into local and currency parts."""
    row = _latest(conn, person_id)
    items = []
    for r in conn.execute(
        """
        SELECT i.import_key, i.name, i.currency, i.asset_class, i.tax_regime,
               i.tax_scope_note, p.*
        FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE p.date = ? AND a.person_id = ?
        ORDER BY p.market_value_eur DESC
        """,
        (row["date"], person_id),
    ):
        items.append(
            {
                "instrument": r["import_key"],
                "name": r["name"],
                "currency": r["currency"],
                "asset_class": r["asset_class"],
                "quantity": _decimal(r["quantity"]),
                "price_local": _decimal(r["price_local"]),
                "price_asof": r["price_asof"],
                "price_stale_days": r["price_stale_days"],
                "fx_rate": _decimal(r["fx_rate"]),
                "market_value_local": _decimal(r["market_value_local"]),
                "market_value_eur": _decimal(r["market_value_eur"]),
                "cost_basis_eur": _decimal(r["cost_basis_eur"]),
                # The three-way split. `unrealized_local_eur` is the figure the
                # source export reports; the difference from `unrealized_eur` is
                # currency movement, and the API says so rather than silently
                # disagreeing with a number the owner can see elsewhere.
                "unrealized_eur": _decimal(r["unrealized_eur"]),
                "unrealized_local_eur": _decimal(r["unrealized_local_eur"]),
                "unrealized_fx_eur": _decimal(r["unrealized_fx_eur"]),
                "valuation_status": r["valuation_status"],
                "tax_regime": r["tax_regime"],
                "tax_scope_note": r["tax_scope_note"],
            }
        )
    return {"as_of": row["date"], "positions": items}


@app.get("/api/returns")
def performance(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Time- and money-weighted return, with the caveats that qualify them."""
    summary = returns.summarise(conn, person_id=person_id)
    if summary.days == 0:
        raise HTTPException(404, "no portfolio history; run `pt rebuild` first")
    return {
        "start": summary.start.isoformat(),
        "end": summary.end.isoformat(),
        "days": summary.days,
        "twr": _decimal(summary.twr),
        "twr_annualised": _decimal(summary.twr_annualised),
        "xirr": _decimal(summary.xirr),
        "xirr_reliability": summary.xirr_reliability,
        "timing_gap": _decimal(summary.timing_gap),
        "volatility": _decimal(summary.volatility),
        "max_drawdown": _decimal(summary.max_drawdown),
        "unit_price_start": _decimal(summary.unit_price_start),
        "unit_price_end": _decimal(summary.unit_price_end),
        "external_flows_eur": _decimal(summary.external_flows_eur),
        "computed_on": (
            "holdings, deposits and uninvested cash. Excluding cash would make a "
            "sale read as a loss on the day it settles."
        ),
        "caveats": summary.caveats,
    }


@app.get("/api/returns/series")
def series(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """The daily unit-price index, base 100 at inception."""
    rows = conn.execute(
        "SELECT date, unit_price, total_value_eur, cash_eur, deposits_eur, "
        "income_cash_eur, income_received_eur, drawdown_pct "
        "FROM portfolio_daily WHERE person_id = ? ORDER BY date",
        (person_id,),
    ).fetchall()
    return {
        "person_id": person_id,
        "points": [
            {
                "date": r["date"],
                "unit_price": _decimal(r["unit_price"]),
                "total_value_eur": _decimal(r["total_value_eur"]),
                "deposits_eur": _decimal(r["deposits_eur"]),
                "uninvested_cash_eur": _decimal(r["cash_eur"]),
                "income_cash_eur": _decimal(r["income_cash_eur"]),
                "income_received_eur": _decimal(r["income_received_eur"]),
                "drawdown_pct": _decimal(r["drawdown_pct"]),
            }
            for r in rows
        ],
    }


@app.get("/api/tax/allowance")
def allowance(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    tax_year: int = Query(..., alias="year"),
) -> dict[str, Any]:
    """German capital-gains position, with what it excludes stated."""
    try:
        state = tax.allowance_state(conn, person_id=person_id, tax_year=tax_year)
    except tax.TaxDataMissing as exc:
        # A refusal, not a zero. Returning 0 would look like an answer.
        raise HTTPException(409, str(exc)) from exc

    excluded = tax.out_of_scope_report(conn, tax_year=tax_year, as_of=date.today())
    return {
        "tax_year": tax_year,
        "person_id": person_id,
        "statutory_cap_eur": _decimal(state.statutory_cap_eur),
        "gross_gains_eur": _decimal(state.gross_gains_eur),
        "distributions_eur": _decimal(state.distributions_eur),
        "losses_offset_eur": _decimal(state.losses_offset_eur),
        "after_losses_eur": _decimal(state.after_losses_eur),
        "allowance_used_eur": _decimal(state.consumed_eur),
        "allowance_remaining_eur": _decimal(state.remaining_eur),
        "taxable_eur": _decimal(state.taxable_after_allowance_eur),
        "estimated_tax_eur": _decimal(state.estimated_tax_eur),
        "losses_carried_forward": {
            "aktien": _decimal(state.carried_forward.aktien),
            "sonstige": _decimal(state.carried_forward.sonstige),
        },
        "out_of_scope": {
            "instruments": excluded.instruments,
            "reasons": excluded.reasons,
            "value_eur": _decimal(excluded.value_eur),
            "warning": (
                "These holdings are excluded from the figures above. The result is "
                "not a complete tax position."
            )
            if not excluded.is_empty
            else None,
        },
        "disclaimer": ("Planning worksheet. The bank's Steuerbescheinigung is authoritative."),
    }


@app.get("/api/tax/vorabpauschale")
def vorabpauschale(
    conn: sqlite3.Connection = Depends(get_db),
    tax_year: int = Query(..., alias="year"),
) -> dict[str, Any]:
    try:
        rows = tax.forecast_vorabpauschale(conn, tax_year=tax_year)
    except tax.TaxDataMissing as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "tax_year": tax_year,
        "taxable_on": f"{tax_year + 1}-01-02",
        "rows": [
            {
                "instrument": r.instrument,
                "quantity": _decimal(r.quantity),
                "jan_first_price": _decimal(r.jan_first_price),
                "year_end_price": _decimal(r.year_end_price),
                "basiszins": _decimal(r.basiszins),
                "basisertrag_raw_eur": _decimal(r.basisertrag_raw),
                "cap_eur": _decimal(r.cap),
                "basisertrag_eur": _decimal(r.basisertrag),
                "distributions_eur": _decimal(r.distributions_eur),
                "full_months_before": r.full_months_before,
                "taxable_eur": _decimal(r.taxable_eur),
            }
            for r in rows
            if r.taxable_eur > ZERO
        ],
    }


def _pending_json(entry: inbox.PendingEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "date": entry.proposed_date.isoformat(),
        "currency": entry.currency,
        "amount": _decimal(entry.amount_local),
        # The arithmetic behind the guess, verbatim. A confirmation without it
        # is a click; with it, it is a decision.
        "basis": entry.basis,
        "derivation": entry.derivation,
        "source_txn_id": entry.source_txn_id,
        "status": entry.status,
        "bookable": entry.bookable,
        "resulting_txn_id": entry.resulting_txn_id,
        "note": entry.note,
    }


@app.get("/api/pending")
def pending(
    conn: sqlite3.Connection = Depends(get_db),
    status: str = Query("PROPOSED"),
) -> dict[str, Any]:
    """Inferences awaiting a decision. Nothing here has reached the ledger."""
    return {
        "status": status,
        "counts": inbox.counts(conn),
        "rebuild_required": inbox.stale_after_decision(conn),
        "entries": [_pending_json(e) for e in inbox.listing(conn, status=status)],
    }


@app.get("/api/pending/{pending_id}")
def pending_detail(
    pending_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """One proposal in full, including how its number was arrived at."""
    try:
        return _pending_json(inbox.get(conn, pending_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/pending/{pending_id}/confirm")
def confirm_pending(
    pending_id: int,
    payload: ConfirmRequest | None = None,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Book a proposal into the ledger, optionally at a corrected amount.

    This is a real write and it is not reversible from here: confirming an
    inferred contribution changes which lots a later sale consumes and every
    return figure downstream. The proposal's own arithmetic is on
    ``GET /api/pending/{id}`` and on the page beside the button, because a
    confirmation that skips reading it is the failure mode this inbox exists
    to prevent.

    The derived tables are *not* rebuilt here. A rebuild recomputes several
    years of daily rows and belongs to an explicit step, so the response says
    ``rebuild_required`` instead of doing it silently.
    """
    amount = payload.amount_local if payload else None
    try:
        with transaction(conn):
            txn_id = inbox.confirm(conn, pending_id, amount_local=amount)
    except inbox.NotBookable as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "id": pending_id,
        "status": "CONFIRMED",
        "transaction_id": txn_id,
        "amount_edited": amount is not None,
        "rebuild_required": True,
        "next": "POST /api/admin/rebuild, or `pt rebuild`",
    }


@app.post("/api/pending/{pending_id}/reject")
def reject_pending(
    pending_id: int,
    payload: RejectRequest,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Decline a proposal. The reason is required, not decoration.

    A rejection is a claim about reality -- "no money arrived that day" -- and
    in six months the only way to re-evaluate it is to know why it was made.
    """
    try:
        with transaction(conn):
            inbox.reject(conn, pending_id, payload.reason)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": pending_id, "status": "REJECTED", "reason": payload.reason}


@app.post("/api/admin/rebuild")
def admin_rebuild(
    payload: RebuildRequest | None = None,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Recompute the whole derived layer from the ledger.

    Always safe in the sense that matters: everything it touches is a pure
    function of ``transaction_``, so it deletes and recomputes rather than
    patching. It is not safe in the sense of being quick -- several years of
    daily rows -- which is why it is a request the owner makes rather than
    something that happens after every confirmation.

    ``breaks`` is the field to read. Non-empty means the contributions in the
    daily series no longer match the ledger, and the difference is being
    reported as investment return.
    """
    as_of = payload.end if payload and payload.end else date.today()
    try:
        result = rebuild.run(conn, end=as_of, strict=False)
    except valuation.StalePriceError as exc:
        raise HTTPException(409, str(exc)) from exc
    except fx.FxUnavailable as exc:
        raise HTTPException(409, f"{exc}; run `pt fetch-fx` first") from exc

    return {
        "as_of": as_of.isoformat(),
        "ok": result.ok,
        "lots": result.lots_created,
        "disposals": result.disposals_created,
        "position_days": result.position_days,
        "grid_days": result.grid_days,
        "cash_rows": result.cash_rows,
        "oversold": result.oversold,
        "lots_without_trade_date_fx": result.missing_fx,
        "stale_valuations": len(result.stale),
        "unvalued_position_days": len(result.unvalued),
        "breaks": result.breaks,
    }


@app.get("/api/positions/{import_key}")
def position_detail(
    import_key: str,
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """One position, with the currency question answered in words.

    For a foreign-currency holding this is the endpoint the whole FX design
    exists to serve: the source export shows the local gain translated at
    today's rate, which is not what the owner made, and the difference has a
    name and a size.
    """
    row = conn.execute(
        """
        SELECT i.import_key, i.name, i.currency, i.asset_class, i.tax_regime,
               i.tax_scope_note, p.*
        FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE i.import_key = ? AND a.person_id = ?
        ORDER BY p.date DESC LIMIT 1
        """,
        (import_key, person_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"no position in {import_key!r}")

    history = [
        {
            "date": r["date"],
            "price_local": _decimal(r["price_local"]),
            "fx_rate": _decimal(r["fx_rate"]),
            "market_value_eur": _decimal(r["market_value_eur"]),
            "unrealized_eur": _decimal(r["unrealized_eur"]),
            "unrealized_local_eur": _decimal(r["unrealized_local_eur"]),
            "unrealized_fx_eur": _decimal(r["unrealized_fx_eur"]),
        }
        for r in conn.execute(
            "SELECT * FROM position_daily WHERE instrument_id = ? ORDER BY date",
            (row["instrument_id"],),
        )
    ]

    decomposition: dict[str, Any] = {
        "true_eur_gain": _decimal(row["unrealized_eur"]),
        "local_gain_at_todays_rate": _decimal(row["unrealized_local_eur"]),
        "currency_effect": _decimal(row["unrealized_fx_eur"]),
        "identity": "true_eur_gain = local_gain_at_todays_rate + currency_effect",
    }
    if row["currency"] != "EUR":
        local = row["unrealized_local_eur"]
        actual = row["unrealized_eur"]
        gap = row["unrealized_fx_eur"]
        direction = "depreciation" if gap < ZERO else "appreciation"
        decomposition["vs_broker_export"] = (
            f"A tracker translating at today's rate shows EUR {local:,.2f}. That is the "
            f"{row['currency']} gain converted as if the capital had been invested today. "
            f"The actual euro gain is EUR {actual:,.2f}; the EUR {abs(gap):,.2f} gap is "
            f"{row['currency']} {direction} against the original capital."
        )

    return {
        "as_of": row["date"],
        "instrument": row["import_key"],
        "name": row["name"],
        "currency": row["currency"],
        "asset_class": row["asset_class"],
        "quantity": _decimal(row["quantity"]),
        "market_value_local": _decimal(row["market_value_local"]),
        "market_value_eur": _decimal(row["market_value_eur"]),
        "cost_basis_eur": _decimal(row["cost_basis_eur"]),
        "cost_basis_eur_at_spot": _decimal(row["cost_basis_eur_at_spot"]),
        "return_decomposition": decomposition,
        "tax_regime": row["tax_regime"],
        "tax_scope_note": row["tax_scope_note"],
        "valuation_status": row["valuation_status"],
        "history": history,
    }


@app.get("/api/allocation")
def allocation(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    dim: str = Query("asset_class", pattern="^(asset_class|currency|account)$"),
) -> dict[str, Any]:
    """Weights by one dimension.

    ``dim=currency`` reports the *quote* currency, which is not the same as the
    economic one: a EUR-quoted S&P 500 tracker carries full dollar risk and is
    counted here as EUR. That is mechanically right and economically misleading,
    so the response says which basis it used rather than leaving it inferred.
    """
    row = _latest(conn, person_id)
    column = {
        "asset_class": "i.asset_class",
        "currency": "i.currency",
        "account": "a.name",
    }[dim]

    buckets: dict[str, Decimal] = {}
    for r in conn.execute(
        f"""
        SELECT {column} AS bucket, p.market_value_eur
        FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE p.date = ? AND a.person_id = ?
        """,  # `column` comes from the fixed map above, never from the request
        (row["date"], person_id),
    ):
        buckets[r["bucket"]] = buckets.get(r["bucket"], ZERO) + r["market_value_eur"]

    total = sum(buckets.values(), ZERO)
    return {
        "as_of": row["date"],
        "dimension": dim,
        "basis": "quote_currency" if dim == "currency" else dim,
        "total_eur": _decimal(total),
        "buckets": sorted(
            (
                {
                    "name": name,
                    "value_eur": _decimal(value),
                    "weight": _decimal(value / total) if total > ZERO else 0.0,
                }
                for name, value in buckets.items()
            ),
            key=lambda b: b["value_eur"] or 0.0,
            reverse=True,
        ),
        "note": (
            "Weights are on holdings only; deposits and cash are reported by "
            "/api/portfolio and are not allocated here."
        ),
    }


@app.get("/api/fx")
def fx_series(
    conn: sqlite3.Connection = Depends(get_db),
    quote: str = Query("TRY"),
    start: str | None = Query(None, alias="from"),
    end: str | None = Query(None, alias="to"),
) -> dict[str, Any]:
    """Stored rates, in the one direction this system stores them."""
    rows = conn.execute(
        "SELECT date, rate, source FROM fx_rate WHERE quote = ? "
        "AND date >= ifnull(?, date) AND date <= ifnull(?, date) ORDER BY date",
        (quote.upper(), start, end),
    ).fetchall()
    if not rows:
        raise HTTPException(404, f"no stored rates for EUR/{quote.upper()}")
    return {
        "base": "EUR",
        "quote": quote.upper(),
        "direction": f"{quote.upper()} per 1 EUR; conversion is always division",
        "points": [
            {"date": r["date"], "rate": _decimal(r["rate"]), "source": r["source"]} for r in rows
        ],
    }


@app.get("/api/accounts/deposits")
def deposit_accounts(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Deposit accounts and their terms.

    ``withholding_rate`` matters more than it looks: Turkish *stopaj* is taken
    at source, so a model that accrues gross drifts above the real balance on
    every payment date.
    """
    rows = conn.execute(
        "SELECT id, name, kind, currency, interest_rate, interest_day_count, "
        "interest_frequency, interest_last_paid, withholding_rate, maturity_date, "
        "rollover_policy FROM account WHERE person_id = ? AND kind IN ('TAGESGELD', 'FESTGELD') "
        "ORDER BY name",
        (person_id,),
    ).fetchall()
    accrued = {
        int(row["account_id"]): row
        for row in conn.execute(
            "SELECT account_id, accrued_cum_local, accrued_cum_eur, date FROM interest_accrual a "
            "WHERE a.date = (SELECT max(b.date) FROM interest_accrual b "
            "               WHERE b.account_id = a.account_id)"
        )
    }
    return {
        "accounts": [
            {
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "currency": r["currency"],
                "interest_rate": _decimal(r["interest_rate"]),
                "day_count": r["interest_day_count"],
                "frequency": r["interest_frequency"],
                "last_paid": r["interest_last_paid"],
                "withholding_rate": _decimal(r["withholding_rate"]),
                "maturity_date": r["maturity_date"],
                "rollover_policy": r["rollover_policy"],
                # Earned since the last credit and not yet paid. A real amount
                # the balance does not show and no statement reports between
                # payment dates, so reporting only the balance understates it.
                "accrued_unpaid_local": _decimal(
                    accrued[r["id"]]["accrued_cum_local"] if r["id"] in accrued else None
                ),
                "accrued_unpaid_eur": _decimal(
                    accrued[r["id"]]["accrued_cum_eur"] if r["id"] in accrued else None
                ),
                "accrued_asof": accrued[r["id"]]["date"] if r["id"] in accrued else None,
            }
            for r in rows
        ],
        "note": (
            "Accrued interest is not in total_value_eur. It has been earned but not "
            "credited, so counting it would report money the account does not hold yet."
        ),
    }


@app.get("/api/goal")
def read_goal(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Progress toward the target, with the projection's assumptions attached."""
    try:
        state = goal.progress(conn, person_id=person_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "as_of": state.as_of.isoformat(),
        "target_eur": _decimal(state.target_eur),
        "current_eur": _decimal(state.current_eur),
        "remaining_eur": _decimal(state.remaining_eur),
        "progress_pct": _decimal(state.progress_pct),
        "basis": state.basis,
        "monthly_contribution_eur": _decimal(state.monthly_contribution_eur),
        "months_at_current_rate": state.months_at_current_rate,
        "projected_date": state.projected_date.isoformat() if state.projected_date else None,
        "caveats": state.caveats,
    }


@app.put("/api/goal")
def write_goal(
    payload: GoalUpdate,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Set the target.

    The only write the HTTP surface can perform. A goal is a preference, not a
    fact about the portfolio; nothing derived depends on it, so it cannot
    corrupt a figure by being wrong. Everything that *would* corrupt a figure --
    a confirmed cash flow, a booked distribution -- stays on the CLI, where the
    inference behind it can be read before it is accepted.
    """
    try:
        with transaction(conn):
            stored = goal.set_target(conn, payload.target_eur)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"target_eur": _decimal(stored)}


@app.get("/", response_class=HTMLResponse)
@app.get("/debug", response_class=HTMLResponse)
def dashboard(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> HTMLResponse:
    """A server-rendered page. No build step, no JavaScript framework."""
    try:
        return HTMLResponse(render_dashboard(conn, person_id=person_id))
    except HTTPException:
        raise
    except LookupError as exc:
        return HTMLResponse(f"<p>{exc}</p>", status_code=404)


@app.exception_handler(sqlite3.OperationalError)
def _database_missing(_request: object, exc: sqlite3.OperationalError) -> JSONResponse:
    """A missing database is a setup problem, not a server fault."""
    return JSONResponse(
        {"detail": f"database not ready ({exc}); run `pt init-db` and `pt rebuild`"},
        status_code=503,
    )
