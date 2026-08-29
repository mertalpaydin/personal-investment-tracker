"""Instruments you do not own but want measured.

Until now the `instrument` table could only be populated by the importer, from
a positions export. That made the tool structurally incapable of answering
"how does this compare to something I might buy instead" -- there was nowhere
to put the something.

Two decisions govern this module.

**The key is the bare symbol, not the Yahoo ticker.** A watchlist entry is
recorded as ``VWCE``, priced via ``price_symbol = VWCE.DE``. That is because
``importers/investing_com/importer.py`` looks instruments up by ``import_key``
against the bare symbol in the export -- so if the thing is later actually
bought, the importer adopts *this* row and keeps the price history already
downloaded for it. A ``.DE``-suffixed key would silently produce a second
instrument with a disjoint series, and both would look right.

**Removing does not delete.** It sets the status back to ``UNIVERSE``, keeping
the price history. Deleting would throw away five years of downloaded prices
to save a table row, and re-adding would cost another backfill.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import cast

from portfolio_tracker.core.peers import DEFAULT_RISK_LEVEL

#: Statuses this module will move an instrument between. `OWNED` is not among
#: them: ownership is established by a transaction existing, never by a click.
WATCHED = "WATCH"
RETIRED_TO = "UNIVERSE"

#: Instruments the price layer should keep current even with no position.
#: `UNIVERSE` is included so a seeded comparison pool is priced too -- that is
#: what makes a peer scan possible at all.
PRICED_STATUSES = (WATCHED, "UNIVERSE")


class WatchlistError(ValueError):
    """The instrument cannot be watched as asked."""


@dataclass
class WatchEntry:
    import_key: str
    name: str
    symbol: str | None
    price_symbol: str | None
    currency: str
    asset_class: str
    status: str
    price_source: str
    ter_bps: int | None = None
    risk_bucket: str | None = None
    risk_level: int | None = None
    prices: int = 0
    first_price: date | None = None
    last_price: date | None = None
    caveats: list[str] = field(default_factory=list)


def _existing(
    conn: sqlite3.Connection, import_key: str, price_symbol: str | None = None
) -> sqlite3.Row | None:
    if price_symbol:
        row = conn.execute(
            "SELECT * FROM instrument WHERE import_key = ? "
            "OR (price_symbol IS NOT NULL AND price_symbol = ?)",
            (import_key, price_symbol),
        ).fetchone()
        if row is not None:
            return cast("sqlite3.Row", row)
    return cast(
        "sqlite3.Row | None",
        conn.execute("SELECT * FROM instrument WHERE import_key = ?", (import_key,)).fetchone(),
    )


def add(
    conn: sqlite3.Connection,
    *,
    import_key: str,
    name: str,
    currency: str,
    asset_class: str = "ETF",
    price_symbol: str | None = None,
    price_source: str = "yfinance",
    isin: str | None = None,
    symbol: str | None = None,
    trading_calendar: str = "XETRA",
    ter_bps: int | None = None,
    risk_bucket: str | None = None,
    risk_level: int | None = None,
    is_fund: bool | None = None,
    notes: str | None = None,
) -> int:
    """Start watching an instrument. Returns its id.

    Refuses an instrument that is already owned rather than silently doing
    nothing: "you already hold this" is the answer to the question that was
    actually asked.
    """
    key = import_key.strip()
    if not key:
        raise WatchlistError("an instrument needs a key")

    fund = is_fund if is_fund is not None else asset_class in ("ETF", "FUND")
    clean_bucket = risk_bucket.strip().upper() if risk_bucket else None
    level = (
        risk_level
        if risk_level is not None
        else (DEFAULT_RISK_LEVEL.get(clean_bucket) if clean_bucket else None)
    )

    row = _existing(conn, key, price_symbol)
    if row is not None:
        if row["status"] == "OWNED":
            raise WatchlistError(
                f"You already hold {key}; it is on the Holdings page rather than the watchlist."
            )
        updates = ["status = ?"]
        params: list[object] = [WATCHED]
        if clean_bucket and not row["risk_bucket"]:
            updates.append("risk_bucket = ?")
            params.append(clean_bucket)
        if level and not row["risk_level"]:
            updates.append("risk_level = ?")
            params.append(level)
        if ter_bps is not None and row["ter_bps"] is None:
            updates.append("ter_bps = ?")
            params.append(ter_bps)
        params.append(row["id"])
        conn.execute(f"UPDATE instrument SET {', '.join(updates)} WHERE id = ?", params)
        return int(row["id"])

    cursor = conn.execute(
        """
        INSERT INTO instrument (
            import_key, isin, symbol, name, asset_class, currency, price_source,
            price_symbol, trading_calendar, ter_bps, risk_bucket, risk_level, is_fund, status, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            isin,
            symbol or price_symbol,
            name,
            asset_class,
            currency,
            price_source,
            price_symbol,
            trading_calendar,
            ter_bps,
            clean_bucket,
            level,
            1 if fund else 0,
            WATCHED,
            notes,
        ),
    )
    return int(cast("int", cursor.lastrowid))


def remove(conn: sqlite3.Connection, import_key: str) -> None:
    """Stop watching. Keeps the instrument and every price downloaded for it."""
    row = _existing(conn, import_key)
    if row is None:
        raise WatchlistError(f"{import_key} is not on the watchlist")
    if row["status"] == "OWNED":
        raise WatchlistError(
            f"You hold {import_key}; it cannot be removed while a position exists."
        )
    conn.execute("UPDATE instrument SET status = ? WHERE id = ?", (RETIRED_TO, row["id"]))


def set_details(
    conn: sqlite3.Connection,
    import_key: str,
    *,
    ter_bps: int | None = None,
    risk_bucket: str | None = None,
    risk_level: int | None = None,
) -> None:
    """Record ongoing charge, risk category, and numerical risk level.

    An ongoing charge is on the fund's key information document and a risk
    category is a judgement; neither can be fetched, and both make a
    comparison sharper. Passing None leaves a field alone rather than clearing
    it, so a partial edit cannot erase the other.
    """
    row = _existing(conn, import_key)
    if row is None:
        raise WatchlistError(f"no instrument called {import_key}")
    updates: dict[str, object] = {}
    if ter_bps is not None:
        if ter_bps < 0 or ter_bps > 1000:
            raise WatchlistError("An ongoing charge is in basis points: 20 means 0.20% a year.")
        updates["ter_bps"] = ter_bps
    if risk_bucket is not None:
        clean_bucket = risk_bucket.strip().upper() or None
        updates["risk_bucket"] = clean_bucket
        if risk_level is None and clean_bucket is not None:
            updates["risk_level"] = DEFAULT_RISK_LEVEL.get(clean_bucket, 4)
    if risk_level is not None:
        if risk_level < 1 or risk_level > 7:
            raise WatchlistError("Risk level must be between 1 (ultra-low) and 7 (speculative).")
        updates["risk_level"] = risk_level
    if not updates:
        return
    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE instrument SET {assignments} WHERE id = ?", (*updates.values(), row["id"])
    )


def listing(conn: sqlite3.Connection, *, include_universe: bool = False) -> list[WatchEntry]:
    """Everything being watched, with how much history each one has."""
    statuses = PRICED_STATUSES if include_universe else (WATCHED,)
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""
        SELECT i.*,
               (SELECT count(*) FROM price p WHERE p.instrument_id = i.id) AS prices,
               (SELECT min(date) FROM price p WHERE p.instrument_id = i.id) AS first_price,
               (SELECT max(date) FROM price p WHERE p.instrument_id = i.id) AS last_price
        FROM instrument i WHERE i.status IN ({placeholders})
        ORDER BY i.import_key
        """,
        statuses,
    ).fetchall()

    out: list[WatchEntry] = []
    for row in rows:
        entry = WatchEntry(
            import_key=row["import_key"],
            name=row["name"],
            symbol=row["symbol"],
            price_symbol=row["price_symbol"],
            currency=row["currency"],
            asset_class=row["asset_class"],
            status=row["status"],
            price_source=row["price_source"],
            ter_bps=row["ter_bps"],
            risk_bucket=row["risk_bucket"],
            risk_level=row["risk_level"] if "risk_level" in row.keys() else None,  # noqa: SIM118
            prices=int(row["prices"] or 0),
            first_price=date.fromisoformat(row["first_price"]) if row["first_price"] else None,
            last_price=date.fromisoformat(row["last_price"]) if row["last_price"] else None,
        )
        if entry.prices == 0:
            entry.caveats.append(
                "No prices yet. They arrive with the next update, which also "
                "fetches this instrument's history."
            )
        if entry.ter_bps is None:
            entry.caveats.append("No ongoing charge recorded, so fee comparisons leave it out.")
        out.append(entry)
    return out


def as_percent(ter_bps: int | None) -> Decimal | None:
    """Basis points as a fraction, for a caller that formats percentages."""
    return None if ter_bps is None else Decimal(ter_bps) / Decimal(10000)
