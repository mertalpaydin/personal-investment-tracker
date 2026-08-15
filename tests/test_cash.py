"""Cash flows across the portfolio boundary.

The scenario tests below are written as the user described the requirement:
sell something for 1,000, buy something for 1,500 three days later, and only
the 500 shortfall should count as new money -- with the 1,000 counting as held
for those three days rather than disappearing and coming back.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.core import cash
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'D', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency) "
        "VALUES (1, 'AAA', 'A', 'ETF', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency) "
        "VALUES (2, 'BBB', 'B', 'ETF', 'EUR')"
    )
    conn.commit()
    return conn


def trade(
    conn: sqlite3.Connection,
    kind: str,
    when: str,
    amount: str,
    instrument: int = 1,
    txn_id: int | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, gross_amount_eur, fx_rate, flow_class) "
        "VALUES (?, 1, ?, ?, ?, 'EUR', 1, ?, ?, 1, 'INVESTMENT')",
        (txn_id, instrument, kind, when, amount, amount),
    )
    return int(cursor.lastrowid or 0)


# ---------------------------------------------------------------------------
# The requirement, stated as scenarios
# ---------------------------------------------------------------------------


def test_a_purchase_with_no_prior_sale_is_entirely_new_money(db: sqlite3.Connection) -> None:
    trade(db, "BUY", "2026-01-05", "1500")
    report = cash.compute_flows(db)
    assert report.total_inferred == {"EUR": Decimal("1500.00")}


def test_only_the_shortfall_counts_as_new_money(db: sqlite3.Connection) -> None:
    """Sell 1,000, buy 1,500 three days later: 500 is new, not 1,500."""
    trade(db, "SELL", "2026-01-05", "1000")
    trade(db, "BUY", "2026-01-08", "1500", instrument=2)
    report = cash.compute_flows(db)
    assert report.total_inferred == {"EUR": Decimal("500.00")}


def test_proceeds_fully_covering_a_purchase_infer_nothing(db: sqlite3.Connection) -> None:
    trade(db, "SELL", "2026-01-05", "2000")
    trade(db, "BUY", "2026-01-08", "1500", instrument=2)
    assert cash.compute_flows(db).inferred_deposits == []


def test_leftover_proceeds_remain_available_for_a_later_purchase(
    db: sqlite3.Connection,
) -> None:
    trade(db, "SELL", "2026-01-05", "2000")
    trade(db, "BUY", "2026-01-08", "1500", instrument=2)
    trade(db, "BUY", "2026-02-01", "600", instrument=2)
    # 500 was left over, so only 100 of the second purchase is new money.
    assert cash.compute_flows(db).total_inferred == {"EUR": Decimal("100.00")}


def test_idle_proceeds_are_held_for_the_days_between(db: sqlite3.Connection) -> None:
    """The reason cash has to be tracked at all.

    Without it the 1,000 vanishes on the sale date and reappears three days
    later, showing as a loss followed by an identical gain.
    """
    trade(db, "SELL", "2026-01-05", "1000")
    trade(db, "BUY", "2026-01-08", "1500", instrument=2)
    db.commit()
    cash.build_cash_daily(db, start=date(2026, 1, 5), end=date(2026, 1, 8))

    balances = {
        r["date"]: r["balance_local"]
        for r in db.execute("SELECT date, balance_local FROM cash_daily ORDER BY date")
    }
    assert balances["2026-01-06"] == Decimal("1000.00")
    assert balances["2026-01-07"] == Decimal("1000.00")
    # Spent on the purchase date.
    assert balances["2026-01-08"] == Decimal("0.00")


def test_a_withdrawal_does_not_fund_a_later_purchase(db: sqlite3.Connection) -> None:
    """Money marked as taken out is gone; the next purchase is all new money."""
    sale = trade(db, "SELL", "2026-01-05", "1000")
    trade(db, "BUY", "2026-01-08", "1500", instrument=2)
    cash.set_disposition(db, sale, "WITHDRAW")
    assert cash.compute_flows(db).total_inferred == {"EUR": Decimal("1500.00")}


def test_a_sale_defaults_to_reinvestment_but_is_reported_as_unresolved(
    db: sqlite3.Connection,
) -> None:
    """An unanswered question stays visible rather than quietly becoming an
    answer."""
    sale = trade(db, "SELL", "2026-01-05", "1000")
    report = cash.compute_flows(db)
    assert report.unresolved_sales == [sale]

    cash.set_disposition(db, sale, "REINVEST")
    assert cash.compute_flows(db).unresolved_sales == []


def test_disposition_rejects_an_unknown_value(db: sqlite3.Connection) -> None:
    sale = trade(db, "SELL", "2026-01-05", "1000")
    with pytest.raises(ValueError, match="REINVEST or WITHDRAW"):
        cash.set_disposition(db, sale, "SPEND")


def test_disposition_only_applies_to_sales(db: sqlite3.Connection) -> None:
    buy = trade(db, "BUY", "2026-01-05", "1000")
    with pytest.raises(LookupError):
        cash.set_disposition(db, buy, "WITHDRAW")


# ---------------------------------------------------------------------------
# Currencies must not be mixed
# ---------------------------------------------------------------------------


def test_balances_are_tracked_per_currency(db: sqlite3.Connection) -> None:
    """A lira sale cannot fund a euro purchase, and the totals never add up
    across currencies -- doing so once produced a nonsensical EUR 913,911."""
    db.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency) "
        "VALUES (3, 'TRY1', 'Lira fund', 'FUND', 'TRY')"
    )
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, flow_class) "
        "VALUES (1, 3, 'SELL', '2026-01-05', 'TRY', 1, '50000', 'INVESTMENT')"
    )
    trade(db, "BUY", "2026-01-08", "1500", instrument=2)

    totals = cash.compute_flows(db).total_inferred
    assert totals == {"EUR": Decimal("1500.00")}
    assert "TRY" not in totals


# ---------------------------------------------------------------------------
# Idle-cash flagging
# ---------------------------------------------------------------------------


def test_long_idle_cash_is_flagged_not_assumed_withdrawn(db: sqlite3.Connection) -> None:
    """Inventing a withdrawal would understate contributions and misstate
    return, so the user is told and decides."""
    trade(db, "SELL", "2026-01-05", "5000")
    trade(db, "BUY", "2026-09-01", "200", instrument=2)
    report = cash.compute_flows(db)
    assert report.idle_periods
    _start, _end, balance, days = report.idle_periods[0]
    assert balance == Decimal("5000")
    assert days > cash.IDLE_FLAG_DAYS
    # Still available: it was flagged, not confiscated.
    assert report.total_inferred == {}


def test_short_gaps_are_not_flagged(db: sqlite3.Connection) -> None:
    trade(db, "SELL", "2026-01-05", "5000")
    trade(db, "BUY", "2026-01-08", "200", instrument=2)
    assert cash.compute_flows(db).idle_periods == []


# ---------------------------------------------------------------------------
# Proposals, not entries
# ---------------------------------------------------------------------------


def test_inference_writes_no_transaction_until_confirmed(db: sqlite3.Connection) -> None:
    """The rule from the original brief: nothing auto-generated reaches the
    ledger. A plausible number nobody checked is how a tracker starts lying."""
    trade(db, "BUY", "2026-01-05", "1500")
    before = db.execute("SELECT count(*) n FROM transaction_").fetchone()["n"]

    created, _ = cash.propose_deposits(db)
    assert created == 1
    assert db.execute("SELECT count(*) n FROM transaction_").fetchone()["n"] == before

    pending = db.execute("SELECT id FROM pending_entry").fetchone()
    txn_id = cash.confirm(db, int(pending["id"]))
    assert db.execute("SELECT count(*) n FROM transaction_").fetchone()["n"] == before + 1

    row = db.execute("SELECT type, flow_class FROM transaction_ WHERE id = ?", (txn_id,)).fetchone()
    assert row["type"] == "DEPOSIT"
    assert row["flow_class"] == "EXTERNAL_IN"


def test_proposing_twice_does_not_duplicate(db: sqlite3.Connection) -> None:
    trade(db, "BUY", "2026-01-05", "1500")
    assert cash.propose_deposits(db)[0] == 1
    assert cash.propose_deposits(db)[0] == 0


def test_a_confirmed_proposal_cannot_be_confirmed_again(db: sqlite3.Connection) -> None:
    trade(db, "BUY", "2026-01-05", "1500")
    cash.propose_deposits(db)
    pending_id = int(db.execute("SELECT id FROM pending_entry").fetchone()["id"])
    cash.confirm(db, pending_id)
    with pytest.raises(LookupError, match="awaiting a decision"):
        cash.confirm(db, pending_id)


def test_a_rejected_proposal_records_why(db: sqlite3.Connection) -> None:
    trade(db, "BUY", "2026-01-05", "1500")
    cash.propose_deposits(db)
    pending_id = int(db.execute("SELECT id FROM pending_entry").fetchone()["id"])
    cash.reject(db, pending_id, "transferred from another broker, not new money")
    row = db.execute(
        "SELECT status, note FROM pending_entry WHERE id = ?", (pending_id,)
    ).fetchone()
    assert row["status"] == "REJECTED"
    assert "another broker" in row["note"]


def test_the_proposal_explains_its_own_arithmetic(db: sqlite3.Connection) -> None:
    """A proposal the user cannot check is one they will rubber-stamp."""
    trade(db, "SELL", "2026-01-05", "1000")
    trade(db, "BUY", "2026-01-08", "1500", instrument=2)
    cash.propose_deposits(db)
    basis = db.execute("SELECT basis FROM pending_entry").fetchone()["basis"]
    assert "1500" in basis and "1000" in basis


# ---------------------------------------------------------------------------
# Provenance: which part of the idle balance is income
# ---------------------------------------------------------------------------


def final(report: cash.FlowReport) -> cash.FlowEvent:
    return report.events[-1]


def test_a_distribution_is_income_and_says_so(db: sqlite3.Connection) -> None:
    trade(db, "SELL", "2026-01-05", "1000")
    trade(db, "DIVIDEND", "2026-01-06", "100")
    state = final(cash.compute_flows(db))
    assert state.balance_after == D(1100)
    assert state.income_after == D(100)
    assert state.income_received == D(100)


def test_a_purchase_spends_capital_before_income(db: sqlite3.Connection) -> None:
    """The conservative direction, and the one that matches intent.

    Spending income first would report the dividend as reinvested and leave the
    owner believing the remaining balance is capital -- the more misleading of
    the two available errors.
    """
    trade(db, "SELL", "2026-01-05", "1000")
    trade(db, "DIVIDEND", "2026-01-06", "100")
    trade(db, "BUY", "2026-01-07", "900")
    state = final(cash.compute_flows(db))
    assert state.balance_after == D(200)
    assert state.income_after == D(100)  # untouched: capital covered the purchase


def test_income_is_spent_only_once_capital_runs_out(db: sqlite3.Connection) -> None:
    trade(db, "SELL", "2026-01-05", "1000")
    trade(db, "DIVIDEND", "2026-01-06", "100")
    trade(db, "BUY", "2026-01-07", "1040")
    state = final(cash.compute_flows(db))
    assert state.balance_after == D(60)
    assert state.income_after == D(60)  # 1000 capital, then 40 of the income


def test_reinvested_income_leaves_the_balance_but_stays_received(
    db: sqlite3.Connection,
) -> None:
    """The distinction the two fields exist for.

    A tool that reports only the cash balance makes an income stream vanish the
    moment it is put back to work, which is how EUR 2,549 of distributions came
    to be booked as a loss in the first place.
    """
    trade(db, "DIVIDEND", "2026-01-06", "100")
    trade(db, "BUY", "2026-01-07", "100")
    state = final(cash.compute_flows(db))
    assert state.balance_after == D(0)
    assert state.income_after == D(0)
    assert state.income_received == D(100)


def test_the_split_always_sums_to_the_balance(db: sqlite3.Connection) -> None:
    trade(db, "SELL", "2026-01-05", "1000")
    trade(db, "DIVIDEND", "2026-01-06", "100")
    trade(db, "BUY", "2026-01-07", "300")
    trade(db, "DIVIDEND", "2026-01-08", "50")
    trade(db, "BUY", "2026-01-09", "800")
    for event in cash.compute_flows(db).events:
        capital = event.balance_after - event.income_after
        assert capital >= D(0), f"{event.kind} on {event.on} attributed negative capital"
        assert event.income_after >= D(0)
        assert event.income_after <= event.balance_after


def test_an_inferred_deposit_resets_both_pots(db: sqlite3.Connection) -> None:
    """The purchase consumed everything, income included, and needed more."""
    trade(db, "DIVIDEND", "2026-01-06", "100")
    trade(db, "BUY", "2026-01-07", "5000")
    report = cash.compute_flows(db)
    assert report.inferred_deposits[0].amount == D("4900.00")
    state = final(report)
    assert state.balance_after == D(0)
    assert state.income_after == D(0)
    assert state.income_received == D(100)
