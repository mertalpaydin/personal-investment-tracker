from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_tracker.api.app import app
from portfolio_tracker.core.stock_indicators import (
    StockFundamentals,
    calculate_value_score,
    get_stock_fundamentals,
    parse_raw_stock_info,
    save_stock_fundamentals,
)
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.commit()
    return conn


def test_calculate_value_score_bargain():
    score, badge, narrative = calculate_value_score(
        forward_pe=Decimal("11.5"),
        trailing_pe=Decimal("13.0"),
        fcf_yield=Decimal("0.085"),
        pct_off_52w_high=Decimal("-0.30"),
        range_52w_pct=Decimal("15.0"),
        ev_ebitda=Decimal("7.5"),
        price_to_book=Decimal("1.4"),
        target_upside_pct=Decimal("0.25"),
        dividend_yield=Decimal("0.038"),
        debt_to_equity=Decimal("60.0"),
        operating_margin=Decimal("0.20"),
    )
    assert score >= 70
    assert badge == "BARGAIN"
    assert "Attractively valued" in narrative or "FCF yield" in narrative


def test_calculate_value_score_dip():
    score, badge, narrative = calculate_value_score(
        forward_pe=Decimal("16.0"),
        trailing_pe=Decimal("17.5"),
        fcf_yield=Decimal("0.04"),
        pct_off_52w_high=Decimal("-0.28"),
        range_52w_pct=Decimal("20.0"),
        ev_ebitda=Decimal("12.0"),
        price_to_book=Decimal("2.5"),
        target_upside_pct=Decimal("0.18"),
        dividend_yield=Decimal("0.02"),
        debt_to_equity=Decimal("80.0"),
        operating_margin=Decimal("0.18"),
    )
    assert score >= 55
    assert badge in ("DIP", "BARGAIN")
    assert "Pullback opportunity" in narrative or "Attractively valued" in narrative


def test_calculate_value_score_premium():
    score, badge, narrative = calculate_value_score(
        forward_pe=Decimal("45.0"),
        trailing_pe=Decimal("50.0"),
        fcf_yield=Decimal("0.015"),
        pct_off_52w_high=Decimal("-0.02"),
        range_52w_pct=Decimal("95.0"),
        ev_ebitda=Decimal("32.0"),
        price_to_book=Decimal("12.0"),
        target_upside_pct=Decimal("0.05"),
        dividend_yield=Decimal("0.005"),
        debt_to_equity=Decimal("120.0"),
        operating_margin=Decimal("0.25"),
    )
    assert score < 40
    assert badge == "PREMIUM"
    assert "premium" in narrative.lower()


def test_calculate_value_score_value_trap():
    # Low P/E but excessive leverage (D/E = 350%)
    score, badge, narrative = calculate_value_score(
        forward_pe=Decimal("7.5"),
        trailing_pe=Decimal("8.0"),
        fcf_yield=Decimal("-0.02"),
        pct_off_52w_high=Decimal("-0.45"),
        range_52w_pct=Decimal("5.0"),
        ev_ebitda=Decimal("18.0"),
        price_to_book=Decimal("0.7"),
        target_upside_pct=Decimal("0.02"),
        dividend_yield=Decimal("0.0"),
        debt_to_equity=Decimal("350.0"),
        operating_margin=Decimal("-0.04"),
    )
    assert badge == "TRAP_RISK"
    assert score <= 38
    assert "Low P/E" in narrative and "leverage" in narrative.lower()


def test_parse_raw_stock_info():
    info = {
        "currency": "EUR",
        "sector": "Technology",
        "industry": "Semiconductors",
        "currentPrice": 85.5,
        "marketCap": 150000000000,
        "forwardPE": 14.2,
        "trailingPE": 15.8,
        "pegRatio": 1.1,
        "priceToBook": 2.8,
        "enterpriseToEbitda": 9.2,
        "freeCashflow": 12000000000,
        "dividendYield": 0.025,
        "payoutRatio": 0.35,
        "debtToEquity": 45.0,
        "operatingMargins": 0.28,
        "profitMargins": 0.22,
        "returnOnEquity": 0.20,
        "fiftyTwoWeekHigh": 120.0,
        "fiftyTwoWeekLow": 75.0,
        "twoHundredDayAverage": 95.0,
        "targetMeanPrice": 110.0,
        "recommendationKey": "buy",
    }
    fundamentals = parse_raw_stock_info(
        instrument_id=99,
        symbol="SEMI.DE",
        name="Semiconductor AG",
        info=info,
        fast_info=None,
        now_iso="2026-09-06T12:00:00Z",
    )
    assert fundamentals.instrument_id == 99
    assert fundamentals.symbol == "SEMI.DE"
    assert fundamentals.sector == "Technology"
    assert fundamentals.forward_pe == Decimal("14.2")
    assert fundamentals.fcf_yield is not None
    assert fundamentals.pct_off_52w_high is not None
    assert fundamentals.pct_off_52w_high < 0
    assert fundamentals.value_badge in ("BARGAIN", "DIP", "FAIR")


def test_db_persistence(db: sqlite3.Connection):
    # Insert test instrument
    db.execute("""
        INSERT INTO instrument
            (id, import_key, symbol, name, asset_class, currency, trading_calendar, price_source)
        VALUES (999, 'TEST_STOCK', 'TEST', 'Test Stock Corp', 'STOCK', 'EUR', 'XETRA', 'yfinance')
    """)

    fund = StockFundamentals(
        instrument_id=999,
        symbol="TEST_STOCK",
        name="Test Stock Corp",
        sector="Technology",
        current_price=Decimal("50.0"),
        forward_pe=Decimal("12.0"),
        fcf_yield=Decimal("0.07"),
        value_score=75,
        value_badge="BARGAIN",
        value_narrative="Bargain test stock",
        fetched_at="2026-09-06T12:00:00Z",
    )

    save_stock_fundamentals(db, [fund])
    retrieved = get_stock_fundamentals(db, instrument_id=999)
    assert len(retrieved) == 1
    assert retrieved[0].symbol == "TEST_STOCK"
    assert retrieved[0].sector == "Technology"
    assert retrieved[0].forward_pe == Decimal("12.0")
    assert retrieved[0].value_badge == "BARGAIN"


def test_api_stocks_fundamentals():
    client = TestClient(app)
    res = client.get("/api/stocks/fundamentals")
    assert res.status_code == 200
    data = res.json()
    assert "count" in data
    assert "sectors" in data
    assert "stocks" in data
    assert isinstance(data["stocks"], list)


def test_peers_scan_stock_mode():
    client = TestClient(app)
    # ABEA is an owned stock in the live test db
    res = client.get("/api/peers/ABEA?mode=value")
    assert res.status_code == 200
    data = res.json()
    assert data["is_stock"] is True
    assert data["mode"] == "value"
    assert "candidates" in data
    if data["candidates"]:
        c = data["candidates"][0]
        assert "forward_pe" in c
        assert "value_score" in c
        assert "value_badge" in c


def test_api_stocks_fundamentals_status():
    client = TestClient(app)
    res = client.get("/api/stocks/fundamentals/status")
    assert res.status_code == 200
    data = res.json()
    assert "running" in data
    assert "last_synced" in data


def test_sync_all_stock_fundamentals_empty(db: sqlite3.Connection):
    from portfolio_tracker.core.stock_indicators import sync_all_stock_fundamentals

    success, total = sync_all_stock_fundamentals(db)
    assert success == 0
    assert total == 0
