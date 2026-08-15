"""FIFO tax lots.

German capital-gains tax is first-in-first-out per depot per instrument, so
lots are the unit everything downstream depends on. They are **derived**: this
module deletes and recomputes them from ``transaction_``, and is safe to re-run.

The load-bearing decision is that a lot's euro cost is **frozen at its
acquisition rate and never revisited**. German law computes the taxable gain on
a foreign asset as proceeds at the sale-date rate minus cost at the
acquisition-date rate, so the currency movement between those two dates is part
of the taxable base. Re-translating an old cost at today's rate would erase
exactly that, which is the error the source export makes (`docs/adr/0004`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_tracker.core import fx
from portfolio_tracker.money import ZERO, plain, quantize_money

#: Types that open a lot, and types that consume one.
OPENING = ("BUY", "TRANSFER_IN")
CLOSING = ("SELL", "TRANSFER_OUT")

#: Quantities can be fractional to eight places; below this a residual is
#: rounding noise rather than a real holding.
DUST = Decimal("0.00000001")


class LotError(RuntimeError):
    """The transaction set cannot produce a coherent set of lots."""


@dataclass
class Lot:
    """An open position with a frozen cost basis."""

    account_id: int
    instrument_id: int
    open_txn_id: int
    open_date: date
    currency: str
    qty_opened: Decimal
    qty_remaining: Decimal
    cost_total_local: Decimal
    cost_per_unit_local: Decimal
    fx_rate_at_open: Decimal | None
    cost_total_eur: Decimal | None
    cost_per_unit_eur: Decimal | None
    basis_quality: str = "EXACT"
    lot_id: int | None = None


@dataclass
class Disposal:
    lot: Lot
    qty: Decimal
    proceeds_local: Decimal
    cost_basis_local: Decimal
    proceeds_eur: Decimal | None
    cost_basis_eur: Decimal | None
    gross_gain_eur: Decimal | None
    fx_gain_eur: Decimal | None
    holding_days: int
    sell_txn_id: int


@dataclass
class RebuildReport:
    lots_created: int = 0
    disposals_created: int = 0
    oversold: list[str] = field(default_factory=list)
    missing_fx: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.oversold


def fifo_consume(lots: Sequence[Lot], qty: Decimal) -> list[tuple[Lot, Decimal]]:
    """Take ``qty`` from ``lots`` oldest-first. Pure: no connection, no I/O.

    Returns the (lot, quantity) pairs consumed. Raises if the lots cannot cover
    the quantity, rather than allowing a negative position: an oversell means
    the transaction set is wrong, and silently continuing would produce a
    plausible tax figure from an impossible history.
    """
    if qty <= ZERO:
        raise LotError(f"disposal quantity must be positive, got {plain(qty)}")

    taken: list[tuple[Lot, Decimal]] = []
    outstanding = qty
    for lot in lots:
        if outstanding <= DUST:
            break
        if lot.qty_remaining <= ZERO:
            continue
        used = min(lot.qty_remaining, outstanding)
        taken.append((lot, used))
        outstanding -= used

    if outstanding > DUST:
        available = sum((lot.qty_remaining for lot in lots), ZERO)
        raise LotError(
            f"cannot dispose of {plain(qty)}: only {plain(available)} held "
            f"({plain(outstanding)} short)"
        )
    return taken


def _fx_at(
    conn: sqlite3.Connection, currency: str, on: date, report: RebuildReport, label: str
) -> Decimal | None:
    """Acquisition-date rate, or None if history does not reach that far back."""
    if currency == fx.BASE:
        return Decimal(1)
    try:
        return fx.get_rate(conn, currency, on, strict=False).rate
    except fx.FxUnavailable:
        report.missing_fx.append(label)
        return None


def rebuild_lots(
    conn: sqlite3.Connection,
    *,
    account_id: int | None = None,
    instrument_id: int | None = None,
) -> RebuildReport:
    """Delete and recompute lots and disposals from the transaction ledger.

    Idempotent by construction. Lots are never patched incrementally: an edit
    anywhere in the history changes which lots a later sale consumes, so the
    only reliable update is a rebuild.
    """
    report = RebuildReport()

    scope = "WHERE 1 = 1"
    params: list[object] = []
    if account_id is not None:
        scope += " AND account_id = ?"
        params.append(account_id)
    if instrument_id is not None:
        scope += " AND instrument_id = ?"
        params.append(instrument_id)

    conn.execute(
        f"DELETE FROM lot_disposal WHERE lot_id IN (SELECT id FROM lot {scope})", tuple(params)
    )
    conn.execute(f"DELETE FROM lot {scope}", tuple(params))

    rows = conn.execute(
        f"""
        SELECT id, account_id, instrument_id, type, trade_date, quantity,
               currency, price_per_unit_local, gross_amount_local, fees_local,
               gross_amount_eur, fees_eur, basis_quality,
               orig_acquire_date, orig_cost_local, orig_cost_eur
        FROM transaction_
        {scope} AND type IN {OPENING + CLOSING}
        ORDER BY account_id, instrument_id, trade_date, id
        """,
        tuple(params),
    ).fetchall()

    open_lots: dict[tuple[int, int], list[Lot]] = {}

    for row in rows:
        key = (int(row["account_id"]), int(row["instrument_id"]))
        bucket = open_lots.setdefault(key, [])
        trade_date = date.fromisoformat(row["trade_date"])
        quantity = Decimal(str(row["quantity"]))
        currency = row["currency"]

        if row["type"] in OPENING:
            lot = _open_lot(conn, row, key, trade_date, quantity, currency, report)
            # Inserted immediately: lot_disposal.lot_id is NOT NULL, so a
            # disposal cannot be written before the lot it consumes exists.
            _insert_lot(conn, lot)
            report.lots_created += 1
            bucket.append(lot)
        else:
            _close_against(conn, row, bucket, trade_date, quantity, currency, report)

    _update_remaining(conn, open_lots)
    return report


def _open_lot(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    key: tuple[int, int],
    trade_date: date,
    quantity: Decimal,
    currency: str,
    report: RebuildReport,
) -> Lot:
    # A transfer in preserves its original acquisition date and cost, or FIFO
    # ordering and the holding period would both silently reset.
    if row["type"] == "TRANSFER_IN" and row["orig_acquire_date"]:
        open_date = date.fromisoformat(row["orig_acquire_date"])
        cost_local = Decimal(str(row["orig_cost_local"] or row["gross_amount_local"] or 0))
    else:
        open_date = trade_date
        cost_local = Decimal(str(row["gross_amount_local"] or 0))

    # Purchase fees increase the cost basis. Applied in local currency first:
    # converting a fee separately at its own rate leaks a few cents.
    fees_local = Decimal(str(row["fees_local"] or 0))
    cost_local += fees_local

    rate = _fx_at(conn, currency, open_date, report, f"lot {key} {open_date}")
    cost_eur: Decimal | None
    if currency == fx.BASE and row["gross_amount_eur"] is not None:
        cost_eur = Decimal(str(row["gross_amount_eur"])) + Decimal(str(row["fees_eur"] or 0))
    elif rate is not None:
        cost_eur = quantize_money(fx.to_eur(cost_local, rate))
    else:
        cost_eur = None

    return Lot(
        account_id=key[0],
        instrument_id=key[1],
        open_txn_id=int(row["id"]),
        open_date=open_date,
        currency=currency,
        qty_opened=quantity,
        qty_remaining=quantity,
        cost_total_local=cost_local,
        cost_per_unit_local=(cost_local / quantity) if quantity else ZERO,
        fx_rate_at_open=rate,
        cost_total_eur=cost_eur,
        cost_per_unit_eur=(cost_eur / quantity) if (cost_eur is not None and quantity) else None,
        basis_quality=row["basis_quality"] or "EXACT",
    )


def _close_against(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    bucket: list[Lot],
    trade_date: date,
    quantity: Decimal,
    currency: str,
    report: RebuildReport,
) -> None:
    try:
        consumed = fifo_consume(bucket, quantity)
    except LotError as exc:
        report.oversold.append(f"txn {row['id']} on {trade_date}: {exc}")
        return

    gross_local = Decimal(str(row["gross_amount_local"] or 0))
    fees_local = Decimal(str(row["fees_local"] or 0))
    # Sale fees reduce proceeds, mirroring purchase fees increasing cost.
    net_local = gross_local - fees_local
    rate_at_sale = _fx_at(conn, currency, trade_date, report, f"sale {row['id']} {trade_date}")

    for lot, used in consumed:
        share = used / quantity if quantity else ZERO
        proceeds_local = net_local * share
        cost_local = lot.cost_per_unit_local * used

        proceeds_eur = cost_eur = gain_eur = fx_gain = None
        if rate_at_sale is not None:
            proceeds_eur = quantize_money(fx.to_eur(proceeds_local, rate_at_sale))
        if lot.cost_per_unit_eur is not None:
            cost_eur = quantize_money(lot.cost_per_unit_eur * used)
        if proceeds_eur is not None and cost_eur is not None:
            gain_eur = proceeds_eur - cost_eur
            if rate_at_sale is not None:
                # The part of the gain attributable purely to the currency:
                # the same original cost, re-expressed at the sale-date rate.
                cost_at_sale_rate = quantize_money(fx.to_eur(cost_local, rate_at_sale))
                fx_gain = cost_at_sale_rate - cost_eur

        lot.qty_remaining -= used
        report.disposals_created += 1
        conn.execute(
            """
            INSERT INTO lot_disposal
                (sell_txn_id, lot_id, qty, currency, proceeds_local, cost_basis_local,
                 proceeds_eur, cost_basis_eur, gross_gain_eur, fx_gain_eur,
                 taxable_gain_eur, holding_days, basis_quality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                int(row["id"]),
                lot.lot_id,
                used,
                currency,
                quantize_money(proceeds_local),
                quantize_money(cost_local),
                proceeds_eur,
                cost_eur,
                gain_eur,
                fx_gain,
                (trade_date - lot.open_date).days,
                lot.basis_quality,
            ),
        )


def _insert_lot(conn: sqlite3.Connection, lot: Lot) -> None:
    cursor = conn.execute(
        """
        INSERT INTO lot
            (account_id, instrument_id, open_txn_id, open_date, qty_opened,
             qty_remaining, currency, cost_total_local, cost_per_unit_local,
             fx_rate_at_open, cost_total_eur, cost_per_unit_eur, basis_quality)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lot.account_id,
            lot.instrument_id,
            lot.open_txn_id,
            lot.open_date.isoformat(),
            lot.qty_opened,
            lot.qty_remaining,
            lot.currency,
            lot.cost_total_local,
            lot.cost_per_unit_local,
            lot.fx_rate_at_open,
            lot.cost_total_eur,
            lot.cost_per_unit_eur,
            lot.basis_quality,
        ),
    )
    lot.lot_id = int(cursor.lastrowid or 0)


def _update_remaining(
    conn: sqlite3.Connection, open_lots: dict[tuple[int, int], list[Lot]]
) -> None:
    """Write back what each lot has left after every disposal has been applied."""
    for bucket in open_lots.values():
        for lot in bucket:
            conn.execute(
                "UPDATE lot SET qty_remaining = ? WHERE id = ?",
                (max(lot.qty_remaining, ZERO), lot.lot_id),
            )


def open_lots_for(
    conn: sqlite3.Connection, account_id: int, instrument_id: int, as_of: date | None = None
) -> list[sqlite3.Row]:
    """Lots still holding quantity, oldest first -- the FIFO order itself."""
    query = "SELECT * FROM lot WHERE account_id = ? AND instrument_id = ? AND qty_remaining > 0"
    params: list[object] = [account_id, instrument_id]
    if as_of is not None:
        query += " AND open_date <= ?"
        params.append(as_of.isoformat())
    return conn.execute(query + " ORDER BY open_date, id", tuple(params)).fetchall()
