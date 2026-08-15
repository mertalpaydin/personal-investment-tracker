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

from portfolio_tracker import __version__
from portfolio_tracker.api.render import render_dashboard
from portfolio_tracker.core import returns, tax
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect
from portfolio_tracker.money import ZERO

DB_PATH = Path(DEFAULT_DB_PATH)


def get_db() -> Any:
    """One connection per request, read-only, closed on the way out.

    Read-only is a design statement as much as a safeguard: every write in this
    system goes through the CLI, where it can be reviewed, so the HTTP surface
    has no path to the ledger at all.
    """
    conn = connect(DB_PATH, read_only=True, same_thread=False)
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
        "SELECT date, unit_price, total_value_eur, cash_eur, deposits_eur, drawdown_pct "
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


@app.get("/api/pending")
def pending(
    conn: sqlite3.Connection = Depends(get_db),
    status: str = Query("PROPOSED"),
) -> dict[str, Any]:
    """Inferences awaiting a decision. Nothing here has reached the ledger."""
    rows = conn.execute(
        "SELECT id, kind, proposed_date, currency, amount_local, basis, status "
        "FROM pending_entry WHERE status = ? ORDER BY proposed_date",
        (status,),
    ).fetchall()
    return {
        "status": status,
        "entries": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "date": r["proposed_date"],
                "currency": r["currency"],
                "amount": _decimal(r["amount_local"]),
                "basis": r["basis"],
            }
            for r in rows
        ],
    }


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
