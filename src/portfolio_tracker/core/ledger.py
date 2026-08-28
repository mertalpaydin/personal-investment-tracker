"""Writing a trade somebody typed, and taking one back.

``core/cash.py`` does this for a flow the tool inferred and a person confirmed.
This is the same operation for a trade a person entered from scratch: freeze
the euro leg at the rate for the trade date, write both legs, and record what
was done.

It lives in ``core`` rather than in the API for the reason ADR 0004 gives and
``tests/test_fx.py`` enforces: currency conversion is allowed only where a rate
is pinned to a stated date. Recording a transaction is one of those places --
the euro amount is frozen at the trade-date rate and never recomputed, because
re-translating a two-year-old purchase at today's rate would fabricate a cost
basis. An HTTP handler is not where that belongs.

Deleting is a hard delete, and that was a decision rather than an oversight. A
``voided_at`` flag would have to be honoured by ``lots.py``, ``cash.py``,
``returns.py``, ``tax.py``, ``attribution.py``, ``vorabpauschale.py``,
``valuation.py`` and every ad-hoc query in the API and the debug page. One
missed filter is not a visible bug -- it is a tax figure that is quietly wrong.
The ledger stays "what is true", and what a person did to it lives in
``manual_entry_log``, which nothing derives from and where a missed filter is
therefore impossible.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from portfolio_tracker.core import fx
from portfolio_tracker.money import ZERO, plain, quantize_money

#: How each transaction type moves money, for unit accounting. Getting one of
#: these wrong does not error -- it silently changes every time-weighted return,
#: because a purchase classified as a deposit issues units and thereby erases
#: the very return it should have earned.
FLOW_CLASS: dict[str, str] = {
    "BUY": "INVESTMENT",
    "SELL": "INVESTMENT",
    "DIVIDEND": "INCOME",
    "INTEREST": "INCOME",
    "FEE": "COST",
    "TAX_WITHHELD": "COST",
    "DEPOSIT": "EXTERNAL_IN",
    "WITHDRAWAL": "EXTERNAL_OUT",
    "TRANSFER_IN": "INTERNAL_TRANSFER",
    "TRANSFER_OUT": "INTERNAL_TRANSFER",
}


@dataclass(frozen=True)
class EuroLeg:
    """An amount frozen at a rate, with the provenance of that rate."""

    rate: Decimal | None
    source: str | None
    as_of: date | None
    gross_eur: Decimal | None
    fees_eur: Decimal | None

    @property
    def known(self) -> bool:
        return self.rate is not None


def rate_for(
    conn: sqlite3.Connection,
    currency: str,
    when: date,
    *,
    supplied: Decimal | None = None,
) -> tuple[Decimal, str, date] | None:
    """The rate to freeze this trade at, or None if there is not one.

    Returning None rather than a guess is the whole point. A fabricated rate
    produces a euro cost basis that looks exact, is not, and is then frozen
    into every gain computed from the resulting lot for as long as it is held.
    """
    if currency == fx.BASE:
        return Decimal(1), "identity", when
    if supplied is not None:
        return supplied, "user", when
    try:
        observed = fx.get_rate(conn, currency, when, strict=False)
    except fx.FxUnavailable:
        return None
    return observed.rate, observed.source, observed.asof


def freeze(
    conn: sqlite3.Connection,
    *,
    currency: str,
    when: date,
    gross_local: Decimal | None,
    fees_local: Decimal = ZERO,
    supplied_rate: Decimal | None = None,
) -> EuroLeg:
    """Convert a trade's amounts to euros at the trade-date rate."""
    found = rate_for(conn, currency, when, supplied=supplied_rate)
    if found is None:
        return EuroLeg(rate=None, source=None, as_of=None, gross_eur=None, fees_eur=None)
    rate, source, as_of = found
    return EuroLeg(
        rate=rate,
        source=source,
        as_of=as_of,
        gross_eur=(
            quantize_money(fx.to_eur(gross_local, rate)) if gross_local is not None else None
        ),
        fees_eur=quantize_money(fx.to_eur(fees_local, rate)),
    )


@dataclass
class Entry:
    """A trade to record. Already validated; this module does not second-guess it."""

    account_id: int
    type: str
    trade_date: date
    currency: str
    instrument_id: int | None = None
    settlement_date: date | None = None
    quantity: Decimal | None = None
    price_per_unit_local: Decimal | None = None
    gross_amount_local: Decimal | None = None
    fees_local: Decimal = ZERO
    tax_withheld_local: Decimal = ZERO
    fees_known: bool = True
    orig_acquire_date: date | None = None
    orig_cost_local: Decimal | None = None
    note: str | None = None


def insert(conn: sqlite3.Connection, entry: Entry, leg: EuroLeg) -> int:
    """Write both currency legs. Returns the new transaction id."""
    cursor = conn.execute(
        """
        INSERT INTO transaction_ (
            account_id, instrument_id, type, trade_date, settlement_date, quantity,
            currency, price_per_unit_local, gross_amount_local, fees_local,
            tax_withheld_local, fx_rate, fx_rate_source, fx_rate_asof,
            gross_amount_eur, fees_eur, orig_acquire_date, orig_cost_local,
            source, confidence, quantity_confidence, basis_quality, fees_known,
            flow_class, external_id, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  'MANUAL', 'ACTUAL', 'EXACT', 'EXACT', ?, ?, ?, ?)
        """,
        (
            entry.account_id,
            entry.instrument_id,
            entry.type,
            entry.trade_date.isoformat(),
            entry.settlement_date.isoformat() if entry.settlement_date else None,
            _opt(entry.quantity),
            entry.currency,
            _opt(entry.price_per_unit_local),
            _opt(entry.gross_amount_local),
            plain(entry.fees_local),
            plain(entry.tax_withheld_local),
            _opt(leg.rate),
            leg.source,
            leg.as_of.isoformat() if leg.as_of else None,
            _opt(leg.gross_eur),
            _opt(leg.fees_eur),
            entry.orig_acquire_date.isoformat() if entry.orig_acquire_date else None,
            _opt(entry.orig_cost_local),
            1 if entry.fees_known else 0,
            FLOW_CLASS[entry.type],
            # Unique, so the same trade arriving later in a broker export
            # collides visibly instead of silently creating a second copy.
            f"manual:{uuid.uuid4().hex}",
            entry.note,
        ),
    )
    return int(cast("int", cursor.lastrowid))


def _opt(value: Decimal | None) -> str | None:
    return plain(value) if value is not None else None


def delete(conn: sqlite3.Connection, txn_id: int) -> None:
    """Remove a transaction and the lots derived from it, in foreign-key order.

    ``PRAGMA foreign_keys = ON`` is set on every connection, so the order here
    is load-bearing rather than tidiness.
    """
    conn.execute("DELETE FROM lot_disposal WHERE sell_txn_id = ?", (txn_id,))
    conn.execute(
        "DELETE FROM lot_disposal WHERE lot_id IN (SELECT id FROM lot WHERE open_txn_id = ?)",
        (txn_id,),
    )
    conn.execute("DELETE FROM lot WHERE open_txn_id = ?", (txn_id,))
    conn.execute("DELETE FROM transaction_ WHERE id = ?", (txn_id,))


def log(
    conn: sqlite3.Connection,
    action: str,
    txn_id: int | None,
    payload: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> None:
    """Record what was done, for a table nothing derives from."""
    conn.execute(
        "INSERT INTO manual_entry_log (at, action, txn_id, payload, previous) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            datetime.now(UTC).isoformat(timespec="seconds"),
            action,
            txn_id,
            json.dumps(payload, default=str),
            json.dumps(previous, default=str) if previous is not None else None,
        ),
    )


def row_json(row: sqlite3.Row) -> dict[str, Any]:
    """A transaction row as JSON, Decimals rendered exactly rather than as floats."""
    return {
        key: (plain(row[key]) if isinstance(row[key], Decimal) else row[key])
        for key in row.keys()  # noqa: SIM118 - sqlite3.Row has no __iter__ over keys
    }


def record_new_money(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    on: date,
    currency: str,
    amount_local: Decimal,
    rate: Decimal,
    label: str | None = None,
) -> int:
    """Book money that arrived from outside, as a deposit.

    Called when somebody says that part of a purchase was funded by new money.
    It is asked rather than deduced: this tool used to infer a contribution
    whenever a purchase exceeded the cash it knew about, and on real data that
    invented 85% of the savings figure -- the money might equally have come
    from an account it cannot see.

    Marked USER and ACTUAL so no classifier second-guesses it and no re-import
    undoes it. The euro leg is frozen at ``rate``, which the caller has already
    pinned to ``on``; this function lives in core precisely so the HTTP layer
    does no currency arithmetic of its own.
    """
    cursor = conn.execute(
        """
        INSERT INTO transaction_
            (account_id, type, trade_date, currency, gross_amount_local,
             gross_amount_eur, fx_rate, source, confidence, flow_class,
             flow_class_source, is_new_savings, savings_label, note)
        VALUES (?, 'DEPOSIT', ?, ?, ?, ?, ?, 'MANUAL', 'ACTUAL', 'EXTERNAL_IN',
                'USER', 1, ?, ?)
        """,
        (
            account_id,
            on.isoformat(),
            currency,
            str(amount_local),
            str(fx.to_eur(amount_local, rate)),
            str(rate),
            label,
            "money from outside, recorded with the trade it funded",
        ),
    )
    return int(cursor.lastrowid or 0)
