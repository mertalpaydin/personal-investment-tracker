"""Map all comparison ETFs with 12-character ISIN import_keys to their official Xetra (.DE) tickers.

Also:
1. Fixes the 19 legacy mismatched ETF names in instrument and universe.yaml.
2. Updates universe.yaml so all keys and price_symbols match.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "portfolio.db"
UNIVERSE_YAML = BASE_DIR / "src" / "portfolio_tracker" / "data" / "universe.yaml"

LEGACY_NAME_FIXES = {
    "EXV6.DE": ("iShares STOXX Europe 600 Basic Resources UCITS ETF (DE)", "EQ_SECTOR", 6),
    "EXH7.DE": (
        "iShares STOXX Europe 600 Personal & Household Goods UCITS ETF (DE)",
        "EQ_SECTOR",
        6,
    ),
    "EXSB.DE": ("iShares DivDAX UCITS ETF (DE)", "EQ_EUROPE", 4),
    "EXSI.DE": ("iShares EURO STOXX UCITS ETF (DE)", "EQ_EUROPE", 4),
    "EXX5.DE": ("iShares Dow Jones U.S. Select Dividend UCITS ETF (DE)", "EQ_US_LARGE", 4),
    "AYEM.DE": ("iShares MSCI EM IMI Screened UCITS ETF USD (Acc)", "EQ_EM", 5),
    "BNXG.DE": ("Invesco CoinShares Global Blockchain UCITS ETF", "EQ_SECTOR", 6),
    "DBXP.DE": ("Xtrackers II Eurozone Government Bond 1-3 UCITS ETF 1C", "BOND", 2),
    "2B7K.DE": ("iShares MSCI World SRI UCITS ETF EUR (Acc)", "EQ_GLOBAL_BROAD", 4),
    "IQQQ.DE": ("iShares Global Water UCITS ETF USD (Dist)", "EQ_SECTOR", 6),
    "QDVC.DE": ("iShares MSCI USA Mid-Cap Equal Weight UCITS ETF", "EQ_US_LARGE", 4),
    "QDVF.DE": ("iShares S&P 500 Energy Sector UCITS ETF USD (Acc)", "EQ_SECTOR", 6),
    "WNRG.DE": ("WisdomTree Energy Enhanced - EUR Daily Hedged ETC", "COMMODITY", 5),
    "EUN5.DE": ("iShares Core € Corp Bond UCITS ETF EUR (Dist)", "BOND", 3),
    "EUNH.DE": ("iShares Core € Govt Bond UCITS ETF EUR (Dist)", "BOND", 3),
    "EUNW.DE": ("iShares € High Yield Corp Bond UCITS ETF EUR (Dist)", "BOND", 3),
    "IS3K.DE": ("iShares $ Short Duration High Yield Corp Bond UCITS ETF USD (Dist)", "BOND", 2),
    "IS3S.DE": ("iShares Edge MSCI World Value Factor UCITS ETF USD (Acc)", "EQ_GLOBAL_BROAD", 4),
    "XDWY.DE": ("Xtrackers MSCI World Screened UCITS ETF 1D", "EQ_GLOBAL_BROAD", 4),
}


def fix_legacy_names(conn: sqlite3.Connection):
    print("Fixing 19 legacy mismatched ETF names...")
    for ticker, (name, bucket, lvl) in LEGACY_NAME_FIXES.items():
        conn.execute(
            """
            UPDATE instrument
            SET name = ?, risk_bucket = ?, risk_level = ?
            WHERE import_key = ? OR price_symbol = ?
            """,
            (name, bucket, lvl, ticker, ticker),
        )
    conn.commit()
    print("Legacy names updated in database.")


def map_isins_to_tickers(conn: sqlite3.Connection):
    rows = conn.execute("""
        SELECT id, import_key, isin, name, status
        FROM instrument
        WHERE asset_class = 'ETF'
          AND (
              import_key LIKE 'IE%'
              OR import_key LIKE 'LU%'
              OR import_key LIKE 'DE%'
              OR import_key LIKE 'FR%'
          )
          AND length(import_key) = 12
        ORDER BY id
    """).fetchall()

    print(f"Found {len(rows)} ETFs with 12-char ISIN import_key.")
    if not rows:
        return

    # Existing import_keys to avoid collisions
    existing_keys = {r[0] for r in conn.execute("SELECT import_key FROM instrument").fetchall()}

    url = "https://api.openfigi.com/v3/mapping"
    headers = {"Content-Type": "application/json"}

    batch_size = 10
    updated_count = 0

    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        payload = json.dumps([{"idType": "ID_ISIN", "idValue": r[2]} for r in chunk]).encode(
            "utf-8"
        )

        retries = 3
        res = None
        while retries > 0:
            try:
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res = json.loads(resp.read().decode())
                    break
            except Exception as e:
                retries -= 1
                time.sleep(3.0)
                if retries == 0:
                    print(f"Batch {i} failed: {e}")

        if not res:
            continue

        for r, item in zip(chunk, res, strict=False):
            inst_id, old_key, isin, _name, _status = r
            entries = item.get("data", [])
            ticker = None
            for e in entries:
                t = e.get("ticker", "").strip()
                ex = e.get("exchCode", "").strip()
                if not t:
                    continue
                if ex in ("GY", "GR", "GF", "GE", "GD", "GM"):
                    ticker = f"{t}.DE"
                    break
                elif ex == "NA":
                    ticker = f"{t}.AS"
                elif ex == "LN":
                    if not ticker:
                        ticker = f"{t}.L"
                elif ex == "FP":
                    if not ticker:
                        ticker = f"{t}.PA"

            if ticker:
                # Disambiguate if collision
                new_key = ticker
                if new_key in existing_keys and new_key != old_key:
                    # Append .DE or suffix
                    new_key = f"{ticker.split('.')[0]}-{isin[-4:]}.{ticker.split('.')[-1]}"

                conn.execute(
                    "UPDATE instrument SET import_key = ?, price_symbol = ? WHERE id = ?",
                    (new_key, ticker, inst_id),
                )
                existing_keys.discard(old_key)
                existing_keys.add(new_key)
                updated_count += 1
                if updated_count % 50 == 0 or updated_count <= 10:
                    print(f"  [{updated_count}/{len(rows)}] {isin} -> {new_key} (ticker: {ticker})")

        conn.commit()
        # Polite delay to respect OpenFIGI rate limit
        time.sleep(1.2)

    print(
        f"\nSuccessfully replaced ISINs with exchange tickers for "
        f"{updated_count} / {len(rows)} ETFs."
    )


def sync_universe_yaml(conn: sqlite3.Connection):
    print(f"Updating {UNIVERSE_YAML}...")
    with open(UNIVERSE_YAML, encoding="utf-8") as f:
        existing_yaml = yaml.safe_load(f) or {}

    old_instruments = existing_yaml.get("instruments", {})
    # Keep stocks intact
    new_yaml_instruments = {
        k: v for k, v in old_instruments.items() if v.get("asset_class") == "STOCK"
    }

    # Fetch all UNIVERSE ETFs from database
    db_etfs = conn.execute("""
        SELECT import_key, isin, name, currency, price_symbol, trading_calendar,
               is_fund, ter_bps, risk_bucket, risk_level
        FROM instrument
        WHERE asset_class = 'ETF' AND status = 'UNIVERSE'
        ORDER BY import_key
    """).fetchall()

    for r in db_etfs:
        key = r["import_key"]
        new_yaml_instruments[key] = {
            "name": r["name"],
            "asset_class": "ETF",
            "currency": r["currency"] or "EUR",
            "price_symbol": r["price_symbol"] or key,
            "trading_calendar": r["trading_calendar"] or "XETRA",
            "is_fund": True,
            "ter_bps": r["ter_bps"],
            "risk_bucket": r["risk_bucket"],
            "risk_level": r["risk_level"],
        }

    with open(UNIVERSE_YAML, "w", encoding="utf-8") as f:
        yaml.dump(
            {"version": 1, "instruments": new_yaml_instruments},
            f,
            sort_keys=False,
            allow_unicode=True,
        )
    print(f"universe.yaml updated with {len(new_yaml_instruments)} instruments.")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    fix_legacy_names(conn)
    map_isins_to_tickers(conn)
    sync_universe_yaml(conn)

    conn.close()
    print("All tasks completed!")


if __name__ == "__main__":
    main()
