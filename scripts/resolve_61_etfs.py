import csv
import sqlite3
import time
from pathlib import Path

import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "portfolio.db"
CSV_OUT = BASE_DIR / "data" / "unpriced_61_etfs.csv"

# Pre-resolved mappings from OpenFIGI for all 61 ETFs
MAPPINGS = {
    "LU2573966905": "AE5A.DE",
    "LU2572257124": "AHYQ.DE",
    "IE000CNSFAR2": "MWOF.DE",
    "LU2197908721": "NADQ.DE",
    "LU2780870932": "GCRE.DE",
    "LU1737653987": "DECR.DE",
    "LU2608817958": "AE5B.DE",
    "LU1681044308": "X13Z.DE",
    "IE000Y77LGG9": "XAM0.DE",
    "FR0013416716": "GLDA.DE",
    "LU2233156582": "PRAB.DE",
    "IE000O5FBC47": "ZPA5.DE",
    "IE000LAP5Z18": "WELE.DE",
    "LU3038520774": "EDFS.DE",
    "LU1407888137": "7USH.DE",
    "DE000ETFL581": "D6RP.DE",
    "IE000MWUQBJ0": "H4ZZ.DE",
    "IE000UQND7H4": "H4ZY.DE",
    "IE00BYXYX521": "XAAG.DE",
    "IE00BZ4BMM98": "EHDV.DE",
    "IE00BRKWGL70": "E500.DE",
    "IE00BNGJJT35": "SP2Q.DE",
    "IE0000UW95D6": "JRGE.DE",
    "IE00BYPLS672": "USPY.DE",
    "IE000MINO564": "ENDH.DE",
    "IE0001UQQ933": "GERD.DE",
    "IE00B4WPHX27": "ETL2.DE",
    "IE00BD8D5G25": "EUHA.DE",
    "IE00BVZ6SP04": "PJSR.DE",
    "IE00BYYW2V44": "SPPE.DE",
    "LU1484799769": "UIMC.DE",
    "LU0969639128": "UEF4.DE",
    "LU0446734104": "EUREUA.DE",
    "IE00BD4TYG73": "UBUJ.DE",
    "LU1971906802": "UET5.DE",
    "IE00BDR55927": "AW1R.DE",
    "IE00BN4Q1675": "AW12.DE",
    "LU1852211991": "MDBE.DE",
    "IE00BMVB5P51": "V60A.DE",
    "IE00BMVB5R75": "V80A.DE",
    "IE00BZ56RG20": "WTDM.DE",
    "IE000I9HGDZ3": "XWSC.DE",
    "LU2263803533": "XWD1.DE",
    "LU0659579733": "XWEH.DE",
    "DE000A1E0HR8": "XAD5.DE",
    "DE000A1E0HS6": "XAD6.DE",
    "IE00B53HP851": "SXRW.DE",
    "IE000ZX8CQG2": "CBUJ.DE",
    "IE00BLDGH553": "SECA.DE",
    "IE00B3VTML14": "SXRP.DE",
    "IE00BYXYYK40": "IS02.DE",
    "IE00B52SF786": "SXR2.DE",
    "IE00BFNM3C07": "SLMB.DE",
    "IE00B3VWMM18": "SXRJ.DE",
    "IE00B53QDK08": "SXR5.DE",
    "IE00BHZPJ908": "EDMU.DE",
    "IE00BJ5JP105": "5MVW.DE",
    "IE00BJ5JNZ06": "CBUF.DE",
    "IE00B52MJD48": "SXRZ.DE",
    "IE00BGPP6473": "CBUE.DE",
    "IE000WA6L436": "CEBE.DE",
}


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Find the 61 unpriced ETFs
    rows = conn.execute("""
        SELECT
            i.id, i.import_key, i.isin, i.name, i.ter_bps,
            i.risk_bucket, i.risk_level, i.distribution_policy
        FROM instrument i
        LEFT JOIN price p ON p.instrument_id = i.id
        WHERE i.asset_class = 'ETF'
        GROUP BY i.id
        HAVING count(p.date) = 0
        ORDER BY i.name
    """).fetchall()

    print(f"Found {len(rows)} unpriced ETFs in DB.")

    # 1. Write CSV
    print(f"Writing {CSV_OUT}...")
    csv_data = []
    for r in rows:
        isin: str = r["isin"] or r["import_key"]
        ticker = MAPPINGS.get(isin, "")
        csv_data.append(
            {
                "ISIN": isin,
                "Fund name": r["name"],
                "Xetra Ticker": ticker,
                "TER (bps)": r["ter_bps"] or "",
                "TER (%)": f"{r['ter_bps'] / 100:.2f}%" if r["ter_bps"] else "",
                "Risk Category": r["risk_bucket"] or "",
                "Risk Level": r["risk_level"] or "",
                "Distribution": r["distribution_policy"] or "",
            }
        )

    with open(CSV_OUT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "ISIN",
                "Fund name",
                "Xetra Ticker",
                "TER (bps)",
                "TER (%)",
                "Risk Category",
                "Risk Level",
                "Distribution",
            ],
        )
        writer.writeheader()
        writer.writerows(csv_data)
    print(f"Saved {len(csv_data)} rows to {CSV_OUT}.")

    # 2. Update price_symbol in database
    print("Updating price_symbol in database...")
    for r in rows:
        isin = r["isin"] or r["import_key"]
        ticker = MAPPINGS.get(isin, "")
        if ticker:
            conn.execute(
                "UPDATE instrument SET price_symbol = ? WHERE id = ?",
                (ticker, r["id"]),
            )
    conn.commit()
    print("Database price_symbols updated.")

    # 3. Fetch 5 years of historical prices for these 61 ETFs in batches of 30
    print("\nFetching 5 years of daily prices via yfinance...")
    tickers_to_fetch = [
        MAPPINGS[r["isin"] or r["import_key"]]
        for r in rows
        if (r["isin"] or r["import_key"]) in MAPPINGS
    ]
    id_by_ticker = {
        MAPPINGS[r["isin"] or r["import_key"]]: r["id"]
        for r in rows
        if (r["isin"] or r["import_key"]) in MAPPINGS
    }

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    total_written = 0

    batch_size = 30
    for i in range(0, len(tickers_to_fetch), batch_size):
        batch = tickers_to_fetch[i : i + batch_size]
        print(
            f"Downloading batch {i // batch_size + 1} ({len(batch)} tickers)... ",
            end="",
            flush=True,
        )
        try:
            df = yf.download(
                batch,
                period="5y",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            print(f"Error: {exc}")
            continue

        if df is None or df.empty:
            print("No data.")
            continue

        batch_written = 0
        has_multi = hasattr(df.columns, "nlevels") and df.columns.nlevels > 1

        for ticker in batch:
            inst_id = id_by_ticker.get(ticker)
            if not inst_id:
                continue

            try:
                if has_multi:
                    if ticker not in df["Close"].columns:
                        continue
                    series_close = df["Close"][ticker]
                    series_adj = (
                        df["Adj Close"][ticker]
                        if "Adj Close" in df.columns and ticker in df["Adj Close"].columns
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
                    records.append(
                        (inst_id, date_str, close_str, adj_str, "EUR", "yfinance", now_iso)
                    )

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
                print(f"\nError processing {ticker}: {e}")

        conn.commit()
        total_written += batch_written
        print(f"wrote {batch_written} price rows.")
        time.sleep(1.0)

    print(f"\nTotal price rows written for 61 ETFs: {total_written}.")

    # Check remaining unpriced
    remaining = conn.execute("""
        SELECT count(DISTINCT i.id) as c
        FROM instrument i
        LEFT JOIN price p ON p.instrument_id = i.id
        WHERE i.asset_class = 'ETF'
        GROUP BY i.id
        HAVING count(p.date) = 0
    """).fetchall()
    print(f"Remaining unpriced ETFs in DB: {len(remaining)}")

    conn.close()


if __name__ == "__main__":
    main()
