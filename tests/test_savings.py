"""New money, as opposed to money that moved.

Every test here guards a way the savings figure could be flattering rather
than wrong-looking:

``test_a_transfer_between_your_own_accounts_is_not_saving``
    The same money counted twice, once as a withdrawal and once as a deposit.

``test_a_reinvested_dividend_is_a_return_not_a_contribution``
    The rule the whole module exists for. Counting income as a contribution
    understates the return *and* overstates the discipline, both in the
    direction that feels good.

``test_the_three_parts_add_up_to_the_portfolio``
    If they do not, some movement was classified as something the sum does not
    account for, and every figure above it is suspect.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_tracker.core import savings
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

AS_OF = date(2026, 6, 30)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    # Both inside the perimeter: money moving between them is not new.
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency, in_perimeter) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR', 1)"
    )
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency, in_perimeter) "
        "VALUES (2, 1, 'Savings', 'TAGESGELD', 'EUR', 1)"
    )
    # Outside it: a salary account tracked for its balance only.
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency, in_perimeter) "
        "VALUES (3, 1, 'Salary', 'CASH', 'EUR', 0)"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, price_source) "
        "VALUES (1, 'ZGEQ', 'World Equity', 'ETF', 'EUR', 'manual')"
    )
    conn.commit()
    return conn


def _txn(
    conn: sqlite3.Connection,
    *,
    account: int,
    kind: str,
    when: str,
    amount: str,
    flow: str,
    external: str | None = None,
    instrument: int | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "gross_amount_local, gross_amount_eur, flow_class, source, external_id) "
        "VALUES (?, ?, ?, ?, 'EUR', ?, ?, ?, 'MANUAL', ?)",
        (account, instrument, kind, when, amount, amount, flow, external),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _value(conn: sqlite3.Connection, when: str, value: str) -> None:
    conn.execute(
        "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
        "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
        "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
        "completeness, drawdown_pct) VALUES (?, 1, ?, ?, ?, '1', '100', '0', '0', '0', "
        "?, '0', '0', 'FULL', '0')",
        (when, value, value, value, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# What counts
# ---------------------------------------------------------------------------


def test_money_arriving_from_outside_is_saving(db: sqlite3.Connection) -> None:
    _txn(db, account=1, kind="DEPOSIT", when="2026-01-05", amount="1000", flow="EXTERNAL_IN")
    savings.classify(db, person_id=1)
    row = db.execute("SELECT is_new_savings, flow_class FROM transaction_").fetchone()
    assert row["flow_class"] == "EXTERNAL_IN"
    assert row["is_new_savings"] == 1


def test_a_transfer_between_your_own_accounts_is_not_saving(db: sqlite3.Connection) -> None:
    """One movement showing up as two rows.

    Taken at face value it is counted as both a withdrawal and a contribution,
    and unit accounting issues units against the second -- so the return the
    money goes on to earn is attributed to a contribution instead.
    """
    _txn(db, account=2, kind="WITHDRAWAL", when="2026-02-10", amount="5000", flow="EXTERNAL_OUT")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-12", amount="5000", flow="EXTERNAL_IN")

    report = savings.classify(db, person_id=1)
    assert report.paired == 1

    classes = [r["flow_class"] for r in db.execute("SELECT flow_class FROM transaction_")]
    assert classes == ["INTERNAL_TRANSFER", "INTERNAL_TRANSFER"]
    assert all(
        r["is_new_savings"] == 0 for r in db.execute("SELECT is_new_savings FROM transaction_")
    )


def test_a_reinvested_dividend_is_a_return_not_a_contribution(db: sqlite3.Connection) -> None:
    """The single most important rule in this module.

    Counting it as saving understates the return and overstates the discipline
    at the same time, both in the flattering direction.
    """
    _txn(
        db,
        account=1,
        kind="DIVIDEND",
        when="2026-03-01",
        amount="250",
        flow="INCOME",
        instrument=1,
    )
    _txn(
        db, account=1, kind="BUY", when="2026-03-02", amount="250", flow="INVESTMENT", instrument=1
    )
    savings.classify(db, person_id=1)

    rows = {
        r["type"]: r
        for r in db.execute("SELECT type, flow_class, is_new_savings FROM transaction_")
    }
    assert rows["DIVIDEND"]["flow_class"] == "INCOME"
    assert rows["DIVIDEND"]["is_new_savings"] == 0
    assert rows["BUY"]["flow_class"] == "INVESTMENT"
    assert rows["BUY"]["is_new_savings"] == 0


def test_selling_and_holding_the_cash_is_not_saving(db: sqlite3.Connection) -> None:
    _txn(
        db,
        account=1,
        kind="SELL",
        when="2026-04-01",
        amount="8000",
        flow="INVESTMENT",
        instrument=1,
    )
    savings.classify(db, person_id=1)
    row = db.execute("SELECT flow_class, is_new_savings FROM transaction_").fetchone()
    assert (row["flow_class"], row["is_new_savings"]) == ("INVESTMENT", 0)


def test_a_deposit_from_outside_the_perimeter_is_still_new_money(
    db: sqlite3.Connection,
) -> None:
    """The salary account is outside, so money leaving it is genuinely new.

    It must not be paired with the deposit it funds, or a real contribution
    would vanish.
    """
    _txn(db, account=3, kind="WITHDRAWAL", when="2026-05-01", amount="2000", flow="EXTERNAL_OUT")
    _txn(db, account=1, kind="DEPOSIT", when="2026-05-01", amount="2000", flow="EXTERNAL_IN")
    report = savings.classify(db, person_id=1)

    assert report.paired == 0, "an account outside the perimeter is not one of yours"
    deposit = db.execute("SELECT is_new_savings FROM transaction_ WHERE account_id = 1").fetchone()
    assert deposit["is_new_savings"] == 1


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def test_a_transfer_that_takes_two_days_is_still_one_movement(db: sqlite3.Connection) -> None:
    _txn(db, account=2, kind="WITHDRAWAL", when="2026-02-10", amount="5000", flow="EXTERNAL_OUT")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-13", amount="5000", flow="EXTERNAL_IN")
    assert savings.classify(db, person_id=1).paired == 1


def test_a_deposit_a_week_later_is_not_the_same_movement(db: sqlite3.Connection) -> None:
    _txn(db, account=2, kind="WITHDRAWAL", when="2026-02-10", amount="5000", flow="EXTERNAL_OUT")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-20", amount="5000", flow="EXTERNAL_IN")
    assert savings.classify(db, person_id=1).paired == 0


def test_a_different_amount_is_not_the_same_movement(db: sqlite3.Connection) -> None:
    _txn(db, account=2, kind="WITHDRAWAL", when="2026-02-10", amount="5000", flow="EXTERNAL_OUT")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-11", amount="5100", flow="EXTERNAL_IN")
    assert savings.classify(db, person_id=1).paired == 0


def test_two_identical_deposits_are_reported_rather_than_guessed(
    db: sqlite3.Connection,
) -> None:
    """Guessing would erase a real contribution or invent one, invisibly."""
    _txn(db, account=2, kind="WITHDRAWAL", when="2026-02-10", amount="5000", flow="EXTERNAL_OUT")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-10", amount="5000", flow="EXTERNAL_IN")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-11", amount="5000", flow="EXTERNAL_IN")

    report = savings.classify(db, person_id=1)
    assert report.paired == 0
    assert len(report.ambiguous) == 1
    assert not report.ok


def test_pairing_is_idempotent(db: sqlite3.Connection) -> None:
    """A rebuild runs this every time; it must not unpick its own work."""
    _txn(db, account=2, kind="WITHDRAWAL", when="2026-02-10", amount="5000", flow="EXTERNAL_OUT")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-12", amount="5000", flow="EXTERNAL_IN")

    savings.classify(db, person_id=1)
    savings.classify(db, person_id=1)
    savings.classify(db, person_id=1)

    assert db.execute("SELECT count(*) c FROM internal_transfer_link").fetchone()["c"] == 1
    classes = {r["flow_class"] for r in db.execute("SELECT flow_class FROM transaction_")}
    assert classes == {"INTERNAL_TRANSFER"}


# ---------------------------------------------------------------------------
# The person's decision
# ---------------------------------------------------------------------------


def test_a_persons_classification_survives_reclassification(db: sqlite3.Connection) -> None:
    """A bonus that looks like a transfer is still a bonus if you say so."""
    txn = _txn(db, account=1, kind="DEPOSIT", when="2026-06-01", amount="3000", flow="EXTERNAL_IN")
    savings.set_flow_class(db, txn, flow_class="EXTERNAL_IN", savings_label="bonus")
    db.commit()

    savings.classify(db, person_id=1)
    row = db.execute(
        "SELECT flow_class, savings_label, flow_class_source FROM transaction_"
    ).fetchone()
    assert (row["flow_class"], row["savings_label"], row["flow_class_source"]) == (
        "EXTERNAL_IN",
        "bonus",
        "USER",
    )


def test_calling_something_a_transfer_stops_it_counting_as_saving(
    db: sqlite3.Connection,
) -> None:
    txn = _txn(db, account=1, kind="DEPOSIT", when="2026-06-01", amount="3000", flow="EXTERNAL_IN")
    savings.set_flow_class(db, txn, flow_class="INTERNAL_TRANSFER")
    db.commit()
    savings.classify(db, person_id=1)
    row = db.execute("SELECT is_new_savings FROM transaction_").fetchone()
    assert row["is_new_savings"] == 0


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


def test_the_three_parts_add_up_to_the_portfolio(db: sqlite3.Connection) -> None:
    """The identity the whole screen rests on.

    Money in plus market growth equals the value. Income is outside the sum:
    it is paid into cash, which the headline value excludes.
    """
    _txn(db, account=1, kind="DEPOSIT", when="2026-01-15", amount="10000", flow="EXTERNAL_IN")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-15", amount="5000", flow="EXTERNAL_IN")
    _txn(
        db, account=1, kind="DIVIDEND", when="2026-02-20", amount="120", flow="INCOME", instrument=1
    )
    _value(db, "2026-01-31", "10200")
    _value(db, "2026-02-28", "15600")

    savings.classify(db, person_id=1)
    savings.build_periods(db, person_id=1)
    summary = savings.summarise(db, person_id=1)

    assert summary.cumulative_eur == Decimal("15000.00")
    assert summary.reconciles == summary.closing_value_eur
    assert summary.reconciliation_gap == Decimal("0.00")
    # Reported, never added in.
    assert summary.income_received_eur == Decimal("120.00")


def test_a_transfer_does_not_inflate_the_savings_figure(db: sqlite3.Connection) -> None:
    """The bug this module exists to prevent, measured end to end."""
    _txn(db, account=1, kind="DEPOSIT", when="2026-01-15", amount="10000", flow="EXTERNAL_IN")
    _txn(db, account=2, kind="WITHDRAWAL", when="2026-02-10", amount="4000", flow="EXTERNAL_OUT")
    _txn(db, account=1, kind="DEPOSIT", when="2026-02-11", amount="4000", flow="EXTERNAL_IN")
    _value(db, "2026-01-31", "10000")
    _value(db, "2026-02-28", "14000")

    savings.classify(db, person_id=1)
    savings.build_periods(db, person_id=1)
    summary = savings.summarise(db, person_id=1)

    # 10,000 of new money, not 14,000: the other 4,000 was already yours.
    assert summary.cumulative_eur == Decimal("10000.00")


def test_monthly_figures_are_reported_per_month(db: sqlite3.Connection) -> None:
    for month in range(1, 4):
        _txn(
            db,
            account=1,
            kind="DEPOSIT",
            when=f"2026-0{month}-10",
            amount="1000",
            flow="EXTERNAL_IN",
        )
        _value(db, f"2026-0{month}-28", str(1000 * month))
    savings.classify(db, person_id=1)
    savings.build_periods(db, person_id=1)
    summary = savings.summarise(db, person_id=1)

    assert [p.period for p in summary.periods] == ["2026-01", "2026-02", "2026-03"]
    assert [p.net_new_money_eur for p in summary.periods] == [Decimal("1000.00")] * 3
    assert summary.periods[-1].cumulative_net_eur == Decimal("3000.00")


def test_no_history_is_said_rather_than_shown_as_zero(db: sqlite3.Connection) -> None:
    summary = savings.summarise(db, person_id=1)
    assert summary.cumulative_eur == Decimal(0)
    assert any("No savings history" in c for c in summary.caveats)


def test_building_periods_is_idempotent(db: sqlite3.Connection) -> None:
    _txn(db, account=1, kind="DEPOSIT", when="2026-01-15", amount="10000", flow="EXTERNAL_IN")
    _value(db, "2026-01-31", "10000")
    savings.classify(db, person_id=1)
    first = savings.build_periods(db, person_id=1)
    second = savings.build_periods(db, person_id=1)
    assert first == second
    assert db.execute("SELECT count(*) c FROM savings_period").fetchone()["c"] == first


def test_a_withdrawal_to_spend_reduces_net_new_money(db: sqlite3.Connection) -> None:
    _txn(db, account=1, kind="DEPOSIT", when="2026-01-15", amount="10000", flow="EXTERNAL_IN")
    _txn(db, account=1, kind="WITHDRAWAL", when="2026-01-20", amount="2500", flow="EXTERNAL_OUT")
    _value(db, "2026-01-31", "7500")
    savings.classify(db, person_id=1)
    savings.build_periods(db, person_id=1)
    summary = savings.summarise(db, person_id=1)
    assert summary.cumulative_eur == Decimal("7500.00")


def test_the_measured_rate_covers_a_year(db: sqlite3.Connection) -> None:
    """The projection defaults to this, so the window has to be stated."""
    start = date(2025, 7, 1)
    for month in range(14):
        when = date(
            start.year + (start.month + month - 1) // 12, (start.month + month - 1) % 12 + 1, 5
        )
        _txn(
            db,
            account=1,
            kind="DEPOSIT",
            when=when.isoformat(),
            amount="500",
            flow="EXTERNAL_IN",
        )
        _value(db, (when + timedelta(days=20)).isoformat(), str(500 * (month + 1)))
    savings.classify(db, person_id=1)
    savings.build_periods(db, person_id=1)
    summary = savings.summarise(db, person_id=1)

    assert summary.trailing_12m_eur == Decimal("6000.00")
    assert summary.average_monthly_12m_eur == Decimal("500.00")
