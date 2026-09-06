"""Score snapshots: one dated row per stock, never touched again.

The snapshot is the raw material for judging the formula later, so what
matters is that it is complete (every block, the inputs, the price on file),
idempotent for one day, and blind to anything after the calculation date.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.core import score_snapshots as ss
from portfolio_tracker.core.stock_indicators import (
    SCORE_FORMULA_VERSION,
    StockFundamentals,
    rescore_stock_fundamentals,
    save_stock_fundamentals,
    score_universe,
)
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    yield conn
    conn.close()


def _stock(conn: sqlite3.Connection, id_: int, key: str) -> None:
    conn.execute(
        "INSERT INTO instrument (id, import_key, symbol, name, asset_class, currency, "
        "trading_calendar, price_source) VALUES (?, ?, ?, ?, 'STOCK', 'EUR', 'XETRA', 'yfinance')",
        (id_, key, key, key),
    )


def _price(conn: sqlite3.Connection, id_: int, on: date, close: str, tr: str | None = None) -> None:
    conn.execute(
        "INSERT INTO price (instrument_id, date, close, total_return_close, currency, source, "
        "fetched_at) VALUES (?, ?, ?, ?, 'EUR', 'yfinance', '2026-01-01T00:00:00Z')",
        (id_, on.isoformat(), close, tr),
    )


def _good(id_: int, key: str, **over: object) -> StockFundamentals:
    base = dict(
        instrument_id=id_,
        symbol=key,
        name=key,
        sector="Technology",
        current_price=Decimal("100"),
        forward_pe=Decimal("11.5"),
        trailing_pe=Decimal("13.0"),
        fcf_yield=Decimal("0.085"),
        pct_off_52w_high=Decimal("-0.40"),
        range_52w_pct=Decimal("15.0"),
        ev_ebitda=Decimal("7.5"),
        price_to_book=Decimal("1.4"),
        target_upside_pct=Decimal("0.25"),
        dividend_yield=Decimal("0.038"),
        debt_to_equity=Decimal("60.0"),
        operating_margin=Decimal("0.20"),
        profit_margin=Decimal("0.15"),
        return_on_equity=Decimal("0.18"),
        analyst_count=12,
        buy_pct=Decimal("0.75"),
        recommendation_mean=Decimal("1.8"),
        fetched_at="2026-09-01T08:00:00Z",
    )
    base.update(over)
    return StockFundamentals(**base)  # type: ignore[arg-type]


def test_price_at_takes_the_last_close_on_or_before_the_day(db: sqlite3.Connection) -> None:
    _stock(db, 1, "AAA")
    _price(db, 1, date(2026, 9, 3), "98", "99")
    _price(db, 1, date(2026, 9, 4), "100")
    _price(db, 1, date(2026, 9, 7), "105")  # after the calc date: must be ignored
    on_file = ss.price_at(db, 1, date(2026, 9, 5))
    assert on_file is not None
    assert on_file.date == date(2026, 9, 4)
    assert on_file.close == Decimal("100")
    assert on_file.tr_close is None
    assert ss.price_at(db, 1, date(2026, 9, 1)) is None


def test_a_snapshot_holds_every_block_and_the_inputs(db: sqlite3.Connection) -> None:
    _stock(db, 1, "AAA")
    _price(db, 1, date(2026, 9, 4), "100", "101")
    items = [_good(1, "AAA")]
    score_universe(items)
    written = ss.record_snapshots(db, items, calc_date=date(2026, 9, 5), source="SYNC")
    assert written == 1

    row = db.execute("SELECT * FROM stock_score_snapshot").fetchone()
    assert row["calc_date"] == "2026-09-05"
    assert row["formula_version"] == SCORE_FORMULA_VERSION
    assert row["source"] == "SYNC"
    assert row["fundamentals_fetched_at"] == "2026-09-01T08:00:00Z"
    assert row["price_date"] == "2026-09-04"
    assert Decimal(str(row["price_close"])) == Decimal("100")
    assert Decimal(str(row["price_tr_close"])) == Decimal("101")
    assert row["value_score"] == items[0].value_score
    assert row["value_badge"] == "BARGAIN"
    blocks = [
        row[f"v_{c}"]
        for c in ("pe", "fcf_yield", "pullback", "ev_or_pb", "analyst", "dividend", "solvency")
    ]
    assert all(b is not None for b in blocks)
    assert sum(blocks) == row["value_raw_total"]
    assert row["v_quality_capped"] == 0 and row["v_trap_capped"] == 0
    q_blocks = [row[f"q_{c}"] for c in ("growth", "profitability", "balance", "steadiness")]
    assert row["quality_score"] == sum(q_blocks)

    inputs = json.loads(row["inputs_json"])
    assert inputs["forward_pe"] == "11.5"
    assert inputs["analyst_count"] == 12
    assert inputs["pe_used"] == "11.5"
    assert "relative" in inputs


def test_the_same_day_is_overwritten_not_duplicated(db: sqlite3.Connection) -> None:
    _stock(db, 1, "AAA")
    items = [_good(1, "AAA")]
    score_universe(items)
    ss.record_snapshots(db, items, calc_date=date(2026, 9, 5), source="SYNC")
    ss.record_snapshots(db, items, calc_date=date(2026, 9, 5), source="DAILY")
    rows = db.execute("SELECT source FROM stock_score_snapshot").fetchall()
    assert [r["source"] for r in rows] == ["DAILY"]

    ss.record_snapshots(db, items, calc_date=date(2026, 9, 6), source="DAILY")
    assert db.execute("SELECT count(*) AS n FROM stock_score_snapshot").fetchone()["n"] == 2


def test_unscored_items_are_skipped_and_bad_sources_refused(db: sqlite3.Connection) -> None:
    _stock(db, 1, "AAA")
    never_scored = _good(1, "AAA")  # score_universe not called: no breakdown
    assert ss.record_snapshots(db, [never_scored], calc_date=date(2026, 9, 5)) == 0
    with pytest.raises(ValueError):
        ss.record_snapshots(db, [], calc_date=date(2026, 9, 5), source="CRON")


def test_no_data_stocks_still_get_a_row_with_null_score(db: sqlite3.Connection) -> None:
    _stock(db, 1, "AAA")
    thin = _good(
        1,
        "AAA",
        forward_pe=None,
        trailing_pe=None,
        fcf_yield=None,
        pct_off_52w_high=None,
        ev_ebitda=None,
        price_to_book=None,
        target_upside_pct=None,
        dividend_yield=None,
        debt_to_equity=None,
        operating_margin=None,
    )
    score_universe([thin])
    assert ss.record_snapshots(db, [thin], calc_date=date(2026, 9, 5)) == 1
    row = db.execute("SELECT value_score, value_badge, v_pe FROM stock_score_snapshot").fetchone()
    assert row["value_score"] is None
    assert row["value_badge"] == "NO_DATA"
    assert row["v_pe"] is None


def test_rescore_writes_a_snapshot_for_every_stored_stock(db: sqlite3.Connection) -> None:
    _stock(db, 1, "AAA")
    _stock(db, 2, "BBB")
    items = [_good(1, "AAA"), _good(2, "BBB", forward_pe=Decimal("40"))]
    score_universe(items)
    save_stock_fundamentals(db, items)

    n = rescore_stock_fundamentals(db, source="DAILY", calc_date=date(2026, 9, 5))
    assert n == 2
    rows = db.execute(
        "SELECT instrument_id, source, calc_date FROM stock_score_snapshot ORDER BY instrument_id"
    ).fetchall()
    assert [(r["instrument_id"], r["source"], r["calc_date"]) for r in rows] == [
        (1, "DAILY", "2026-09-05"),
        (2, "DAILY", "2026-09-05"),
    ]
    summary = ss.snapshot_summary(db)
    assert summary["snapshots"] == 2 and summary["instruments"] == 2 and summary["days"] == 1


def test_reset_derived_leaves_snapshots_alone(db: sqlite3.Connection) -> None:
    _stock(db, 1, "AAA")
    items = [_good(1, "AAA")]
    score_universe(items)
    ss.record_snapshots(db, items, calc_date=date(2026, 9, 5))
    M.reset_derived(db)
    assert db.execute("SELECT count(*) AS n FROM stock_score_snapshot").fetchone()["n"] == 1
