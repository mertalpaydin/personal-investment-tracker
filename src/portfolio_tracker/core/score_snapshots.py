"""Dated snapshots of the stock scores, so the formulas can be judged later.

The Value and Quality scores are opinions written by a formula. Whether the
opinion is any good can only be seen in hindsight: does a high score today go
with a better return over the next months? To ask that question later, the
score has to be written down *today*, with its parts, and never touched again.

Each row in `stock_score_snapshot` is one stock on one calculation day: both
scores, the points of every block, the raw inputs as JSON, the close price
that was on file that day, and the formula version. The price is the last
stored close on or before the calculation day, never Yahoo's live quote, so a
later forward-return calculation starts from a number that really existed.

`core/score_backtest.py` reads these rows back.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from portfolio_tracker.core.stock_indicators import (
    QUALITY_COMPONENTS,
    SCORE_FORMULA_VERSION,
    VALUE_COMPONENTS,
    StockFundamentals,
)

log = logging.getLogger(__name__)

SOURCES = ("SYNC", "RESCORE", "DAILY")

#: Scoring inputs written into `inputs_json`, so a row can be replayed or
#: audited even after the formula moved on.
_INPUT_FIELDS = (
    "current_price",
    "market_cap",
    "currency",
    "trailing_pe",
    "forward_pe",
    "peg_ratio",
    "price_to_book",
    "ev_ebitda",
    "fcf_yield",
    "dividend_yield",
    "payout_ratio",
    "debt_to_equity",
    "operating_margin",
    "profit_margin",
    "return_on_equity",
    "earnings_growth",
    "revenue_growth",
    "pct_off_52w_high",
    "range_52w_pct",
    "target_upside_pct",
    "analyst_count",
    "buy_pct",
    "recommendation_mean",
    "revenue_cagr_3y",
    "revenue_up_years",
    "income_up_years",
    "years_reported",
    "vol_1y",
    "max_dd_3y",
)

_VALUE_COLUMNS = tuple(f"v_{name}" for name in VALUE_COMPONENTS)
_QUALITY_COLUMNS = tuple(f"q_{name}" for name in QUALITY_COMPONENTS)

_COLUMNS = (
    "instrument_id",
    "calc_date",
    "formula_version",
    "source",
    "fundamentals_fetched_at",
    "sector",
    "data_points",
    "price_date",
    "price_close",
    "price_tr_close",
    "price_currency",
    "value_score",
    "value_badge",
    "value_raw_total",
    *_VALUE_COLUMNS,
    "v_quality_capped",
    "v_trap_capped",
    "quality_score",
    *_QUALITY_COLUMNS,
    "inputs_json",
    "created_at",
)


@dataclass(frozen=True)
class PriceOnFile:
    date: date
    close: Decimal
    tr_close: Decimal | None
    currency: str


def price_at(conn: sqlite3.Connection, instrument_id: int, on: date) -> PriceOnFile | None:
    """The last stored close on or before ``on``; None when there is none."""
    row = conn.execute(
        """
        SELECT date, close, total_return_close, currency
        FROM price
        WHERE instrument_id = ? AND date <= ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (instrument_id, on.isoformat()),
    ).fetchone()
    if row is None:
        return None
    tr = row["total_return_close"]
    return PriceOnFile(
        date=date.fromisoformat(str(row["date"])),
        close=Decimal(str(row["close"])),
        tr_close=Decimal(str(tr)) if tr is not None else None,
        currency=str(row["currency"]),
    )


def _inputs_json(item: StockFundamentals) -> str:
    payload: dict[str, Any] = {name: getattr(item, name) for name in _INPUT_FIELDS}
    if item.value_breakdown is not None:
        payload["pe_used"] = item.value_breakdown.pe_used
    payload["relative"] = item.relative_ranks or {}
    return json.dumps(payload, sort_keys=True, default=str)


def _row(
    item: StockFundamentals, *, calc_date: date, source: str, on_file: PriceOnFile | None, now: str
) -> tuple[Any, ...]:
    vb = item.value_breakdown
    qb = item.quality_breakdown
    v_parts = vb.components if vb is not None else {}
    q_parts = qb.components if qb is not None else {}
    return (
        item.instrument_id,
        calc_date.isoformat(),
        SCORE_FORMULA_VERSION,
        source,
        item.fetched_at or None,
        item.sector,
        item.data_points,
        on_file.date.isoformat() if on_file else None,
        str(on_file.close) if on_file else None,
        str(on_file.tr_close) if on_file and on_file.tr_close is not None else None,
        on_file.currency if on_file else None,
        item.value_score,
        item.value_badge,
        vb.raw_total if vb is not None and vb.score is not None else None,
        *(v_parts.get(name) for name in VALUE_COMPONENTS),
        int(bool(vb and vb.quality_capped)),
        int(bool(vb and vb.trap_capped)),
        item.quality_score,
        *(q_parts.get(name) for name in QUALITY_COMPONENTS),
        _inputs_json(item),
        now,
    )


def record_snapshots(
    conn: sqlite3.Connection,
    items: list[StockFundamentals],
    *,
    calc_date: date | None = None,
    source: str = "RESCORE",
) -> int:
    """Write one snapshot row per scored item for ``calc_date`` (default today).

    A second call for the same day overwrites the first, so a sync followed by
    the daily job leaves one row, stamped with whichever ran last. Items that
    were never scored (no breakdown attached) are skipped. Returns rows written.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, not {source!r}")
    when = calc_date or date.today()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        _row(
            item,
            calc_date=when,
            source=source,
            on_file=price_at(conn, item.instrument_id, when),
            now=now,
        )
        for item in items
        if item.value_breakdown is not None
    ]
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _COLUMNS)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in _COLUMNS if c not in ("instrument_id", "calc_date")
    )
    conn.executemany(
        f"INSERT INTO stock_score_snapshot ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(instrument_id, calc_date) DO UPDATE SET {updates}",
        rows,
    )
    conn.commit()
    log.info("score snapshots: %d rows for %s (%s)", len(rows), when.isoformat(), source)
    return len(rows)


def load_snapshots(
    conn: sqlite3.Connection, *, formula_version: str | None = None
) -> list[sqlite3.Row]:
    """Every snapshot row joined with the instrument's key, oldest first."""
    where = "WHERE s.formula_version = ?" if formula_version else ""
    params: tuple[Any, ...] = (formula_version,) if formula_version else ()
    return conn.execute(
        f"""
        SELECT s.*, i.import_key AS symbol, i.currency AS instrument_currency
        FROM stock_score_snapshot s
        JOIN instrument i ON i.id = s.instrument_id
        {where}
        ORDER BY s.calc_date, s.instrument_id
        """,
        params,
    ).fetchall()


def snapshot_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Row count, distinct stocks and date range, for status lines."""
    row = conn.execute(
        """
        SELECT count(*) AS n, count(DISTINCT instrument_id) AS stocks,
               min(calc_date) AS first_date, max(calc_date) AS last_date,
               count(DISTINCT calc_date) AS days
        FROM stock_score_snapshot
        """
    ).fetchone()
    return {
        "snapshots": int(row["n"]),
        "instruments": int(row["stocks"]),
        "days": int(row["days"]),
        "first_date": row["first_date"],
        "last_date": row["last_date"],
    }
