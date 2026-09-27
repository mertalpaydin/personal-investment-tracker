"""Dividends and deposit interest reach the inbox on their own.

Both detectors existed before; neither ran unless someone called it by hand.
These tests pin the two things that made that unsafe to automate: a dividend
already typed in from a statement must not be proposed again, and the daily
update must create the proposals without booking anything.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.ingest import dividends
from portfolio_tracker.jobs import update

EX_DATE = date(2026, 9, 4)


def _seed(conn: sqlite3.Connection) -> None:
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, price_source, "
        "price_symbol) VALUES (1, 'ABECG', 'Alphabet C', 'STOCK', 'EUR', 'manual', 'ABEC.DE')"
    )
    conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, quantity, "
        "currency, price_per_unit_local, gross_amount_local, fx_rate, gross_amount_eur, "
        "flow_class) VALUES (1, 1, 'BUY', '2026-01-05', '38.5685', 'EUR', '150', "
        "'5785.28', '1', '5785.28', 'INVESTMENT')"
    )
    # Dividend discovery reads the quantity held from the daily position
    # series; one row before the ex-date is all it needs.
    conn.execute(
        "INSERT INTO position_daily (date, account_id, instrument_id, quantity, currency, "
        "price_local, price_asof, fx_rate, fx_asof, market_value_local, market_value_eur, "
        "cost_basis_local, cost_basis_eur, cost_basis_eur_at_spot, unrealized_eur, "
        "unrealized_local_eur, unrealized_fx_eur) VALUES ('2026-09-03', 1, 1, '38.5685', "
        "'EUR', '150', '2026-09-03', '1', '2026-09-03', '5785.28', '5785.28', '5785.28', "
        "'5785.28', '5785.28', '0', '0', '0')"
    )
    conn.commit()


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    _seed(conn)
    return conn


def one_dividend(symbol: str) -> list[tuple[date, Decimal]]:
    return [(EX_DATE, Decimal("0.192962"))]


def pending(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM pending_entry").fetchone()[0])


def test_a_new_dividend_is_proposed(db: sqlite3.Connection) -> None:
    report = dividends.propose(db, since=date(2026, 8, 1), fetcher=one_dividend)
    assert report.proposed == 1
    assert pending(db) == 1


def test_a_dividend_booked_from_a_statement_on_the_pay_date_is_not_proposed_again(
    db: sqlite3.Connection,
) -> None:
    """The statement says 15 Sep and 7.35; the feed says ex-date 4 Sep and 7.44."""
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "gross_amount_local, tax_withheld_local, fx_rate, gross_amount_eur, flow_class) "
        "VALUES (1, 1, 'DIVIDEND', '2026-09-15', 'EUR', '7.35', '1.88', '1', '7.35', 'INCOME')"
    )
    report = dividends.propose(db, since=date(2026, 8, 1), fetcher=one_dividend)
    assert report.proposed == 0
    assert report.skipped_known == 1
    assert pending(db) == 0


def test_a_rejected_proposal_is_not_raised_again_at_a_different_amount(
    db: sqlite3.Connection,
) -> None:
    dividends.propose(db, since=date(2026, 8, 1), fetcher=one_dividend)
    db.execute("UPDATE pending_entry SET status = 'REJECTED'")

    def corrected(symbol: str) -> list[tuple[date, Decimal]]:
        return [(EX_DATE, Decimal("0.2"))]

    report = dividends.propose(db, since=date(2026, 8, 1), fetcher=corrected)
    assert report.proposed == 0
    assert pending(db) == 1


def test_the_daily_update_proposes_income_and_books_nothing(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    conn = connect(path)
    _seed(conn)
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency, interest_rate, "
        "interest_day_count, interest_frequency, interest_first_accrual, withholding_rate, "
        "maturity_date, rollover_policy) VALUES (2, 1, 'Term', 'FESTGELD', 'EUR', '0.03', "
        "'ACT/365', 'AT_MATURITY', '2026-08-01', '0.26375', '2026-09-01', 'CAPITALIZE')"
    )
    conn.execute(
        "INSERT INTO transaction_ (account_id, type, trade_date, currency, quantity, "
        "gross_amount_local, fx_rate, gross_amount_eur, flow_class) "
        "VALUES (2, 'DEPOSIT', '2026-08-01', 'EUR', '10000', '10000', '1', '10000', 'EXTERNAL_IN')"
    )
    conn.commit()
    before = conn.execute("SELECT count(*) FROM transaction_").fetchone()[0]
    conn.close()

    outcome = update.run_update(
        path, trigger="SCHEDULED", end=date(2026, 9, 20), dividend_fetcher=one_dividend
    )

    conn = connect(path)
    kinds = sorted(r[0] for r in conn.execute("SELECT kind FROM pending_entry"))
    assert kinds == ["DIVIDEND", "INTEREST"]
    assert outcome.proposed == 2
    assert conn.execute("SELECT count(*) FROM transaction_").fetchone()[0] == before
    conn.close()


def test_a_second_update_does_not_duplicate_the_proposals(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    conn = connect(path)
    _seed(conn)
    conn.close()
    update.run_update(
        path, trigger="SCHEDULED", end=date(2026, 9, 20), dividend_fetcher=one_dividend
    )
    second = update.run_update(
        path, trigger="SCHEDULED", end=date(2026, 9, 21), dividend_fetcher=one_dividend
    )
    assert second.proposed == 0
    conn = connect(path)
    assert pending(conn) == 1
    conn.close()
