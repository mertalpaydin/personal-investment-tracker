"""Interest earned but not yet credited, and how it reaches the ledger.

The rule under test is the one the whole review inbox exists for, applied to
the one place it is easiest to skip: an accrual is a *model*, and a model that
writes itself into the ledger becomes indistinguishable from a bank's own
figure the moment it lands there.

The second theme is ``flow_class``. Interest booked as a contribution makes
unit accounting issue units for it, which cancels exactly the return the
interest represents -- the tool would report the owner as having saved his own
interest. One column value, and every return figure depends on it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.core import accounts, inbox, interest
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal
START = date(2026, 1, 1)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.commit()
    return conn


def account(
    conn: sqlite3.Connection,
    *,
    kind: str = "TAGESGELD",
    rate: str = "0.025",
    frequency: str = "MONTHLY",
    withholding: str = "0.26375",
    rollover: str = "CAPITALIZE",
    maturity: str | None = None,
    currency: str = "EUR",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO account
            (person_id, name, kind, currency, interest_rate, interest_day_count,
             interest_frequency, interest_first_accrual, withholding_rate,
             maturity_date, rollover_policy)
        VALUES (1, 'Deposit', ?, ?, ?, 'ACT/365', ?, ?, ?, ?, ?)
        """,
        (kind, currency, rate, frequency, START.isoformat(), withholding, maturity, rollover),
    )
    account_id = int(cursor.lastrowid or 0)
    accounts.ensure_cash_instrument(conn, account_id)
    conn.execute(
        "INSERT INTO transaction_ (account_id, type, trade_date, currency, quantity, "
        "gross_amount_local, gross_amount_eur, fx_rate, flow_class) "
        "VALUES (?, 'DEPOSIT', ?, ?, 10000, 10000, 10000, 1, 'EXTERNAL_IN')",
        (account_id, START.isoformat(), currency),
    )
    conn.commit()
    return account_id


def accrued(conn: sqlite3.Connection, account_id: int, *, to: date) -> None:
    accounts.accrue_interest(conn, account_id=account_id, start=START, end=to)
    conn.commit()


# ---------------------------------------------------------------------------
# What is due
# ---------------------------------------------------------------------------


def test_a_monthly_schedule_proposes_every_missed_credit(db: sqlite3.Connection) -> None:
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 4, 15))
    due = interest.due_payments(db, account_id=account_id, as_of=date(2026, 4, 15))
    assert [d.isoformat() for d, *_ in due] == ["2026-02-01", "2026-03-01", "2026-04-01"]


def test_a_proposal_is_net_of_withholding_taken_at_source(db: sqlite3.Connection) -> None:
    """Proposing the gross figure would suggest a credit the balance never gets."""
    account_id = account(db, withholding="0.26375")
    accrued(db, account_id, to=date(2026, 2, 2))
    _when, gross, withheld, net = interest.due_payments(
        db, account_id=account_id, as_of=date(2026, 2, 2)
    )[0]
    assert gross > D(0)
    assert withheld == (gross * D("0.26375")).quantize(D("0.01"))
    assert net == gross - withheld


def test_an_already_booked_payment_is_not_proposed_again(db: sqlite3.Connection) -> None:
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 3, 15))
    db.execute(
        "INSERT INTO transaction_ (account_id, type, trade_date, currency, quantity, "
        "gross_amount_local, flow_class) VALUES (?, 'INTEREST', '2026-02-01', 'EUR', 15, 20, "
        "'INCOME')",
        (account_id,),
    )
    db.commit()
    due = interest.due_payments(db, account_id=account_id, as_of=date(2026, 3, 15))
    assert [d.isoformat() for d, *_ in due] == ["2026-03-01"]


def test_an_account_with_no_rate_proposes_nothing(db: sqlite3.Connection) -> None:
    account_id = account(db)
    db.execute("UPDATE account SET interest_rate = NULL WHERE id = ?", (account_id,))
    db.commit()
    assert interest.due_payments(db, account_id=account_id, as_of=date(2026, 6, 1)) == []


def test_a_term_deposit_proposes_nothing_before_it_matures(db: sqlite3.Connection) -> None:
    """Proposing early would book income that has not been earned."""
    account_id = account(
        db, kind="FESTGELD", frequency="AT_MATURITY", maturity="2026-09-15", rollover="PAYOUT"
    )
    assert interest.due_payments(db, account_id=account_id, as_of=date(2026, 8, 1)) == []
    due = interest.due_payments(db, account_id=account_id, as_of=date(2026, 9, 20))
    assert len(due) == 1
    assert due[0][0] == date(2026, 9, 15)


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (date(2026, 1, 31), date(2026, 2, 28)),  # clamps into a short month
        (date(2028, 1, 31), date(2028, 2, 29)),  # and knows about leap years
        (date(2026, 12, 15), date(2027, 1, 15)),  # crosses the year boundary
    ],
)
def test_the_schedule_stays_on_the_calendar(start: date, expected: date) -> None:
    assert interest.next_payment_date(start, "MONTHLY") == expected


def test_a_maturity_only_account_has_no_schedule(db: sqlite3.Connection) -> None:
    assert interest.next_payment_date(date(2026, 1, 1), "AT_MATURITY") is None


# ---------------------------------------------------------------------------
# Nothing reaches the ledger without a decision
# ---------------------------------------------------------------------------


def test_proposing_writes_no_transaction(db: sqlite3.Connection) -> None:
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 4, 15))
    created = interest.propose(db, account_id=account_id, as_of=date(2026, 4, 15))
    db.commit()
    assert created == 3
    assert (
        db.execute("SELECT count(*) FROM transaction_ WHERE type = 'INTEREST'").fetchone()[0] == 0
    )


def test_proposing_twice_queues_nothing_new(db: sqlite3.Connection) -> None:
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 4, 15))
    interest.propose(db, account_id=account_id, as_of=date(2026, 4, 15))
    db.commit()
    assert interest.propose(db, account_id=account_id, as_of=date(2026, 4, 15)) == 0


def test_a_confirmed_payment_is_income_not_a_contribution(db: sqlite3.Connection) -> None:
    """The column value every return figure depends on."""
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 2, 2))
    interest.propose(db, account_id=account_id, as_of=date(2026, 2, 2))
    db.commit()
    pending_id = db.execute("SELECT id FROM pending_entry").fetchone()["id"]

    txn_id = interest.confirm(db, int(pending_id))
    row = db.execute("SELECT * FROM transaction_ WHERE id = ?", (txn_id,)).fetchone()
    assert row["flow_class"] == "INCOME"
    assert row["type"] == "INTEREST"
    assert row["confidence"] == "ESTIMATE"  # nobody has checked it against a statement


def test_confirming_at_the_banks_figure_records_it_as_a_fact(db: sqlite3.Connection) -> None:
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 2, 2))
    interest.propose(db, account_id=account_id, as_of=date(2026, 2, 2))
    db.commit()
    pending_id = int(db.execute("SELECT id FROM pending_entry").fetchone()["id"])

    txn_id = interest.confirm(db, pending_id, gross_local=D("21.37"), tax_local=D("5.64"))
    row = db.execute("SELECT * FROM transaction_ WHERE id = ?", (txn_id,)).fetchone()
    assert row["gross_amount_local"] == D("21.37")
    assert row["tax_withheld_local"] == D("5.64")
    assert row["confidence"] == "ACTUAL"


def test_capitalised_interest_raises_the_balance_and_paid_out_does_not(
    db: sqlite3.Connection,
) -> None:
    """Crediting a balance for money that was sent elsewhere overstates it."""
    for rollover, expects_units in (("CAPITALIZE", True), ("PAYOUT", False)):
        conn = connect(":memory:")
        M.migrate(conn)
        conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
        conn.commit()
        account_id = account(conn, rollover=rollover)
        accrued(conn, account_id, to=date(2026, 2, 2))
        interest.propose(conn, account_id=account_id, as_of=date(2026, 2, 2))
        conn.commit()
        pending_id = int(conn.execute("SELECT id FROM pending_entry").fetchone()["id"])
        txn_id = interest.confirm(conn, pending_id)
        quantity = conn.execute(
            "SELECT quantity FROM transaction_ WHERE id = ?", (txn_id,)
        ).fetchone()["quantity"]
        assert (quantity > D(0)) is expects_units, rollover
        conn.close()


def test_withholding_greater_than_the_gross_is_refused(db: sqlite3.Connection) -> None:
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 2, 2))
    interest.propose(db, account_id=account_id, as_of=date(2026, 2, 2))
    db.commit()
    pending_id = int(db.execute("SELECT id FROM pending_entry").fetchone()["id"])
    with pytest.raises(ValueError, match="exceeds the gross"):
        interest.confirm(db, pending_id, gross_local=D("10"), tax_local=D("11"))


def test_confirming_twice_is_refused(db: sqlite3.Connection) -> None:
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 2, 2))
    interest.propose(db, account_id=account_id, as_of=date(2026, 2, 2))
    db.commit()
    pending_id = int(db.execute("SELECT id FROM pending_entry").fetchone()["id"])
    interest.confirm(db, pending_id)
    with pytest.raises(LookupError):
        interest.confirm(db, pending_id)


def test_the_shared_inbox_routes_interest_too(db: sqlite3.Connection) -> None:
    """One place decides what confirming means, whatever the front end."""
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 2, 2))
    interest.propose(db, account_id=account_id, as_of=date(2026, 2, 2))
    db.commit()
    entry = inbox.listing(db)[0]
    assert entry.kind == "INTEREST"
    assert entry.bookable
    txn_id = inbox.confirm(db, entry.id)
    assert (
        db.execute("SELECT flow_class FROM transaction_ WHERE id = ?", (txn_id,)).fetchone()[
            "flow_class"
        ]
        == "INCOME"
    )


# ---------------------------------------------------------------------------
# Rate drift
# ---------------------------------------------------------------------------


def test_a_bank_figure_far_from_the_model_is_measurable(db: sqlite3.Connection) -> None:
    """Banks move instant-access rates without announcing it twice."""
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 2, 2))
    interest.propose(db, account_id=account_id, as_of=date(2026, 2, 2))
    db.commit()
    pending_id = int(db.execute("SELECT id FROM pending_entry").fetchone()["id"])
    modelled = D(
        str(
            db.execute(
                "SELECT amount_local FROM pending_entry WHERE id = ?", (pending_id,)
            ).fetchone()["amount_local"]
        )
    )

    drift = interest.rate_drift(
        db, pending_id=pending_id, confirmed_gross_local=modelled * D("1.10")
    )
    assert drift is not None
    assert drift > interest.DRIFT_THRESHOLD

    matching = interest.rate_drift(db, pending_id=pending_id, confirmed_gross_local=modelled)
    assert matching == D(0)


def test_drift_on_an_unknown_proposal_is_none(db: sqlite3.Connection) -> None:
    assert interest.rate_drift(db, pending_id=999, confirmed_gross_local=D("1")) is None


def test_a_proposal_without_a_source_transaction_still_deduplicates(
    db: sqlite3.Connection,
) -> None:
    """Migration 0008. SQLite treats NULLs as distinct in a UNIQUE constraint.

    Three modules called themselves idempotent on the strength of a constraint
    that never fired for them, because a distribution and a modelled interest
    credit have no source transaction. Every re-run silently duplicated the
    inbox, and confirming the list twice booked the same income twice.
    """
    account_id = account(db)
    accrued(db, account_id, to=date(2026, 4, 15))
    interest.propose(db, account_id=account_id, as_of=date(2026, 4, 15))
    db.commit()
    before = db.execute("SELECT count(*) FROM pending_entry").fetchone()[0]

    for _ in range(3):
        interest.propose(db, account_id=account_id, as_of=date(2026, 4, 15))
    db.commit()
    assert db.execute("SELECT count(*) FROM pending_entry").fetchone()[0] == before
