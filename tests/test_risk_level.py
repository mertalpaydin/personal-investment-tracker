"""Tests for the numerical risk_level representation (1-7 SRI scale) and risk-group rankings."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_tracker.core import classify, peers, performance, watchlist
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

AS_OF = date(2026, 6, 30)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.commit()
    return conn


def _instrument(
    conn: sqlite3.Connection,
    key: str,
    *,
    currency: str = "EUR",
    bucket: str | None = None,
    risk_level: int | None = None,
    ter_bps: int | None = None,
    asset_class: str = "ETF",
    status: str = "WATCH",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO instrument (
            import_key, name, asset_class, currency, is_fund,
            price_source, risk_bucket, risk_level, ter_bps, status
        ) VALUES (?, ?, ?, ?, 1, 'manual', ?, ?, ?, ?)
        """,
        (key, f"{key} Fund", asset_class, currency, bucket, risk_level, ter_bps, status),
    )
    return int(cursor.lastrowid or 0)


def _prices(
    conn: sqlite3.Connection,
    instrument_id: int,
    *,
    currency: str = "EUR",
    start: date,
    days: int,
    first: Decimal,
    daily_growth: Decimal,
    adjusted: bool = True,
) -> None:
    level = first
    for offset in range(days):
        on = start + timedelta(days=offset)
        conn.execute(
            """
            INSERT INTO price (instrument_id, date, close, total_return_close, currency,
            source, fetched_at) VALUES (?, ?, ?, ?, ?, 'manual', '2026-01-01T00:00:00Z')
            """,
            (
                instrument_id,
                on.isoformat(),
                str(level),
                str(level) if adjusted else None,
                currency,
            ),
        )
        variation = Decimal("1.002") if offset % 2 == 0 else Decimal("0.998")
        level *= daily_growth * variation
    conn.commit()


def test_default_risk_levels_cover_all_standard_buckets() -> None:
    for b in peers.BUCKETS:
        assert b in peers.DEFAULT_RISK_LEVEL
        assert 1 <= peers.DEFAULT_RISK_LEVEL[b] <= 7


def test_risk_level_for_resolves_stated_and_fallback(db: sqlite3.Connection) -> None:
    _instrument(db, "EXPLICIT", bucket="EQ_GLOBAL_BROAD", risk_level=5)
    _instrument(db, "FROM_BUCKET", bucket="EQ_GLOBAL_BROAD", risk_level=None)
    _instrument(db, "CASH_INST", asset_class="CASH", bucket=None, risk_level=None)
    db.commit()

    r1 = db.execute("SELECT * FROM instrument WHERE import_key = 'EXPLICIT'").fetchone()
    r2 = db.execute("SELECT * FROM instrument WHERE import_key = 'FROM_BUCKET'").fetchone()
    r3 = db.execute("SELECT * FROM instrument WHERE import_key = 'CASH_INST'").fetchone()

    assert peers.risk_level_for(r1) == 5
    assert peers.risk_level_for(r2) == 4
    assert peers.risk_level_for(r3) == 1


def test_watchlist_set_details_records_and_validates_risk_level(db: sqlite3.Connection) -> None:
    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    watchlist.set_details(db, "IWDA", ter_bps=20, risk_bucket="EQ_GLOBAL_BROAD", risk_level=4)

    row = db.execute(
        "SELECT ter_bps, risk_bucket, risk_level FROM instrument WHERE import_key='IWDA'"
    ).fetchone()
    assert row["ter_bps"] == 20
    assert row["risk_bucket"] == "EQ_GLOBAL_BROAD"
    assert row["risk_level"] == 4

    # Invalid risk levels
    with pytest.raises(watchlist.WatchlistError, match="Risk level must be between 1"):
        watchlist.set_details(db, "IWDA", risk_level=0)
    with pytest.raises(watchlist.WatchlistError, match="Risk level must be between 1"):
        watchlist.set_details(db, "IWDA", risk_level=8)


def test_classify_suggest_proposes_risk_level() -> None:
    s1 = classify.suggest("EUNL", "iShares Core MSCI World UCITS ETF", "ETF")
    assert s1.bucket == "EQ_GLOBAL_BROAD"
    assert s1.risk_level == 4

    s2 = classify.suggest("BTC", "Bitcoin Tracker", "CRYPTO")
    assert s2.bucket == "CRYPTO"
    assert s2.risk_level == 7

    s3 = classify.suggest("SEC0", "iShares Semiconductor ETF", "ETF")
    assert s3.bucket == "EQ_SECTOR"
    assert s3.risk_level == 6

    s4 = classify.suggest("EGV2", "Lyxor Smart Overnight Return", "ETF")
    assert s4.bucket == "MONEY_MARKET"
    assert s4.risk_level == 1


def test_sharpe_ratio_computation_and_fallback(db: sqlite3.Connection) -> None:
    start = date(2025, 6, 1)
    k1 = _instrument(db, "ONE_YEAR", bucket="EQ_GLOBAL_BROAD", risk_level=4)
    # 390 days of history (~1.07 years)
    _prices(db, k1, start=start, days=390, first=Decimal(100), daily_growth=Decimal("1.0006"))

    m = performance.metrics(db, k1, import_key="ONE_YEAR", currency="EUR", as_of=AS_OF)
    assert m.ret_1y is not None
    assert m.vol_1y is not None
    assert m.sharpe_1y is not None
    # 3Y is None since only ~1 year of history exists
    assert m.ret_3y_ann is None
    assert m.sharpe_3y is None


def test_peers_leaderboard_ranks_instruments_in_risk_group(db: sqlite3.Connection) -> None:
    start = date(2025, 1, 1)
    k_high = _instrument(db, "HIGH_PERF", bucket="EQ_GLOBAL_BROAD", risk_level=4)
    k_low = _instrument(db, "LOW_PERF", bucket="EQ_GLOBAL_BROAD", risk_level=4)
    k_other_level = _instrument(db, "CRYPTO_TOP", bucket="CRYPTO", risk_level=7)

    _prices(db, k_high, start=start, days=400, first=Decimal(100), daily_growth=Decimal("1.0008"))
    _prices(db, k_low, start=start, days=400, first=Decimal(100), daily_growth=Decimal("1.0002"))
    _prices(
        db, k_other_level, start=start, days=400, first=Decimal(100), daily_growth=Decimal("1.0020")
    )

    # Leaderboard for Risk Level 4
    top_lvl4 = peers.leaderboard(db, risk_level=4, top=10, as_of=AS_OF)
    assert len(top_lvl4) == 2
    assert top_lvl4[0].import_key == "HIGH_PERF"
    assert top_lvl4[1].import_key == "LOW_PERF"
    assert top_lvl4[0].risk_level == 4
    assert top_lvl4[0].sharpe_1y is not None

    # Leaderboard for Risk Level 7
    top_lvl7 = peers.leaderboard(db, risk_level=7, top=10, as_of=AS_OF)
    assert len(top_lvl7) == 1
    assert top_lvl7[0].import_key == "CRYPTO_TOP"
    assert top_lvl7[0].risk_level == 7
