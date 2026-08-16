"""The January charge: proposing it, booking it, and not being taxed twice.

`tax.forecast_vorabpauschale` has computed the §18 InvStG charge since v1 and
`tax.compute_disposal_tax` has applied the relief for it since v1. Neither end
was connected to anything. The forecast was never persisted, never proposed and
never bookable -- `inbox.BOOKABLE` has no entry for the kind, so confirming one
raised `NotBookable`, which was the right refusal to ship and the wrong one to
keep. And `lot.vap_accumulated_eur` and `lot_disposal.vap_offset_eur`, the two
columns the relief reads, were written by nothing at all.

This module is the middle of that chain. What it has to get right is the second
half, and it is the half that is invisible when wrong.

## Why the basis adjustment is the point

A Vorabpauschale is a *deemed* distribution: the fund is treated as having paid
out a notional minimum return that it did not pay. Tax is due on it, and no
money arrives.

When the fund is eventually sold, the gain is computed against the original
cost. Unless the Vorabpauschalen already taxed are deducted, **the same euros
are taxed a second time** -- once as a deemed distribution in January, again as
a capital gain on sale. Over a decade of accumulating funds that is a large and
entirely silent overpayment, and it is invisible because both figures are
individually correct.

`§ 20 Abs. 1 Nr. 3 Satz 5 EStG` is the relief. The tool implements it as
`lot.vap_accumulated_eur`, carried to `lot_disposal.vap_offset_eur` when the lot
is sold, and applied to the gross gain *before* Teilfreistellung by the tax
engine that already exists.

## What booking one means, precisely

**It is income, and it moves no money.** The transaction carries
``type = 'VORABPAUSCHALE'`` and ``flow_class = 'INCOME'``. Nothing in the cash
engine, the return series, the attribution or the external-flow reconciliation
looks at that type, which is correct and load-bearing: no euro entered or left
the portfolio, no value changed, and the deemed distribution must not appear as
a return. It is a tax event wearing a ledger row.

**The gross is stored, not the taxable figure.** Teilfreistellung is applied by
`tax.py`, exactly as it is for a real distribution, so there is one place that
knows the exemption rate. Storing the net would also break the basis relief,
which deducts the gross from the gain before the exemption applies.

**Dated 2 January of the following year.** The charge accrues at the end of the
year it is computed for and is taxable at the start of the next, so a 2025
Vorabpauschale is 2026 income. `tax.forecast_vorabpauschale` already dates it
that way; this module keeps that date rather than recomputing it.

## The one stated convention

The schema aggregates the charge per `(account, instrument, tax_year)` -- that
is what `vorabpauschale`'s UNIQUE constraint says -- while the relief attaches
to individual *lots*, which are derived and rebuilt from scratch on every
`pt rebuild`. A ledger row cannot reference a lot id without pointing a fact at
a derivation.

So the booked amount is allocated across the instrument's open lots **pro rata
by the quantity each still holds**, at rebuild time, by `lots.py`. It is a
convention rather than an observation. It affects *which* future sale collects
the relief, never how much relief exists in total, and it is stable under
rebuild because it depends only on stored quantities.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from portfolio_tracker.core import tax
from portfolio_tracker.money import ZERO, quantize_money

#: A charge smaller than this is not worth a review decision. Chosen at a cent
#: rather than at zero because a fund that rose by a rounding error produces a
#: Basisertrag of fractions of a cent, and an inbox full of those is an inbox
#: nobody reads.
MINIMUM_CHARGE = Decimal("0.01")


@dataclass(frozen=True)
class Charge:
    """One fund's Vorabpauschale for one year, aggregated across its lots."""

    account_id: int
    instrument_id: int
    import_key: str
    tax_year: int
    gross_eur: Decimal
    taxable_eur: Decimal
    teilfreistellung_eur: Decimal
    distributions_eur: Decimal
    basisertrag_eur: Decimal
    basiszins: Decimal
    quantity: Decimal
    jan_first_price: Decimal | None
    year_end_price: Decimal | None
    entry_date: date
    lots: int = 1

    @property
    def basis(self) -> str:
        """The derivation, in words, for the review screen.

        The whole reason a confirmation is a decision rather than a click: the
        arithmetic sits next to the button, including the fact that no money
        will arrive and that the cost basis moves.
        """
        return (
            f"{self.import_key}: deemed distribution for {self.tax_year} under section 18 "
            f"InvStG, across {self.lots} lot(s). Basiszins {self.basiszins:.4%}, "
            f"Basisertrag EUR {self.basisertrag_eur:,.2f}"
            + (
                f", less EUR {self.distributions_eur:,.2f} actually distributed"
                if self.distributions_eur > ZERO
                else ""
            )
            + f" -> gross EUR {self.gross_eur:,.2f}, taxable EUR {self.taxable_eur:,.2f} "
            f"after Teilfreistellung. No cash arrives; the cost basis rises by the gross, "
            f"so the same amount is not taxed again on sale. Taxable in {self.tax_year + 1}."
        )


@dataclass
class ProposalResult:
    tax_year: int
    charges: list[Charge] = field(default_factory=list)
    created: int = 0
    skipped_zero: int = 0
    already_pending: int = 0

    @property
    def total_taxable_eur(self) -> Decimal:
        return quantize_money(sum((c.taxable_eur for c in self.charges), ZERO))


def compute(conn: sqlite3.Connection, *, tax_year: int) -> list[Charge]:
    """Aggregate the per-lot forecast into one charge per fund.

    `tax.forecast_vorabpauschale` works per lot, because the pro-ration by
    acquisition month is a per-lot rule. The schema stores one row per fund per
    year. Summing is the correct reconciliation of the two: the charge on a
    holding is the charge on its lots.
    """
    grouped: dict[tuple[int, int], list[tax.VorabpauschaleRow]] = {}
    for row in tax.forecast_vorabpauschale(conn, tax_year=tax_year):
        grouped.setdefault((row.account_id, row.instrument_id), []).append(row)

    charges: list[Charge] = []
    for (account_id, instrument_id), rows in grouped.items():
        first = rows[0]
        charges.append(
            Charge(
                account_id=account_id,
                instrument_id=instrument_id,
                import_key=first.instrument,
                tax_year=tax_year,
                gross_eur=quantize_money(sum((r.vap_gross_eur for r in rows), ZERO)),
                taxable_eur=quantize_money(sum((r.taxable_eur for r in rows), ZERO)),
                teilfreistellung_eur=quantize_money(
                    sum((r.teilfreistellung_eur for r in rows), ZERO)
                ),
                # Distributions are a property of the fund and the year, not of
                # a lot, so `forecast_vorabpauschale` repeats the same figure on
                # every lot row. Summing it would multiply it by the lot count.
                distributions_eur=first.distributions_eur,
                basisertrag_eur=quantize_money(sum((r.basisertrag for r in rows), ZERO)),
                basiszins=first.basiszins,
                quantity=sum((r.quantity for r in rows), ZERO),
                jan_first_price=first.jan_first_price,
                year_end_price=first.year_end_price,
                entry_date=first.entry_date,
                lots=len(rows),
            )
        )
    return sorted(charges, key=lambda c: c.import_key)


def persist(conn: sqlite3.Connection, charges: list[Charge], *, status: str = "FORECAST") -> int:
    """Store the computed charge, replacing any earlier figure for that year.

    Replaced rather than merged: a forecast recomputed after a price update or a
    newly booked distribution is a *better* figure for the same year, not an
    additional one, and keeping both would leave two answers to one question.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    for charge in charges:
        conn.execute(
            "DELETE FROM vorabpauschale WHERE account_id = ? AND instrument_id = ? "
            "AND tax_year = ?",
            (charge.account_id, charge.instrument_id, charge.tax_year),
        )
        conn.execute(
            """
            INSERT INTO vorabpauschale
                (account_id, instrument_id, tax_year, jan_first_price, year_end_price,
                 qty_held, basiszins, basisertrag_eur, distributions_eur,
                 full_months_before, teilfreistellung_eur, taxable_eur, status, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                charge.account_id,
                charge.instrument_id,
                charge.tax_year,
                charge.jan_first_price,
                charge.year_end_price,
                charge.quantity,
                charge.basiszins,
                charge.basisertrag_eur,
                charge.distributions_eur,
                charge.teilfreistellung_eur,
                charge.taxable_eur,
                status,
                now,
            ),
        )
        written += 1
    return written


def propose(conn: sqlite3.Connection, *, tax_year: int) -> ProposalResult:
    """Compute the year's charge, store it, and queue it for review.

    Idempotent, like every other proposer here: the uniqueness index on
    `pending_entry` means a second run against unchanged inputs queues nothing.
    Nothing reaches the ledger without a person confirming it -- and this is the
    kind where that matters most, because the figure rests on a statutory rate
    that is published once a year and on a Teilfreistellung classification that
    the tool cannot verify.
    """
    result = ProposalResult(tax_year=tax_year)
    charges = compute(conn, tax_year=tax_year)
    persist(conn, charges)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for charge in charges:
        result.charges.append(charge)
        if charge.gross_eur < MINIMUM_CHARGE:
            result.skipped_zero += 1
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO pending_entry
                (kind, account_id, proposed_date, currency, amount_local,
                 basis, derivation, source_txn_id, created_at)
            VALUES ('VORABPAUSCHALE', ?, ?, 'EUR', ?, ?, ?, NULL, ?)
            """,
            (
                str(charge.account_id),
                charge.entry_date.isoformat(),
                charge.gross_eur,
                charge.basis,
                json.dumps(
                    {
                        "instrument_id": charge.instrument_id,
                        "import_key": charge.import_key,
                        "tax_year": charge.tax_year,
                        "gross_eur": str(charge.gross_eur),
                        "taxable_eur": str(charge.taxable_eur),
                        "basiszins": str(charge.basiszins),
                        "rule": "section 18 InvStG deemed distribution",
                        "no_cash_movement": True,
                    }
                ),
                now,
            ),
        )
        if cursor.rowcount:
            result.created += 1
        else:
            result.already_pending += 1
    return result


def confirm(conn: sqlite3.Connection, pending_id: int) -> int:
    """Book a Vorabpauschale. Returns the new transaction id.

    Two things make this different from every other booking rule here.

    It records **income that is not a cash flow and not a return**. No euro
    moves, so the cash engine, the unit series and the attribution all
    deliberately ignore `type = 'VORABPAUSCHALE'`. Booking it as a DIVIDEND
    instead would credit cash that does not exist and report a return the
    portfolio did not earn.

    It stores the **gross**. Teilfreistellung is applied by the tax engine, and
    the cost-basis relief deducts the gross from the gain before the exemption
    applies -- so storing the net would silently halve the relief.
    """
    row = conn.execute(
        "SELECT * FROM pending_entry WHERE id = ? AND status = 'PROPOSED' "
        "AND kind = 'VORABPAUSCHALE'",
        (pending_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no Vorabpauschale proposal {pending_id} awaiting a decision")

    detail = json.loads(row["derivation"] or "{}")
    amount = Decimal(str(row["amount_local"]))
    instrument_id = detail.get("instrument_id")
    if instrument_id is None:
        raise ValueError(
            f"proposal {pending_id} has no instrument; the relief attaches to a holding "
            "and cannot be booked against the account alone"
        )

    cursor = conn.execute(
        """
        INSERT INTO transaction_
            (account_id, instrument_id, type, trade_date, currency, quantity,
             gross_amount_local, gross_amount_eur, fx_rate,
             source, confidence, flow_class, external_id, note)
        VALUES (?, ?, 'VORABPAUSCHALE', ?, 'EUR', 0, ?, ?, 1, 'AUTO_CONFIRMED', 'ESTIMATE',
                'INCOME', ?, ?)
        """,
        (
            int(row["account_id"]),
            int(instrument_id),
            row["proposed_date"],
            amount,
            amount,
            # One charge per fund per year, and the year is the one it accrued
            # for rather than the one it is taxed in -- so re-proposing after a
            # price correction cannot produce a second row for the same charge.
            f"vap:v1:{detail.get('import_key')}:{detail.get('tax_year')}",
            row["basis"],
        ),
    )
    txn_id = int(cursor.lastrowid or 0)
    conn.execute(
        "UPDATE vorabpauschale SET status = 'ACTUAL' WHERE account_id = ? "
        "AND instrument_id = ? AND tax_year = ?",
        (int(row["account_id"]), int(instrument_id), detail.get("tax_year")),
    )
    conn.execute(
        "UPDATE pending_entry SET status = 'CONFIRMED', resulting_txn_id = ?, decided_at = ? "
        "WHERE id = ?",
        (txn_id, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), pending_id),
    )
    return txn_id


def booked_for(
    conn: sqlite3.Connection, *, instrument_id: int, account_id: int, up_to: date | None = None
) -> Decimal:
    """Vorabpauschale booked on a holding, in euro, gross.

    What `lots.py` allocates across the open lots. Bounded by date so a rebuild
    to a past date does not apply relief that had not been charged yet.
    """
    query = (
        "SELECT gross_amount_eur FROM transaction_ WHERE type = 'VORABPAUSCHALE' "
        "AND instrument_id = ? AND account_id = ?"
    )
    params: list[object] = [instrument_id, account_id]
    if up_to is not None:
        query += " AND trade_date <= ?"
        params.append(up_to.isoformat())

    total = ZERO
    for row in conn.execute(query, tuple(params)):
        if row["gross_amount_eur"] is not None:
            total += Decimal(str(row["gross_amount_eur"]))
    return total


def taxable_income(
    conn: sqlite3.Connection, *, tax_year: int, person_id: int | None = None
) -> list[tuple[str, Decimal]]:
    """Booked Vorabpauschale as taxable income, after Teilfreistellung.

    Symmetric with `tax.distribution_income`, and for the same reason: it is
    income in the year it is charged, taxed on the same footing as a realised
    gain, and consuming the same Sparerpauschbetrag. Leaving it out understates
    income and overstates the headroom a harvesting decision is made against.
    """
    query = """
        SELECT i.id, i.import_key, t.gross_amount_eur
        FROM transaction_ t
        JOIN instrument i ON i.id = t.instrument_id
        JOIN account a ON a.id = t.account_id
        WHERE t.type = 'VORABPAUSCHALE' AND i.tax_regime = ?
          AND t.trade_date BETWEEN ? AND ?
    """
    params: list[object] = [tax.IN_SCOPE_REGIME, f"{tax_year}-01-01", f"{tax_year}-12-31"]
    if person_id is not None:
        query += " AND a.person_id = ?"
        params.append(person_id)

    out: list[tuple[str, Decimal]] = []
    for row in conn.execute(query, tuple(params)):
        if row["gross_amount_eur"] is None:
            continue
        rate = tax.teilfreistellung(conn, int(row["id"]))
        out.append((row["import_key"], quantize_money(row["gross_amount_eur"] * (1 - rate))))
    return out
