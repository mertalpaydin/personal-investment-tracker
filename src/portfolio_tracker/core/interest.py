"""Interest that has been earned but not yet credited.

``accounts.accrue_interest`` computes what a deposit earns each day and stores
it. Nothing did anything with those rows: between payment dates the accrued
amount was real money the balance did not show and no screen reported.

This module closes that loop, and it closes it the same way every other
inference in this system is closed -- through the review inbox. An accrual is a
*model*: a rate, a day-count convention and an opening balance. Banks round
differently, move instant-access rates without announcing it twice, and value-
date credits by their own rules. A modelled payment that wrote itself into the
ledger would be indistinguishable from a bank's own figure once it was there,
and the balance would drift away from the real one with nothing to say so.

So: propose, review, confirm at the bank's numbers when they arrive. And when
they do arrive, compare them against the model, because a gap that persists
across payments means the stored rate is stale.
"""

from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from datetime import UTC, date, datetime
from decimal import Decimal

from portfolio_tracker.core.accounts import ensure_cash_instrument, project_maturity
from portfolio_tracker.money import ZERO, quantize_money

#: Months between credits, by the account's stated frequency. ``AT_MATURITY``
#: is absent deliberately -- it has no schedule, only an end date.
SCHEDULE_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "SEMIANNUAL": 6, "ANNUAL": 12}

#: A confirmed figure this far from the model is worth saying out loud. Below
#: it the difference is the bank's rounding; above it, the stored rate is
#: probably stale.
DRIFT_THRESHOLD = Decimal("0.02")


def next_payment_date(last: date, frequency: str) -> date | None:
    """The next credit date, clamped to the end of a short month."""
    months = SCHEDULE_MONTHS.get(frequency)
    if months is None:
        return None
    total = last.month - 1 + months
    year, month = last.year + total // 12, total % 12 + 1
    return date(year, month, min(last.day, monthrange(year, month)[1]))


def due_payments(
    conn: sqlite3.Connection, *, account_id: int, as_of: date
) -> list[tuple[date, Decimal, Decimal, Decimal]]:
    """Credits the schedule says are due and that nothing has booked.

    Returns ``(when, gross, withheld, net)``. Withholding is the account's own
    rate: it is taken at source, so proposing the gross figure alone would
    suggest a credit the balance is never going to receive.
    """
    account = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise LookupError(f"no account {account_id}")
    if account["interest_rate"] is None or account["interest_first_accrual"] is None:
        return []

    frequency = account["interest_frequency"] or ""
    withholding = Decimal(str(account["withholding_rate"] or 0))
    booked = {
        date.fromisoformat(row["trade_date"])
        for row in conn.execute(
            "SELECT trade_date FROM transaction_ WHERE account_id = ? AND type = 'INTEREST'",
            (account_id,),
        )
    }

    if frequency == "AT_MATURITY":
        # No schedule to walk: a term deposit pays once, and only once it has
        # actually matured. Proposing before then would book unearned income.
        maturity = account["maturity_date"]
        if maturity is None:
            return []
        matures_on = date.fromisoformat(maturity)
        if matures_on > as_of or matures_on in booked:
            return []
        projected = project_maturity(conn, account_id=account_id)
        return [(matures_on, *projected)] if projected else []

    start = date.fromisoformat(account["interest_first_accrual"])
    due: list[tuple[date, Decimal, Decimal, Decimal]] = []
    previous = start
    scheduled = next_payment_date(start, frequency)
    while scheduled is not None and scheduled <= as_of:
        if scheduled not in booked:
            # Summed from the stored daily rows rather than recomputed, so the
            # proposal cannot disagree with the accrual table it came from.
            gross = ZERO
            for row in conn.execute(
                "SELECT accrued_local FROM interest_accrual WHERE account_id = ? "
                "AND date > ? AND date <= ?",
                (account_id, previous.isoformat(), scheduled.isoformat()),
            ):
                gross += Decimal(str(row["accrued_local"]))
            if gross > ZERO:
                gross = quantize_money(gross)
                withheld = quantize_money(gross * withholding)
                due.append((scheduled, gross, withheld, gross - withheld))
        previous = scheduled
        scheduled = next_payment_date(scheduled, frequency)
    return due


def propose(conn: sqlite3.Connection, *, account_id: int, as_of: date) -> int:
    """Queue due-but-unbooked interest for review. Returns how many are new.

    Idempotent: the uniqueness constraint on ``pending_entry`` means re-running
    after a statement arrives does not duplicate a waiting proposal.
    """
    account = conn.execute(
        "SELECT currency, name FROM account WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None:
        raise LookupError(f"no account {account_id}")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    created = 0
    for when, gross, withheld, net in due_payments(conn, account_id=account_id, as_of=as_of):
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO pending_entry
                (kind, account_id, proposed_date, currency, amount_local,
                 basis, derivation, created_at)
            VALUES ('INTEREST', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(account_id),
                when.isoformat(),
                account["currency"],
                gross,
                f"{account['name']}: {gross} {account['currency']} accrued to {when}, "
                f"less {withheld} withheld at source, net {net}",
                json.dumps(
                    {
                        "modelled": True,
                        "gross": str(gross),
                        "withheld": str(withheld),
                        "net": str(net),
                        "rule": "daily accrual on the opening balance since the last payment",
                    }
                ),
                now,
            ),
        )
        created += cursor.rowcount
    return created


def confirm(
    conn: sqlite3.Connection,
    pending_id: int,
    *,
    gross_local: Decimal | None = None,
    tax_local: Decimal | None = None,
) -> int:
    """Book a proposed payment, at the bank's figures when they are known.

    Two column values carry the design.

    ``flow_class = 'INCOME'``. Interest is return, not money the owner saved.
    Booking it as a contribution would make unit accounting issue units for it,
    cancelling exactly the return it represents -- and the tool would report
    the owner as having saved his own interest.

    ``quantity`` = the net amount **only when the account capitalises**. Interest
    paid away leaves the account; crediting the balance for money that was sent
    elsewhere overstates the position on every payment date.
    """
    row = conn.execute(
        "SELECT * FROM pending_entry WHERE id = ? AND status = 'PROPOSED' AND kind = 'INTEREST'",
        (pending_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no interest proposal {pending_id} awaiting a decision")

    detail = json.loads(row["derivation"] or "{}")
    account_id = int(row["account_id"])
    account = conn.execute(
        "SELECT rollover_policy, cash_instrument_id FROM account WHERE id = ?", (account_id,)
    ).fetchone()

    from_bank = gross_local is not None
    gross = (
        quantize_money(gross_local)
        if gross_local is not None
        else Decimal(str(row["amount_local"]))
    )
    withheld = (
        quantize_money(tax_local)
        if tax_local is not None
        else Decimal(str(detail.get("withheld", "0")))
    )
    if withheld > gross:
        raise ValueError(f"withholding {withheld} exceeds the gross interest {gross}")

    net = gross - withheld
    capitalised = net if account["rollover_policy"] == "CAPITALIZE" else ZERO

    cursor = conn.execute(
        """
        INSERT INTO transaction_
            (account_id, instrument_id, type, trade_date, currency, quantity,
             gross_amount_local, tax_withheld_local, fx_rate, source, confidence,
             fees_known, flow_class, external_id, note)
        VALUES (?, ?, 'INTEREST', ?, ?, ?, ?, ?, NULL, 'AUTO_CONFIRMED', ?, 1, 'INCOME', ?, ?)
        """,
        (
            account_id,
            account["cash_instrument_id"] or ensure_cash_instrument(conn, account_id),
            row["proposed_date"],
            row["currency"],
            capitalised,
            gross,
            withheld,
            # A confirmed bank figure is a fact; a confirmed model output is
            # still an estimate, and the ledger says which it is holding.
            "ACTUAL" if from_bank else "ESTIMATE",
            f"interest:v1:{account_id}:{row['proposed_date']}",
            row["basis"],
        ),
    )
    txn_id = int(cursor.lastrowid or 0)
    conn.execute(
        "UPDATE pending_entry SET status = 'CONFIRMED', resulting_txn_id = ?, decided_at = ? "
        "WHERE id = ?",
        (txn_id, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), pending_id),
    )
    return txn_id


def rate_drift(
    conn: sqlite3.Connection, *, pending_id: int, confirmed_gross_local: Decimal
) -> Decimal | None:
    """How far a bank's figure sits from the model, as a signed fraction.

    Banks move instant-access rates without announcing it twice. A single
    payment differing by a percent is rounding; a run of them differing the
    same way means the rate stored here is stale and every projection built on
    it is wrong in the same direction.
    """
    row = conn.execute(
        "SELECT amount_local FROM pending_entry WHERE id = ?", (pending_id,)
    ).fetchone()
    if row is None:
        return None
    modelled = Decimal(str(row["amount_local"]))
    if modelled <= ZERO:
        return None
    return (quantize_money(confirmed_gross_local) - modelled) / modelled
