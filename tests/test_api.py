"""The HTTP surface.

Two properties are worth more than the rest of this file put together.

**A missing price must not become a 500.** Every endpoint has to keep answering
when an instrument has no price on the valuation date, because that is the
normal state of affairs during a Bayram gap, and a dashboard that goes blank on
a Turkish public holiday is a dashboard nobody trusts on any other day either.

**A missing tax input must not become a zero.** ``/api/tax/allowance`` returns
409 when the Basiszins for the year is not published. Zero is a plausible-looking
answer that would quietly get filed on an Anlage KAP; a refusal cannot be.

The third property, less general but the one this whole project exists for: the
headline total counts holdings and deposits, and never the uninvested cash.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_tracker.api.app import app, get_db
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal

# One EUR holding, one TRY holding whose euro gain differs from the export's,
# a deposit balance and some idle cash -- the smallest portfolio that exercises
# every claim the API makes.
AS_OF = "2026-06-01"


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, "
        "teilfreistellung_pct, is_fund, tax_regime, price_symbol) "
        "VALUES (1, 'ZGEQ', 'World Equity', 'ETF', 'EUR', '0.30', 1, 'DE_INVESTMENT', 'ZGEQ')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, "
        "is_fund, tax_regime, tax_scope_note) "
        "VALUES (2, 'ZTFA', 'Turkish Fund', 'FUND', 'TRY', 1, 'OUT_OF_SCOPE', "
        "'not held in the taxpayer''s name')"
    )
    conn.executemany(
        """
        INSERT INTO position_daily
            (date, account_id, instrument_id, quantity, currency, price_local,
             price_asof, price_stale_days, fx_rate, fx_asof, fx_stale_days,
             market_value_local, market_value_eur, cost_basis_local, cost_basis_eur,
             cost_basis_eur_at_spot, unrealized_eur, unrealized_local_eur,
             unrealized_fx_eur, valuation_status)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                AS_OF,
                1,
                D("100"),
                "EUR",
                D("60"),
                AS_OF,
                0,
                D(1),
                AS_OF,
                D("6000"),
                D("6000"),
                D("5000"),
                D("5000"),
                D("5000"),
                D("1000"),
                D("1000"),
                D("0"),
                "OK",
            ),
            # The point of the whole exercise: the export would print +EUR 1,000
            # of gain here; the true euro gain is +EUR 200, and the EUR 800 gap
            # is lira depreciation on the original capital. Stale by 3 days, so
            # the response also has to survive a forward-filled price.
            (
                AS_OF,
                2,
                D("50000"),
                "TRY",
                D("4"),
                "2026-05-29",
                3,
                D("50"),
                AS_OF,
                D("200000"),
                D("4000"),
                D("150000"),
                D("3800"),
                D("3000"),
                D("200"),
                D("1000"),
                D("-800"),
                "STALE",
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO portfolio_daily
            (date, person_id, market_value_eur, total_value_eur, cost_basis_eur,
             units_outstanding, unit_price, cash_eur, deposits_eur,
             realized_gain_eur, valuation_basis_eur, completeness, drawdown_pct)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PARTIAL', ?)
        """,
        (
            AS_OF,
            D("10000"),
            D("12500"),
            D("8800"),
            D("100"),
            D("110"),
            D("300"),
            D("2500"),
            D("40"),
            D("12800"),
            D("-0.02"),
        ),
    )
    conn.execute(
        "INSERT INTO pending_entry (kind, account_id, proposed_date, currency, "
        "amount_local, basis, status, created_at) "
        "VALUES ('DIVIDEND', 1, '2026-05-20', 'EUR', '12.50', 'ZGEQ paid out', "
        "'PROPOSED', '2026-05-21T00:00:00Z')"
    )
    conn.commit()


@pytest.fixture
def client() -> Iterator[TestClient]:
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    _seed(conn)
    app.dependency_overrides[get_db] = lambda: conn
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    conn.close()


@pytest.fixture
def empty_client() -> Iterator[TestClient]:
    """A migrated but never-rebuilt database: the state after `pt init-db`."""
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.commit()
    app.dependency_overrides[get_db] = lambda: conn
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    conn.close()


# ---------------------------------------------------------------------------
# The headline: what the total is, and what it deliberately is not
# ---------------------------------------------------------------------------


def test_total_is_holdings_plus_deposits_and_excludes_cash(client: TestClient) -> None:
    body = client.get("/api/portfolio").json()
    assert body["holdings_eur"] == 10000.0
    assert body["deposits_eur"] == 2500.0
    assert body["total_value_eur"] == 12500.0
    # Present, named, and outside the sum. Whether that money is still held is
    # not knowable from the source data, so it is never counted as if it were.
    assert body["uninvested_cash_eur"] == 300.0
    assert body["holdings_eur"] + body["deposits_eur"] == body["total_value_eur"]


def test_the_total_basis_is_stated_in_the_response(client: TestClient) -> None:
    body = client.get("/api/portfolio").json()
    assert "cash excluded" in body["total_basis"]


# ---------------------------------------------------------------------------
# The currency decomposition -- the reason this tool disagrees with the export
# ---------------------------------------------------------------------------


def test_the_three_way_gain_split_is_an_identity(client: TestClient) -> None:
    positions = client.get("/api/positions").json()["positions"]
    for row in positions:
        assert row["unrealized_eur"] == pytest.approx(
            row["unrealized_local_eur"] + row["unrealized_fx_eur"]
        )


def test_the_try_position_reports_the_true_euro_gain(client: TestClient) -> None:
    row = next(
        p for p in client.get("/api/positions").json()["positions"] if p["currency"] == "TRY"
    )
    assert row["unrealized_local_eur"] == 1000.0  # what the export prints
    assert row["unrealized_eur"] == 200.0  # what the owner actually made
    assert row["unrealized_fx_eur"] == -800.0  # lira depreciation on the capital


def test_out_of_scope_holdings_carry_their_reason(client: TestClient) -> None:
    row = next(
        p for p in client.get("/api/positions").json()["positions"] if p["currency"] == "TRY"
    )
    assert row["tax_regime"] == "OUT_OF_SCOPE"
    assert row["tax_scope_note"]


# ---------------------------------------------------------------------------
# Degradation: a gap in the data changes the answer, never the status code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/health/data",
        "/api/portfolio",
        "/api/positions",
        "/api/returns/series",
        "/api/pending",
        "/",
        "/debug",
    ],
)
def test_a_stale_price_never_becomes_a_server_error(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


def test_staleness_is_reported_rather_than_hidden(client: TestClient) -> None:
    body = client.get("/api/health/data").json()
    assert body["completeness"] == "PARTIAL"
    assert body["data_quality"]["stale_or_missing_prices"] == 1
    row = next(
        p for p in client.get("/api/positions").json()["positions"] if p["currency"] == "TRY"
    )
    assert row["price_stale_days"] == 3
    assert row["valuation_status"] == "STALE"


@pytest.mark.parametrize(
    "path", ["/api/health/data", "/api/portfolio", "/api/positions", "/api/returns"]
)
def test_an_unbuilt_database_is_a_404_not_a_crash(empty_client: TestClient, path: str) -> None:
    response = empty_client.get(path)
    assert response.status_code == 404
    assert "rebuild" in response.json()["detail"]


def test_the_debug_page_explains_itself_when_there_is_nothing_to_show(
    empty_client: TestClient,
) -> None:
    response = empty_client.get("/")
    assert response.status_code == 404
    assert "rebuild" in response.text


# ---------------------------------------------------------------------------
# Tax: a missing input is a refusal, not a zero
# ---------------------------------------------------------------------------


def test_a_missing_basiszins_is_a_conflict_not_a_zero(client: TestClient) -> None:
    response = client.get("/api/tax/vorabpauschale", params={"year": 2099})
    assert response.status_code == 409
    assert "2099" in response.json()["detail"]


def test_allowance_names_what_it_leaves_out(client: TestClient) -> None:
    body = client.get("/api/tax/allowance", params={"year": 2026}).json()
    assert "ZTFA" in " ".join(body["out_of_scope"]["instruments"])
    assert body["out_of_scope"]["warning"]
    assert "Steuerbescheinigung" in body["disclaimer"]


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_every_endpoint_dates_its_answer(client: TestClient) -> None:
    for path in ("/api/health/data", "/api/portfolio", "/api/positions"):
        assert client.get(path).json()["as_of"] == AS_OF


def test_the_portfolio_response_carries_its_own_data_quality(client: TestClient) -> None:
    quality = client.get("/api/portfolio").json()["data_quality"]
    assert quality["instruments_out_of_tax_scope"] == 1
    assert set(quality) >= {
        "inferred_flows",
        "trades_without_fees",
        "estimated_quantities",
        "unresolved_teilfreistellung",
        "stale_or_missing_prices",
    }


def test_pending_entries_are_listed_before_they_reach_the_ledger(client: TestClient) -> None:
    body = client.get("/api/pending").json()
    assert [e["kind"] for e in body["entries"]] == ["DIVIDEND"]
    assert body["entries"][0]["amount"] == 12.5


# ---------------------------------------------------------------------------
# The debug page
# ---------------------------------------------------------------------------


def test_the_page_shows_cash_outside_the_total(client: TestClient) -> None:
    page = client.get("/debug").text
    assert "not in the total" in page
    assert "12,500.00" in page  # the total
    assert "300.00" in page  # the cash, present but separate


def test_the_page_flags_unreviewed_inferences(client: TestClient) -> None:
    assert "Awaiting review" in client.get("/debug").text
