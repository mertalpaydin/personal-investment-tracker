"""Fetch fundamental valuation indicators for all single stocks in the universe.

Extracts valuation multiples (P/E, P/B, EV/EBITDA, FCF yield), 52-week discount
metrics, financial health ratios, and analyst consensus from Yahoo Finance,
computes composite Value Scores, and saves them to `stock_fundamental`.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

# Ensure src is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from portfolio_tracker.core.stock_indicators import (  # noqa: E402
    sync_all_stock_fundamentals,
)

DB_PATH = BASE_DIR / "data" / "portfolio.db"


def sync_stocks():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    def progress(done: int, total: int, key: str) -> None:
        print(f"[{done}/{total}] Last fetched: {key}")

    print("Fetching stock fundamentals from Yahoo Finance (6 concurrent workers)...")
    t0 = time.time()
    success, total = sync_all_stock_fundamentals(conn, max_workers=6, progress_cb=progress)
    elapsed = time.time() - t0

    print(f"\nSuccessfully synced fundamentals for {success}/{total} stocks in {elapsed:.1f}s.")
    conn.close()


if __name__ == "__main__":
    sync_stocks()
