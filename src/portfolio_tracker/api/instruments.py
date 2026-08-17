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
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from portfolio_tracker.api import deps
from portfolio_tracker.api.deps import get_db, get_writable_db
from portfolio_tracker.core import peers, performance, watchlist
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


@router.get("/api/watchlist/lookup")
def lookup(symbol: str = Query(..., min_length=1)) -> dict[str, Any]:
    """What a ticker appears to be. Writes nothing.

    Answers 200 with ``found: false`` rather than an error status when the
    symbol is unknown: "I could not find that" is a normal outcome of typing
    a ticker, not a fault, and the form needs to say so beside the field.
    """
    try:
        found = YFinanceProvider().describe(symbol)
    except ProviderUnavailable as exc:
        return {"found": False, "symbol": symbol, "problem": str(exc)}

    return {
        "found": True,
        "symbol": found.symbol,
        "name": found.name,
        "currency": found.currency,
        "exchange": found.exchange,
        "quote_type": found.quote_type,
        "last_close": _num(found.last_close),
        "last_date": found.last_date.isoformat() if found.last_date else None,
        "observations": found.observations,
        # The bare key is what the importer matches against, so proposing it
        # here is what makes a watched ticker become the owned one if bought.
        "suggested_key": found.symbol.split(".")[0].upper(),
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
    notes: str | None = None


class DetailsRequest(BaseModel):
    """The two facts no free source supplies."""

    ter_bps: int | None = Field(default=None, ge=0, le=1000)
    risk_bucket: str | None = None


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
        "caveats": m.caveats,
    }


@router.get("/api/watchlist")
def index(
    conn: sqlite3.Connection = Depends(get_db),
    include_universe: bool = Query(False, alias="all"),
) -> dict[str, Any]:
    """Watched instruments, each with how it has actually done."""
    entries = watchlist.listing(conn, include_universe=include_universe)
    out: list[dict[str, Any]] = []
    for entry in entries:
        row = conn.execute(
            "SELECT id, currency FROM instrument WHERE import_key = ?", (entry.import_key,)
        ).fetchone()
        body = _entry_json(entry)
        if row is not None and entry.prices:
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
    """Record the ongoing charge and the risk category.

    Neither can be fetched. No free source supplies a UCITS fund's ongoing
    charge -- it is on the key information document -- and a risk category is
    a judgement about what the fund *is*, which no price series contains.
    """
    try:
        with transaction(conn):
            watchlist.set_details(
                conn, import_key, ter_bps=body.ter_bps, risk_bucket=body.risk_bucket
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
) -> dict[str, Any]:
    """One instrument's level series, for charting against another."""
    row = conn.execute(
        "SELECT id, import_key, name, currency FROM instrument "
        "WHERE import_key = ? OR isin = ? OR symbol = ?",
        (import_key, import_key, import_key),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"no instrument called {import_key!r}")

    points, basis, caveats = performance.series(
        conn, int(row["id"]), currency=row["currency"], start=since, end=until
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
        "vol_band": c.vol_band,
        "basis": c.basis,
        "comparable": c.comparable,
        "observations": c.observations,
        "ret_1y": _num(c.ret_1y),
        "ret_3y_ann": _num(c.ret_3y_ann),
        "vol_1y": _num(c.vol_1y),
        "vol_3y": _num(c.vol_3y),
        "max_dd_3y": _num(c.max_dd_3y),
        "ret_1y_delta": _num(c.ret_1y_delta),
        "ret_3y_ann_delta": _num(c.ret_3y_ann_delta),
        "vol_3y_delta": _num(c.vol_3y_delta),
        "ter_bps": c.ter_bps,
        "ter_bps_delta": c.ter_bps_delta,
        "overlap_pct": _num(c.overlap_pct),
        "caveats": c.caveats,
    }


def _scan_json(scan: peers.PeerScan) -> dict[str, Any]:
    return {
        "as_of": scan.as_of.isoformat(),
        "subject": _candidate_json(scan.subject),
        "bucket": scan.bucket,
        "bucket_method": scan.bucket_method,
        "vol_band": scan.vol_band,
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


@router.get("/api/peers/{import_key:path}")
def peer_scan(
    import_key: str,
    conn: sqlite3.Connection = Depends(get_writable_db),
    top: int = Query(5, ge=1, le=25),
    include_owned: bool = Query(False, alias="owned"),
    narrate: bool = Query(False),
) -> dict[str, Any]:
    """Instruments comparable to this one, ranked by how they actually did.

    ``narrate`` is opt-in and off by default: the comparison is complete
    without it, and each narration costs a call against a free tier.
    """
    try:
        found = peers.scan(conn, instrument=import_key, top=top, include_owned=include_owned)
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
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    top: int = Query(3, ge=1, le=10),
) -> dict[str, Any]:
    """One comparison per holding that has something to be compared with."""
    found = peers.scan_portfolio(conn, person_id=person_id, top=top)
    return {
        "scans": [_scan_json(s) for s in found.scans],
        # Named rather than silently skipped: "nothing to compare" and "I do
        # not know what these are" are different answers, and only one of them
        # is something the reader can fix.
        "needs_category": found.needs_category,
        "note": (
            "Ranked by three-year return where three years exist, otherwise by one year."
            if found.scans
            else "Nothing to compare yet. Add instruments to the watchlist, and give "
            "your holdings a risk category so like is compared with like."
        ),
    }
