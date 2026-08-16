"""Pricing a sale before making it.

Three properties carry this file.

**It agrees with the sale it models.** A scenario that says one number and the
real disposal says another is worse than no scenario, so
``test_the_scenario_matches_what_actually_booking_the_sale_produces`` runs the
hypothetical, then books the sale for real and rebuilds, and asserts the two
agree to the cent.

**The marginal figure is the answer.** The same EUR 900 gain costs nothing when
the allowance is untouched and EUR 237 when it is spent. A per-sale tax number
without the year around it is not an answer.

**Nothing is written.** A scenario that left rows behind would be
indistinguishable from a decision afterwards.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from portfolio_tracker.api.app import app as api_app
from portfolio_tracker.api.app import get_db, get_writable_db
from portfolio_tracker.cli import app as cli_app
from portfolio_tracker.core import lots, scenario, tax
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal
runner = CliRunner()

TODAY = date(2026, 6, 1)
YEAR = 2026


def _price(conn: sqlite3.Connection, instrument_id: int, when: str, close: str) -> None:
    conn.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (?, ?, ?, 'EUR', 'manual', '2026-06-01T00:00:00Z')",
        (instrument_id, when, close),
    )


def _buy(
    conn: sqlite3.Connection, txn_id: int, instrument_id: int, when: str, qty: str, price: str
) -> None:
    total = D(qty) * D(price)
    conn.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, price_per_unit_local, gross_amount_local, gross_amount_eur, fx_rate, "
        "source, confidence, flow_class, external_id) "
        "VALUES (?, 1, ?, 'BUY', ?, 'EUR', ?, ?, ?, ?, '1', 'IMPORT', 'ACTUAL', "
        "'INVESTMENT', ?)",
        (txn_id, instrument_id, when, D(qty), D(price), total, total, f"buy-{txn_id}"),
    )


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Two holdings, built so every figure below is hand-checkable.

    ``ZFND``  an equity ETF, 30% Teilfreistellung, two lots at different prices
    ``ZSTK``  a share: no exemption, and the AKTIEN loss pot rather than SONSTIGE
    """
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "teilfreistellung_pct, tax_regime) VALUES (1, 'ZFND', 'Equity Fund', 'ETF', 'EUR', 1, "
        "'0.30', 'DE_INVESTMENT')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "teilfreistellung_pct, tax_regime) VALUES (2, 'ZSTK', 'A Company', 'STOCK', 'EUR', 0, "
        "'0.00', 'DE_INVESTMENT')"
    )
    # 100 units at 100, then 100 at 150. Now worth 200.
    _buy(conn, 1, 1, "2024-06-01", "100", "100")
    _buy(conn, 2, 1, "2025-06-01", "100", "150")
    # 50 shares at 80, now worth 60: a loss.
    _buy(conn, 3, 2, "2025-03-01", "50", "80")

    _price(conn, 1, "2026-06-01", "200")
    _price(conn, 2, "2026-06-01", "60")
    conn.commit()
    lots.rebuild_lots(conn)
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# What a sale realises
# ---------------------------------------------------------------------------


def test_selling_everything_realises_both_lots(db: sqlite3.Connection) -> None:
    """Proceeds 200x200 = 40,000; cost 10,000 + 15,000 = 25,000; gain 15,000."""
    result = scenario.sell(db, instrument="ZFND", as_of=TODAY, tax_year=YEAR)
    assert result.quantity == D(200)
    assert result.proceeds_eur == D("40000.00")
    assert result.cost_basis_eur == D("25000.00")
    assert result.gross_gain_eur == D("15000.00")
    # 30% Teilfreistellung on the whole gain.
    assert result.teilfreistellung_eur == D("4500.00")
    assert result.taxable_gain_eur == D("10500.00")


def test_a_partial_sale_takes_the_oldest_lots_first(db: sqlite3.Connection) -> None:
    """Not a detail. The oldest lot cost 100 and the newest 150, so selling
    'half' realises a much bigger gain than half the total."""
    result = scenario.sell(db, instrument="ZFND", quantity=D(100), as_of=TODAY, tax_year=YEAR)
    assert len(result.consumed) == 1
    assert result.consumed[0].open_date == date(2024, 6, 1)
    # 100 x (200 - 100) = 10,000, not half of 15,000.
    assert result.gross_gain_eur == D("10000.00")


def test_a_sale_spanning_two_lots_splits_them(db: sqlite3.Connection) -> None:
    result = scenario.sell(db, instrument="ZFND", quantity=D(150), as_of=TODAY, tax_year=YEAR)
    assert [lot.quantity for lot in result.consumed] == [D(100), D(50)]
    # 10,000 from the old lot, 50 x (200 - 150) = 2,500 from the new one.
    assert result.gross_gain_eur == D("12500.00")


def test_a_loss_is_reported_as_a_loss_in_its_own_pot(db: sqlite3.Connection) -> None:
    """A share's loss cannot offset a fund's gain, so the pot is part of the answer."""
    result = scenario.sell(db, instrument="ZSTK", as_of=TODAY, tax_year=YEAR)
    assert result.gross_gain_eur == D("-1000.00")
    assert result.taxable_gain_eur == D("-1000.00")
    assert result.pot == "AKTIEN"
    assert any("realised loss" in c for c in result.caveats)


def test_selling_more_than_is_held_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(scenario.ScenarioError, match="only"):
        scenario.sell(db, instrument="ZFND", quantity=D(500), as_of=TODAY)


def test_an_unknown_instrument_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(scenario.ScenarioError, match="no instrument"):
        scenario.sell(db, instrument="NOPE", as_of=TODAY)


def test_an_unpriced_holding_says_what_to_run(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM price WHERE instrument_id = 1")
    with pytest.raises(scenario.ScenarioError, match="fetch-prices"):
        scenario.sell(db, instrument="ZFND", as_of=TODAY)


def test_a_stale_price_is_reported_rather_than_hidden(db: sqlite3.Connection) -> None:
    result = scenario.sell(db, instrument="ZFND", as_of=date(2026, 6, 20), tax_year=YEAR)
    assert result.price_stale_days == 19
    assert any("19 day(s) old" in c for c in result.caveats)


# ---------------------------------------------------------------------------
# The marginal cost -- the number being asked for
# ---------------------------------------------------------------------------


def test_a_gain_inside_the_allowance_costs_nothing(db: sqlite3.Connection) -> None:
    result = scenario.sell(db, instrument="ZFND", quantity=D(10), as_of=TODAY, tax_year=YEAR)
    # 10 x (200 - 100) = 1,000 gross, 700 taxable after the exemption.
    assert result.taxable_gain_eur == D("700.00")
    assert result.allowance_remaining_before_eur == D("1000.00")
    assert result.allowance_remaining_after_eur == D("300.00")
    assert result.marginal_tax_eur == D("0.00")


def test_the_same_gain_costs_real_money_once_the_allowance_is_gone(
    db: sqlite3.Connection,
) -> None:
    """The reason a per-sale tax figure means nothing without the year."""
    result = scenario.sell(db, instrument="ZFND", as_of=TODAY, tax_year=YEAR)
    assert result.taxable_gain_eur == D("10500.00")
    assert result.allowance_remaining_after_eur == D("0.00")
    # (10,500 - 1,000) x 26.375%
    assert result.marginal_tax_eur == D("2505.63")
    assert result.net_proceeds_eur == D("37494.37")


def test_the_allowance_already_spent_makes_the_next_sale_dearer(
    db: sqlite3.Connection,
) -> None:
    """Same holding, same price, different answer -- because the year moved."""
    free = scenario.sell(db, instrument="ZFND", quantity=D(10), as_of=TODAY, tax_year=YEAR)

    # A distribution consumes the whole allowance first.
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, gross_amount_eur, fx_rate, source, confidence, "
        "flow_class, external_id) VALUES (1, 1, 'DIVIDEND', '2026-02-01', 'EUR', 0, "
        "'3000', '3000', '1', 'SYNTHESIZED', 'ESTIMATE', 'INCOME', 'div-1')"
    )
    db.commit()
    costly = scenario.sell(db, instrument="ZFND", quantity=D(10), as_of=TODAY, tax_year=YEAR)

    assert costly.taxable_gain_eur == free.taxable_gain_eur
    assert free.marginal_tax_eur == D("0.00")

    # EUR 184.62, not the 184.63 that 700 x 26.375% rounds to on its own. The
    # marginal figure is the difference between two independently rounded
    # yearly bills (474.75 - 290.13), which is what the owner will actually be
    # charged. Computing it as a standalone product would be a cent adrift from
    # the return it is supposed to predict.
    assert costly.marginal_tax_eur == D("184.62")
    assert costly.tax_after_eur - costly.tax_before_eur == D("184.62")


def test_an_out_of_scope_holding_computes_no_tax_and_says_so(
    db: sqlite3.Connection,
) -> None:
    """Silence about a figure is not the same as a figure of zero."""
    db.execute("UPDATE instrument SET tax_regime = 'OUT_OF_SCOPE' WHERE id = 1")
    result = scenario.sell(db, instrument="ZFND", as_of=TODAY, tax_year=YEAR)
    assert result.in_tax_scope is False
    assert result.taxable_gain_eur == D(0)
    assert result.marginal_tax_eur == D("0.00")
    assert any("not the same as owing none" in c for c in result.caveats)


# ---------------------------------------------------------------------------
# It must agree with the real thing
# ---------------------------------------------------------------------------


def test_the_scenario_matches_what_actually_booking_the_sale_produces(
    db: sqlite3.Connection,
) -> None:
    """The property that makes any of this worth reading.

    A scenario that disagrees with the disposal it models is worse than no
    scenario: it is a wrong number with the authority of a computed one.
    """
    predicted = scenario.sell(db, instrument="ZFND", quantity=D(150), as_of=TODAY, tax_year=YEAR)

    db.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, price_per_unit_local, gross_amount_local, gross_amount_eur, fx_rate, "
        "source, confidence, flow_class, external_id) "
        "VALUES (9, 1, 1, 'SELL', '2026-06-01', 'EUR', '150', '200', '30000', '30000', '1', "
        "'MANUAL', 'ACTUAL', 'INVESTMENT', 'sell-real')"
    )
    db.commit()
    lots.rebuild_lots(db)
    db.commit()

    actual = tax.compute_disposal_tax(db, tax_year=YEAR, person_id=1)
    assert sum((d.gross_gain_eur for d in actual), D(0)) == predicted.gross_gain_eur
    assert sum((d.taxable_gain_eur for d in actual), D(0)) == predicted.taxable_gain_eur
    assert sum((d.vap_offset_eur for d in actual), D(0)) == predicted.vap_offset_eur


def test_the_vorabpauschale_relief_is_applied_the_same_way(db: sqlite3.Connection) -> None:
    """The relief travels with the units in a scenario exactly as in a disposal."""
    db.execute("UPDATE lot SET vap_accumulated_eur = '300.00' WHERE open_date = '2024-06-01'")
    db.commit()

    result = scenario.sell(db, instrument="ZFND", quantity=D(50), as_of=TODAY, tax_year=YEAR)
    # Half of the oldest lot, so half of its accumulated relief.
    assert result.vap_offset_eur == D("150.00")
    # 50 x (200 - 100) = 5,000 gain, less 150 already taxed, less 30%.
    assert result.taxable_gain_eur == D("3395.00")
    assert any("not taxed a second time" in c for c in result.caveats)


def test_nothing_is_written(db: sqlite3.Connection) -> None:
    """Structural: a scenario that left rows behind would read as a decision."""
    before = {
        table: db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        for table in ("transaction_", "lot", "lot_disposal", "pending_entry", "vorabpauschale")
    }
    scenario.sell(db, instrument="ZFND", as_of=TODAY, tax_year=YEAR)
    scenario.harvest(db, tax_year=YEAR, as_of=TODAY)
    after = {
        table: db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in before
    }
    assert before == after


# ---------------------------------------------------------------------------
# Using up the allowance
# ---------------------------------------------------------------------------


def test_harvest_reports_the_headroom_and_what_could_fill_it(db: sqlite3.Connection) -> None:
    result = scenario.harvest(db, tax_year=YEAR, as_of=TODAY)
    assert result.allowance_cap_eur == D("1000.00")
    assert result.allowance_remaining_eur == D("1000.00")

    gains = {c.instrument for c in result.gains}
    losses = {c.instrument for c in result.losses}
    assert gains == {"ZFND"}
    assert losses == {"ZSTK"}


def test_harvest_separates_the_two_loss_pots(db: sqlite3.Connection) -> None:
    """A share loss cannot offset a fund gain, so lumping them would mislead."""
    result = scenario.harvest(db, tax_year=YEAR, as_of=TODAY)
    assert {c.pot for c in result.gains} == {"SONSTIGE"}
    assert {c.pot for c in result.losses} == {"AKTIEN"}


def test_harvest_says_when_the_allowance_is_already_gone(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, gross_amount_eur, fx_rate, source, confidence, "
        "flow_class, external_id) VALUES (1, 1, 'DIVIDEND', '2026-02-01', 'EUR', 0, "
        "'5000', '5000', '1', 'SYNTHESIZED', 'ESTIMATE', 'INCOME', 'div-1')"
    )
    db.commit()
    result = scenario.harvest(db, tax_year=YEAR, as_of=TODAY)
    assert result.allowance_remaining_eur == D("0.00")
    assert "already used up" in result.caveats[0]


def test_harvest_skips_a_holding_it_cannot_price_rather_than_showing_zero(
    db: sqlite3.Connection,
) -> None:
    """A candidate list with a silent hole is worse than a shorter one."""
    db.execute("DELETE FROM price WHERE instrument_id = 2")
    result = scenario.harvest(db, tax_year=YEAR, as_of=TODAY)
    assert {c.instrument for c in result.candidates} == {"ZFND"}


def test_harvest_refuses_to_recommend(db: sqlite3.Connection) -> None:
    """It knows the tax and nothing about what the owner wants to keep."""
    result = scenario.harvest(db, tax_year=YEAR, as_of=TODAY)
    assert any("not a recommendation" in c for c in result.caveats)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    api_app.dependency_overrides[get_db] = lambda: db
    api_app.dependency_overrides[get_writable_db] = lambda: db
    with TestClient(api_app) as test_client:
        yield test_client
    api_app.dependency_overrides.clear()


def test_the_sell_endpoint_answers_with_the_marginal_cost(client: TestClient) -> None:
    body = client.get("/api/scenario/sell", params={"instrument": "ZFND", "year": YEAR}).json()
    assert body["gross_gain_eur"] == 15000.0
    assert body["taxable_gain_eur"] == 10500.0
    assert body["marginal_tax_eur"] == 2505.63
    assert body["wrote_nothing"] is True
    assert len(body["fifo_consumes"]) == 2


def test_an_unknown_instrument_is_a_404_not_a_zero(client: TestClient) -> None:
    assert client.get("/api/scenario/sell", params={"instrument": "NOPE"}).status_code == 404


def test_a_missing_statutory_input_is_a_refusal(db: sqlite3.Connection, client: TestClient) -> None:
    """Same rule as /api/tax/*: 409, never a plausible-looking zero."""
    db.execute("UPDATE instrument SET teilfreistellung_pct = NULL WHERE id = 1")
    response = client.get("/api/scenario/sell", params={"instrument": "ZFND", "year": YEAR})
    assert response.status_code == 409


def test_the_harvest_endpoint_splits_gains_from_losses(client: TestClient) -> None:
    body = client.get("/api/scenario/harvest", params={"year": YEAR}).json()
    assert body["allowance"]["remaining_eur"] == 1000.0
    assert [g["instrument"] for g in body["gains"]] == ["ZFND"]
    assert [loss["instrument"] for loss in body["losses"]] == ["ZSTK"]
    assert body["caveats"]


def test_the_scenario_routes_take_the_read_only_connection(client: TestClient) -> None:
    """Write access is something a route asks for. These do not ask."""
    import inspect

    from portfolio_tracker.api import app as module

    for route in (module.scenario_sell, module.scenario_harvest):
        default = inspect.signature(route).parameters["conn"].default
        assert default.dependency is module.get_db


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def db_file(tmp_path, db: sqlite3.Connection):  # type: ignore[no-untyped-def]
    target = tmp_path / "p.db"
    on_disk = connect(target)
    db.backup(on_disk)
    on_disk.commit()
    on_disk.close()
    return target


def test_sell_prints_the_marginal_tax_and_the_lots(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        cli_app,
        ["scenario", "sell", "-i", "ZFND", "--year", str(YEAR),
         "--as-of", TODAY.isoformat(), "--db", str(db_file)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "TAX THIS SALE ADDS" in result.output
    assert "2,505.63" in result.output
    assert "FIFO would consume" in result.output
    assert "Nothing was written" in result.output


def test_sell_refuses_a_bad_quantity(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        cli_app,
        ["scenario", "sell", "-i", "ZFND", "--qty", "lots", "--db", str(db_file)],
    )
    assert result.exit_code == 1
    assert "must be a number" in result.output


def test_sell_exits_non_zero_on_an_unknown_instrument(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(cli_app, ["scenario", "sell", "-i", "NOPE", "--db", str(db_file)])
    assert result.exit_code == 1


def test_harvest_prints_the_headroom(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        cli_app,
        ["scenario", "harvest", "--year", str(YEAR), "--as-of", TODAY.isoformat(),
         "--db", str(db_file)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "still free" in result.output
    assert "1,000.00" in result.output
    assert "not a recommendation" in result.output
