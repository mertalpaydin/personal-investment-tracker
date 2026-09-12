"""Sync ETFs in comparison set / watchlist with data/ETF_List.csv.

Rules:
1. Match existing UNIVERSE ETFs to ETF_List.csv to keep them and preserve their price history.
2. Remove UNIVERSE ETFs that are not in ETF_List.csv.
3. Keep all OWNED ETFs and all STOCK tickers completely untouched.
4. Add all other ETFs from ETF_List.csv into instrument and universe.yaml.
5. Extract historical prices (5 years) for newly added ETFs and store in price table.
"""

from __future__ import annotations

import contextlib
import csv
import re
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

# Ensure src is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from portfolio_tracker.core import classify  # noqa: E402
from portfolio_tracker.core.peers import DEFAULT_RISK_LEVEL  # noqa: E402

DB_PATH = BASE_DIR / "data" / "portfolio.db"
CSV_PATH = BASE_DIR / "data" / "ETF_List.csv"
UNIVERSE_YAML_PATH = BASE_DIR / "src" / "portfolio_tracker" / "data" / "universe.yaml"


def normalize_name(s: str) -> str:
    s = s.lower()
    for w in [
        "ucits",
        "etf",
        "(acc)",
        "(dist)",
        "acc",
        "dist",
        "eur",
        "usd",
        "swap",
        "based",
        "unfunded",
        "hedged",
        "daily",
        "1c",
        "1d",
        "dr",
        "distributing",
        "accumulating",
        "class",
    ]:
        s = re.sub(r"\b" + w + r"\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def main() -> None:
    print("=== Starting ETF Watchlist Sync ===")

    # 1. Restore from backup if needed or backup now
    backup_path = DB_PATH.with_suffix(".db.backup-before-etf-sync")
    if not backup_path.exists():
        print(f"Backing up database to {backup_path.name}...")
        shutil.copy2(DB_PATH, backup_path)
    else:
        print(f"Restoring clean database from {backup_path.name}...")
        shutil.copy2(backup_path, DB_PATH)

    # 2. Read CSV
    print(f"Reading {CSV_PATH}...")
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        csv_rows = list(csv.DictReader(f))
    print(f"Found {len(csv_rows)} ETFs in CSV.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 3. Existing instruments
    existing_universe_etfs = conn.execute(
        "SELECT id, import_key, price_symbol, name, status, isin FROM instrument "
        "WHERE asset_class = 'ETF' AND status = 'UNIVERSE'"
    ).fetchall()

    owned_etfs = conn.execute(
        "SELECT id, import_key, name FROM instrument WHERE asset_class = 'ETF' AND status = 'OWNED'"
    ).fetchall()
    print(f"Existing OWNED ETFs (kept untouched): {[r['import_key'] for r in owned_etfs]}")
    print(f"Existing UNIVERSE ETFs: {len(existing_universe_etfs)}")

    # Index CSV by ISIN and normalized name
    csv_by_isin = {r["ISIN"]: r for r in csv_rows}
    csv_by_norm = {}
    for r in csv_rows:
        n = normalize_name(r["Fund name"])
        if n not in csv_by_norm:
            csv_by_norm[n] = r

    matched_existing_ids: set[int] = set()
    matched_existing_keys: set[str] = set()
    covered_csv_isins: set[str] = set()

    for e in existing_universe_etfs:
        e_norm = normalize_name(e["name"])
        matched_csv = None
        if e["isin"] and e["isin"] in csv_by_isin and e["isin"] not in covered_csv_isins:
            matched_csv = csv_by_isin[e["isin"]]
        elif e_norm in csv_by_norm and csv_by_norm[e_norm]["ISIN"] not in covered_csv_isins:
            matched_csv = csv_by_norm[e_norm]
        else:
            for cn, r in csv_by_norm.items():
                if (
                    r["ISIN"] not in covered_csv_isins
                    and len(e_norm) > 8
                    and (e_norm in cn or cn in e_norm)
                ):
                    matched_csv = r
                    break

        if matched_csv and matched_csv["ISIN"] not in covered_csv_isins:
            matched_existing_ids.add(e["id"])
            matched_existing_keys.add(e["import_key"])
            covered_csv_isins.add(matched_csv["ISIN"])
            with contextlib.suppress(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE instrument SET isin = ? WHERE id = ? AND (isin IS NULL OR isin = '')",
                    (matched_csv["ISIN"], e["id"]),
                )

    print(f"Matched existing UNIVERSE ETFs to KEEP: {len(matched_existing_keys)}")

    # Identify UNIVERSE ETFs to remove
    to_remove = [e for e in existing_universe_etfs if e["id"] not in matched_existing_ids]
    print(f"UNIVERSE ETFs to REMOVE: {len(to_remove)}")
    for r in to_remove:
        conn.execute("DELETE FROM price WHERE instrument_id = ?", (r["id"],))
        conn.execute("DELETE FROM instrument WHERE id = ?", (r["id"],))
    conn.commit()

    # 4. Insert new ETFs from CSV
    all_current_keys = {r[0] for r in conn.execute("SELECT import_key FROM instrument").fetchall()}
    all_current_isins = {
        r[0] for r in conn.execute("SELECT isin FROM instrument WHERE isin IS NOT NULL").fetchall()
    }

    new_etfs = [
        r
        for r in csv_rows
        if r["ISIN"] not in covered_csv_isins
        and r["ISIN"] not in all_current_keys
        and r["ISIN"] not in all_current_isins
    ]
    print(f"New ETFs to ADD from CSV: {len(new_etfs)}")

    inserted_new: list[tuple[int, str, str]] = []  # (id, isin, name)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    for r in new_etfs:
        isin = r["ISIN"].strip()
        fname = r["Fund name"].strip()

        ter_bps = None
        if r.get("TER p.a."):
            with contextlib.suppress(ValueError):
                ter_bps = round(float(r["TER p.a."]) * 10000)

        dist_policy = "DIST" if "dist" in r.get("Distribution", "").lower() else "ACC"

        sugg = classify.suggest(isin, fname, "ETF")
        bucket = sugg.bucket or "EQ_GLOBAL_BROAD"
        risk_lvl = sugg.risk_level or DEFAULT_RISK_LEVEL.get(bucket, 4)

        try:
            cursor = conn.execute(
                """
                INSERT INTO instrument
                    (import_key, isin, name, asset_class, currency, price_symbol,
                     price_source, trading_calendar, is_fund, ter_bps,
                     distribution_policy, risk_bucket, risk_level, status)
                VALUES (?, ?, ?, 'ETF', 'EUR', ?, 'yfinance', 'XETRA', 1, ?, ?, ?, ?, 'UNIVERSE')
                """,
                (isin, isin, fname, isin, ter_bps, dist_policy, bucket, risk_lvl),
            )
            inserted_new.append((int(cursor.lastrowid or 0), isin, fname))
        except sqlite3.IntegrityError as err:
            print(f"Warning: skipped {isin} ({fname}) due to {err}")

    conn.commit()
    print(f"Successfully inserted {len(inserted_new)} new ETFs into instrument table.")

    # 5. Update universe.yaml
    print(f"Updating {UNIVERSE_YAML_PATH}...")
    with open(UNIVERSE_YAML_PATH, encoding="utf-8") as f:
        existing_yaml = yaml.safe_load(f) or {}

    yaml_instruments = existing_yaml.get("instruments", {})
    new_yaml_instruments = {
        k: v for k, v in yaml_instruments.items() if v.get("asset_class") == "STOCK"
    }

    for k in matched_existing_keys:
        if k in yaml_instruments:
            new_yaml_instruments[k] = yaml_instruments[k]

    all_db_universe_etfs = conn.execute(
        "SELECT import_key, isin, name, currency, price_symbol, trading_calendar, "
        "is_fund, ter_bps, risk_bucket, risk_level FROM instrument "
        "WHERE asset_class = 'ETF' AND status = 'UNIVERSE'"
    ).fetchall()

    for row in all_db_universe_etfs:
        key = row["import_key"]
        if key not in new_yaml_instruments:
            new_yaml_instruments[key] = {
                "name": row["name"],
                "asset_class": "ETF",
                "currency": row["currency"] or "EUR",
                "price_symbol": row["price_symbol"] or key,
                "trading_calendar": row["trading_calendar"] or "XETRA",
                "is_fund": True,
                "ter_bps": row["ter_bps"],
                "risk_bucket": row["risk_bucket"],
                "risk_level": row["risk_level"],
            }

    with open(UNIVERSE_YAML_PATH, "w", encoding="utf-8") as f:
        yaml.dump(
            {"version": 1, "instruments": new_yaml_instruments},
            f,
            sort_keys=False,
            allow_unicode=True,
        )
    print(f"Saved universe.yaml with {len(new_yaml_instruments)} total instruments.")

    # 6. Extract historical prices (5 years) for newly added ETFs
    print(f"\n=== Extracting historical prices for {len(inserted_new)} new ETFs ===")
    import yfinance as yf

    batch_size = 40
    total_written = 0
    t0 = time.time()

    for i in range(0, len(inserted_new), batch_size):
        batch = inserted_new[i : i + batch_size]
        batch_isins = [item[1] for item in batch]
        id_map = {item[1]: item[0] for item in batch}

        batch_num = i // batch_size + 1
        total_batches = (len(inserted_new) + batch_size - 1) // batch_size
        print(
            f"Fetching batch {batch_num}/{total_batches} ({len(batch)} symbols)... ",
            end="",
            flush=True,
        )
        try:
            df = yf.download(
                batch_isins,
                period="5y",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            print(f"Error: {exc}")
            continue

        if df is None or df.empty:
            print("No data returned.")
            continue

        batch_written = 0
        has_multi = hasattr(df.columns, "nlevels") and df.columns.nlevels > 1

        for isin, inst_id in id_map.items():
            try:
                if has_multi:
                    if isin not in df["Close"].columns:
                        continue
                    series_close = df["Close"][isin]
                    series_adj = (
                        df["Adj Close"][isin]
                        if "Adj Close" in df.columns and isin in df["Adj Close"].columns
                        else None
                    )
                else:
                    series_close = df["Close"]
                    series_adj = df.get("Adj Close")

                records = []
                for dt, close_val in series_close.items():
                    if close_val is None or close_val != close_val:
                        continue
                    adj_val = series_adj[dt] if series_adj is not None else None
                    adj_str = (
                        str(round(float(adj_val), 6))
                        if adj_val is not None and adj_val == adj_val
                        else None
                    )
                    close_str = str(round(float(close_val), 6))
                    date_str = dt.strftime("%Y-%m-%d")
                    records.append((inst_id, date_str, close_str, adj_str, "EUR", "yfinance", now))

                if records:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO price
                            (instrument_id, date, close, total_return_close,
                             currency, source, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        records,
                    )
                    batch_written += len(records)
            except Exception as e:
                pass

        conn.commit()
        total_written += batch_written
        print(f"wrote {batch_written} price rows (cumulative: {total_written}).")
        # Brief pause to avoid rate limits
        time.sleep(0.5)

    elapsed = time.time() - t0
    print(f"\nFinished price extraction! Total prices written: {total_written} in {elapsed:.1f}s.")
    conn.close()
    print("=== ETF Watchlist Sync Complete ===")


if __name__ == "__main__":
    main()
