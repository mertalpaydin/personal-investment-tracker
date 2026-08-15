"""Deposit accounts and interest.

A deposit is carried as a **synthetic instrument** priced at exactly one unit of
its own currency, with the balance as the quantity. Everything else then falls
out of machinery that already exists: daily FX revaluation, allocation, the
portfolio's total value, and the tax engine's scope test all treat it as one
more position, with no `if kind == 'CASH'` anywhere.

For a foreign-currency deposit that is the whole point. Its price never moves
and its euro value moves every day, so the currency effect on cash -- which the
owner of a lira account almost certainly cannot see anywhere -- comes out for
free as ``unrealized_fx_eur``.

Interest accrues daily on the opening balance. The brief suggested using an
average balance over the period; daily accrual *is* that average, computed
exactly rather than approximated, at the same cost, and it needs no special
case for a mid-period deposit.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from portfolio_tracker.core import fx
from portfolio_tracker.money import ZERO, quantize_money

#: Day-count conventions. ACT/365 is the Turkish deposit-market convention and
#: the German one used here; the others exist because a term deposit elsewhere
#: may use them and getting it wrong is a silent few-percent error on interest.
DAY_COUNTS = ("ACT/365", "ACT/360", "30E/360")


class DepositError(RuntimeError):
    """The account's terms are incomplete or inconsistent."""


@dataclass(frozen=True)
class Accrual:
    on: date
    opening_balance: Decimal
    rate: Decimal
    day_count_fraction: Decimal
    accrued: Decimal
    accrued_cumulative: Decimal


@dataclass
class AccrualReport:
    account_id: int
    currency: str
    days: int = 0
    gross_accrued: Decimal = ZERO
    rows: list[Accrual] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def day_count_fraction(start: date, end: date, convention: str = "ACT/365") -> Decimal:
    """The fraction of a year between two dates.

    30E/360 treats every month as 30 days and the year as 360, so a full month
    is exactly 1/12 regardless of its length.
    """
    if convention not in DAY_COUNTS:
        raise DepositError(f"unknown day count {convention!r}; expected one of {DAY_COUNTS}")
    if end <= start:
        return ZERO

    if convention == "ACT/365":
        return Decimal((end - start).days) / Decimal(365)
    if convention == "ACT/360":
        return Decimal((end - start).days) / Decimal(360)

    d1 = min(start.day, 30)
    d2 = min(end.day, 30)
    days = (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)
    return Decimal(days) / Decimal(360)


def ensure_cash_instrument(conn: sqlite3.Connection, account_id: int) -> int:
    """Create (or find) the synthetic instrument that carries this balance."""
    account = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise LookupError(f"no account {account_id}")
    if account["cash_instrument_id"]:
        return int(account["cash_instrument_id"])

    key = f"CASH_{account['currency']}_{account_id}"
    existing = conn.execute("SELECT id FROM instrument WHERE import_key = ?", (key,)).fetchone()
    if existing is not None:
        instrument_id = int(existing["id"])
    else:
        cursor = conn.execute(
            """
            INSERT INTO instrument
                (import_key, name, asset_class, currency, price_source, trading_calendar,
                 is_cash_proxy, linked_account_id, is_fund, teilfreistellung_pct,
                 tax_regime, tax_scope_note, status)
            VALUES (?, ?, 'CASH', ?, 'synthetic', 'TARGET2', 1, ?, 0, '0.00', ?, ?, 'OWNED')
            """,
            (
                key,
                f"{account['name']} ({account['currency']} balance)",
                account["currency"],
                account_id,
                # Interest is section 20(1)(7) EStG income: fully taxable, no
                # Teilfreistellung, and it consumes the same allowance as
                # dividends and realised gains.
                "DE_INVESTMENT",
                "deposit interest, section 20 EStG",
            ),
        )
        instrument_id = int(cursor.lastrowid or 0)

    conn.execute(
        "UPDATE account SET cash_instrument_id = ? WHERE id = ?", (instrument_id, account_id)
    )
    return instrument_id


def balance_on(conn: sqlite3.Connection, account_id: int, when: date) -> Decimal:
    """The deposit's balance, from its own ledger entries."""
    total = ZERO
    for row in conn.execute(
        """
        SELECT t.type, t.gross_amount_local, t.quantity
        FROM transaction_ t
        WHERE t.account_id = ? AND t.trade_date <= ?
          AND t.type IN ('DEPOSIT', 'WITHDRAWAL', 'INTEREST')
        ORDER BY t.trade_date, t.id
        """,
        (account_id, when.isoformat()),
    ):
        amount = Decimal(str(row["gross_amount_local"] or 0))
        if row["type"] == "WITHDRAWAL":
            total -= amount
        elif row["type"] == "DEPOSIT":
            total += amount
        else:
            # Interest only changes the balance when it is capitalised, which
            # is recorded as a quantity against the cash proxy.
            total += Decimal(str(row["quantity"] or 0))
    return total


def _capital_moved_on(conn: sqlite3.Connection, account_id: int, when: date) -> Decimal:
    """Deposits less withdrawals value-dated to a single day."""
    total = ZERO
    for row in conn.execute(
        "SELECT type, gross_amount_local FROM transaction_ "
        "WHERE account_id = ? AND trade_date = ? AND type IN ('DEPOSIT', 'WITHDRAWAL')",
        (account_id, when.isoformat()),
    ):
        amount = Decimal(str(row["gross_amount_local"] or 0))
        total += -amount if row["type"] == "WITHDRAWAL" else amount
    return total


def accrue_interest(
    conn: sqlite3.Connection, *, account_id: int, start: date, end: date
) -> AccrualReport:
    """Daily gross accrual on the opening balance.

    Gross: withholding is applied when interest is *paid*, not as it accrues,
    so deducting it here would understate the balance every day of the period.
    """
    account = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise LookupError(f"no account {account_id}")
    if account["interest_rate"] is None:
        raise DepositError(f"account {account_id} has no interest rate set")

    rate = Decimal(str(account["interest_rate"]))
    convention = account["interest_day_count"]
    report = AccrualReport(account_id=account_id, currency=account["currency"])

    conn.execute(
        "DELETE FROM interest_accrual WHERE account_id = ? AND date BETWEEN ? AND ?",
        (account_id, start.isoformat(), end.isoformat()),
    )

    # Cumulative accrual resets whenever interest is actually paid.
    payments = {
        date.fromisoformat(row["trade_date"])
        for row in conn.execute(
            "SELECT trade_date FROM transaction_ WHERE account_id = ? AND type = 'INTEREST'",
            (account_id,),
        )
    }

    cumulative = ZERO
    current = start
    while current <= end:
        previous = current - timedelta(days=1)
        # Capital paid in on this day earns from this day: banks value-date a
        # deposit to the day it lands, and starting the day after loses a
        # day's interest on every opening balance. Interest capitalised today
        # is deliberately excluded -- it was earned by yesterday's balance.
        opening = balance_on(conn, account_id, previous) + _capital_moved_on(
            conn, account_id, current
        )
        if opening <= ZERO:
            current += timedelta(days=1)
            continue

        fraction = day_count_fraction(previous, current, convention)
        # NOT quantised per day. Rounding each day's fraction of a cent to the
        # cent loses about half a cent daily -- roughly EUR 1.79 a year on a
        # 20,000 balance. Banks accrue at full precision and round when
        # interest is actually paid, so the running total does too.
        accrued = opening * rate * fraction
        if current in payments:
            cumulative = ZERO
        cumulative += accrued

        rate_info = None
        if account["currency"] != fx.BASE:
            try:
                rate_info = fx.get_rate(conn, account["currency"], current, strict=False)
            except fx.FxUnavailable:
                rate_info = None

        conn.execute(
            """
            INSERT INTO interest_accrual
                (account_id, date, opening_balance_local, rate, day_count_frac,
                 accrued_local, accrued_cum_local, fx_rate, accrued_cum_eur)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                current.isoformat(),
                quantize_money(opening),
                rate,
                fraction,
                quantize_money(accrued),
                quantize_money(cumulative),
                rate_info.rate
                if rate_info
                else (Decimal(1) if account["currency"] == "EUR" else None),
                quantize_money(fx.to_eur(cumulative, rate_info.rate))
                if rate_info
                else (quantize_money(cumulative) if account["currency"] == "EUR" else None),
            ),
        )
        report.rows.append(
            Accrual(
                current,
                quantize_money(opening),
                rate,
                fraction,
                quantize_money(accrued),
                quantize_money(cumulative),
            )
        )
        report.gross_accrued += accrued
        report.days += 1
        current += timedelta(days=1)

    report.gross_accrued = quantize_money(report.gross_accrued)
    return report


def project_maturity(
    conn: sqlite3.Connection, *, account_id: int
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Gross, withheld and net interest a term deposit will pay at maturity.

    Returns ``None`` for an account with no maturity date -- an instant-access
    balance has no single figure to project.
    """
    account = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if account is None or not account["maturity_date"] or account["interest_rate"] is None:
        return None

    maturity = date.fromisoformat(account["maturity_date"])
    opened = date.fromisoformat(account["interest_first_accrual"])
    principal = balance_on(conn, account_id, opened)
    fraction = day_count_fraction(opened, maturity, account["interest_day_count"])

    gross = quantize_money(principal * Decimal(str(account["interest_rate"])) * fraction)
    withheld = quantize_money(gross * Decimal(str(account["withholding_rate"] or 0)))
    return gross, withheld, quantize_money(gross - withheld)
