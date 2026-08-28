"""The January charge: proposing it, booking it, and not paying tax twice.

The §18 InvStG arithmetic itself is v1's and is tested in ``test_tax.py``
against hand-computed fixtures. What is tested here is the chain that was
missing around it, and one link in that chain is worth more than the rest:

**A Vorabpauschale that is taxed and then not deducted from the eventual gain
means the same euros are taxed twice.** Both figures look right on their own,
nothing reconciles them, and the overpayment surfaces years later in a number
nobody can trace. ``test_the_same_euros_are_not_taxed_twice`` computes the tax
both ways and asserts on the difference.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from portfolio_tracker.cli import app as cli_app
from portfolio_tracker.core import doctor, inbox, lots, tax, vorabpauschale
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal
runner = CliRunner()

YEAR = 2025
CHARGE_DATE = date(2026, 1, 2)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    # An accumulating equity ETF: 30% Teilfreistellung, in scope, is_fund.
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "teilfreistellung_pct, tax_regime, distribution_policy) "
        "VALUES (1, 'ZACC', 'Accumulating World', 'ETF', 'EUR', 1, '0.30', "
        "'DE_INVESTMENT', 'ACC')"
    )
    conn.execute(
        # Replaced rather than inserted: the migration seeds the published
        # rates, and this fixture pins its own so the arithmetic is hand-checkable.
        "INSERT OR REPLACE INTO tax_constant (tax_year, basiszins, kest_rate) "
        "VALUES (?, '0.0255', '0.25')",
        (YEAR,),
    )
    # 100 units at EUR 100, bought before the tax year so no month pro-ration.
    conn.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, price_per_unit_local, gross_amount_local, gross_amount_eur, fx_rate, "
        "source, confidence, flow_class, external_id) "
        "VALUES (1, 1, 1, 'BUY', '2024-06-01', 'EUR', '100', '100', '10000', '10000', '1', "
        "'IMPORT', 'ACTUAL', 'INVESTMENT', 'buy-1')"
    )
    # The fund rose from 100 to 120 across the year: the charge is not capped.
    for when, close in (("2025-01-02", "100"), ("2025-12-30", "120"), ("2026-06-01", "130")):
        conn.execute(
            "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
            "VALUES (1, ?, ?, 'EUR', 'manual', '2026-01-01T00:00:00Z')",
            (when, close),
        )
    conn.commit()


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    _seed(conn)
    lots.rebuild_lots(conn)
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Computing and proposing
# ---------------------------------------------------------------------------


def test_the_charge_is_computed_and_dated_the_following_january(db: sqlite3.Connection) -> None:
    """A 2025 Vorabpauschale is 2026 income, charged on 2 January."""
    charges = vorabpauschale.compute(db, tax_year=YEAR)
    assert len(charges) == 1
    charge = charges[0]

    # Basisertrag = 100.00 x 100 units x 2.55% x 0.7 = 178.50, uncapped
    # because the fund rose by 2,000.
    assert charge.basisertrag_eur == D("178.50")
    assert charge.gross_eur == D("178.50")
    # Taxable after 30% Teilfreistellung.
    assert charge.taxable_eur == D("124.95")
    assert charge.entry_date == CHARGE_DATE


def test_a_fund_that_did_not_rise_owes_nothing(db: sqlite3.Connection) -> None:
    """The charge tops a fund up to a notional return. It cannot create one."""
    db.execute("UPDATE price SET close = '90' WHERE date = '2025-12-30'")
    charges = vorabpauschale.compute(db, tax_year=YEAR)
    assert charges[0].gross_eur == D(0)


def test_a_missing_basiszins_is_a_refusal_not_a_zero(db: sqlite3.Connection) -> None:
    """A Vorabpauschale of EUR 0.00 is a number somebody files."""
    db.execute("DELETE FROM tax_constant")
    with pytest.raises(tax.TaxDataMissing, match="Basiszins"):
        vorabpauschale.compute(db, tax_year=YEAR)


def test_proposing_stores_the_forecast_and_queues_it(db: sqlite3.Connection) -> None:
    result = vorabpauschale.propose(db, tax_year=YEAR)
    assert result.created == 1

    stored = db.execute("SELECT * FROM vorabpauschale WHERE tax_year = ?", (YEAR,)).fetchone()
    assert stored["status"] == "FORECAST"
    assert stored["taxable_eur"] == D("124.95")

    pending = db.execute("SELECT * FROM pending_entry WHERE kind = 'VORABPAUSCHALE'").fetchone()
    # The gross is queued, not the taxable figure: Teilfreistellung is the tax
    # engine's job and the basis relief deducts the gross.
    assert pending["amount_local"] == D("178.50")
    assert pending["proposed_date"] == CHARGE_DATE.isoformat()


def test_the_basis_says_no_cash_arrives(db: sqlite3.Connection) -> None:
    """The one thing a reader will not expect, printed beside the button."""
    charge = vorabpauschale.compute(db, tax_year=YEAR)[0]
    assert "No cash arrives" in charge.basis
    assert "not taxed again on sale" in charge.basis
    assert "section 18 InvStG" in charge.basis


def test_proposing_twice_queues_one_proposal(db: sqlite3.Connection) -> None:
    vorabpauschale.propose(db, tax_year=YEAR)
    again = vorabpauschale.propose(db, tax_year=YEAR)
    assert again.created == 0
    assert again.already_pending == 1
    assert (
        db.execute(
            "SELECT count(*) AS n FROM pending_entry WHERE kind = 'VORABPAUSCHALE'"
        ).fetchone()["n"]
        == 1
    )


def test_recomputing_replaces_the_stored_forecast_rather_than_adding_one(
    db: sqlite3.Connection,
) -> None:
    """A better figure for the same year is not an additional figure."""
    vorabpauschale.propose(db, tax_year=YEAR)
    db.execute("UPDATE price SET close = '110' WHERE date = '2025-12-30'")
    vorabpauschale.propose(db, tax_year=YEAR)
    rows = db.execute("SELECT * FROM vorabpauschale WHERE tax_year = ?", (YEAR,)).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------


def test_the_kind_is_now_bookable_through_the_shared_inbox(db: sqlite3.Connection) -> None:
    """It was the last kind that raised NotBookable, which was correct then."""
    vorabpauschale.propose(db, tax_year=YEAR)
    entry = inbox.listing(db)[0]
    assert entry.kind == "VORABPAUSCHALE"
    assert entry.bookable
    assert inbox.confirm(db, entry.id) > 0


def test_booking_records_income_that_moves_no_money(db: sqlite3.Connection) -> None:
    """The defining property. No euro entered the portfolio.

    If this were booked as a DIVIDEND it would credit cash that does not exist
    and report a return the portfolio did not earn.
    """
    from portfolio_tracker.core import cash

    # The cash picture before the charge exists at all. The purchase is
    # unfunded; that is expected and has nothing to do with this.
    before = cash.compute_flows(db)

    vorabpauschale.propose(db, tax_year=YEAR)
    txn_id = inbox.confirm(db, inbox.listing(db)[0].id)

    row = db.execute("SELECT * FROM transaction_ WHERE id = ?", (txn_id,)).fetchone()
    assert row["type"] == "VORABPAUSCHALE"
    assert row["flow_class"] == "INCOME"
    assert row["gross_amount_eur"] == D("178.50")

    # Booking it changes nothing about the money: no event, no balance, no
    # unfunded purchase. A DIVIDEND here would credit EUR 178.50 of cash
    # that does not exist and report a return the portfolio did not earn.
    after = cash.compute_flows(db)
    assert len(after.events) == len(before.events)
    assert after.total_unfunded == before.total_unfunded
    assert all(e.kind != "VORABPAUSCHALE" for e in after.events)


def test_booking_marks_the_stored_forecast_as_actual(db: sqlite3.Connection) -> None:
    vorabpauschale.propose(db, tax_year=YEAR)
    inbox.confirm(db, inbox.listing(db)[0].id)
    assert (
        db.execute("SELECT status FROM vorabpauschale WHERE tax_year = ?", (YEAR,)).fetchone()[
            "status"
        ]
        == "ACTUAL"
    )


def test_a_proposal_without_an_instrument_is_refused(db: sqlite3.Connection) -> None:
    """The relief attaches to a holding; there is nothing to book it against."""
    db.execute(
        "INSERT INTO pending_entry (id, kind, account_id, proposed_date, currency, "
        "amount_local, basis, derivation, status, created_at) VALUES (99, 'VORABPAUSCHALE', "
        "1, '2026-01-02', 'EUR', '10', 'x', '{}', 'PROPOSED', '2026-01-02T00:00:00Z')"
    )
    with pytest.raises(ValueError, match="no instrument"):
        vorabpauschale.confirm(db, 99)


# ---------------------------------------------------------------------------
# The relief -- the half that is invisible when wrong
# ---------------------------------------------------------------------------


def _book_and_rebuild(conn: sqlite3.Connection) -> None:
    vorabpauschale.propose(conn, tax_year=YEAR)
    inbox.confirm(conn, inbox.listing(conn)[0].id)
    conn.commit()
    lots.rebuild_lots(conn)
    conn.commit()


def test_the_relief_attaches_to_the_lot(db: sqlite3.Connection) -> None:
    _book_and_rebuild(db)
    lot = db.execute("SELECT * FROM lot").fetchone()
    assert lot["vap_accumulated_eur"] == D("178.50")


def test_a_charge_does_not_reach_holdings_bought_after_it(db: sqlite3.Connection) -> None:
    """Chronology, not aggregation. A lot bought in March cannot carry a
    January charge, and giving it one would create relief out of nothing."""
    db.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, price_per_unit_local, gross_amount_local, gross_amount_eur, fx_rate, "
        "source, confidence, flow_class, external_id) "
        "VALUES (2, 1, 1, 'BUY', '2026-03-01', 'EUR', '50', '125', '6250', '6250', '1', "
        "'IMPORT', 'ACTUAL', 'INVESTMENT', 'buy-2')"
    )
    _book_and_rebuild(db)

    by_date = {
        r["open_date"]: r["vap_accumulated_eur"]
        for r in db.execute("SELECT open_date, vap_accumulated_eur FROM lot")
    }
    assert by_date["2024-06-01"] == D("178.50")
    assert by_date["2026-03-01"] == D("0.00")


def test_the_charge_is_split_across_the_lots_open_when_it_was_raised(
    db: sqlite3.Connection,
) -> None:
    """Pro rata by quantity held -- a stated convention, and it must add up."""
    db.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, price_per_unit_local, gross_amount_local, gross_amount_eur, fx_rate, "
        "source, confidence, flow_class, external_id) "
        "VALUES (2, 1, 1, 'BUY', '2025-11-01', 'EUR', '300', '115', '34500', '34500', '1', "
        "'IMPORT', 'ACTUAL', 'INVESTMENT', 'buy-2')"
    )
    _book_and_rebuild(db)

    relief = [r["vap_accumulated_eur"] for r in db.execute("SELECT * FROM lot ORDER BY open_date")]
    total = db.execute(
        "SELECT total(gross_amount_eur) AS g FROM transaction_ WHERE type = 'VORABPAUSCHALE'"
    ).fetchone()["g"]
    # 100 units and 300 units: a quarter and three quarters.
    assert relief[0] * 3 == pytest.approx(relief[1], abs=D("0.02"))
    assert sum(relief, D(0)) == pytest.approx(D(str(total)), abs=D("0.02"))


def test_the_same_euros_are_not_taxed_twice(db: sqlite3.Connection) -> None:
    """The number this whole feature exists to protect.

    The fund is charged EUR 178.50 of Vorabpauschale in January and sold in
    June for a EUR 3,000 gain. Without the relief the owner pays tax on the
    full gain *and* on the deemed distribution -- the same EUR 178.50 twice.
    Both figures look correct in isolation, and nothing else reconciles them.
    """
    _book_and_rebuild(db)
    db.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, price_per_unit_local, gross_amount_local, gross_amount_eur, fx_rate, "
        "source, confidence, flow_class, external_id) "
        "VALUES (3, 1, 1, 'SELL', '2026-06-01', 'EUR', '100', '130', '13000', '13000', '1', "
        "'IMPORT', 'ACTUAL', 'INVESTMENT', 'sell-1')"
    )
    db.commit()
    lots.rebuild_lots(db)
    db.commit()

    disposal = db.execute("SELECT * FROM lot_disposal").fetchone()
    assert disposal["gross_gain_eur"] == D("3000.00")
    # The relief followed the units into the disposal.
    assert disposal["vap_offset_eur"] == D("178.50")

    taxed = tax.compute_disposal_tax(db, tax_year=2026, person_id=1)[0]
    # Gain 3,000 less the 178.50 already taxed, then 30% Teilfreistellung.
    assert taxed.vap_offset_eur == D("178.50")
    assert taxed.taxable_gain_eur == D("1975.05")

    without_relief = (D(3000) - D(0)) * (1 - D("0.30"))
    saved = without_relief - taxed.taxable_gain_eur
    # Exactly the deemed distribution, net of the same exemption. Taxed once.
    assert saved == D("178.50") * (1 - D("0.30"))


def test_selling_half_a_lot_takes_half_the_relief(db: sqlite3.Connection) -> None:
    """The rest stays on what is still held, rather than being spent early."""
    _book_and_rebuild(db)
    db.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, price_per_unit_local, gross_amount_local, gross_amount_eur, fx_rate, "
        "source, confidence, flow_class, external_id) "
        "VALUES (3, 1, 1, 'SELL', '2026-06-01', 'EUR', '50', '130', '6500', '6500', '1', "
        "'IMPORT', 'ACTUAL', 'INVESTMENT', 'sell-1')"
    )
    db.commit()
    lots.rebuild_lots(db)
    db.commit()

    assert db.execute("SELECT vap_offset_eur FROM lot_disposal").fetchone()["vap_offset_eur"] == D(
        "89.25"
    )
    assert db.execute("SELECT vap_accumulated_eur FROM lot").fetchone()["vap_accumulated_eur"] == D(
        "89.25"
    )


def test_a_charge_with_no_open_lot_is_reported_rather_than_silently_dropped(
    db: sqlite3.Connection,
) -> None:
    """Relief with nothing to attach to is an overpayment, and must be visible."""
    _book_and_rebuild(db)
    db.execute("UPDATE transaction_ SET trade_date = '2024-01-01' WHERE type = 'VORABPAUSCHALE'")
    db.commit()
    report = lots.rebuild_lots(db)
    assert report.orphan_vap
    assert "relief is lost" in report.orphan_vap[0]


# ---------------------------------------------------------------------------
# The tax base
# ---------------------------------------------------------------------------


def test_a_booked_charge_is_income_in_the_year_it_is_charged(db: sqlite3.Connection) -> None:
    """2025's Vorabpauschale is 2026 income, and consumes 2026's allowance."""
    _book_and_rebuild(db)

    assert vorabpauschale.taxable_income(db, tax_year=YEAR) == []
    booked = vorabpauschale.taxable_income(db, tax_year=YEAR + 1)
    assert booked == [("ZACC", D("124.95"))]


def test_the_allowance_counts_it_and_says_so_separately(db: sqlite3.Connection) -> None:
    """Omitting it understates income by exactly what the bank will withhold on."""
    _book_and_rebuild(db)
    state = tax.allowance_state(db, person_id=1, tax_year=YEAR + 1)
    assert state.vorabpauschale_eur == D("124.95")
    assert state.gross_gains_eur == D("124.95")


def test_an_unbooked_forecast_changes_no_tax_figure(db: sqlite3.Connection) -> None:
    """Proposing is not booking. Nothing reaches the tax base without a person."""
    vorabpauschale.propose(db, tax_year=YEAR)
    db.commit()
    state = tax.allowance_state(db, person_id=1, tax_year=YEAR + 1)
    assert state.vorabpauschale_eur == D(0)


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def test_clean_data_raises_no_errors(db: sqlite3.Connection) -> None:
    _book_and_rebuild(db)
    report = doctor.DoctorReport()
    doctor.check_vorabpauschale(db, report)
    assert not report.errors


def test_relief_that_went_missing_is_caught(db: sqlite3.Connection) -> None:
    """The failure nothing else would notice: two correct-looking figures and
    an overpayment that only appears years later."""
    _book_and_rebuild(db)
    db.execute("UPDATE lot SET vap_accumulated_eur = '0'")
    report = doctor.DoctorReport()
    doctor.check_vorabpauschale(db, report)
    assert "VAP_RELIEF_LOST" in report.codes
    assert "taxed a second time" in report.findings[0].message


def test_a_forecast_nobody_has_decided_on_is_a_warning(db: sqlite3.Connection) -> None:
    vorabpauschale.propose(db, tax_year=YEAR)
    db.commit()
    report = doctor.DoctorReport()
    doctor.check_vorabpauschale(db, report)
    assert "VAP_FORECAST_UNBOOKED" in report.codes
    assert not report.errors


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


def test_forecast_prints_the_charge_and_writes_nothing(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        cli_app, ["vorabpauschale", "forecast", "--year", str(YEAR), "--db", str(db_file)]
    )
    assert result.exit_code == 0, result.output
    assert "178.50" in result.output
    assert "No cash arrives" in result.output

    conn = connect(db_file, read_only=True)
    try:
        assert conn.execute("SELECT count(*) AS n FROM vorabpauschale").fetchone()["n"] == 0
    finally:
        conn.close()


def test_forecast_without_a_basiszins_exits_non_zero(db_file) -> None:  # type: ignore[no-untyped-def]
    conn = connect(db_file)
    conn.execute("DELETE FROM tax_constant")
    conn.commit()
    conn.close()
    result = runner.invoke(
        cli_app, ["vorabpauschale", "forecast", "--year", str(YEAR), "--db", str(db_file)]
    )
    assert result.exit_code == 1
    assert "Basiszins" in result.output


def test_propose_queues_and_points_at_the_review_command(db_file) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        cli_app, ["vorabpauschale", "propose", "--year", str(YEAR), "--db", str(db_file)]
    )
    assert result.exit_code == 0, result.output
    assert "1 proposal(s) queued" in result.output
    assert "pt flows confirm" in result.output
    assert "Anlagebedingungen" in result.output


def test_check_reports_the_relief_carried(db_file) -> None:  # type: ignore[no-untyped-def]
    conn = connect(db_file)
    _book_and_rebuild(conn)
    conn.close()
    result = runner.invoke(cli_app, ["vorabpauschale", "check", "--db", str(db_file)])
    assert result.exit_code == 0, result.output
    assert "178.50" in result.output
    assert "not taxed twice" in result.output


def test_list_distinguishes_a_forecast_from_a_booked_charge(db_file) -> None:  # type: ignore[no-untyped-def]
    conn = connect(db_file)
    vorabpauschale.propose(conn, tax_year=YEAR)
    conn.commit()
    conn.close()
    result = runner.invoke(
        cli_app, ["vorabpauschale", "list", "--year", str(YEAR), "--db", str(db_file)]
    )
    assert result.exit_code == 0, result.output
    assert "FORECAST" in result.output
