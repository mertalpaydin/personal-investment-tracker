"""The comparison set: instruments priced so your holdings have peers.

A peer scan with nothing to scan is a blank panel, and that is what the tool
shipped with -- `core/peers.py` was complete and had a candidate pool of zero.
This loads the catalogue that fills it.

Four decisions worth stating.

**Loaded on request, never on startup.** Two hundred instruments is two
hundred more symbols in every daily price fetch. That is a real cost in time
and in someone else's rate limit, and it is the owner's to accept.

**`UNIVERSE`, not `WATCH`.** Watching is a choice a person made about one
instrument; the universe is background furniture. They are separated so that
"what am I following" does not become a list of two hundred things nobody
chose, and so removing an instrument from a watchlist can return it to the
pool rather than delete five years of downloaded prices.

**An owned instrument is never touched.** The catalogue and the portfolio
overlap by design -- you may well hold something in it. Adopting a row that
has lots against it, or rewriting its metadata from a generic catalogue entry,
would corrupt real holdings to populate a comparison.

**Nothing here is a recommendation.** It is chosen for size and liquidity, so
that a scan has somewhere to look. `core/peers.py` decides what is comparable;
this module only supplies the candidates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from portfolio_tracker.core.peers import DEFAULT_RISK_LEVEL

#: Ships inside the package: it is public market data with nothing personal in
#: it, which is why it does not live in the gitignored seeds/ directory.
CATALOGUE = Path(__file__).resolve().parent.parent / "data" / "universe.yaml"

#: The status a catalogue instrument carries. Priced, never valued: it has no
#: lots, so it cannot reach `core/valuation.py` or move a single figure about
#: the portfolio.
UNIVERSE = "UNIVERSE"


class UniverseError(RuntimeError):
    """The catalogue cannot be read."""


@dataclass
class LoadReport:
    """What loading the catalogue did, and what it deliberately left alone."""

    added: int = 0
    updated: int = 0
    #: Rows that are yours. Skipped whole -- see the module docstring.
    owned: list[str] = field(default_factory=list)
    #: Rows you are already watching by choice. Left as WATCH.
    watched: list[str] = field(default_factory=list)
    total: int = 0

    @property
    def sentence(self) -> str:
        bits = [f"{self.added} added"]
        if self.updated:
            bits.append(f"{self.updated} refreshed")
        if self.owned:
            bits.append(f"{len(self.owned)} already yours, left alone")
        if self.watched:
            bits.append(f"{len(self.watched)} already watched, left alone")
        return ", ".join(bits) + "."


@lru_cache(maxsize=1)
def catalogue() -> dict[str, dict[str, Any]]:
    """The catalogue, parsed once.

    Cached because it is a static file read on every status check, and reading
    two hundred YAML entries per request to answer "is it loaded" would be
    silly.
    """
    if not CATALOGUE.is_file():
        raise UniverseError(f"no catalogue at {CATALOGUE}")
    raw = yaml.safe_load(CATALOGUE.read_text(encoding="utf-8")) or {}
    entries = raw.get("instruments") or {}
    if not isinstance(entries, dict) or not entries:
        raise UniverseError("the catalogue lists no instruments")
    return {str(k): dict(v or {}) for k, v in entries.items()}


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """How much of the catalogue is present, and how much of it has prices.

    Coverage is reported separately from presence because the two diverge for
    days: instruments land immediately, their history arrives with the next
    price fetch, and a peer scan is useless until it does.
    """
    entries = catalogue()
    keys = list(entries)
    marks = ",".join("?" * len(keys))
    present = conn.execute(
        f"SELECT count(*) AS n FROM instrument WHERE import_key IN ({marks})",
        keys,
    ).fetchone()["n"]
    priced = conn.execute(
        f"""
        SELECT count(*) AS n FROM instrument i
        WHERE i.import_key IN ({marks})
          AND EXISTS (SELECT 1 FROM price p WHERE p.instrument_id = i.id)
        """,
        keys,
    ).fetchone()["n"]
    return {
        "catalogue_size": len(keys),
        "present": present,
        "priced": priced,
        "loaded": present >= len(keys),
    }


def load(conn: sqlite3.Connection, *, refresh: bool = False) -> LoadReport:
    """Insert the catalogue as UNIVERSE instruments. Idempotent.

    ``refresh`` rewrites the metadata of rows that are already in the universe
    -- for a corrected fee, say. It never touches an owned or watched row
    whatever it is set to.
    """
    entries = catalogue()
    report = LoadReport(total=len(entries))

    existing = {
        str(r["import_key"] if isinstance(r, (sqlite3.Row, dict)) else r[1]): r
        for r in conn.execute("SELECT id, import_key, status FROM instrument")
    }

    for key, spec in entries.items():
        row = existing.get(key)
        bucket = spec.get("risk_bucket")
        level = spec.get("risk_level") or (DEFAULT_RISK_LEVEL.get(bucket, 4) if bucket else None)

        if row is not None:
            r_status = row["status"] if isinstance(row, (sqlite3.Row, dict)) else row[2]
            r_id = row["id"] if isinstance(row, (sqlite3.Row, dict)) else row[0]
            if r_status == "OWNED":
                report.owned.append(key)
                continue
            if r_status == "WATCH":
                report.watched.append(key)
                continue
            if not refresh:
                continue
            conn.execute(
                """
                UPDATE instrument SET name = ?, asset_class = ?, currency = ?,
                       price_symbol = ?, trading_calendar = ?, is_fund = ?,
                       ter_bps = ?, risk_bucket = ?, risk_level = ?, price_source = 'yfinance'
                WHERE id = ?
                """,
                (
                    spec.get("name", key),
                    spec.get("asset_class", "ETF"),
                    spec.get("currency", "EUR"),
                    spec.get("price_symbol", key),
                    spec.get("trading_calendar", "XETRA"),
                    1 if spec.get("is_fund") else 0,
                    spec.get("ter_bps"),
                    bucket,
                    level,
                    r_id,
                ),
            )
            report.updated += 1
            continue

        conn.execute(
            """
            INSERT INTO instrument
                (import_key, name, asset_class, currency, price_symbol,
                 price_source, trading_calendar, is_fund, ter_bps, risk_bucket,
                 risk_level, status)
            VALUES (?, ?, ?, ?, ?, 'yfinance', ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                spec.get("name", key),
                spec.get("asset_class", "ETF"),
                spec.get("currency", "EUR"),
                spec.get("price_symbol", key),
                spec.get("trading_calendar", "XETRA"),
                1 if spec.get("is_fund") else 0,
                spec.get("ter_bps"),
                bucket,
                level,
                UNIVERSE,
            ),
        )
        report.added += 1

    return report


def unload(conn: sqlite3.Connection) -> int:
    """Remove catalogue instruments that are neither owned nor watched.

    Deletes the rows outright, prices included. Safe only because a universe
    instrument has no lots and no transactions by construction -- anything you
    bought became OWNED and is skipped here.
    """
    keys = list(catalogue())
    marks = ",".join("?" * len(keys))
    conn.execute(
        f"""
        DELETE FROM price WHERE instrument_id IN (
            SELECT id FROM instrument WHERE status = ? AND import_key IN ({marks})
        )
        """,
        [UNIVERSE, *keys],
    )
    cursor = conn.execute(
        f"DELETE FROM instrument WHERE status = ? AND import_key IN ({marks})",
        [UNIVERSE, *keys],
    )
    return cursor.rowcount or 0
