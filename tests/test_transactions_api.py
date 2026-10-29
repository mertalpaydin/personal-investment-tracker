"""Recording a trade from the page.

The properties under test are the ones that make this surface safe enough to
have written at all:

- validate writes nothing, ever, and answers 200 even when it objects;
- a refusal names a remedy rather than leaving a dead end;
- a missing exchange rate is refused, never invented;
- saving recomputes, so a trade that was accepted actually shows up;
- imported rows cannot be edited, because the next import would undo it;
- what a person did to the ledger survives what they did to it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from portfolio_tracker.api.app import app as api_app
from portfolio_tracker.api.app import get_db, get_writable_db
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture
def db() -> sqlite3.Connection:
    # same_thread=False because TestClient runs the endpoint on a worker
    # thread while the fixture created the connection on this one.
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "teilfreistellung_pct, tax_regime, price_source) VALUES "
        "(1, 'ZGEQ', 'Global Equity', 'ETF', 'EUR', 1, '0.30', 'DE_INVESTMENT', 'manual')"
    )
    # A lira-denominated holding, to exercise the refusal when no rate exists.
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "tax_regime, price_source) VALUES "
        "(2, 'TFND', 'Lira Fund', 'ETF', 'TRY', 1, 'OUT_OF_SCOPE', 'manual')"
    )
    # The migrations already ship the published rates; only add one if the
    # test is running in a year they do not cover.
    conn.execute(
        "INSERT OR IGNORE INTO tax_constant (tax_year, basiszins) VALUES (?, '0.0320')",
        (TODAY.year,),
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


def buy(**overrides: object) -> dict[str, object]:
    return {
        "type": "BUY",
        "account_id": 1,
        "instrument": "ZGEQ",
        "trade_date": YESTERDAY.isoformat(),
        "quantity": "100",
        "price_per_unit_local": "50",
        **overrides,
    }


def _count(db: sqlite3.Connection, table: str) -> int:
    return int(db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_writes_nothing(client: TestClient, db: sqlite3.Connection) -> None:
    """The property the whole preview rests on."""
    before = (_count(db, "transaction_"), _count(db, "lot"), _count(db, "manual_entry_log"))
    response = client.post("/api/transactions/validate", json=buy())
    assert response.status_code == 200
    assert response.json()["ok"] is True
    after = (_count(db, "transaction_"), _count(db, "lot"), _count(db, "manual_entry_log"))
    assert before == after


def test_an_invalid_trade_still_answers_200(client: TestClient) -> None:
    """A form that 422s on every keystroke cannot validate as you type."""
    response = client.post("/api/transactions/validate", json=buy(instrument=None))
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert [e["code"] for e in body["errors"]] == ["INSTRUMENT_REQUIRED"]
    assert body["errors"][0]["field"] == "instrument"


def test_an_unknown_instrument_carries_a_remedy(client: TestClient) -> None:
    body = client.post("/api/transactions/validate", json=buy(instrument="NOPE")).json()
    error = next(e for e in body["errors"] if e["code"] == "INSTRUMENT_UNKNOWN")
    assert error["remedy"], "a refusal with no way forward is a dead end"


def test_a_future_trade_is_refused(client: TestClient) -> None:
    future = (TODAY + timedelta(days=2)).isoformat()
    body = client.post("/api/transactions/validate", json=buy(trade_date=future)).json()
    assert "FUTURE_DATE" in [e["code"] for e in body["errors"]]


def test_a_missing_exchange_rate_is_refused_rather_than_invented(client: TestClient) -> None:
    """The most consequential refusal on this surface.

    A fabricated rate produces a euro cost basis that looks exact, is not, and
    is then frozen into every gain computed from the lot for as long as it is
    held. Nothing about it would ever look wrong.
    """
    body = client.post(
        "/api/transactions/validate",
        json=buy(instrument="TFND", price_per_unit_local="120"),
    ).json()
    error = next(e for e in body["errors"] if e["code"] == "FX_UNAVAILABLE")
    assert "guessed" in error["message"]
    assert error["remedy"]


def test_a_supplied_rate_satisfies_the_refusal(client: TestClient) -> None:
    body = client.post(
        "/api/transactions/validate",
        json=buy(instrument="TFND", price_per_unit_local="120", fx_rate="35"),
    ).json()
    assert body["ok"] is True
    assert body["normalised"]["fx_rate_source"] == "user"


def test_a_cash_movement_may_not_name_a_holding(client: TestClient) -> None:
    body = client.post(
        "/api/transactions/validate",
        json={
            "type": "DEPOSIT",
            "account_id": 1,
            "instrument": "ZGEQ",
            "trade_date": YESTERDAY.isoformat(),
            "gross_amount_local": "1000",
        },
    ).json()
    assert "INSTRUMENT_FORBIDDEN" in [e["code"] for e in body["errors"]]


# ---------------------------------------------------------------------------
# saving
# ---------------------------------------------------------------------------


def test_saving_records_the_trade_and_recomputes(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """The reason the rebuild is synchronous.

    Returning "saved, now go and recompute" is exactly the pattern that pushed
    the owner of this tool back to a terminal.
    """
    response = client.post("/api/transactions", json=buy())
    assert response.status_code == 201
    body = response.json()
    assert body["transaction"]["source"] == "MANUAL"
    assert body["transaction"]["flow_class"] == "INVESTMENT"
    assert body["rebuild"]["lots_created"] == 1
    assert _count(db, "lot") == 1


def test_buying_a_watched_instrument_sets_status_to_owned(
    client: TestClient, db: sqlite3.Connection
) -> None:
    db.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, status, price_source) "
        "VALUES (3, 'WATCHME', 'Watched ETF', 'ETF', 'EUR', 'WATCH', 'manual')"
    )
    db.commit()
    response = client.post("/api/transactions", json=buy(instrument="WATCHME"))
    assert response.status_code == 201
    status = db.execute("SELECT status FROM instrument WHERE id = 3").fetchone()["status"]
    assert status == "OWNED"


def test_a_purchase_is_investment_not_a_contribution(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Misclassifying this does not error; it silently erases the return.

    A buy recorded as EXTERNAL_IN would issue units in the unit-accounting
    series, and the gain it goes on to earn would be attributed to a
    contribution rather than to performance.
    """
    client.post("/api/transactions", json=buy())
    client.post(
        "/api/transactions",
        json={
            "type": "DEPOSIT",
            "account_id": 1,
            "trade_date": YESTERDAY.isoformat(),
            "gross_amount_local": "5000",
        },
    )
    classes = dict(
        db.execute("SELECT type, flow_class FROM transaction_").fetchall()  # type: ignore[arg-type]
    )
    assert classes["BUY"] == "INVESTMENT"
    assert classes["DEPOSIT"] == "EXTERNAL_IN"


def test_saving_an_invalid_trade_returns_the_same_shape_as_validate(client: TestClient) -> None:
    response = client.post("/api/transactions", json=buy(instrument="NOPE"))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["errors"][0]["code"] == "INSTRUMENT_UNKNOWN"


def test_a_second_identical_trade_is_flagged_but_allowed(client: TestClient) -> None:
    """Warned, not blocked: buying the same thing twice in a day is legal."""
    client.post("/api/transactions", json=buy())
    body = client.post("/api/transactions/validate", json=buy()).json()
    assert body["ok"] is True
    assert "DUPLICATE_SUSPECT" in [w["code"] for w in body["warnings"]]


# ---------------------------------------------------------------------------
# selling
# ---------------------------------------------------------------------------


def test_a_sale_previews_the_lots_it_would_consume(client: TestClient) -> None:
    client.post("/api/transactions", json=buy())
    body = client.post(
        "/api/transactions/validate",
        json=buy(
            type="SELL", quantity="40", price_per_unit_local="72.50", trade_date=TODAY.isoformat()
        ),
    ).json()

    sale = body["sale"]
    assert sale["quantity"] == 40
    assert sale["proceeds_eur"] == pytest.approx(2900.0)
    assert sale["cost_basis_eur"] == pytest.approx(2000.0)
    assert sale["gross_gain_eur"] == pytest.approx(900.0)
    # 30% of an equity fund's gain is exempt.
    assert sale["teilfreistellung_eur"] == pytest.approx(270.0)
    assert sale["taxable_gain_eur"] == pytest.approx(630.0)
    assert len(sale["consumes"]) == 1


def test_selling_more_than_is_held_is_refused(client: TestClient) -> None:
    client.post("/api/transactions", json=buy())
    body = client.post(
        "/api/transactions/validate",
        json=buy(type="SELL", quantity="500", price_per_unit_local="72.50"),
    ).json()
    assert body["ok"] is False
    assert "OVERSELL" in [e["code"] for e in body["errors"]]


def test_a_sale_priced_at_the_entered_figure_says_so(client: TestClient) -> None:
    client.post("/api/transactions", json=buy())
    body = client.post(
        "/api/transactions/validate",
        json=buy(type="SELL", quantity="10", price_per_unit_local="99"),
    ).json()
    assert any("figure you entered" in c for c in body["sale"]["caveats"])


# ---------------------------------------------------------------------------
# editing and deleting
# ---------------------------------------------------------------------------


def test_an_imported_row_refuses_to_be_edited_and_explains_why(
    client: TestClient, db: sqlite3.Connection
) -> None:
    client.post("/api/transactions", json=buy())
    txn_id = db.execute("SELECT id FROM transaction_").fetchone()["id"]
    db.execute("UPDATE transaction_ SET source = 'IMPORT' WHERE id = ?", (txn_id,))
    db.commit()

    response = client.request("DELETE", f"/api/transactions/{txn_id}")
    assert response.status_code == 409
    assert "import" in response.json()["detail"].lower()


def test_deleting_removes_the_trade_and_its_lots(
    client: TestClient, db: sqlite3.Connection
) -> None:
    created = client.post("/api/transactions", json=buy()).json()
    assert _count(db, "lot") == 1

    response = client.request("DELETE", f"/api/transactions/{created['id']}")
    assert response.status_code == 200
    assert _count(db, "transaction_") == 0
    assert _count(db, "lot") == 0
    # Returned so the page can offer a one-click undo.
    assert response.json()["was"]["type"] == "BUY"


def test_every_hand_edit_is_recorded_where_nothing_derives_from_it(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """The trade-off for a hard delete.

    The ledger stays "what is true"; the history of what a person did to it
    lives in a table no calculation reads, so a missed filter cannot make a
    tax figure quietly wrong.
    """
    created = client.post("/api/transactions", json=buy()).json()
    client.request("DELETE", f"/api/transactions/{created['id']}")

    actions = [r["action"] for r in db.execute("SELECT action FROM manual_entry_log ORDER BY id")]
    assert actions == ["CREATE", "DELETE"]
    deleted = db.execute(
        "SELECT previous FROM manual_entry_log WHERE action = 'DELETE'"
    ).fetchone()["previous"]
    assert "BUY" in deleted


def test_the_ledger_lists_what_can_and_cannot_be_edited(
    client: TestClient, db: sqlite3.Connection
) -> None:
    client.post("/api/transactions", json=buy())
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_eur, flow_class, source, external_id) VALUES "
        "(1, 1, 'BUY', ?, 'EUR', '10', '500', 'INVESTMENT', 'IMPORT', 'broker:1')",
        (YESTERDAY.isoformat(),),
    )
    db.commit()

    rows = client.get("/api/transactions").json()["transactions"]
    editable = {r["source"]: r["editable"] for r in rows}
    assert editable == {"MANUAL": True, "IMPORT": False}


def test_quantities_survive_the_round_trip_exactly(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Fund quantities carry more significant figures than a float holds."""
    precise = "123.456789012345"
    client.post("/api/transactions", json=buy(quantity=precise, price_per_unit_local="1"))
    listed = client.get("/api/transactions").json()["transactions"][0]
    assert listed["quantity"] == precise
