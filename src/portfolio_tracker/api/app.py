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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from portfolio_tracker import __version__
from portfolio_tracker.api import (
    admin,
    deps,
    instruments,
    narrative,
    planning,
    transactions,
)
from portfolio_tracker.api.deps import configure, get_db, get_writable_db
from portfolio_tracker.api.render import render_dashboard
from portfolio_tracker.core import (
    attribution,
    cash,
    fx,
    glossary,
    goal,
    inbox,
    incoming,
    look_through,
    projection,
    rebuild,
    returns,
    scenario,
    tax,
    valuation,
    vorabpauschale,
)
from portfolio_tracker.db.connection import transaction
from portfolio_tracker.money import ZERO

#: Re-exported so `from portfolio_tracker.api.app import get_db` keeps working
#: -- and, more importantly, keeps returning the *same function object* the
#: routers depend on. `dependency_overrides` is keyed on identity.
__all__ = ["app", "configure", "get_db", "get_writable_db"]

#: The installation these routes read. Owned by `deps` rather than declared
#: here, because the routers in sibling modules need the same value and two
#: module-level copies would drift the moment `configure()` moved one of them.
#: Read as `deps.DB_PATH` / `deps.INCOMING_DIR` at call time, never bound into
#: a local at import time.
STATIC_DIR = Path(__file__).parent / "static"


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


def _unexplained_funding(conn: sqlite3.Connection) -> tuple[int, float]:
    """Purchases larger than any cash this tool knows about.

    The single most consequential data gap there is. Unit accounting computes
    the unit price as ``(value - external_flow) / units``, so money that funded
    a purchase but was never recorded is read as pure investment return -- on
    this ledger that is the difference between a 49% return and a 13,000% one.

    Cheap enough to compute per request: it walks the transaction table once,
    and this is a single-household ledger of a few hundred rows.
    """
    try:
        report = cash.compute_flows(conn)
    except Exception:
        return 0, 0.0
    total = sum(float(event.amount) for event in report.unfunded if event.currency == fx.BASE)
    return len(report.unfunded), round(total, 2)


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
          -- Watched instruments are excluded from both counts. Nothing is
          -- taxed on something nobody owns, so a missing exemption rate on a
          -- watchlist entry is not a gap in anything -- and an alarm that
          -- cannot be acted on is how people learn to ignore the real ones.
          (SELECT count(*) FROM instrument i WHERE i.tax_regime <> 'DE_INVESTMENT'
             AND EXISTS (SELECT 1 FROM transaction_ t WHERE t.instrument_id = i.id
                         UNION ALL
                         SELECT 1 FROM position_daily d WHERE d.instrument_id = i.id))
            AS instruments_out_of_tax_scope,
          (SELECT count(*) FROM instrument i WHERE i.tax_regime = 'DE_INVESTMENT'
             AND i.teilfreistellung_pct IS NULL AND i.is_fund = 1
             AND EXISTS (SELECT 1 FROM transaction_ t WHERE t.instrument_id = i.id
                         UNION ALL
                         SELECT 1 FROM position_daily d WHERE d.instrument_id = i.id))
            AS unresolved_teilfreistellung,
          (SELECT count(*) FROM position_daily WHERE date = ? AND valuation_status <> 'OK')
            AS stale_or_missing_prices
        """,
        (person_id, as_of),
    ).fetchone()
    quality = {key: counts[key] for key in counts.keys()}  # noqa: SIM118
    unfunded, unfunded_eur = _unexplained_funding(conn)
    quality["unexplained_purchases"] = unfunded
    quality["unexplained_funding_eur"] = unfunded_eur
    return quality


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

    # A return computed over unexplained funding is not a weak figure, it is a
    # wrong one: money that paid for a purchase but was never recorded is read
    # as investment return. Reported as unreliable rather than quietly shown.
    unfunded, unfunded_eur = _unexplained_funding(conn)
    reliable = unfunded == 0

    return {
        "reliable": reliable,
        "unreliable_because": (
            None
            if reliable
            else (
                f"{unfunded} purchase(s) worth about EUR {unfunded_eur:,.0f} were made "
                "with money this tool has no record of. Until you say where it came "
                "from, that money is counted as investment return and every figure "
                "below is overstated."
            )
        ),
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
        "attribution": _attribution(conn, person_id),
        "attribution_by_holding": _attribution_by_holding(conn, person_id),
        "caveats": summary.caveats,
    }


def _attribution_by_holding(conn: sqlite3.Connection, person_id: int) -> dict[str, Any] | None:
    """Which holdings produced the return, in percentage points of the total.

    The portfolio-level split says the return was 82 points of prices and -26 of
    currency. This says which holdings those points came from -- the level at
    which decisions are actually made, since nobody can sell "currency".
    """
    state = attribution.by_holding(conn, person_id=person_id)
    if state is None:
        return None
    return {
        "total": _decimal(state.total),
        "holdings": [
            {
                "instrument": h.instrument,
                "local": _decimal(h.local),
                "currency": _decimal(h.currency),
                "total": _decimal(h.total),
            }
            for h in state.holdings
        ],
        "income": _decimal(state.income),
        "unexplained": _decimal(state.unexplained),
        "residual": _decimal(state.residual),
        "unit": "percentage points of the portfolio return, not each holding's own return",
        "caveats": state.caveats,
    }


def _attribution(conn: sqlite3.Connection, person_id: int) -> dict[str, Any] | None:
    """Where the return came from, with the parts summing to the whole.

    ``residual`` is reported even though it is zero by construction, and that is
    the point of reporting it: chaining daily contributions naively leaves a gap
    of eleven percentage points on this portfolio, and a reader has no way to
    tell a smoothed decomposition from an unsmoothed one except by checking.
    """
    state = attribution.summarise(conn, person_id=person_id)
    if state is None:
        return None
    return {
        "total": _decimal(state.total),
        "local": _decimal(state.local),
        "currency": _decimal(state.currency),
        "income": _decimal(state.income),
        "unexplained": _decimal(state.unexplained),
        "residual": _decimal(state.residual),
        "linking_method": state.linking,
        "identity": "total = local + currency + income + unexplained",
        "meanings": {
            "local": "asset prices, in the currency each is quoted in",
            "currency": "the opening capital re-translated as rates moved",
            "income": "distributions and credited interest",
            "unexplained": (
                "trading: fills away from the closing price, fees, and quantities "
                "that changed within a day. Named, not distributed."
            ),
        },
        "caveats": state.caveats,
    }


@app.get("/api/returns/series")
def series(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """The daily unit-price index, base 100 at inception.

    ``net_contributions_eur`` is a running total of external flows, and it is
    here so the page can draw value against money-put-in on one pair of axes.
    That single picture answers "how is it going" better than any percentage:
    the gap between the two lines *is* the return, and a rising total value
    that merely tracks a rising contribution line is visibly not a gain.

    It is summed here rather than stored because it is a pure function of a
    column that already exists, and a stored copy is one more thing a rebuild
    could leave disagreeing with its source.
    """
    rows = conn.execute(
        "SELECT date, unit_price, total_value_eur, cash_eur, deposits_eur, "
        "income_cash_eur, income_received_eur, drawdown_pct, net_external_flow_eur "
        "FROM portfolio_daily WHERE person_id = ? ORDER BY date",
        (person_id,),
    ).fetchall()

    points: list[dict[str, Any]] = []
    contributed = ZERO
    for r in rows:
        contributed += r["net_external_flow_eur"] or ZERO
        points.append(
            {
                "date": r["date"],
                "unit_price": _decimal(r["unit_price"]),
                "total_value_eur": _decimal(r["total_value_eur"]),
                "net_contributions_eur": _decimal(contributed),
                "deposits_eur": _decimal(r["deposits_eur"]),
                "uninvested_cash_eur": _decimal(r["cash_eur"]),
                "income_cash_eur": _decimal(r["income_cash_eur"]),
                "income_received_eur": _decimal(r["income_received_eur"]),
                "drawdown_pct": _decimal(r["drawdown_pct"]),
            }
        )

    return {
        "person_id": person_id,
        "points": points,
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
        # Inside gross_gains_eur, and broken out because it is the one component
        # on which tax is due without any money having arrived.
        "vorabpauschale_eur": _decimal(state.vorabpauschale_eur),
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
        "disclaimer": glossary.PLANNING_ONLY,
        # Additive. Existing keys are untouched; these carry the plain-English
        # name and a sentence of explanation for each statutory term above, so
        # a reader who does not speak German is not left decoding field names.
        "labels": glossary.labels_for(_ALLOWANCE_TERMS),
        "glossary": glossary.glossary_for(_ALLOWANCE_TERMS),
    }


#: The statutory concepts the allowance response uses, in reading order.
_ALLOWANCE_TERMS = (
    "statutory_cap_eur",
    "vorabpauschale_eur",
    "teilfreistellung_eur",
    "allowance_used_eur",
    "allowance_remaining_eur",
    "AKTIEN",
    "SONSTIGE",
    "estimated_tax_eur",
    "freistellungsauftrag_eur",
    "steuerbescheinigung",
)

_VAP_TERMS = ("vorabpauschale", "basisertrag_eur", "basiszins", "teilfreistellung_pct")


@app.get("/api/tax/vorabpauschale")
def vorabpauschale_forecast(
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
        "labels": glossary.labels_for(_VAP_TERMS),
        "glossary": glossary.glossary_for(_VAP_TERMS),
        "disclaimer": glossary.PLANNING_ONLY,
    }


class TaxConstantUpdate(BaseModel):
    """The published rates for one tax year.

    ``basiszins`` may be zero: the ministry has published 0.00% before, and it
    is a real answer meaning "no advance charge this year". Only a negative
    figure is refused.
    """

    tax_year: int = Field(ge=2000, le=2100)
    basiszins: Decimal = Field(ge=0, le=1, description="e.g. 0.032 for 3.20%")
    kest_rate: Decimal = Field(default=Decimal("0.25"), ge=0, le=1)
    soli_rate: Decimal = Field(default=Decimal("0.055"), ge=0, le=1)


@app.get("/api/tax/constants")
def tax_constants(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """The published rates this tool has been told about, and which are missing.

    Without the base rate for a year, the advance charge for that year cannot
    be computed and every tax endpoint correctly refuses rather than returning
    zero. Until now the only way to supply it was to write to the database by
    hand, which left the refusal permanent for anyone who does not. It is a
    number off a government publication, so the page can take it.
    """
    rows = conn.execute(
        "SELECT tax_year, basiszins, kest_rate, soli_rate FROM tax_constant ORDER BY tax_year"
    ).fetchall()
    known = {r["tax_year"] for r in rows}
    this_year = date.today().year
    return {
        "years": [
            {
                "tax_year": r["tax_year"],
                "basiszins": _decimal(r["basiszins"]),
                "kest_rate": _decimal(r["kest_rate"]),
                "soli_rate": _decimal(r["soli_rate"]),
            }
            for r in rows
        ],
        # Named explicitly so the page can offer the missing year rather than
        # making the reader work out which one it is short of.
        "missing_years": [y for y in range(this_year - 3, this_year + 1) if y not in known],
        "source": (
            "The government base rate (Basiszins) is published each January by "
            "the Bundesfinanzministerium and applies to the year just started."
        ),
        "glossary": glossary.glossary_for(("basiszins", "kapitalertragsteuer")),
    }


@app.put("/api/tax/constants")
def write_tax_constant(
    payload: TaxConstantUpdate,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Record the published rates for a year. Overwrites if already present."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO tax_constant (tax_year, basiszins, kest_rate, soli_rate) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (tax_year) DO UPDATE SET "
            "basiszins = excluded.basiszins, kest_rate = excluded.kest_rate, "
            "soli_rate = excluded.soli_rate",
            (
                payload.tax_year,
                str(payload.basiszins),
                str(payload.kest_rate),
                str(payload.soli_rate),
            ),
        )
    return {
        "tax_year": payload.tax_year,
        "basiszins": _decimal(payload.basiszins),
        "note": (
            "Stored. Tax figures for this year can now be computed; nothing was "
            "recalculated, because they are derived on read."
        ),
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


def _now_or_zero(row: sqlite3.Row | None, column: str, open_now: bool) -> float | None:
    """A current figure, or zero once the holding has been sold."""
    return _decimal(row[column]) if open_now and row is not None else 0.0


# `:path` because an import key may legitimately contain a slash -- `BTC/EUR`
# is one of the keys in the live database. Without it the route 404s on that
# holding whether the slash is sent raw or percent-encoded, since Starlette
# does not decode %2F inside a path segment. Declared after `/api/positions`,
# which is an exact match and therefore still wins.
@app.get("/api/positions/{import_key:path}")
def position_detail(
    import_key: str,
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """One holding over its whole life, with the currency question answered.

    For a foreign-currency holding this is the endpoint the whole FX design
    exists to serve: the source export shows the local gain translated at
    today's rate, which is not what the owner made, and the difference has a
    name and a size.

    **A sold holding is still a holding here.** ``core/valuation.py`` writes a
    ``position_daily`` row only while the quantity is positive, so resolving
    this endpoint through that table -- as it used to -- made everything ever
    sold return 404. The question "how did that investment do" is not answered
    by refusing to discuss it, and a realised gain is the one figure that is
    finally certain. So the instrument is resolved first, and an empty current
    position is a state (``closed: true``), not an error.
    """
    instrument = conn.execute(
        """
        SELECT i.id, i.import_key, i.name, i.currency, i.asset_class, i.is_fund,
               i.tax_regime, i.tax_scope_note, i.teilfreistellung_pct, i.status
        FROM instrument i WHERE i.import_key = ? OR i.isin = ?
        """,
        (import_key, import_key),
    ).fetchone()
    if instrument is None:
        raise HTTPException(404, f"no instrument called {import_key!r}")

    instrument_id = instrument["id"]

    rows = conn.execute(
        """
        SELECT p.* FROM position_daily p
        JOIN account a ON a.id = p.account_id
        WHERE p.instrument_id = ? AND a.person_id = ?
        ORDER BY p.date
        """,
        (instrument_id, person_id),
    ).fetchall()

    history = [
        {
            "date": r["date"],
            # Quantity and cost basis are here so the page can draw value
            # against what was paid for it. Without the cost line, a chart of
            # market value alone cannot show whether a rise is a gain or just
            # more money going in.
            "quantity": _decimal(r["quantity"]),
            "price_local": _decimal(r["price_local"]),
            "price_asof": r["price_asof"],
            "fx_rate": _decimal(r["fx_rate"]),
            "market_value_eur": _decimal(r["market_value_eur"]),
            "market_value_local": _decimal(r["market_value_local"]),
            "cost_basis_eur": _decimal(r["cost_basis_eur"]),
            "unrealized_eur": _decimal(r["unrealized_eur"]),
            "unrealized_local_eur": _decimal(r["unrealized_local_eur"]),
            "unrealized_fx_eur": _decimal(r["unrealized_fx_eur"]),
            "valuation_status": r["valuation_status"],
        }
        for r in rows
    ]

    # The current position, if there still is one. `rows` is ordered by date,
    # so the last row is the most recent valuation -- which for a closed
    # holding is the last day it was held, not today.
    last = rows[-1] if rows else None
    open_now = last is not None and (last["quantity"] or ZERO) > ZERO

    lots = [
        {
            "lot_id": r["id"],
            "open_date": r["open_date"],
            "qty_opened": _decimal(r["qty_opened"]),
            "qty_remaining": _decimal(r["qty_remaining"]),
            "currency": r["currency"],
            "cost_total_local": _decimal(r["cost_total_local"]),
            "cost_per_unit_local": _decimal(r["cost_per_unit_local"]),
            "cost_total_eur": _decimal(r["cost_total_eur"]),
            "cost_per_unit_eur": _decimal(r["cost_per_unit_eur"]),
            "fx_rate_at_open": _decimal(r["fx_rate_at_open"]),
            "vap_accumulated_eur": _decimal(r["vap_accumulated_eur"]),
            # Surfaced rather than hidden: a lira cost basis reconstructed from
            # a positions export is not the same evidence as one read off a
            # contract note, and it is the open lots where that matters.
            "basis_quality": r["basis_quality"],
        }
        for r in conn.execute(
            "SELECT l.* FROM lot l JOIN account a ON a.id = l.account_id "
            "WHERE l.instrument_id = ? AND a.person_id = ? "
            "ORDER BY l.open_date, l.id",
            (instrument_id, person_id),
        )
    ]

    disposals = [
        {
            "disposal_id": r["id"],
            "sell_date": r["sell_date"],
            "lot_id": r["lot_id"],
            "lot_open_date": r["lot_open_date"],
            "quantity": _decimal(r["qty"]),
            "proceeds_eur": _decimal(r["proceeds_eur"]),
            "cost_basis_eur": _decimal(r["cost_basis_eur"]),
            "gross_gain_eur": _decimal(r["gross_gain_eur"]),
            "fx_gain_eur": _decimal(r["fx_gain_eur"]),
            "vap_offset_eur": _decimal(r["vap_offset_eur"]),
            "teilfreistellung_eur": _decimal(r["teilfreistellung_eur"]),
            "taxable_gain_eur": _decimal(r["taxable_gain_eur"]),
            "holding_days": r["holding_days"],
            "basis_quality": r["basis_quality"],
        }
        for r in conn.execute(
            """
            SELECT d.*, t.trade_date AS sell_date, l.open_date AS lot_open_date
            FROM lot_disposal d
            JOIN transaction_ t ON t.id = d.sell_txn_id
            JOIN lot l ON l.id = d.lot_id
            JOIN account a ON a.id = l.account_id
            WHERE l.instrument_id = ? AND a.person_id = ?
            ORDER BY t.trade_date, d.id
            """,
            (instrument_id, person_id),
        )
    ]

    transactions = [
        {
            "id": r["id"],
            "type": r["type"],
            "trade_date": r["trade_date"],
            "quantity": _decimal(r["quantity"]),
            "price_per_unit_local": _decimal(r["price_per_unit_local"]),
            "gross_amount_eur": _decimal(r["gross_amount_eur"]),
            "currency": r["currency"],
            "fees_local": _decimal(r["fees_local"]),
            "fees_known": bool(r["fees_known"]),
            "source": r["source"],
            "confidence": r["confidence"],
            "flow_class": r["flow_class"],
            "note": r["note"],
        }
        for r in conn.execute(
            "SELECT t.* FROM transaction_ t JOIN account a ON a.id = t.account_id "
            "WHERE t.instrument_id = ? AND a.person_id = ? "
            "ORDER BY t.trade_date, t.id",
            (instrument_id, person_id),
        )
    ]

    # Life-to-date and final: these survive the sale, which is exactly what
    # makes them the figures worth showing on a holding you no longer own.
    realised = sum((d["gross_gain_eur"] or 0.0 for d in disposals), 0.0)
    income = sum(
        (
            t["gross_amount_eur"] or 0.0
            for t in transactions
            if t["type"] in ("DIVIDEND", "INTEREST")
        ),
        0.0,
    )

    decomposition: dict[str, Any] = {}
    if last is not None:
        decomposition = {
            "true_eur_gain": _decimal(last["unrealized_eur"]),
            "local_gain_at_todays_rate": _decimal(last["unrealized_local_eur"]),
            "currency_effect": _decimal(last["unrealized_fx_eur"]),
            "identity": "true_eur_gain = local_gain_at_todays_rate + currency_effect",
        }
        if instrument["currency"] != "EUR":
            local = last["unrealized_local_eur"]
            actual = last["unrealized_eur"]
            gap = last["unrealized_fx_eur"]
            direction = "depreciation" if gap < ZERO else "appreciation"
            decomposition["vs_broker_export"] = (
                f"A tracker translating at today's rate shows EUR {local:,.2f}. That is the "
                f"{instrument['currency']} gain converted as if the capital had been invested "
                f"today. The actual euro gain is EUR {actual:,.2f}; the EUR {abs(gap):,.2f} gap "
                f"is {instrument['currency']} {direction} against the original capital."
            )

    return {
        "as_of": last["date"] if last is not None else None,
        "instrument": instrument["import_key"],
        "name": instrument["name"],
        "currency": instrument["currency"],
        "asset_class": instrument["asset_class"],
        "is_fund": bool(instrument["is_fund"]),
        "status": instrument["status"],
        # `closed` is the whole reason this endpoint no longer 404s. The UI
        # reads it to say "sold on 4 March" instead of showing a live position.
        "closed": not open_now,
        "first_held": rows[0]["date"] if rows else None,
        "last_held": last["date"] if last is not None else None,
        # Zero rather than the last observed figure, for a holding that is
        # closed: "worth EUR 4,200" about something sold in March would be a
        # false statement, where zero is a true one.
        "quantity": _now_or_zero(last, "quantity", open_now),
        "market_value_local": _now_or_zero(last, "market_value_local", open_now),
        "market_value_eur": _now_or_zero(last, "market_value_eur", open_now),
        "cost_basis_eur": _now_or_zero(last, "cost_basis_eur", open_now),
        "cost_basis_eur_at_spot": _now_or_zero(last, "cost_basis_eur_at_spot", open_now),
        # Realised gain and income received are life-to-date and survive the
        # sale, which is exactly what makes them worth showing on a closed
        # holding: they are the only figures about it that are now final.
        "realised_gain_eur": realised,
        "income_eur": income,
        "return_decomposition": decomposition,
        "performance_basis": (
            "Price return only. The price line follows the raw closing price, "
            "not a dividend-adjusted one, so anything the holding paid out is "
            "counted under income received rather than shown as price growth. "
            "For a distributing fund that makes this line understate the total "
            "return by roughly its yield."
        ),
        "tax_regime": instrument["tax_regime"],
        "tax_scope_note": instrument["tax_scope_note"],
        "teilfreistellung_pct": _decimal(instrument["teilfreistellung_pct"]),
        "valuation_status": last["valuation_status"] if last is not None else None,
        "history": history,
        "lots": lots,
        "disposals": disposals,
        "transactions": transactions,
    }


@app.get("/api/allocation")
def allocation(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    dim: str = Query("asset_class", pattern="^(asset_class|currency|account|sector|country)$"),
    basis: str = Query(
        "quote",
        pattern="^(quote|look_through)$",
        description="quote: the instrument's own attribute. look_through: what its "
        "constituents are, where a composition has been imported.",
    ),
) -> dict[str, Any]:
    """Weights by one dimension.

    With ``basis=quote`` -- the default, and all v1 could do -- ``dim=currency``
    reports the *quote* currency, which is not the same as the economic one: a
    EUR-quoted S&P 500 tracker carries full dollar risk and is counted here as
    EUR. That is mechanically right and economically misleading, so the response
    says which basis it used rather than leaving it inferred.

    With ``basis=look_through`` the fund is opened up and its market value is
    distributed across what it actually holds. The answer is then only as
    complete as the compositions imported, so the response carries a
    ``coverage`` block saying how much of the portfolio each basis accounts
    for. A breakdown that is 60% evidence and 40% wrapper is a different claim
    from one that is 100% evidence, and the two must not look alike.
    """
    if basis == "look_through":
        return _look_through_allocation(conn, person_id, dim)

    row = _latest(conn, person_id)
    column = {
        "asset_class": "i.asset_class",
        "currency": "i.currency",
        "account": "a.name",
        "sector": "i.sector",
        "country": "i.domicile",
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
        # `sector` and `domicile` are nullable, and a NULL bucket is a real
        # answer -- "not recorded" -- rather than a row to drop. Dropping it
        # would make the remaining weights sum to 1 over a smaller portfolio.
        bucket = r["bucket"] or look_through.UNKNOWN
        buckets[bucket] = buckets.get(bucket, ZERO) + r["market_value_eur"]

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


def _coverage_block(coverage: look_through.Coverage) -> dict[str, Any]:
    """How much of the portfolio each basis accounts for.

    Present on every look-through response, and the reason they can be trusted:
    the same breakdown computed over a portfolio with one composition imported
    and over one with all of them looks identical without it.
    """
    return {
        "total_eur": _decimal(coverage.total_eur),
        "look_through_pct": _decimal(coverage.look_through_pct),
        "by_basis": {
            key: _decimal(coverage.by_basis.get(key, ZERO))
            for key in look_through.BASES
            if coverage.by_basis.get(key, ZERO) > ZERO
        },
        "uncovered": [
            {
                "instrument": entry.import_key,
                "name": entry.name,
                "value_eur": _decimal(entry.value_eur),
            }
            for entry in coverage.uncovered
        ],
        "compositions": [
            {
                "instrument": entry.import_key,
                "as_of": entry.composition_date.isoformat() if entry.composition_date else None,
                "age_days": entry.stale_days,
                "stale": entry.stale_days is not None
                and entry.stale_days > look_through.STALE_AFTER_DAYS,
            }
            for entry in coverage.positions
            if entry.composition_date is not None
        ],
        "basis_meanings": {
            "look_through": "distributed across a published constituent list",
            "declared": "the owner's recorded economic_currency; an assertion, not evidence",
            "direct": "not a fund, so the instrument is its own exposure",
            "quote_only": "a fund with no composition; reported on its wrapper",
        },
    }


def _look_through_allocation(conn: sqlite3.Connection, person_id: int, dim: str) -> dict[str, Any]:
    if dim not in look_through.DIMENSIONS:
        raise HTTPException(
            400,
            f"dim={dim} has no meaning below the fund wrapper; look-through supports "
            f"{', '.join(look_through.DIMENSIONS)}",
        )
    try:
        result = look_through.exposure(conn, person_id=person_id, dimension=dim)
    except look_through.NoValuation as exc:
        raise HTTPException(404, str(exc)) from exc

    return {
        "as_of": result.as_of.isoformat(),
        "dimension": dim,
        "basis": "look_through",
        "total_eur": _decimal(result.total_eur),
        "buckets": [
            {
                "name": bucket.name,
                "value_eur": _decimal(bucket.value_eur),
                "weight": _decimal(bucket.weight),
            }
            for bucket in result.buckets
        ],
        "coverage": _coverage_block(result.coverage),
        "caveats": result.caveats,
        "note": (
            "Exposure, not attribution. The local/currency split in /api/returns is "
            "computed from the rate each position was actually valued at and is an "
            "exact identity; this is an estimate of what the portfolio is exposed to "
            "next. The two answer different questions and are not reconciled."
        ),
    }


@app.get("/api/exposure/concentration")
def concentration(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    top: int = Query(25, ge=1, le=500),
) -> dict[str, Any]:
    """Underlying issuers, aggregated across every fund and direct holding.

    The view no instrument-level report can produce: one company held through
    three funds *and* directly is one exposure, and only the constituent lists
    show it.

    Weights are against total holdings including the part that could not be
    resolved, so an unlooked-through fund dilutes every figure rather than
    inflating it. Erring in the other direction would be the dangerous one.
    """
    try:
        result = look_through.concentration(conn, person_id=person_id, top=top)
    except look_through.NoValuation as exc:
        raise HTTPException(404, str(exc)) from exc

    return {
        "as_of": result.as_of.isoformat(),
        "total_eur": _decimal(result.total_eur),
        "resolved_eur": _decimal(result.resolved_eur),
        "top": top,
        "underlyings": [
            {
                "name": u.name,
                "identifier": u.identifier,
                "value_eur": _decimal(u.value_eur),
                "weight": _decimal(u.weight),
                "via": [{"instrument": key, "value_eur": _decimal(value)} for key, value in u.via],
            }
            for u in result.underlyings
        ],
        "coverage": _coverage_block(result.coverage),
        "caveats": result.caveats,
    }


@app.get("/api/look-through")
def look_through_status(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Which held instruments have a composition, and how good it is.

    The maintenance view. Every figure under ``basis=look_through`` is bounded
    by this table, so it is served as a first-class resource rather than being
    reconstructible from a coverage block.
    """
    try:
        as_of = look_through.latest_valuation_date(conn, person_id)
        funds = look_through.fund_coverage(conn, person_id=person_id, as_of=as_of)
    except look_through.NoValuation as exc:
        raise HTTPException(404, str(exc)) from exc

    held = sum((f.value_eur for f in funds), ZERO)
    missing = [f for f in funds if f.is_fund and not f.covered]
    return {
        # The valuation date, which is what every composition is selected
        # against -- not the date of any one of them.
        "as_of": as_of.isoformat(),
        "holdings_eur": _decimal(held),
        "covered_eur": _decimal(sum((f.value_eur for f in funds if f.covered), ZERO)),
        "stale_after_days": look_through.STALE_AFTER_DAYS,
        "instruments": [
            {
                "instrument": f.import_key,
                "name": f.name,
                "value_eur": _decimal(f.value_eur),
                "is_fund": f.is_fund,
                "composition_as_of": f.composition_date.isoformat() if f.composition_date else None,
                "holdings": f.holdings,
                "weight_sum": _decimal(f.weight_sum) if f.weight_sum is not None else None,
                "source": f.source,
                "age_days": f.stale_days,
                "stale": f.stale,
            }
            for f in funds
        ],
        "missing": [f.import_key for f in missing],
        "note": (
            "Import a composition with `pt look-through import`. A fund with none is "
            "reported on its wrapper's own currency and sector, which is a listing fact."
        ),
    }


@app.get("/api/incoming")
def incoming_files(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """What is sitting in the drop folder, and what each file appears to be.

    Read-only and safe to call on every page load. The detection is a proposal:
    a holdings file does not say which fund it belongs to, and guessing would
    attach one issuer's constituents to another fund without erroring anywhere.
    The person chooses, with the evidence in front of them.
    """
    found = incoming.scan(deps.INCOMING_DIR)
    funds = [
        {"instrument": r["import_key"], "name": r["name"]}
        for r in conn.execute(
            "SELECT import_key, name FROM instrument WHERE is_fund = 1 ORDER BY import_key"
        )
    ]
    return {
        "folder": str(deps.INCOMING_DIR),
        "files": [
            {
                "name": f.name,
                "size_bytes": f.size_bytes,
                "kind": f.kind,
                "summary": f.summary,
                "problem": f.problem,
                "importable": f.importable,
                "holdings": f.lines or None,
                "weight_sum": _decimal(f.weight_sum),
                "unit": f.unit,
                "columns": f.columns,
                "suggested_as_of": (f.suggested_as_of.isoformat() if f.suggested_as_of else None),
            }
            for f in found
        ],
        "funds": funds,
        "note": (
            "Drop issuer holdings files here. Nothing is imported until you choose "
            "which fund a file belongs to and confirm it."
        ),
    }


class ImportRequest(BaseModel):
    """Which dropped file, against which fund, published when.

    All three are required and none is inferred. The file names the issuer's
    own product code at best, and attaching constituents to the wrong fund
    corrupts every exposure figure silently.
    """

    name: str = Field(min_length=1, description="Filename in the incoming folder")
    instrument: str = Field(min_length=1, description="Import key or ISIN of the fund")
    as_of: date = Field(description="The date the FUND published this composition")


@app.post("/api/incoming/import")
def import_incoming(
    request: ImportRequest,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Import one dropped holdings file. A real write, so it asks for one."""
    try:
        with transaction(conn):
            result = incoming.import_holdings(
                conn,
                name=request.name,
                instrument=request.instrument,
                as_of=request.as_of,
                directory=deps.INCOMING_DIR,
            )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

    return {
        "instrument": result.instrument,
        "as_of": result.as_of.isoformat(),
        "holdings": result.holdings,
        "replaced": result.replaced,
        "archived_to": result.archived_to,
        "rebuild_required": False,
        "note": "Exposure is derived on read, so this takes effect immediately.",
    }


@app.post("/api/vorabpauschale/propose")
def propose_vorabpauschale(
    tax_year: int = Query(
        ...,
        alias="year",
        ge=2018,
        le=2100,
        description="The full year the charge accrues FOR, e.g. 2025",
    ),
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Compute the January charge and queue it for review.

    Proposing is not booking. The figures land in the same review inbox as
    every other inference, and reach the ledger only when a person confirms
    them -- which matters more here than anywhere else, because this is the one
    number that ends up on an *Anlage KAP*.
    """
    try:
        with transaction(conn):
            result = vorabpauschale.propose(conn, tax_year=tax_year)
    except tax.TaxDataMissing as exc:
        # A refusal, not a zero. Same rule as /api/tax/*.
        raise HTTPException(409, str(exc)) from exc

    return {
        "tax_year": tax_year,
        "queued": result.created,
        "already_pending": result.already_pending,
        "owing_nothing": result.skipped_zero,
        "total_taxable_eur": _decimal(result.total_taxable_eur),
        "charges": [
            {
                "instrument": c.import_key,
                "gross_eur": _decimal(c.gross_eur),
                "taxable_eur": _decimal(c.taxable_eur),
                "basis": c.basis,
            }
            for c in result.charges
        ],
        "note": (
            "Check the Basiszins and each fund's Teilfreistellung before confirming. "
            "The rate follows the fund's Anlagebedingungen, not what it holds."
        ),
    }


@app.get("/api/goal/projection")
def goal_projection(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    target: Decimal | None = Query(None, gt=0, description="Override the stored target"),
    monthly: Decimal | None = Query(None, ge=0, description="Override the measured rate"),
    years: int = Query(projection.HORIZON_YEARS, ge=1, le=50),
    paths: int = Query(projection.DEFAULT_PATHS, ge=100, le=20000),
    block_days: int = Query(projection.BLOCK_DAYS, ge=5, le=756, description="Resample block"),
    window: int = Query(
        projection.CONTRIBUTION_WINDOW_MONTHS,
        ge=1,
        le=60,
        description="Months of history the contribution suggestion is measured over",
    ),
) -> dict[str, Any]:
    """When the target is reached, and what that answer depends on.

    Two answers, neither a forecast. ``scenarios`` is a **sensitivity table**:
    the reach date at each of several stated returns, so the spread shows how
    much the answer rests on the assumption. ``bootstrap`` resamples this
    portfolio's own daily returns, and carries its sample window in every
    response because that window is the entire content of the claim.

    The tool never picks a rate. A single projected date would be a number
    nobody chose, presented as one somebody did.
    """
    try:
        result = projection.project(
            conn,
            person_id=person_id,
            target_eur=target,
            monthly_eur=monthly,
            horizon_years=years,
            paths=paths,
            block_days=block_days,
            contribution_window_months=window,
        )
        observed = projection.observed_annual_return(conn, person_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except projection.ProjectionError as exc:
        raise HTTPException(422, str(exc)) from exc

    bootstrap = None
    if result.bootstrap is not None:
        b = result.bootstrap
        bootstrap = {
            "paths": b.paths,
            "block_days": b.block_days,
            "effective_samples": _decimal(b.effective_samples),
            "model": {
                "days": b.model.days,
                "from": b.model.start.isoformat() if b.model.start else None,
                "to": b.model.end.isoformat() if b.model.end else None,
                "covers_pct_of_portfolio": _decimal(b.model.covered_pct),
                "holdings": [
                    {"instrument": key, "weight": _decimal(weight)}
                    for key, weight in b.model.holdings
                ],
                "excluded": [{"instrument": key, "why": why} for key, why in b.model.excluded],
                "basis": (
                    "price history of the holdings you own today, at today's weights -- "
                    "not the portfolio's own past performance"
                ),
            },
            "earliest_decile": b.earliest_decile.isoformat() if b.earliest_decile else None,
            "median": b.median.isoformat() if b.median else None,
            "latest_decile": b.latest_decile.isoformat() if b.latest_decile else None,
            "never_reached_pct": _decimal(b.never_reached_pct),
            "caveats": b.caveats,
        }

    return {
        "as_of": result.as_of.isoformat(),
        "target_eur": _decimal(result.target_eur),
        "current_eur": _decimal(result.current_eur),
        "contributions": {
            "monthly_eur": _decimal(result.contributions.monthly_eur),
            # measured = a suggestion drawn from a stated window; supplied = your
            # decision. Never presentable as the same kind of number.
            "source": result.contributions.source,
            "window_months": result.contributions.window_months,
            "total_eur": _decimal(result.contributions.total_eur),
            "measured_from": (
                result.contributions.measured_from.isoformat()
                if result.contributions.measured_from
                else None
            ),
            "measured_to": (
                result.contributions.measured_to.isoformat()
                if result.contributions.measured_to
                else None
            ),
            "explanation": result.contributions.sentence,
            "editable": True,
        },
        "monthly_contribution_eur": _decimal(result.monthly_contribution_eur),
        "horizon_years": result.horizon_years,
        "already_reached": result.already_reached,
        "basis": "a sensitivity table over stated returns; the tool chooses none of them",
        "scenarios": [
            {
                "annual_return": _decimal(s.annual_return),
                "reached_on": s.reached_on.isoformat() if s.reached_on else None,
                "months": s.months,
                "value_at_horizon_eur": _decimal(s.value_at_horizon_eur),
            }
            for s in result.scenarios
        ],
        # Context, deliberately not one of the scenarios: what this portfolio
        # has done is history, not a rate anybody should plan on.
        "observed_annual_return": _decimal(observed),
        "bootstrap": bootstrap,
        "caveats": result.caveats,
    }


@app.get("/api/scenario/sell")
def scenario_sell(
    conn: sqlite3.Connection = Depends(get_db),
    instrument: str = Query(..., description="Import key, ISIN or symbol"),
    qty: Decimal | None = Query(None, gt=0, description="Units; default is the whole holding"),
    person_id: int = Query(1, alias="person"),
    year: int | None = Query(None, description="Tax year; default is the current one"),
) -> dict[str, Any]:
    """What selling a holding today would realise, and what it would cost.

    Read-only, and takes the read-only connection to prove it: a scenario that
    left rows behind would be indistinguishable from a decision afterwards.

    Priced at the last observed close. No future return is assumed anywhere —
    the same rule ``/api/goal`` follows when it projects on contributions alone.
    """
    try:
        result = scenario.sell(
            conn, instrument=instrument, quantity=qty, person_id=person_id, tax_year=year
        )
    except scenario.ScenarioError as exc:
        raise HTTPException(404, str(exc)) from exc
    except tax.TaxDataMissing as exc:
        # Same rule as /api/tax/*: a missing statutory input is a refusal.
        raise HTTPException(409, str(exc)) from exc
    except fx.FxUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        "as_of": result.as_of.isoformat(),
        "instrument": result.instrument,
        "quantity": _decimal(result.quantity),
        "quantity_held": _decimal(result.quantity_held),
        "price": {
            "local": _decimal(result.price_local),
            "currency": result.currency,
            "as_of": result.price_asof.isoformat(),
            "stale_days": result.price_stale_days,
            "fx_rate": _decimal(result.fx_rate),
        },
        "proceeds_eur": _decimal(result.proceeds_eur),
        "cost_basis_eur": _decimal(result.cost_basis_eur),
        "gross_gain_eur": _decimal(result.gross_gain_eur),
        "vap_offset_eur": _decimal(result.vap_offset_eur),
        "teilfreistellung_eur": _decimal(result.teilfreistellung_eur),
        "taxable_gain_eur": _decimal(result.taxable_gain_eur),
        "pot": result.pot,
        "in_tax_scope": result.in_tax_scope,
        # The figure being asked for. The same gain costs nothing or 26.375%
        # depending only on what the allowance has already absorbed, so a
        # per-sale tax number means nothing without the year around it.
        "marginal_tax_eur": _decimal(result.marginal_tax_eur),
        "net_proceeds_eur": _decimal(result.net_proceeds_eur),
        "allowance": {
            "remaining_before_eur": _decimal(result.allowance_remaining_before_eur),
            "remaining_after_eur": _decimal(result.allowance_remaining_after_eur),
        },
        "fifo_consumes": [
            {
                "lot_id": lot.lot_id,
                "opened": lot.open_date.isoformat(),
                "quantity": _decimal(lot.quantity),
                "cost_basis_eur": _decimal(lot.cost_basis_eur),
                "proceeds_eur": _decimal(lot.proceeds_eur),
                "gross_gain_eur": _decimal(lot.gross_gain_eur),
                "vap_offset_eur": _decimal(lot.vap_offset_eur),
                "holding_days": lot.holding_days,
            }
            for lot in result.consumed
        ],
        "caveats": result.caveats,
        "wrote_nothing": True,
    }


@app.get("/api/scenario/harvest")
def scenario_harvest(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    year: int | None = Query(None),
) -> dict[str, Any]:
    """What could be sold to use up the year's tax-free allowance.

    The Sparerpauschbetrag does not carry forward, so unused headroom expires
    on 31 December. Deliberately a list rather than a recommendation: which
    holding to sell depends on what the owner wants to keep owning, and this
    endpoint knows the tax and nothing else.
    """
    try:
        result = scenario.harvest(conn, person_id=person_id, tax_year=year)
    except tax.TaxDataMissing as exc:
        raise HTTPException(409, str(exc)) from exc

    def entry(candidate: scenario.Candidate) -> dict[str, Any]:
        return {
            "instrument": candidate.instrument,
            "quantity": _decimal(candidate.quantity),
            "market_value_eur": _decimal(candidate.market_value_eur),
            "gross_gain_eur": _decimal(candidate.gross_gain_eur),
            "taxable_gain_eur": _decimal(candidate.taxable_gain_eur),
            "pot": candidate.pot,
        }

    return {
        "tax_year": result.tax_year,
        "as_of": result.as_of.isoformat(),
        "allowance": {
            "cap_eur": _decimal(result.allowance_cap_eur),
            "used_eur": _decimal(result.allowance_used_eur),
            "remaining_eur": _decimal(result.allowance_remaining_eur),
        },
        "realisable_tax_free_eur": _decimal(result.realisable_free_eur),
        "gains": [entry(c) for c in result.gains],
        "losses": [entry(c) for c in result.losses],
        "caveats": result.caveats,
        "wrote_nothing": True,
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

    The safest write on this surface: a goal is a preference, not a fact about
    the portfolio, and nothing derived depends on it, so it cannot corrupt a
    figure by being wrong.

    It was once the *only* write, which is no longer true -- see ADR 0010. What
    survives from that rule is the part that mattered: an inference still
    cannot reach the ledger without somebody reading its derivation and
    confirming it.
    """
    try:
        with transaction(conn):
            stored = goal.set_target(conn, payload.target_eur)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"target_eur": _decimal(stored)}


@app.get("/debug", response_class=HTMLResponse)
def dashboard(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    monthly: Decimal | None = Query(
        None, ge=0, description="Override the contribution the projection assumes"
    ),
) -> HTMLResponse:
    """The server-rendered page, kept as the ground truth behind the app.

    It answers without JavaScript, without charts and without a router, which
    makes it the thing to open when the app itself looks wrong: if a figure
    differs between here and the main page, the bug is in the front end.

    ``monthly`` is the one figure this page takes from the URL: a measured
    savings rate is a suggestion, and the owner is the authority on what they
    actually plan to keep doing.
    """
    try:
        return HTMLResponse(render_dashboard(conn, person_id=person_id, monthly_eur=monthly))
    except HTTPException:
        raise
    except LookupError as exc:
        return HTMLResponse(f"<p>{exc}</p>", status_code=404)


# Recording a trade lives in its own module: it is the largest single feature
# on this surface and app.py is long enough. Included after the routes above
# so the OpenAPI page lists reporting before recording.
app.include_router(transactions.router)
app.include_router(admin.router)
app.include_router(instruments.router)
app.include_router(narrative.router)
app.include_router(planning.router)


# The app itself. Mounted last so nothing here can shadow an /api route, and
# deliberately not `html=True` on a root mount: that would make StaticFiles
# answer every unmatched path, turning a typo in an endpoint name into a
# silent 200 with an HTML body.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The application shell.

    Serves a file and touches no database, which is the point: a database that
    has not been built yet should produce a page that explains that, not a 503
    from a dependency that ran before any code could catch it.

    ``no-store`` because the shell is small and a stale one silently pins the
    user to an old version of the front end after an update. The CSP is
    belt-and-braces alongside the vendoring rule -- if a remote script ever
    creeps into the page, it fails loudly here rather than working on a
    developer's machine and failing on the owner's.
    """
    return FileResponse(
        STATIC_DIR / "index.html",
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
            ),
        },
    )


@app.exception_handler(sqlite3.OperationalError)
def _database_missing(_request: object, exc: sqlite3.OperationalError) -> JSONResponse:
    """A missing database is a setup problem, not a server fault."""
    return JSONResponse(
        {"detail": f"database not ready ({exc}); run `pt init-db` and `pt rebuild`"},
        status_code=503,
    )
