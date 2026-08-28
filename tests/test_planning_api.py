"""The shape the plan page reads, pinned.

The front end is plain ES modules with no build step and no type checking, so
a renamed or dropped field is not a compile error -- it is a blank panel, or a
figure silently rendering as "—", in front of the only person who uses this.
These tests are the contract standing in for a compiler.

Three things they hold in place:

``test_a_projection_carries_the_return_it_is_assuming``
    The form offers "assume a different return". That question is unanswerable
    unless the page can say what the projection is already assuming, and the
    bootstrap never states a return -- it resamples price history. The figure
    has to be computed and sent, or the field is asking the reader to adjust a
    number they have never seen.

``test_measured_returns_are_flagged_when_the_ledger_cannot_support_them``
    Unrecorded funding reads as investment return. Offering "+13,000% a year"
    as something to assume would launder a data gap into a plan.

``test_levers_are_absolute_not_differences``
    "Save EUR 1,500 a month" can be checked against a bank statement. "Save
    EUR 300 a month more" cannot be checked against anything unless the reader
    already trusts the figure the tool measured -- and on this ledger that
    figure was wrong for months without anyone being able to see it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_tracker.api.app import app as api_app
from portfolio_tracker.api.app import get_db, get_writable_db
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

#: A fixed day, so the windows the API measures over are the same every run.
AS_OF = date(2026, 6, 30)


@pytest.fixture
def db() -> sqlite3.Connection:
    """The scenarios fixture, on a connection the TestClient can share.

    Deliberately the same ledger `tests/test_scenarios.py` builds: two years of
    gently rising prices and a EUR 1,000 deposit every thirtieth day. Keeping
    them identical means a difference between the core answer and the answer
    the page receives is a bug in the serialising, which is the only thing
    these tests are for.

    ``same_thread=False`` because TestClient runs the endpoint on a worker
    thread while the fixture built the connection on this one.
    """
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, price_source) "
        "VALUES (1, 'ZGEQ', 'World Equity', 'ETF', 'EUR', 'manual')"
    )
    conn.execute(
        "INSERT INTO setting (key, value, updated_at) "
        "VALUES ('goal_target_eur', '200000', '2026-01-01T00:00:00Z') "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
    )

    level = Decimal(100)
    start = AS_OF - timedelta(days=760)
    for offset in range(760):
        on = start + timedelta(days=offset)
        conn.execute(
            "INSERT INTO price (instrument_id, date, close, total_return_close, currency, "
            "source, fetched_at) VALUES (1, ?, ?, ?, 'EUR', 'manual', '2026-01-01T00:00:00Z')",
            (on.isoformat(), str(level), str(level)),
        )
        level *= Decimal("1.0003")

    for offset in range(400):
        on = AS_OF - timedelta(days=399 - offset)
        conn.execute(
            "INSERT INTO position_daily (date, account_id, instrument_id, quantity, currency, "
            "price_local, price_asof, fx_rate, fx_asof, market_value_local, market_value_eur, "
            "cost_basis_local, cost_basis_eur, cost_basis_eur_at_spot, unrealized_eur, "
            "unrealized_local_eur, unrealized_fx_eur) VALUES (?, 1, 1, '1000', 'EUR', "
            "'100', ?, '1', ?, '100000', '100000', '90000', '90000', '90000', "
            "'10000', '10000', '0')",
            (on.isoformat(), on.isoformat(), on.isoformat()),
        )
        conn.execute(
            "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
            "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
            "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
            "completeness, drawdown_pct, net_external_flow_eur) VALUES (?, 1, '100000', "
            "'100000', '90000', '1000', '100', '0', '0', '0', '100000', '0', '0', "
            "'FULL', '0', ?)",
            (on.isoformat(), "1000" if offset % 30 == 0 else "0"),
        )
        if offset % 30 == 0:
            # A real ledger row, not just a number in the derived table:
            # `measure_contributions` reads what was actually saved, so that
            # one figure cannot disagree with the savings page's.
            conn.execute(
                "INSERT INTO transaction_ (account_id, type, trade_date, currency, "
                "gross_amount_local, gross_amount_eur, flow_class, source, confidence, "
                "is_new_savings) VALUES (1, 'DEPOSIT', ?, 'EUR', '1000', '1000', "
                "'EXTERNAL_IN', 'MANUAL', 'ACTUAL', 1)",
                (on.isoformat(),),
            )
    conn.commit()
    return conn


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    api_app.dependency_overrides[get_db] = lambda: db
    api_app.dependency_overrides[get_writable_db] = lambda: db
    with TestClient(api_app) as test_client:
        yield test_client
    api_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# The assumptions the form has to show before it can offer to change them
# ---------------------------------------------------------------------------


def test_a_projection_carries_the_return_it_is_assuming(client: TestClient) -> None:
    body = client.get("/api/projection", params={"paths": 200, "years": 15}).json()
    returns = body["returns"]

    assert returns["implied_annual"] is not None
    assert returns["history_days"] > 0
    assert returns["history_from"]

    # Each window carries the days behind it, so "+24% a year" from twelve
    # weeks can be recognised as a different claim from the same figure over
    # four years.
    for label in ("3m", "6m", "12m", "since_start"):
        window = returns["measured"][label]
        assert set(window) == {"period", "annualised", "days"}
        assert window["days"] >= 0


def test_a_projection_carries_what_was_actually_paid_in(client: TestClient) -> None:
    body = client.get("/api/projection", params={"paths": 200, "years": 15}).json()
    windows = body["contributions"]
    assert set(windows) == {"6m", "12m", "24m"}
    for window in windows.values():
        assert window["average_monthly_eur"] is not None
        assert window["median_monthly_eur"] is not None
        assert window["months"], "the months behind the average must be inspectable"


def test_the_scenario_answer_repeats_its_assumptions(client: TestClient) -> None:
    """An earlier date means nothing without them in the same view."""
    body = client.post(
        "/api/projection/scenario",
        params={"paths": 200, "years": 15},
        json={"name": "More", "monthly_contribution_eur": "2000"},
    ).json()
    assert body["returns"]["implied_annual"] is not None
    assert body["seed"], "both runs share a seed, and the page says so"
    assert body["as_of"]


def test_measured_returns_are_flagged_when_the_ledger_cannot_support_them(
    client: TestClient, db: sqlite3.Connection
) -> None:
    clean = client.get("/api/projection", params={"paths": 100, "years": 15}).json()
    assert clean["returns"]["measured_reliable"] is True

    # A purchase far larger than any cash on record: unit accounting reads
    # the missing funding as return, so every measured figure is inflated.
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, trade_date, type, quantity, "
        "currency, price_per_unit_local, gross_amount_local, fees_local, fx_rate, "
        "gross_amount_eur, fees_eur, flow_class, source, confidence) VALUES "
        "(1, 1, ?, 'BUY', '500', 'EUR', '1000', '500000', '0', '1', '500000', '0', "
        "'INVESTMENT', 'MANUAL', 'ACTUAL')",
        (AS_OF.isoformat(),),
    )
    db.commit()

    dirty = client.get("/api/projection", params={"paths": 100, "years": 15}).json()
    assert dirty["returns"]["measured_reliable"] is False


# ---------------------------------------------------------------------------
# Levers state a destination, not a difference
# ---------------------------------------------------------------------------


def test_levers_are_absolute_not_differences(client: TestClient) -> None:
    body = client.get("/api/projection", params={"paths": 200, "years": 15}).json()
    measured = body["baseline"]["monthly_contribution_eur"]

    # Half of what is being paid in now must arrive later, not sooner. Read as
    # a delta, "1000" would mean a thousand *extra* and the sign would flip --
    # which is exactly the confusion the absolute form removes.
    lower = Decimal(str(measured)) / 2
    result = client.post(
        "/api/projection/scenario",
        params={"paths": 200, "years": 15},
        json={"name": "Half", "monthly_contribution_eur": str(lower)},
    ).json()

    assert result["scenario"]["monthly_contribution_eur"] == pytest.approx(float(lower))
    assert result["baseline"]["monthly_contribution_eur"] == pytest.approx(measured)
    earlier = result["months_earlier"]["0.75"]
    assert earlier is None or earlier <= 0


def test_an_omitted_lever_keeps_what_is_measured(client: TestClient) -> None:
    """Null means "leave this", not zero.

    A blank savings box read as EUR 0 a month would quietly answer a question
    nobody asked, and the date it produced would be badly wrong.
    """
    result = client.post(
        "/api/projection/scenario",
        params={"paths": 200, "years": 15},
        json={"name": "Nothing"},
    ).json()
    assert result["scenario"]["monthly_contribution_eur"] == pytest.approx(
        result["baseline"]["monthly_contribution_eur"]
    )
    assert result["levers"]["monthly_contribution_eur"] is None
    assert result["levers"]["return_annual_pct"] is None
    assert result["lever_kinds"] == []


def test_a_return_is_labelled_an_assumption_and_a_contribution_is_not(
    client: TestClient,
) -> None:
    """The most important framing on the screen."""
    assumed = client.post(
        "/api/projection/scenario",
        params={"paths": 200, "years": 15},
        json={"name": "Better markets", "return_annual_pct": "0.12"},
    ).json()
    assert assumed["lever_kinds"] == ["assumption"]
    assert assumed["assumption_warning"]

    decided = client.post(
        "/api/projection/scenario",
        params={"paths": 200, "years": 15},
        json={"name": "Save more", "monthly_contribution_eur": "3000"},
    ).json()
    assert decided["lever_kinds"] == ["controllable"]
    assert decided["assumption_warning"] is None


def test_the_fee_lever_is_gone(client: TestClient) -> None:
    """Removed as unnecessary, and it must not come back by accident.

    It was expressed in basis points -- a unit nobody outside fund management
    reads -- and it did the same arithmetic as the return lever sitting beside
    it, which meant two boxes that could disagree about one thing.
    """
    body = client.get("/api/projection", params={"paths": 100, "years": 15}).json()
    assert "ter_delta_bps" not in body["levers"]
    assert "return_delta_annual_pct" not in body["levers"]
    assert "monthly_contribution_delta_eur" not in body["levers"]
