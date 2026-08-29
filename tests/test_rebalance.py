"""Unit tests for portfolio rebalancing core engine and API."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_tracker.api.app import app
from portfolio_tracker.api.deps import get_db
from portfolio_tracker.core.rebalance import PRESETS, compute_rebalance


def _mock_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE instrument (
            id INTEGER PRIMARY KEY,
            import_key TEXT UNIQUE,
            name TEXT,
            currency TEXT,
            asset_class TEXT,
            risk_bucket TEXT,
            risk_level INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE portfolio_daily (
            person_id INTEGER,
            date TEXT,
            PRIMARY KEY (person_id, date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE position_daily (
            person_id INTEGER,
            date TEXT,
            instrument_id INTEGER REFERENCES instrument (id),
            quantity REAL,
            market_value_eur REAL,
            price_local REAL
        )
        """
    )

    conn.execute(
        "INSERT INTO instrument VALUES (1, 'VWCE', 'Vanguard All-World', 'EUR', 'ETF', 'EQ_GLOBAL_BROAD', 4)"
    )
    conn.execute(
        "INSERT INTO instrument VALUES (2, 'IS3N', 'iShares MSCI EM', 'EUR', 'ETF', 'EQ_EM', 5)"
    )
    conn.execute(
        "INSERT INTO instrument VALUES (3, 'XG01', 'Xtrackers EUR Govt Bond', 'EUR', 'BOND', 'BOND_EUR_GOVT', 2)"
    )

    conn.execute("INSERT INTO portfolio_daily VALUES (1, '2026-08-28')")
    conn.execute("INSERT INTO position_daily VALUES (1, '2026-08-28', 1, 100.0, 10000.0, 100.0)")
    conn.execute("INSERT INTO position_daily VALUES (1, '2026-08-28', 2, 20.0, 1000.0, 50.0)")
    conn.execute("INSERT INTO position_daily VALUES (1, '2026-08-28', 3, 0.0, 0.0, 200.0)")
    conn.commit()
    return conn


def test_presets_structure():
    assert "world_em_80_20" in PRESETS
    assert "classic_60_40" in PRESETS
    assert "all_weather_approx" in PRESETS
    for p in PRESETS.values():
        assert "label" in p
        assert "targets" in p
        assert sum(p["targets"].values()) == Decimal("1.0")


def test_compute_rebalance_drift():
    conn = _mock_db()
    # Total portfolio is 11,000 EUR (10,000 VWCE ~ 90.9%, 1,000 IS3N ~ 9.1%)
    # Target: 80% World (8,800 EUR), 20% EM (2,200 EUR)
    targets = {
        "EQ_GLOBAL_BROAD": Decimal("0.80"),
        "EQ_EM": Decimal("0.20"),
    }
    plan = compute_rebalance(
        conn, targets=targets, dimension="risk_bucket", cash_inflow_eur=Decimal(0)
    )

    assert plan.total_current_value_eur == Decimal("11000.0")
    assert plan.cash_inflow_eur == Decimal(0)
    assert plan.total_post_value_eur == Decimal("11000.0")

    world = next(b for b in plan.buckets if b.bucket == "EQ_GLOBAL_BROAD")
    em = next(b for b in plan.buckets if b.bucket == "EQ_EM")

    assert world.current_value_eur == Decimal("10000.0")
    assert world.target_value_eur == Decimal("8800.00")
    assert world.drift_eur == Decimal("1200.00")  # overweight by 1,200

    assert em.current_value_eur == Decimal("10000.0") * 0 + Decimal("1000.0")
    assert em.target_value_eur == Decimal("2200.00")
    assert em.drift_eur == Decimal("-1200.00")  # underweight by 1,200


def test_compute_rebalance_smart_inflow():
    conn = _mock_db()
    # Total portfolio is 11,000 EUR.
    # We add 2,000 EUR cash inflow. Total post-value = 13,000 EUR.
    # Target: 80% World (10,400 EUR), 20% EM (2,600 EUR).
    # Current: World 10,000 EUR (deficit 400), EM 1,000 EUR (deficit 1,600).
    # Total deficit = 2,000 EUR.
    # Smart inflow should allocate exactly 400 EUR to World and 1,600 EUR to EM!
    targets = {
        "EQ_GLOBAL_BROAD": Decimal("0.80"),
        "EQ_EM": Decimal("0.20"),
    }
    plan = compute_rebalance(
        conn,
        targets=targets,
        dimension="risk_bucket",
        cash_inflow_eur=Decimal("2000.0"),
    )

    assert plan.total_post_value_eur == Decimal("13000.0")
    world = next(b for b in plan.buckets if b.bucket == "EQ_GLOBAL_BROAD")
    em = next(b for b in plan.buckets if b.bucket == "EQ_EM")

    assert world.inflow_allocation_eur == Decimal("400.00")
    assert em.inflow_allocation_eur == Decimal("1600.00")

    # Verify suggested orders are BUYs with correct amounts
    orders = {o["instrument"]: o for o in plan.suggested_orders}
    assert orders["VWCE"]["action"] == "BUY"
    assert orders["VWCE"]["amount_eur"] == Decimal("400.00")
    assert orders["IS3N"]["action"] == "BUY"
    assert orders["IS3N"]["amount_eur"] == Decimal("1600.00")


def test_api_rebalance_endpoints():
    conn = _mock_db()
    app.dependency_overrides[get_db] = lambda: conn
    client = TestClient(app)
    try:
        # Test presets endpoint
        resp = client.get("/api/rebalance/presets")
        assert resp.status_code == 200
        data = resp.json()
        assert "presets" in data
        assert len(data["presets"]) >= 3

        # Test calculate endpoint
        calc_resp = client.post(
            "/api/rebalance/calculate",
            json={
                "dimension": "risk_bucket",
                "targets": {"EQ_GLOBAL_BROAD": 0.8, "EQ_EM": 0.2},
                "cash_inflow_eur": 500.0,
            },
        )
        assert calc_resp.status_code == 200
        res_data = calc_resp.json()
        assert "buckets" in res_data
        assert "suggested_orders" in res_data
        assert "total_current_value_eur" in res_data
    finally:
        app.dependency_overrides.clear()
