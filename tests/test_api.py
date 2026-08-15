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

from portfolio_tracker.api.app import app, get_db, get_writable_db
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
             realized_gain_eur, valuation_basis_eur, income_cash_eur,
             income_received_eur, completeness, drawdown_pct)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PARTIAL', ?)
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
            # Of the EUR 300 idle, EUR 220 is dividend income never reinvested
            # and EUR 80 is sale proceeds. EUR 260 has been received in all,
            # so EUR 40 of income went back into the market.
            D("220"),
            D("260"),
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
    app.dependency_overrides[get_writable_db] = lambda: conn
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
    app.dependency_overrides[get_writable_db] = lambda: conn
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


def test_idle_cash_is_split_by_where_it_came_from(client: TestClient) -> None:
    """ "EUR 300 uninvested" answers the wrong question.

    Capital awaiting reinvestment and income that may already have been spent
    have different meanings, so the balance is attributed rather than lumped.
    """
    split = client.get("/api/portfolio").json()["uninvested_cash"]
    assert split["from_income_eur"] == 220.0
    assert split["from_capital_eur"] == 80.0
    assert split["from_income_eur"] + split["from_capital_eur"] == split["total_eur"]
    assert "capital spent before income" in split["attribution_rule"]


def test_income_received_outlives_the_cash_it_arrived_as(client: TestClient) -> None:
    """A reinvested dividend leaves the balance without ceasing to be income."""
    body = client.get("/api/portfolio").json()
    assert body["income_received_eur"] == 260.0
    assert body["uninvested_cash"]["from_income_eur"] == 220.0


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
    assert "Outside the total" in page
    assert "12,500.00" in page  # the total
    assert "Dividends in cash" in page and "220.00" in page
    assert "Capital in cash" in page and "80.00" in page
    assert "Income received" in page and "260.00" in page


def test_the_page_flags_unreviewed_inferences(client: TestClient) -> None:
    assert "Awaiting review" in client.get("/debug").text


# ---------------------------------------------------------------------------
# Position detail: the sentence this whole FX design exists to produce
# ---------------------------------------------------------------------------


def test_a_foreign_position_explains_why_it_disagrees_with_the_export(
    client: TestClient,
) -> None:
    body = client.get("/api/positions/ZTFA").json()
    split = body["return_decomposition"]
    assert split["true_eur_gain"] == 200.0
    assert split["local_gain_at_todays_rate"] == 1000.0
    assert split["currency_effect"] == -800.0
    explanation = split["vs_broker_export"]
    assert "1,000.00" in explanation and "200.00" in explanation
    assert "800.00" in explanation
    assert "TRY depreciation" in explanation


def test_a_euro_position_makes_no_currency_claim(client: TestClient) -> None:
    """There is nothing to explain, so nothing is said. A EUR gain is the gain."""
    split = client.get("/api/positions/ZGEQ").json()["return_decomposition"]
    assert "vs_broker_export" not in split
    assert split["currency_effect"] == 0.0


def test_an_unknown_instrument_is_a_404(client: TestClient) -> None:
    assert client.get("/api/positions/NOPE").status_code == 404


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


def test_allocation_weights_sum_to_one(client: TestClient) -> None:
    for dim in ("asset_class", "currency", "account"):
        body = client.get("/api/allocation", params={"dim": dim}).json()
        assert sum(b["weight"] for b in body["buckets"]) == pytest.approx(1.0)
        assert body["total_eur"] == 10000.0


def test_currency_allocation_names_the_basis_it_used(client: TestClient) -> None:
    """A EUR-quoted S&P tracker carries dollar risk. The label is the honesty."""
    body = client.get("/api/allocation", params={"dim": "currency"}).json()
    assert body["basis"] == "quote_currency"


def test_an_unknown_dimension_is_rejected_rather_than_interpolated(client: TestClient) -> None:
    assert client.get("/api/allocation", params={"dim": "instrument"}).status_code == 422


# ---------------------------------------------------------------------------
# The goal
# ---------------------------------------------------------------------------


def test_the_default_target_is_the_one_its_owner_chose(client: TestClient) -> None:
    body = client.get("/api/goal").json()
    assert body["target_eur"] == 200000.0
    assert body["current_eur"] == 12500.0
    assert body["remaining_eur"] == 187500.0
    assert body["basis"] == "holdings plus deposit balances"


def test_too_little_history_yields_no_projected_date(client: TestClient) -> None:
    """One day of history cannot support a date, so none is offered."""
    body = client.get("/api/goal").json()
    assert body["projected_date"] is None
    assert body["months_at_current_rate"] is None
    assert any("too few to measure" in c for c in body["caveats"])


def test_the_target_can_be_changed_and_persists(client: TestClient) -> None:
    assert client.put("/api/goal", json={"target_eur": "50000"}).status_code == 200
    assert client.get("/api/goal").json()["target_eur"] == 50000.0


def test_a_non_positive_target_is_refused(client: TestClient) -> None:
    for bad in ("0", "-100"):
        assert client.put("/api/goal", json={"target_eur": bad}).status_code == 422
    # And the stored value is untouched.
    assert client.get("/api/goal").json()["target_eur"] == 200000.0


def test_the_page_shows_the_goal_and_its_assumptions(client: TestClient) -> None:
    page = client.get("/debug").text
    assert "Goal" in page
    assert "200,000.00" in page
    assert "Change target" in page


# ---------------------------------------------------------------------------
# FX and deposits
# ---------------------------------------------------------------------------


def test_fx_states_its_direction(client: TestClient) -> None:
    client.app.state  # noqa: B018 - touch nothing; the seed has no rates
    assert client.get("/api/fx", params={"quote": "TRY"}).status_code == 404


def test_deposit_terms_include_the_withholding_rate(client: TestClient) -> None:
    """Turkish stopaj is taken at source; a model that accrues gross drifts high."""
    body = client.get("/api/accounts/deposits").json()
    assert body["accounts"] == []  # the seed has a depot, not a deposit account


# ---------------------------------------------------------------------------
# The write surface: confirming, rejecting, rebuilding
# ---------------------------------------------------------------------------


def test_a_proposal_shows_its_arithmetic_before_it_is_confirmed(client: TestClient) -> None:
    """The reason a confirm button is safe to expose at all."""
    body = client.get("/api/pending/1").json()
    assert body["basis"] == "ZGEQ paid out"
    assert body["status"] == "PROPOSED"
    assert body["bookable"] is True


def test_confirming_books_it_and_says_the_figures_are_now_stale(client: TestClient) -> None:
    body = client.post("/api/pending/1/confirm").json()
    assert body["status"] == "CONFIRMED"
    assert body["transaction_id"] > 0
    # Nothing is recomputed here: a rebuild is several years of daily rows and
    # belongs to a step the owner takes, not a side effect of a click.
    assert body["rebuild_required"] is True
    assert client.get("/api/pending").json()["rebuild_required"] is True


def test_a_confirmation_reaches_the_ledger(client: TestClient) -> None:
    txn_id = client.post("/api/pending/1/confirm").json()["transaction_id"]
    conn = next(iter(app.dependency_overrides.values()))()
    row = conn.execute(
        "SELECT type, flow_class FROM transaction_ WHERE id = ?", (txn_id,)
    ).fetchone()
    # INCOME, not a contribution: unit accounting must count it as return, or
    # the dividend cancels the very return it represents.
    assert row["type"] == "DIVIDEND"
    assert row["flow_class"] == "INCOME"


def test_confirming_twice_is_refused(client: TestClient) -> None:
    assert client.post("/api/pending/1/confirm").status_code == 200
    second = client.post("/api/pending/1/confirm")
    assert second.status_code == 404
    assert "confirmed" in second.json()["detail"]


def test_an_edited_amount_is_booked_and_the_original_kept(client: TestClient) -> None:
    """The owner is often the better authority. The guess is still evidence."""
    body = client.post("/api/pending/1/confirm", json={"amount_local": "99.99"}).json()
    assert body["amount_edited"] is True
    detail = client.get("/api/pending/1").json()
    assert detail["amount"] == 99.99
    assert detail["derivation"]["amount_as_inferred"] == "12.50"
    assert detail["derivation"]["edited_by_owner"] is True
    assert "corrected by the owner" in detail["basis"]


def test_a_non_positive_edited_amount_is_refused(client: TestClient) -> None:
    assert client.post("/api/pending/1/confirm", json={"amount_local": "0"}).status_code == 422
    assert client.get("/api/pending/1").json()["status"] == "PROPOSED"


def test_confirming_something_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert client.post("/api/pending/999/confirm").status_code == 404


def test_rejecting_requires_a_reason(client: TestClient) -> None:
    """A rejection is a claim about reality; in six months the why is all there is."""
    assert client.post("/api/pending/1/reject", json={"reason": ""}).status_code == 422
    assert client.post("/api/pending/1/reject", json={}).status_code == 422
    assert client.get("/api/pending/1").json()["status"] == "PROPOSED"


def test_rejecting_records_the_reason(client: TestClient) -> None:
    response = client.post("/api/pending/1/reject", json={"reason": "already spent it"})
    assert response.status_code == 200
    detail = client.get("/api/pending/1").json()
    assert detail["status"] == "REJECTED"
    assert detail["note"] == "already spent it"
    assert detail["resulting_txn_id"] is None


def test_a_rejected_proposal_cannot_then_be_confirmed(client: TestClient) -> None:
    client.post("/api/pending/1/reject", json={"reason": "no"})
    assert client.post("/api/pending/1/confirm").status_code == 404


def test_a_kind_with_no_booking_rule_is_refused_rather_than_silently_skipped(
    client: TestClient,
) -> None:
    """Reporting success while recording nothing is the worst available answer."""
    conn = next(iter(app.dependency_overrides.values()))()
    conn.execute(
        "INSERT INTO pending_entry (id, kind, account_id, proposed_date, currency, "
        "amount_local, basis, status, created_at) VALUES (2, 'VORABPAUSCHALE', 1, "
        "'2026-01-02', 'EUR', '10.00', 'forecast charge', 'PROPOSED', '2026-01-02T00:00:00Z')"
    )
    conn.commit()
    assert client.get("/api/pending/2").json()["bookable"] is False
    response = client.post("/api/pending/2/confirm")
    assert response.status_code == 422
    assert "cannot be booked" in response.json()["detail"]


def test_the_page_shows_the_basis_next_to_the_button(client: TestClient) -> None:
    page = client.get("/debug").text
    assert "Awaiting review" in page
    assert "ZGEQ paid out" in page  # the arithmetic, beside the control
    assert "Confirm" in page and "Reject" in page


def test_the_page_warns_when_the_figures_predate_a_decision(client: TestClient) -> None:
    client.post("/api/pending/1/confirm")
    page = client.get("/debug").text
    assert "out of date" in page
    assert "Rebuild now" in page


def test_rebuild_on_an_empty_database_succeeds_with_nothing_to_do(
    empty_client: TestClient,
) -> None:
    body = empty_client.post("/api/admin/rebuild").json()
    assert body["ok"] is True
    assert body["lots"] == 0
    assert body["breaks"] == []


def test_rebuild_clears_the_stale_warning(client: TestClient) -> None:
    """The loop the owner actually walks: confirm, notice, rebuild, done."""
    client.post("/api/pending/1/confirm")
    assert client.get("/api/pending").json()["rebuild_required"] is True
    assert client.post("/api/admin/rebuild", json={"end": "2026-06-01"}).json()["ok"] is True
    assert client.get("/api/pending").json()["rebuild_required"] is False


def test_rebuild_reports_a_reconciliation_break_rather_than_hiding_it(
    client: TestClient,
) -> None:
    """`breaks` is the field to read: non-empty means a contribution vanished."""
    body = client.post("/api/admin/rebuild", json={"end": "2026-06-01"}).json()
    assert "breaks" in body
    assert isinstance(body["breaks"], list)
