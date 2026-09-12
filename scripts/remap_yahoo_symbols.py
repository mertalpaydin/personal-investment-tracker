"""Remap the 110 Xetra-listed ETFs whose Yahoo Finance symbols vanished around 2026-09-05..08.

Yahoo dropped these tickers, but the underlying ETFs still trade under other Yahoo
symbols (often a new Xetra ticker, sometimes only on another German exchange, or
only on a non-German exchange). This script re-resolves each failing symbol by ISIN
search, validates the best German candidate against the last known close, and
(with --apply) updates instrument.price_symbol in the database plus the matching
price_symbol entry in universe.yaml.

The 110 failing symbols are read from update_run.problems for update_run.id = 48:
each problems entry looks like "<symbol>: <symbol>: yfinance returned nothing...",
so the old symbol is the prefix before the first ':'.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "portfolio.db"
UNIVERSE_YAML = BASE_DIR / "src" / "portfolio_tracker" / "data" / "universe.yaml"
REPORT_CSV = BASE_DIR / "data" / "remap_report.csv"

PROBLEM_RUN_ID = 48

VALIDATE_START = "2026-09-01"
VALIDATE_END = "2026-09-06"  # yfinance end is exclusive; covers through 2026-09-05
CHECK_DATE = "2026-09-04"
TOLERANCE = 0.01  # 1%
SLEEP_SECONDS = 0.5

OTHER_GERMAN_SUFFIXES = {"F", "SG", "MU", "DU", "HM", "HA", "BE"}

STATUS_REMAPPED = "REMAPPED"
STATUS_NOT_GERMAN = "NOT_GERMAN"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_MISMATCH = "MISMATCH"


@dataclass
class Instrument:
    id: int
    import_key: str
    isin: str
    name: str
    old_symbol: str
    stored_close: float | None


@dataclass
class Candidate:
    symbol: str
    exchange: str
    tier: int  # 0 = Xetra (.DE / GER), 1 = other German exchange, 2 = everything else


@dataclass
class Result:
    instrument: Instrument
    status: str
    new_symbol: str | None = None
    exchange: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    new_close: float | None = None


def load_failing_instruments(conn: sqlite3.Connection) -> list[Instrument]:
    row = conn.execute("SELECT problems FROM update_run WHERE id = ?", (PROBLEM_RUN_ID,)).fetchone()
    if row is None:
        raise SystemExit(f"update_run.id = {PROBLEM_RUN_ID} not found")

    problems = json.loads(row["problems"])
    old_symbols = [p.split(":", 1)[0].strip() for p in problems]

    placeholders = ",".join("?" * len(old_symbols))
    rows = conn.execute(
        f"""
        SELECT id, isin, import_key, name, price_symbol
        FROM instrument
        WHERE price_symbol IN ({placeholders}) AND status = 'UNIVERSE'
        ORDER BY id
        """,
        old_symbols,
    ).fetchall()

    instruments = []
    for r in rows:
        close_row = conn.execute(
            "SELECT close FROM price WHERE instrument_id = ? AND date = ?",
            (r["id"], CHECK_DATE),
        ).fetchone()
        stored_close = float(close_row["close"]) if close_row else None
        instruments.append(
            Instrument(
                id=r["id"],
                import_key=r["import_key"],
                isin=r["isin"],
                name=r["name"],
                old_symbol=r["price_symbol"],
                stored_close=stored_close,
            )
        )

    if len(instruments) != len(old_symbols):
        found_symbols = {i.old_symbol for i in instruments}
        missing = [s for s in old_symbols if s not in found_symbols]
        print(f"WARNING: {len(missing)} problem symbols had no matching instrument row: {missing}")

    return instruments


def classify_candidate(symbol: str, exchange: str) -> int:
    suffix = symbol.rsplit(".", 1)[-1] if "." in symbol else ""
    if exchange == "GER" and suffix == "DE":
        return 0
    if suffix in OTHER_GERMAN_SUFFIXES:
        return 1
    return 2


def search_candidates(isin: str, old_symbol: str) -> list[Candidate]:
    search = yf.Search(isin, max_results=8)
    candidates = []
    for quote in search.quotes:
        symbol = (quote.get("symbol") or "").strip()
        exchange = (quote.get("exchange") or "").strip()
        if not symbol or symbol == old_symbol:
            continue
        tier = classify_candidate(symbol, exchange)
        candidates.append(Candidate(symbol=symbol, exchange=exchange, tier=tier))
    candidates.sort(key=lambda c: c.tier)
    return candidates


def fetch_close_on(symbol: str, date: str) -> float | None:
    hist = yf.Ticker(symbol).history(start=VALIDATE_START, end=VALIDATE_END, auto_adjust=False)
    if hist.empty:
        return None
    hist.index = hist.index.strftime("%Y-%m-%d")
    if date not in hist.index:
        return None
    return float(hist.loc[date, "Close"])


def resolve_instrument(inst: Instrument) -> Result:
    try:
        candidates = search_candidates(inst.isin, inst.old_symbol)
    except Exception as e:
        print(f"  ERROR searching {inst.import_key} ({inst.isin}): {e}")
        return Result(instrument=inst, status=STATUS_NOT_FOUND, candidates=[])

    if not candidates:
        return Result(instrument=inst, status=STATUS_NOT_FOUND, candidates=[])

    german_candidates = [c for c in candidates if c.tier <= 1]

    if not german_candidates:
        return Result(instrument=inst, status=STATUS_NOT_GERMAN, candidates=candidates)

    best = german_candidates[0]
    if inst.stored_close is None:
        return Result(
            instrument=inst, status=STATUS_MISMATCH, candidates=candidates, exchange=best.exchange
        )

    try:
        new_close = fetch_close_on(best.symbol, CHECK_DATE)
    except Exception as e:
        print(f"  ERROR validating {best.symbol} for {inst.import_key}: {e}")
        return Result(
            instrument=inst, status=STATUS_MISMATCH, candidates=candidates, exchange=best.exchange
        )

    if new_close is None:
        return Result(
            instrument=inst, status=STATUS_MISMATCH, candidates=candidates, exchange=best.exchange
        )

    diff = abs(new_close - inst.stored_close) / inst.stored_close
    if diff <= TOLERANCE:
        return Result(
            instrument=inst,
            status=STATUS_REMAPPED,
            new_symbol=best.symbol,
            exchange=best.exchange,
            candidates=candidates,
            new_close=new_close,
        )

    return Result(
        instrument=inst,
        status=STATUS_MISMATCH,
        exchange=best.exchange,
        candidates=candidates,
        new_close=new_close,
    )


def apply_db_update(conn: sqlite3.Connection, inst_id: int, new_symbol: str) -> None:
    conn.execute("UPDATE instrument SET price_symbol = ? WHERE id = ?", (new_symbol, inst_id))


def apply_yaml_update(text: str, old_symbol: str, new_symbol: str) -> tuple[str, bool]:
    """Replace the price_symbol value inside the entry whose price_symbol == old_symbol.

    Targeted text replacement of the ``    price_symbol: <old>`` line only; the
    dict key (import_key) and everything else in the file is left untouched.
    """
    pattern = re.compile(r"^(\s*price_symbol:\s*)" + re.escape(old_symbol) + r"\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return text, False
    if len(matches) > 1:
        print(
            f"  WARNING: {len(matches)} price_symbol lines match {old_symbol!r} "
            "in universe.yaml; replacing all"
        )
    new_text = pattern.sub(lambda m: f"{m.group(1)}{new_symbol}", text)
    return new_text, True


def write_report(results: list[Result]) -> None:
    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "import_key",
                "isin",
                "name",
                "old_symbol",
                "new_symbol",
                "exchange",
                "status",
                "candidates",
                "stored_close_0904",
                "new_close_0904",
            ]
        )
        for r in results:
            inst = r.instrument
            candidates_str = "; ".join(f"{c.symbol}({c.exchange})" for c in r.candidates)
            writer.writerow(
                [
                    inst.import_key,
                    inst.isin,
                    inst.name,
                    inst.old_symbol,
                    r.new_symbol or "",
                    r.exchange or "",
                    r.status,
                    candidates_str,
                    inst.stored_close if inst.stored_close is not None else "",
                    r.new_close if r.new_close is not None else "",
                ]
            )
    print(f"Report written to {REPORT_CSV}")


def print_summary(results: list[Result]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("\nSummary:")
    for status in (STATUS_REMAPPED, STATUS_NOT_GERMAN, STATUS_NOT_FOUND, STATUS_MISMATCH):
        print(f"  {status}: {counts.get(status, 0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Path to portfolio.db")
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument(
        "--dry-run", action="store_true", default=True, help="Do not write changes (default)"
    )
    apply_group.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write changes to the database and universe.yaml",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    instruments = load_failing_instruments(conn)
    print(f"Loaded {len(instruments)} failing instruments.")

    results: list[Result] = []
    for i, inst in enumerate(instruments, start=1):
        print(f"[{i}/{len(instruments)}] {inst.import_key} ({inst.isin})...")
        result = resolve_instrument(inst)
        results.append(result)
        print(f"  -> {result.status}" + (f" ({result.new_symbol})" if result.new_symbol else ""))
        time.sleep(SLEEP_SECONDS)

    print_summary(results)
    write_report(results)

    remapped = [r for r in results if r.status == STATUS_REMAPPED]

    if dry_run:
        print(
            f"\nDRY RUN: would update {len(remapped)} instruments. "
            "Re-run with --apply to write changes."
        )
        conn.close()
        return

    print(f"\nApplying {len(remapped)} updates...")
    yaml_text = UNIVERSE_YAML.read_text(encoding="utf-8")
    yaml_changed = False
    for r in remapped:
        assert r.new_symbol is not None
        apply_db_update(conn, r.instrument.id, r.new_symbol)
        yaml_text, changed = apply_yaml_update(yaml_text, r.instrument.old_symbol, r.new_symbol)
        if changed:
            yaml_changed = True
        else:
            print(
                "  WARNING: no price_symbol line found in universe.yaml "
                f"for {r.instrument.old_symbol}"
            )

    conn.commit()
    conn.close()

    if yaml_changed:
        UNIVERSE_YAML.write_text(yaml_text, encoding="utf-8")
        print(f"universe.yaml updated ({UNIVERSE_YAML}).")

    print(f"\nApplied {len(remapped)} price_symbol updates to the database.")


if __name__ == "__main__":
    main()
