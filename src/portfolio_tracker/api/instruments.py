"""Watching an instrument, and comparing it with what you already hold.

The question this answers, in the owner's words: *"this ETF has the same risk
level as one of yours, and over the last three years it earned 7% more."*

Both halves are computed in `core/peers.py` from stored prices and a stated
taxonomy. Nothing here forecasts and nothing recommends; the endpoint reports
differences and leaves what to do about them to the person reading it.

Adding a ticker validates it against the provider first, then backfills its
history in the background. The backfill is threaded because five years of
daily prices is twenty to forty seconds and a request that hangs that long
reads as a broken page.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from portfolio_tracker.api import deps
from portfolio_tracker.api.deps import get_db, get_writable_db
from portfolio_tracker.core import classify, peers, performance, universe, watchlist
from portfolio_tracker.db.connection import transaction
from portfolio_tracker.ingest.providers.base import ProviderUnavailable
from portfolio_tracker.ingest.providers.market import YFinanceProvider
from portfolio_tracker.jobs import update as job
from portfolio_tracker.llm import tasks

router = APIRouter(tags=["watchlist"])


def _num(value: object) -> float | None:
    from decimal import Decimal

    return float(value) if isinstance(value, Decimal) else None


# ---------------------------------------------------------------------------
# Looking a ticker up
# ---------------------------------------------------------------------------


def _near_misses(symbol: str) -> list[dict[str, Any]]:
    """Catalogue entries whose ticker shares a stem with what was typed.

    Matched on the part before the exchange suffix, which is where the
    confusion lives: the fund is right and the listing is wrong.
    """
    stem = symbol.strip().upper().split(".")[0]
    if len(stem) < 2:
        return []
    out: list[dict[str, Any]] = []
    for key, spec in universe.catalogue().items():
        other = key.upper().split(".")[0]
        if (
            other == stem
            or (len(stem) >= 3 and stem in other)
            or (len(other) >= 3 and other in stem)
        ):
            out.append({"symbol": key, "name": spec.get("name"), "currency": spec.get("currency")})
    return out[:6]


@router.get("/api/watchlist/lookup")
def lookup(
    symbol: str = Query(..., min_length=1),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """What a ticker appears to be. Writes nothing.

    Answers 200 with ``found: false`` rather than an error status when the
    symbol is unknown: "I could not find that" is a normal outcome of typing
    a ticker, not a fault, and the form needs to say so beside the field.
    """
    sym_clean = symbol.strip()
    existing = conn.execute(
        """
        SELECT import_key, isin, name, asset_class, currency, price_symbol,
               ter_bps, risk_bucket, risk_level
        FROM instrument
        WHERE import_key = ? OR price_symbol = ? OR import_key = ? OR price_symbol = ?
        LIMIT 1
        """,
        (sym_clean, sym_clean, sym_clean.upper(), sym_clean.upper()),
    ).fetchone()

    try:
        found = YFinanceProvider().describe(sym_clean)
    except ProviderUnavailable as exc:
        if existing is not None:
            return {
                "found": True,
                "symbol": existing["price_symbol"] or existing["import_key"],
                "name": existing["name"],
                "currency": existing["currency"],
                "exchange": "XETRA",
                "quote_type": existing["asset_class"],
                "last_close": None,
                "last_date": None,
                "observations": 0,
                "suggested_key": existing["import_key"],
                "risk_bucket": existing["risk_bucket"],
                "risk_level": existing["risk_level"],
                "ter_bps": existing["ter_bps"],
                "isin": existing["isin"],
            }
        return {
            "found": False,
            "symbol": sym_clean,
            "problem": str(exc),
            "did_you_mean": _near_misses(sym_clean),
        }

    suggested_key = (
        existing["import_key"]
        if existing is not None
        else (found.symbol if "." in sym_clean else found.symbol.split(".")[0].upper())
    )

    inferred_bucket = (
        existing["risk_bucket"]
        if existing and existing["risk_bucket"]
        else classify.infer_category(found.name, found.quote_type or "ETF")
    )
    inferred_level = (
        existing["risk_level"]
        if existing and existing["risk_level"]
        else (classify.DEFAULT_RISK_LEVEL.get(inferred_bucket) if inferred_bucket else None)
    )

    return {
        "found": True,
        "symbol": found.symbol,
        "name": existing["name"] if existing and existing["name"] else found.name,
        "currency": found.currency,
        "exchange": found.exchange,
        "quote_type": found.quote_type,
        "last_close": _num(found.last_close),
        "last_date": found.last_date.isoformat() if found.last_date else None,
        "observations": found.observations,
        "suggested_key": suggested_key,
        "risk_bucket": inferred_bucket,
        "risk_level": inferred_level,
        "ter_bps": existing["ter_bps"] if existing else None,
        "isin": existing["isin"] if existing else None,
        "note": (
            "Check the name and currency before adding. The currency in "
            "particular has to be right: prices in another one are refused."
        ),
    }


# ---------------------------------------------------------------------------
# The watchlist
# ---------------------------------------------------------------------------


class WatchRequest(BaseModel):
    import_key: str = Field(min_length=1, description="Bare symbol, e.g. IWDA")
    name: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    price_symbol: str = Field(min_length=1, description="Provider ticker, e.g. IWDA.DE")
    asset_class: str = Field(default="ETF")
    isin: str | None = None
    ter_bps: int | None = Field(default=None, ge=0, le=1000)
    risk_bucket: str | None = None
    risk_level: int | None = Field(default=None, ge=1, le=7)
    notes: str | None = None


class DetailsRequest(BaseModel):
    """The facts no free source supplies."""

    ter_bps: int | None = Field(default=None, ge=0, le=1000)
    risk_bucket: str | None = None
    risk_level: int | None = Field(default=None, ge=1, le=7)


def _entry_json(entry: watchlist.WatchEntry) -> dict[str, Any]:
    return {
        "instrument": entry.import_key,
        "name": entry.name,
        "price_symbol": entry.price_symbol,
        "currency": entry.currency,
        "asset_class": entry.asset_class,
        "status": entry.status,
        "ter_bps": entry.ter_bps,
        "ter_pct": _num(watchlist.as_percent(entry.ter_bps)),
        "risk_bucket": entry.risk_bucket,
        "risk_level": entry.risk_level,
        "prices": entry.prices,
        "first_price": entry.first_price.isoformat() if entry.first_price else None,
        "last_price": entry.last_price.isoformat() if entry.last_price else None,
        "caveats": entry.caveats,
    }


def _metrics_json(m: performance.Metrics) -> dict[str, Any]:
    return {
        "basis": m.basis,
        "observations": m.observations,
        "history_from": m.history_from.isoformat() if m.history_from else None,
        "ret_1m": _num(m.ret_1m),
        "ret_3m": _num(m.ret_3m),
        "ret_6m": _num(m.ret_6m),
        "ret_1y": _num(m.ret_1y),
        "ret_3y_ann": _num(m.ret_3y_ann),
        "ret_5y_ann": _num(m.ret_5y_ann),
        "vol_1y": _num(m.vol_1y),
        "vol_3y": _num(m.vol_3y),
        "max_dd_3y": _num(m.max_dd_3y),
        "sharpe_1y": _num(m.sharpe_1y),
        "sharpe_3y": _num(m.sharpe_3y),
        "caveats": m.caveats,
    }


@router.get("/api/watchlist")
def index(
    conn: sqlite3.Connection = Depends(get_db),
    include_universe: bool = Query(False, alias="all"),
    include_metrics: bool = Query(True, alias="metrics"),
) -> dict[str, Any]:
    """Watched instruments, each with how it has actually done."""
    entries = watchlist.listing(conn, include_universe=include_universe)
    fundamentals = peers.load_fundamentals(conn)
    out: list[dict[str, Any]] = []
    for entry in entries:
        row = conn.execute(
            "SELECT id, currency FROM instrument WHERE import_key = ?", (entry.import_key,)
        ).fetchone()
        body = _entry_json(entry)
        fund = fundamentals.get(int(row["id"])) if row is not None else None
        if fund is not None:
            body["fundamentals"] = {
                "sector": fund.get("sector"),
                "value_score": fund.get("value_score"),
                "value_badge": fund.get("value_badge"),
                "value_narrative": fund.get("value_narrative"),
                "quality_score": fund.get("quality_score"),
                "quality_narrative": fund.get("quality_narrative"),
                "forward_pe": _num(fund.get("forward_pe")),
                "dividend_yield": _num(fund.get("dividend_yield")),
                "target_upside_pct": _num(fund.get("target_upside_pct")),
                "analyst_count": fund.get("analyst_count"),
                "buy_pct": _num(fund.get("buy_pct")),
                "fetched_at": fund.get("fetched_at"),
            }
        if include_metrics and row is not None and entry.prices:
            body["performance"] = _metrics_json(
                performance.metrics(
                    conn,
                    int(row["id"]),
                    import_key=entry.import_key,
                    currency=row["currency"],
                )
            )
        out.append(body)
    return {
        "watchlist": out,
        "basis": (
            "Returns are in euros, converted at each day's rate, so a "
            "foreign-currency instrument shows what a euro investor would have "
            "earned rather than its local return."
        ),
    }


@router.post("/api/watchlist", status_code=201)
def add(
    body: WatchRequest,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Start watching an instrument, then fetch its history in the background."""
    try:
        with transaction(conn):
            instrument_id = watchlist.add(
                conn,
                import_key=body.import_key,
                name=body.name,
                currency=body.currency.upper(),
                asset_class=body.asset_class,
                price_symbol=body.price_symbol,
                isin=body.isin,
                ter_bps=body.ter_bps,
                risk_bucket=body.risk_bucket,
                risk_level=body.risk_level,
                notes=body.notes,
            )
    except watchlist.WatchlistError as exc:
        raise HTTPException(409, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"Could not add {body.import_key}: {exc}") from exc

    db_path = deps.DB_PATH

    def backfill() -> None:
        # `rebuild_after=False`: a watched instrument has no lots, so it cannot
        # change a single derived figure, and rebuilding would be minutes of
        # work to reproduce exactly what is already there.
        job.run_update(db_path, trigger="BACKFILL", only=[instrument_id], rebuild_after=False)

    threading.Thread(target=backfill, daemon=True, name="watchlist-backfill").start()

    return {
        "instrument": body.import_key,
        "backfilling": True,
        "note": (
            "Fetching several years of prices in the background. The figures "
            "fill in once it finishes; it usually takes under a minute."
        ),
    }


@router.delete("/api/watchlist/{import_key:path}")
def remove(
    import_key: str,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Stop watching. Keeps every price already downloaded."""
    try:
        with transaction(conn):
            watchlist.remove(conn, import_key)
    except watchlist.WatchlistError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "instrument": import_key,
        "note": "Removed from the watchlist. Its price history is kept, so re-adding is instant.",
    }


@router.patch("/api/instruments/{import_key:path}/details")
def details(
    import_key: str,
    body: DetailsRequest,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Record ongoing charge, risk category, and risk level.

    Neither can be fetched. No free source supplies a UCITS fund's ongoing
    charge -- it is on the key information document -- and a risk category is
    a judgement about what the fund *is*, which no price series contains.
    """
    try:
        with transaction(conn):
            watchlist.set_details(
                conn,
                import_key,
                ter_bps=body.ter_bps,
                risk_bucket=body.risk_bucket,
                risk_level=body.risk_level,
            )
    except watchlist.WatchlistError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"instrument": import_key, "saved": True}


# ---------------------------------------------------------------------------
# Series and peers
# ---------------------------------------------------------------------------


@router.get("/api/instruments/{import_key:path}/series")
def series(
    import_key: str,
    conn: sqlite3.Connection = Depends(get_db),
    since: date | None = Query(None, alias="from"),
    until: date | None = Query(None, alias="to"),
    basis: str = Query("auto"),
) -> dict[str, Any]:
    """Indexed price series for one instrument, in euros."""
    try:
        row = peers._instrument(conn, import_key)
    except peers.PeerError as exc:
        raise HTTPException(404, str(exc)) from exc

    points, basis, caveats = performance.series(
        conn,
        int(row["id"]),
        currency=row["currency"],
        start=since,
        end=until,
        basis=basis,
    )
    return {
        "instrument": row["import_key"],
        "name": row["name"],
        "currency": row["currency"],
        "basis": basis,
        "unit": "EUR",
        "points": [{"date": p.on.isoformat(), "level": float(p.level)} for p in points],
        "caveats": caveats,
    }


def _candidate_json(c: peers.Candidate) -> dict[str, Any]:
    return {
        "instrument": c.import_key,
        "name": c.name,
        "status": c.status,
        "bucket": c.bucket,
        "risk_level": c.risk_level,
        "vol_band": c.vol_band,
        "basis": c.basis,
        "comparable": c.comparable,
        "observations": c.observations,
        "ret_1m": _num(c.ret_1m),
        "ret_3m": _num(c.ret_3m),
        "ret_6m": _num(c.ret_6m),
        "ret_1y": _num(c.ret_1y),
        "ret_3y_ann": _num(c.ret_3y_ann),
        "vol_1y": _num(c.vol_1y),
        "vol_3y": _num(c.vol_3y),
        "max_dd_3y": _num(c.max_dd_3y),
        "sharpe_1y": _num(c.sharpe_1y),
        "sharpe_3y": _num(c.sharpe_3y),
        "ret_1y_delta": _num(c.ret_1y_delta),
        "ret_3y_ann_delta": _num(c.ret_3y_ann_delta),
        "vol_3y_delta": _num(c.vol_3y_delta),
        "ter_bps": c.ter_bps,
        "ter_bps_delta": c.ter_bps_delta,
        "overlap_pct": _num(c.overlap_pct),
        "sector": c.sector,
        "industry": c.industry,
        "forward_pe": _num(c.forward_pe),
        "trailing_pe": _num(c.trailing_pe),
        "fcf_yield": _num(c.fcf_yield),
        "dividend_yield": _num(c.dividend_yield),
        "price_to_book": _num(c.price_to_book),
        "ev_ebitda": _num(c.ev_ebitda),
        "pct_off_52w_high": _num(c.pct_off_52w_high),
        "target_upside_pct": _num(c.target_upside_pct),
        "analyst_count": c.analyst_count,
        "buy_pct": _num(c.buy_pct),
        "quality_score": c.quality_score,
        "quality_narrative": c.quality_narrative,
        "value_score": c.value_score,
        "value_badge": c.value_badge,
        "value_narrative": c.value_narrative,
        "caveats": c.caveats,
    }


def _scan_json(scan: peers.PeerScan) -> dict[str, Any]:
    return {
        "as_of": scan.as_of.isoformat(),
        "subject": _candidate_json(scan.subject),
        "bucket": scan.bucket,
        "risk_level": scan.risk_level,
        "bucket_method": scan.bucket_method,
        "vol_band": scan.vol_band,
        "mode": getattr(scan, "mode", "value"),
        "is_stock": getattr(scan, "is_stock", False),
        "sector": getattr(scan, "sector", None),
        "universe_size": scan.universe_size,
        "considered": scan.considered,
        "candidates": [_candidate_json(c) for c in scan.candidates],
        "caveats": scan.caveats,
        "disclaimer": (
            "A difference in past return, not a prediction and not a "
            "recommendation. A fund that returned more took some risk to do "
            "it; the volatility and worst fall are shown so that is visible."
        ),
    }


@router.get("/api/peers/leaderboard")
def peer_leaderboard(
    conn: sqlite3.Connection = Depends(get_db),
    risk_level: int | None = Query(None, ge=1, le=7),
    risk_bucket: str | None = Query(None),
    top: int = Query(500, ge=1, le=1000),
    include_owned: bool = Query(True, alias="owned"),
) -> dict[str, Any]:
    """Top-performing instruments by risk group, ranked by Sharpe and returns."""
    items = peers.leaderboard(
        conn,
        risk_level=risk_level,
        risk_bucket=risk_bucket,
        top=top,
        include_owned=include_owned,
    )
    return {
        "risk_level": risk_level,
        "risk_bucket": risk_bucket,
        "count": len(items),
        "candidates": [_candidate_json(c) for c in items],
        "note": (
            "Ranked by risk-adjusted return (Sharpe ratio and 3Y/1Y returns). "
            "Tickers with at least 1 year of history are fully included."
        ),
    }


@router.get("/api/peers/{import_key:path}")
def peer_scan(
    import_key: str,
    conn: sqlite3.Connection = Depends(get_writable_db),
    top: int = Query(5, ge=1, le=25),
    include_owned: bool = Query(False, alias="owned"),
    mode: str | None = Query(None, pattern="^(value|momentum)$"),
    narrate: bool = Query(False),
) -> dict[str, Any]:
    """Instruments comparable to this one, ranked by how they actually did.

    ``narrate`` is opt-in and off by default: the comparison is complete
    without it, and each narration costs a call against a free tier.
    """
    try:
        found = peers.scan(
            conn, instrument=import_key, top=top, include_owned=include_owned, mode=mode
        )
    except peers.PeerError as exc:
        raise HTTPException(404, str(exc)) from exc

    body = _scan_json(found)
    if not narrate:
        return {**body, "narrative": None, "narrative_status": "NOT_REQUESTED"}

    with transaction(conn):
        result = tasks.narrate(
            conn, task="peer_scan", payload=body, subject=found.subject.import_key, max_sentences=6
        )
    return {**body, **result.as_dict()}


@router.get("/api/peers")
def portfolio_peers(
    conn: sqlite3.Connection = Depends(get_writable_db),
    person_id: int = Query(1, alias="person"),
    top: int = Query(3, ge=1, le=10),
    mode: str | None = Query(None, pattern="^(value|momentum)$"),
    narrate: bool = Query(False),
) -> dict[str, Any]:
    """One comparison per holding that has something to be compared with.

    ``narrate`` writes the whole portfolio up in one paragraph rather than one
    per holding: the question is "is there something better than what I have",
    and answering it eleven times separately is eleven calls against a free
    tier to say what one paragraph says.
    """
    found = peers.scan_portfolio(conn, person_id=person_id, top=top, mode=mode)
    body = {
        "scans": [_scan_json(s) for s in found.scans],
        "needs_category": found.needs_category,
        "note": (
            "Ranked by valuation and forward P/E discount for stocks; "
            "ranked by return differences for funds."
            if found.scans
            else "Nothing to compare yet. Add instruments to the watchlist, and give "
            "your holdings a risk category so like is compared with like."
        ),
    }
    if not narrate:
        return {**body, "narrative": None, "narrative_status": "NOT_REQUESTED"}
    if not found.scans:
        return {**body, "narrative": None, "narrative_status": "NOTHING_TO_SAY"}

    with transaction(conn):
        result = tasks.narrate(
            conn, task="peer_scan", payload=body, subject="portfolio", max_sentences=8
        )
    return {**body, **result.as_dict()}


@router.get("/api/stocks/fundamentals")
def stock_fundamentals_list(
    conn: sqlite3.Connection = Depends(get_db),
    sector: str | None = Query(None),
    min_score: int | None = Query(None, ge=0, le=100),
    max_pe: float | None = Query(None),
    min_fcf_yield: float | None = Query(None),
) -> dict[str, Any]:
    """List fundamental valuation indicators and scores for single stocks."""
    from portfolio_tracker.core import stock_indicators

    items = stock_indicators.get_stock_fundamentals(
        conn,
        sector=sector,
        min_score=min_score,
        max_forward_pe=Decimal(str(max_pe)) if max_pe is not None else None,
        min_fcf_yield=Decimal(str(min_fcf_yield)) if min_fcf_yield is not None else None,
    )
    sectors = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT sector FROM stock_fundamental WHERE sector IS NOT NULL ORDER BY sector"
        ).fetchall()
    ]
    last_synced_row = conn.execute("SELECT max(fetched_at) FROM stock_fundamental").fetchone()
    last_synced = str(last_synced_row[0]) if last_synced_row and last_synced_row[0] else None
    return {
        "count": len(items),
        "sectors": sectors,
        "last_synced": last_synced,
        "stocks": [
            {
                "instrument_id": item.instrument_id,
                "symbol": item.symbol,
                "name": item.name,
                "sector": item.sector,
                "industry": item.industry,
                "current_price": _num(item.current_price),
                "market_cap": _num(item.market_cap),
                "currency": item.currency,
                "trailing_pe": _num(item.trailing_pe),
                "forward_pe": _num(item.forward_pe),
                "peg_ratio": _num(item.peg_ratio),
                "price_to_book": _num(item.price_to_book),
                "price_to_sales": _num(item.price_to_sales),
                "ev_ebitda": _num(item.ev_ebitda),
                "fcf_yield": _num(item.fcf_yield),
                "dividend_yield": _num(item.dividend_yield),
                "payout_ratio": _num(item.payout_ratio),
                "debt_to_equity": _num(item.debt_to_equity),
                "operating_margin": _num(item.operating_margin),
                "profit_margin": _num(item.profit_margin),
                "return_on_equity": _num(item.return_on_equity),
                "fifty_two_week_high": _num(item.fifty_two_week_high),
                "fifty_two_week_low": _num(item.fifty_two_week_low),
                "pct_off_52w_high": _num(item.pct_off_52w_high),
                "range_52w_pct": _num(item.range_52w_pct),
                "two_hundred_dma": _num(item.two_hundred_dma),
                "dist_200_dma_pct": _num(item.dist_200_dma_pct),
                "target_mean_price": _num(item.target_mean_price),
                "target_median_price": _num(item.target_median_price),
                "target_low_price": _num(item.target_low_price),
                "target_high_price": _num(item.target_high_price),
                "target_upside_pct": _num(item.target_upside_pct),
                "recommendation_key": item.recommendation_key,
                "recommendation_mean": _num(item.recommendation_mean),
                "analyst_count": item.analyst_count,
                "strong_buy": item.strong_buy,
                "buy": item.buy,
                "hold": item.hold,
                "sell": item.sell,
                "strong_sell": item.strong_sell,
                "buy_pct": _num(item.buy_pct),
                "earnings_growth": _num(item.earnings_growth),
                "revenue_growth": _num(item.revenue_growth),
                "financial_currency": item.financial_currency,
                "data_points": item.data_points,
                "quality_score": item.quality_score,
                "quality_narrative": item.quality_narrative,
                "revenue_cagr_3y": _num(item.revenue_cagr_3y),
                "revenue_up_years": item.revenue_up_years,
                "income_up_years": item.income_up_years,
                "years_reported": item.years_reported,
                "vol_1y": _num(item.vol_1y),
                "max_dd_3y": _num(item.max_dd_3y),
                "value_score": item.value_score,
                "value_badge": item.value_badge,
                "value_narrative": item.value_narrative,
                "fetched_at": item.fetched_at,
            }
            for item in items
        ],
    }


_FUNDAMENTALS_SYNC_STATE: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "progress": 0,
    "total": 0,
    "current": None,
    "error": None,
    "last_result": None,
}
_SYNC_LOCK = threading.Lock()


@router.get("/api/stocks/fundamentals/status")
def stock_fundamentals_status(
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Check whether stock fundamentals sync is currently running and when it last ran."""
    last_synced_row = conn.execute("SELECT max(fetched_at) FROM stock_fundamental").fetchone()
    last_synced = str(last_synced_row[0]) if last_synced_row and last_synced_row[0] else None
    with _SYNC_LOCK:
        return {
            **_FUNDAMENTALS_SYNC_STATE,
            "last_synced": last_synced,
        }


@router.post("/api/stocks/fundamentals/sync")
def trigger_stock_fundamentals_sync(
    workers: int = Query(4, ge=1, le=10),
) -> dict[str, Any]:
    """Trigger an asynchronous background refresh of stock fundamentals."""
    with _SYNC_LOCK:
        if _FUNDAMENTALS_SYNC_STATE["running"]:
            return {
                "started": False,
                "message": "Stock fundamentals sync is already in progress.",
                **_FUNDAMENTALS_SYNC_STATE,
            }
        _FUNDAMENTALS_SYNC_STATE.update(
            {
                "running": True,
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "progress": 0,
                "total": 0,
                "current": None,
                "error": None,
                "last_result": None,
            }
        )

    db_path = deps.DB_PATH

    def run_worker() -> None:
        from contextlib import closing

        from portfolio_tracker.core.stock_indicators import sync_all_stock_fundamentals
        from portfolio_tracker.db.connection import connect

        try:
            with closing(connect(db_path, read_only=False)) as wconn:

                def on_progress(done: int, total: int, key: str) -> None:
                    with _SYNC_LOCK:
                        _FUNDAMENTALS_SYNC_STATE["progress"] = done
                        _FUNDAMENTALS_SYNC_STATE["total"] = total
                        _FUNDAMENTALS_SYNC_STATE["current"] = key

                success, total = sync_all_stock_fundamentals(
                    wconn, max_workers=workers, progress_cb=on_progress
                )
                skipped = total - success
                result = f"Synced {success}/{total} stocks"
                if skipped:
                    result += f"; {skipped} kept their previous data (Yahoo gave no answer)"
                with _SYNC_LOCK:
                    _FUNDAMENTALS_SYNC_STATE["running"] = False
                    _FUNDAMENTALS_SYNC_STATE["finished_at"] = datetime.now(UTC).isoformat()
                    _FUNDAMENTALS_SYNC_STATE["last_result"] = result
        except Exception as exc:
            with _SYNC_LOCK:
                _FUNDAMENTALS_SYNC_STATE["running"] = False
                _FUNDAMENTALS_SYNC_STATE["finished_at"] = datetime.now(UTC).isoformat()
                _FUNDAMENTALS_SYNC_STATE["error"] = str(exc)

    threading.Thread(target=run_worker, daemon=True, name="stock-fundamentals-sync").start()

    return {
        "started": True,
        "message": "Stock fundamentals sync started in background.",
    }


# ---------------------------------------------------------------------------
# The comparison universe
# ---------------------------------------------------------------------------


@router.get("/api/universe")
def universe_status(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Whether the comparison set is loaded, and whether it has prices yet.

    Two numbers, not one. Instruments land the moment the catalogue is
    applied; their price history arrives with the next fetch, and a peer scan
    is a blank panel until it does. Reporting only "loaded" would make that
    gap look like a broken feature rather than a wait.
    """
    body = universe.status(conn)
    body["buckets"] = list(peers.BUCKETS)
    body["what_this_is"] = (
        "A set of well-known shares and funds the tool prices so your own "
        "holdings have something to be compared against. None of it is owned, "
        "none of it is a recommendation, and none of it affects a single "
        "figure about your portfolio."
    )
    return body


@router.post("/api/universe")
def universe_load(
    refresh: bool = Query(False),
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Load the catalogue. Idempotent, and never touches what you own."""
    try:
        with transaction(conn):
            report = universe.load(conn, refresh=refresh)
    except universe.UniverseError as exc:
        raise HTTPException(500, str(exc)) from exc

    return {
        "added": report.added,
        "updated": report.updated,
        "already_yours": report.owned,
        "already_watched": report.watched,
        "summary": report.sentence,
        "next": (
            "Prices arrive with the next update. Until then a peer scan has nothing to measure."
        ),
        **universe.status(conn),
    }


@router.delete("/api/universe")
def universe_unload(conn: sqlite3.Connection = Depends(get_writable_db)) -> dict[str, Any]:
    """Remove the comparison set, keeping anything owned or watched."""
    with transaction(conn):
        removed = universe.unload(conn)
    return {"removed": removed, **universe.status(conn)}


# ---------------------------------------------------------------------------
# Proposing risk categories
# ---------------------------------------------------------------------------


@router.get("/api/instruments/categories/suggested")
def suggested_categories(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    unset_only: bool = Query(False),
) -> dict[str, Any]:
    """What each holding looks like, from its own name.

    Proposals, not decisions. `core/peers.py` compares only where the category
    was stated, and it is right to: the difference between a semiconductor
    fund and a world tracker is invisible in `asset_class`. But making the
    owner type one for every holding before any comparison worked at all was
    a wall in front of a finished feature.

    Each row carries the words it matched on, so accepting is a judgement
    about visible evidence rather than trust in a black box.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT i.import_key, i.name, i.asset_class, i.risk_bucket, i.risk_level
        FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE a.person_id = ? AND p.date = (
            SELECT max(date) FROM position_daily
        )
        ORDER BY i.import_key
        """,
        (person_id,),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        if unset_only and row["risk_bucket"]:
            continue
        found = classify.suggest(row["import_key"], row["name"], row["asset_class"])
        out.append(
            {
                "instrument": found.import_key,
                "name": row["name"],
                "current": row["risk_bucket"],
                "current_risk_level": row["risk_level"] if "risk_level" in row.keys() else None,  # noqa: SIM118
                "suggested": found.bucket,
                "suggested_risk_level": found.risk_level,
                "why": found.why,
                "matched": found.matched,
                "hedged": classify.is_hedged(row["name"]),
            }
        )
    unknown = [r["instrument"] for r in out if not r["suggested"]]
    return {
        "suggestions": out,
        "unknown": unknown,
        "note": (
            "Read the reason before accepting. These come from each fund's own "
            "name, which is usually a fair description of what it holds and "
            "occasionally is not."
        ),
    }


class CategoryDecisions(BaseModel):
    """Categories and risk levels the owner has agreed to, keyed by instrument."""

    categories: dict[str, str] = Field(default_factory=dict)
    risk_levels: dict[str, int] = Field(default_factory=dict)


@router.post("/api/instruments/categories")
def accept_categories(
    body: CategoryDecisions,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Save agreed categories. Once saved they are stated, not guessed."""
    unknown = sorted(set(body.categories.values()) - set(peers.BUCKETS))
    if unknown:
        raise HTTPException(422, f"not risk categories: {unknown}")

    saved: list[str] = []
    with transaction(conn):
        for key, bucket in body.categories.items():
            level = body.risk_levels.get(key)
            try:
                watchlist.set_details(conn, key, risk_bucket=bucket, risk_level=level)
            except watchlist.WatchlistError as exc:
                raise HTTPException(409, str(exc)) from exc
            saved.append(key)
    return {"saved": saved, "count": len(saved)}
