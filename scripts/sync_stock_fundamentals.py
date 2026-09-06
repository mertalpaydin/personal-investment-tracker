"""Fetch fundamental valuation indicators for all single stocks in the universe.

Extracts valuation multiples (P/E, P/B, EV/EBITDA, FCF yield), 52-week discount
metrics, financial health ratios, and analyst consensus from Yahoo Finance,
computes composite Value Scores, and saves them to `stock_fundamental`.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Ensure src is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

import yfinance as yf  # noqa: E402

from portfolio_tracker.core.stock_indicators import (  # noqa: E402
    StockFundamentals,
    parse_raw_stock_info,
    save_stock_fundamentals,
)

DB_PATH = BASE_DIR / "data" / "portfolio.db"


def sync_stocks():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, import_key, price_symbol, name, status, sector
        FROM instrument
        WHERE asset_class = 'STOCK'
        ORDER BY id
    """).fetchall()

    print(f"Found {len(rows)} single stocks in database.")
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    success_count = 0
    batch: list[StockFundamentals] = []

    for i, r in enumerate(rows, 1):
        inst_id = r["id"]
        key = r["import_key"]
        sym = r["price_symbol"] or key
        name = r["name"]

        print(f"[{i}/{len(rows)}] Fetching {key} ({sym})... ", end="", flush=True)

        try:
            ticker = yf.Ticker(sym)
            fast_info = ticker.fast_info
            info = ticker.info or {}

            fundamentals = parse_raw_stock_info(
                instrument_id=inst_id,
                symbol=key,
                name=name,
                info=info,
                fast_info=fast_info,
                now_iso=now_iso,
            )
            batch.append(fundamentals)
            success_count += 1
            print(
                f"Done: {fundamentals.sector or 'No Sector'} | "
                f"Fwd P/E: {fundamentals.forward_pe or '-'} | "
                f"Score: {fundamentals.value_score} ({fundamentals.value_badge})"
            )
        except Exception as e:
            print(f"Failed ({e})")

        # Save in batches of 15
        if len(batch) >= 15:
            save_stock_fundamentals(conn, batch)
            batch = []

        # Polite rate limiting
        time.sleep(0.3)

    if batch:
        save_stock_fundamentals(conn, batch)

    print(f"\nSuccessfully synced fundamentals for {success_count}/{len(rows)} stocks.")
    conn.close()


if __name__ == "__main__":
    sync_stocks()
