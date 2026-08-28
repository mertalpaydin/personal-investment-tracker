"""Deposit accounts, interest accrual, and statement import.

The statement parsers check each file against its own arithmetic rather than
trusting it. That is not defensive programming for its own sake: the real
German statement contains a transcription error (a net figure typed as `30054`
instead of `30.54`), and importing it silently would have put a wrong figure
into an interest total that feeds a tax return.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.core import accounts
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.importers import deposits

D = Decimal

GERMAN_STATEMENT = (
    "date, added capital, interest gain, tax, net\n"
    "01/09/2026, 0, 43.13, -, -\n"
    "01/08/2026, 0, 43.06, 11.36, 31.70\n"
    "01/05/2026, 0, 41.48, 10.94, 30054\n"
    "01/04/2026, 20186.08, 0, 0, 0\n"
)

TURKISH_STATEMENT = (
    "start date, capital, interest rate, return date, total return, tax, net return\n"
    "14/08/2026,176177,41.5,15/09/2026,182586.95,1121.74,181465.21\n"
)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.execute(
        """INSERT INTO account
           (id, person_id, name, kind, currency, interest_rate, interest_day_count,
            interest_frequency, interest_first_accrual, withholding_rate, rollover_policy)
           VALUES (1, 1, 'Deposit', 'TAGESGELD', 'EUR', '0.025', 'ACT/365',
                   'MONTHLY', '2026-04-01', '0.26375', 'CAPITALIZE')"""
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Day count
# ---------------------------------------------------------------------------


def test_act_365_counts_actual_days() -> None:
    assert accounts.day_count_fraction(date(2026, 1, 1), date(2026, 1, 31)) == D(30) / D(365)


def test_act_360_uses_a_360_day_year() -> None:
    result = accounts.day_count_fraction(date(2026, 1, 1), date(2026, 1, 31), "ACT/360")
    assert result == D(30) / D(360)


def test_30e_360_makes_every_month_a_twelfth() -> None:
    """February and August are the same length under this convention."""
    feb = accounts.day_count_fraction(date(2026, 2, 1), date(2026, 3, 1), "30E/360")
    aug = accounts.day_count_fraction(date(2026, 8, 1), date(2026, 9, 1), "30E/360")
    assert feb == aug == D(30) / D(360)


def test_a_backwards_range_accrues_nothing() -> None:
    assert accounts.day_count_fraction(date(2026, 5, 1), date(2026, 4, 1)) == D(0)


def test_an_unknown_convention_is_refused() -> None:
    with pytest.raises(accounts.DepositError, match="unknown day count"):
        accounts.day_count_fraction(date(2026, 1, 1), date(2026, 2, 1), "ACT/ACT")


# ---------------------------------------------------------------------------
# The German statement
# ---------------------------------------------------------------------------


def test_a_transcription_error_is_caught_and_corrected() -> None:
    """`30054` cannot be a net figure when gross is 41.48 and tax is 10.94."""
    result = deposits.parse_interest_statement(GERMAN_STATEMENT)
    payment = next(p for p in result.payments if p.on == date(2026, 5, 1))
    assert payment.net_was_corrected
    assert payment.net_interest == D("30.54")
    assert any("30054" in finding for finding in result.findings)


def test_the_correction_is_reported_not_silent() -> None:
    result = deposits.parse_interest_statement(GERMAN_STATEMENT)
    assert any("does not match" in finding for finding in result.findings)


def test_an_unsettled_payment_is_flagged() -> None:
    """Interest with no tax or net figure yet has accrued but not been paid."""
    result = deposits.parse_interest_statement(GERMAN_STATEMENT)
    assert any("accrued but unpaid" in finding for finding in result.findings)


def test_german_withholding_is_recognised() -> None:
    """26.375% is Kapitalertragsteuer plus Solidaritätszuschlag; a different
    rate would mean the account is not what it appears to be."""
    result = deposits.parse_interest_statement(GERMAN_STATEMENT)
    assert not any("not the expected 26.375%" in finding for finding in result.findings)


def test_an_unexpected_withholding_rate_is_flagged() -> None:
    odd = "date, added capital, interest gain, tax, net\n01/08/2026, 0, 100.00, 40.00, 60.00\n"
    assert any("not the expected" in f for f in deposits.parse_interest_statement(odd).findings)


def test_rows_are_returned_oldest_first() -> None:
    """The file is newest-first; the ledger needs the opposite."""
    payments = deposits.parse_interest_statement(GERMAN_STATEMENT).payments
    assert [p.on for p in payments] == sorted(p.on for p in payments)


def test_dates_are_day_first() -> None:
    """`01/09/2026` is 1 September. The broker export is month-first, so both
    conventions live in this codebase and neither may be guessed."""
    payments = deposits.parse_interest_statement(GERMAN_STATEMENT).payments
    assert date(2026, 9, 1) in {p.on for p in payments}


# ---------------------------------------------------------------------------
# The Turkish term deposit
# ---------------------------------------------------------------------------


def test_term_interest_is_verified_against_the_contract() -> None:
    """176,177 at 41.5% over 32 days on ACT/365 is 6,409.95 -- exactly what the
    statement says."""
    term = deposits.parse_term_deposit(TURKISH_STATEMENT).term
    assert term is not None
    assert term.gross_interest == D("6409.95")
    assert term.annual_rate == D("0.415")
    assert (term.maturity - term.start).days == 32


def test_net_interest_is_not_the_maturity_proceeds() -> None:
    """The statement's "net return" column includes the principal. Booking that
    as income would record the whole balance as interest."""
    term = deposits.parse_term_deposit(TURKISH_STATEMENT).term
    assert term is not None
    assert term.net_interest == D("5288.21")
    assert term.net_proceeds == D("181465.21")
    assert term.principal + term.net_interest == term.net_proceeds


def test_turkish_withholding_is_reported() -> None:
    findings = deposits.parse_term_deposit(TURKISH_STATEMENT).findings
    assert any("17.50%" in finding for finding in findings)


def test_a_contract_whose_arithmetic_does_not_hold_is_flagged() -> None:
    wrong = (
        "start date, capital, interest rate, return date, total return, tax, net return\n"
        "14/08/2026,100000,10,15/09/2026,150000,0,150000\n"
    )
    assert any("does not match" in f for f in deposits.parse_term_deposit(wrong).findings)


def test_a_maturity_before_the_start_is_rejected() -> None:
    backwards = (
        "start date, capital, interest rate, return date, total return, tax, net return\n"
        "15/09/2026,100,10,14/08/2026,110,0,110\n"
    )
    with pytest.raises(deposits.DepositParseError, match="not after start"):
        deposits.parse_term_deposit(backwards)


def test_the_shape_is_chosen_by_header(tmp_path) -> None:  # type: ignore[no-untyped-def]
    term_file = tmp_path / "term.csv"
    term_file.write_text(TURKISH_STATEMENT, encoding="utf-8")
    statement_file = tmp_path / "statement.csv"
    statement_file.write_text(GERMAN_STATEMENT, encoding="utf-8")

    assert deposits.load(term_file).term is not None
    assert deposits.load(statement_file).term is None


# ---------------------------------------------------------------------------
# Accrual
# ---------------------------------------------------------------------------


def open_deposit(conn: sqlite3.Connection, amount: str, when: str = "2026-04-01") -> None:
    instrument_id = accounts.ensure_cash_instrument(conn, 1)
    conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, flow_class) "
        "VALUES (1, ?, 'DEPOSIT', ?, 'EUR', ?, ?, 'EXTERNAL_IN')",
        (instrument_id, when, amount, amount),
    )
    conn.commit()


def test_interest_accrues_from_the_day_money_lands(db: sqlite3.Connection) -> None:
    """A bank value-dates a deposit to the day it arrives.

    Starting the day after quietly loses a day's interest on every opening
    balance -- worth EUR 1.46 on the first month of the real account.
    """
    open_deposit(db, "20186.08")
    report = accounts.accrue_interest(
        db, account_id=1, start=date(2026, 4, 1), end=date(2026, 4, 30)
    )
    assert report.days == 30
    # 20,186.08 x 2.5% x 30/365
    assert abs(report.gross_accrued - D("41.48")) < D("0.05")


def test_accrual_is_gross_of_withholding(db: sqlite3.Connection) -> None:
    """Tax is deducted when interest is paid, not as it accrues; netting it
    daily would understate the balance every day of the period."""
    open_deposit(db, "10000")
    report = accounts.accrue_interest(
        db, account_id=1, start=date(2026, 4, 1), end=date(2026, 4, 30)
    )
    expected = D("10000") * D("0.025") * D(30) / D(365)
    assert abs(report.gross_accrued - expected) < D("0.05")


def test_an_empty_account_accrues_nothing(db: sqlite3.Connection) -> None:
    report = accounts.accrue_interest(
        db, account_id=1, start=date(2026, 4, 1), end=date(2026, 4, 30)
    )
    assert report.days == 0
    assert report.gross_accrued == D(0)


def test_accrual_is_idempotent(db: sqlite3.Connection) -> None:
    open_deposit(db, "10000")
    first = accounts.accrue_interest(
        db, account_id=1, start=date(2026, 4, 1), end=date(2026, 4, 30)
    )
    second = accounts.accrue_interest(
        db, account_id=1, start=date(2026, 4, 1), end=date(2026, 4, 30)
    )
    assert first.gross_accrued == second.gross_accrued
    assert db.execute("SELECT count(*) n FROM interest_accrual").fetchone()["n"] == 30


def test_an_account_with_no_rate_refuses_to_accrue(db: sqlite3.Connection) -> None:
    db.execute("UPDATE account SET interest_rate = NULL WHERE id = 1")
    with pytest.raises(accounts.DepositError, match="no interest rate"):
        accounts.accrue_interest(db, account_id=1, start=date(2026, 4, 1), end=date(2026, 4, 2))


# ---------------------------------------------------------------------------
# The cash proxy
# ---------------------------------------------------------------------------


def test_a_deposit_becomes_a_position_not_a_special_case(db: sqlite3.Connection) -> None:
    """Carried as a synthetic instrument, so daily FX revaluation, allocation
    and the tax scope test all treat it as one more holding."""
    instrument_id = accounts.ensure_cash_instrument(db, 1)
    row = db.execute("SELECT * FROM instrument WHERE id = ?", (instrument_id,)).fetchone()
    assert row["is_cash_proxy"] == 1
    assert row["asset_class"] == "CASH"
    assert row["price_source"] == "synthetic"
    assert row["linked_account_id"] == 1


def test_deposit_interest_is_in_german_tax_scope(db: sqlite3.Connection) -> None:
    """Section 20(1)(7) EStG: fully taxable, no Teilfreistellung, and it
    consumes the same allowance as dividends and realised gains."""
    instrument_id = accounts.ensure_cash_instrument(db, 1)
    row = db.execute(
        "SELECT tax_regime, teilfreistellung_pct FROM instrument WHERE id = ?", (instrument_id,)
    ).fetchone()
    assert row["tax_regime"] == "DE_INVESTMENT"
    assert row["teilfreistellung_pct"] == D("0.00")


def test_the_cash_instrument_is_created_once(db: sqlite3.Connection) -> None:
    first = accounts.ensure_cash_instrument(db, 1)
    assert accounts.ensure_cash_instrument(db, 1) == first


def test_balance_follows_deposits_and_capitalised_interest(db: sqlite3.Connection) -> None:
    instrument_id = accounts.ensure_cash_instrument(db, 1)
    open_deposit(db, "1000")
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, flow_class) "
        "VALUES (1, ?, 'INTEREST', '2026-05-01', 'EUR', '2.05', '2.78', 'INCOME')",
        (instrument_id,),
    )
    db.commit()
    # Only the capitalised amount changes the balance, not the gross interest.
    assert accounts.balance_on(db, 1, date(2026, 5, 1)) == D("1002.05")


# ---------------------------------------------------------------------------
# Term projection
# ---------------------------------------------------------------------------


def test_a_term_deposit_projects_to_maturity(db: sqlite3.Connection) -> None:
    db.execute(
        """INSERT INTO account
           (id, person_id, name, kind, currency, interest_rate, interest_day_count,
            interest_frequency, interest_first_accrual, maturity_date, withholding_rate)
           VALUES (2, 1, 'Term', 'FESTGELD', 'TRY', '0.415', 'ACT/365',
                   'AT_MATURITY', '2026-08-14', '2026-09-15', '0.175')"""
    )
    instrument_id = accounts.ensure_cash_instrument(db, 2)
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, flow_class) "
        "VALUES (2, ?, 'DEPOSIT', '2026-08-14', 'TRY', '176177', '176177', 'EXTERNAL_IN')",
        (instrument_id,),
    )
    db.commit()

    projection = accounts.project_maturity(db, account_id=2)
    assert projection is not None
    gross, withheld, net = projection
    # Matches the real contract to the kurus.
    assert gross == D("6409.95")
    assert withheld == D("1121.74")
    assert net == D("5288.21")


def test_an_instant_access_account_has_no_maturity_projection(db: sqlite3.Connection) -> None:
    assert accounts.project_maturity(db, account_id=1) is None


# ---------------------------------------------------------------------------
# The balance an account joins with
# ---------------------------------------------------------------------------


def test_the_first_capital_row_is_not_booked_as_saving(db: sqlite3.Connection) -> None:
    """A Tagesgeld export opens with the whole balance on its first line.

    Booked as new money it made one April worth EUR 22,686 against a normal
    month of about EUR 1,500, and the projection then assumed that pace for
    fifteen years. The flow itself must stay -- the money really did enter the
    portfolio -- so only its status as *saving* changes.
    """
    result = deposits.book(db, deposits.parse_interest_statement(GERMAN_STATEMENT), account_id=1)
    assert result.opening_transfers == 1
    assert any("not as saving" in f for f in result.findings)

    row = db.execute(
        "SELECT flow_class, is_opening_transfer, gross_amount_local FROM transaction_ "
        "WHERE type = 'DEPOSIT'"
    ).fetchone()
    assert row["gross_amount_local"] == D("20186.08")
    assert row["is_opening_transfer"] == 1
    assert row["flow_class"] == "EXTERNAL_IN"

    # The balance still counts it, or the account would appear empty.
    assert accounts.balance_on(db, 1, date(2026, 4, 1)) == D("20186.08")


def test_a_later_capital_row_is_ordinary_saving(db: sqlite3.Connection) -> None:
    """Only the *first* one is the balance the account arrived with."""
    deposits.book(db, deposits.parse_interest_statement(GERMAN_STATEMENT), account_id=1)
    later = "date, added capital, interest gain, tax, net\n01/10/2026, 500, 0, 0, 0\n"
    result = deposits.book(db, deposits.parse_interest_statement(later), account_id=1)
    assert result.opening_transfers == 0

    row = db.execute(
        "SELECT is_opening_transfer FROM transaction_ WHERE trade_date = '2026-10-01'"
    ).fetchone()
    assert row["is_opening_transfer"] == 0


def test_a_term_deposit_principal_is_money_you_already_had(db: sqlite3.Connection) -> None:
    db.execute(
        """INSERT INTO account
           (id, person_id, name, kind, currency, interest_day_count, withholding_rate,
            rollover_policy)
           VALUES (2, 1, 'Term', 'FESTGELD', 'TRY', 'ACT/365', '0.175', 'PAYOUT')"""
    )
    result = deposits.book(db, deposits.parse_term_deposit(TURKISH_STATEMENT), account_id=2)
    assert result.opening_transfers == 1

    row = db.execute(
        "SELECT is_opening_transfer, gross_amount_local FROM transaction_ "
        "WHERE account_id = 2 AND type = 'DEPOSIT'"
    ).fetchone()
    assert row["gross_amount_local"] == D("176177.00")
    assert row["is_opening_transfer"] == 1
