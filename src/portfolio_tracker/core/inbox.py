"""The review inbox: one place that decides what a confirmation means.

Nothing the tool infers writes to the ledger. Inferences land in
``pending_entry`` and stay there until a person accepts them, which is the rule
the whole design rests on -- a plausible number nobody checked is how a tracker
starts lying convincingly.

Different kinds of proposal book differently: an inferred contribution becomes a
DEPOSIT, a discovered distribution or a modelled interest credit becomes income
with ``flow_class = INCOME``. That routing lived only in the CLI. Two callers now need it, and two
implementations of "what does confirming this mean" is how they drift, so it
lives here and both call it.

The part worth stating: **the amount can be edited on the way in, and the
original is kept.** An inferred contribution is a guess about money the source
never recorded, so the owner is frequently the better authority. Overwriting the
guess silently would destroy the evidence that a guess was ever made.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_tracker.core import cash, interest, vorabpauschale
from portfolio_tracker.ingest import dividends
from portfolio_tracker.money import ZERO, quantize_money

#: Kinds that have a booking rule. A kind can be proposed before anything knows
#: how to turn it into a transaction; confirming one of those must fail loudly
#: rather than quietly do nothing.
BOOKABLE = {
    "EXTERNAL_IN": cash.confirm,
    "EXTERNAL_OUT": cash.confirm,
    "DIVIDEND": dividends.confirm,
    "INTEREST": interest.confirm,
    # Added last, and the odd one out: it books income that moves no money.
    # Confirming it also raises the cost basis of the lots it applies to, so
    # the same euros are not taxed again on sale -- which is why it needed a
    # rule of its own rather than being routed through the dividend path.
    "VORABPAUSCHALE": vorabpauschale.confirm,
}


class NotBookable(RuntimeError):
    """The proposal is valid but nothing yet knows how to book its kind."""


@dataclass(frozen=True)
class PendingEntry:
    id: int
    kind: str
    account_id: int | None
    proposed_date: date
    currency: str
    amount_local: Decimal
    basis: str
    derivation: dict[str, Any]
    source_txn_id: int | None
    status: str
    resulting_txn_id: int | None
    note: str | None

    @property
    def bookable(self) -> bool:
        return self.kind in BOOKABLE


def _row_to_entry(row: sqlite3.Row) -> PendingEntry:
    return PendingEntry(
        id=int(row["id"]),
        kind=row["kind"],
        account_id=int(row["account_id"]) if row["account_id"] is not None else None,
        proposed_date=date.fromisoformat(row["proposed_date"]),
        currency=row["currency"],
        amount_local=Decimal(str(row["amount_local"])),
        basis=row["basis"] or "",
        derivation=json.loads(row["derivation"]) if row["derivation"] else {},
        source_txn_id=int(row["source_txn_id"]) if row["source_txn_id"] is not None else None,
        status=row["status"],
        resulting_txn_id=(
            int(row["resulting_txn_id"]) if row["resulting_txn_id"] is not None else None
        ),
        note=row["note"],
    )


def get(conn: sqlite3.Connection, pending_id: int) -> PendingEntry:
    row = conn.execute("SELECT * FROM pending_entry WHERE id = ?", (pending_id,)).fetchone()
    if row is None:
        raise LookupError(f"no proposal {pending_id}")
    return _row_to_entry(row)


def listing(
    conn: sqlite3.Connection,
    *,
    status: str = "PROPOSED",
    limit: int = 200,
    due_by: date | None = None,
) -> list[PendingEntry]:
    """Proposals awaiting a decision.

    ``due_by`` hides anything dated later than that day. An advance tax charge
    forecast for January 2027 is a real proposal and worth keeping, but it is
    not something anybody can act on today -- and an alert counting it sits on
    every page for months, teaching the reader that the alert strip is noise.
    Callers that nag pass today; callers that list everything do not.
    """
    query = "SELECT * FROM pending_entry WHERE status = ?"
    params: list[object] = [status]
    if due_by is not None:
        query += " AND proposed_date <= ?"
        params.append(due_by.isoformat())
    query += " ORDER BY proposed_date, id LIMIT ?"
    params.append(limit)
    return [_row_to_entry(row) for row in conn.execute(query, params)]


def due_now(conn: sqlite3.Connection, *, on: date | None = None) -> int:
    """How many proposals are actually actionable today.

    What the alert strip counts. Everything else waits until its date.
    """
    return len(listing(conn, due_by=on or date.today()))


def confirm(
    conn: sqlite3.Connection, pending_id: int, *, amount_local: Decimal | None = None
) -> int:
    """Book a proposal, optionally at a corrected amount. Returns the txn id.

    Editing rewrites ``amount_local`` before the kind-specific booking runs, so
    every booking path sees one amount and there is no second code path that
    only the edited case exercises. The original figure and the fact that it was
    overridden are appended to ``derivation``, which is the record that this
    number came from a person rather than from the arithmetic.
    """
    entry = get(conn, pending_id)
    if entry.status != "PROPOSED":
        raise LookupError(f"proposal {pending_id} is already {entry.status.lower()}")
    if not entry.bookable:
        raise NotBookable(
            f"proposals of kind {entry.kind} cannot be booked yet; "
            "confirming one would record nothing and report success"
        )

    if amount_local is not None:
        if amount_local <= ZERO:
            raise ValueError("a confirmed amount must be positive")
        corrected = quantize_money(amount_local)
        derivation = dict(entry.derivation)
        derivation["amount_as_inferred"] = str(entry.amount_local)
        derivation["amount_as_confirmed"] = str(corrected)
        derivation["edited_by_owner"] = True
        conn.execute(
            "UPDATE pending_entry SET amount_local = ?, derivation = ?, "
            "basis = basis || ? WHERE id = ?",
            (
                corrected,
                json.dumps(derivation),
                f" [corrected by the owner from {entry.amount_local} to {corrected}]",
                pending_id,
            ),
        )

    return BOOKABLE[entry.kind](conn, pending_id)


def reject(conn: sqlite3.Connection, pending_id: int, reason: str) -> None:
    """Decline a proposal. A reason is required, not optional.

    A rejected inference is a claim about reality -- "no money arrived that day"
    -- and in six months the only way to re-evaluate it is to know why.
    """
    if not reason.strip():
        raise ValueError("a rejection needs a reason; it is a claim about what happened")
    entry = get(conn, pending_id)
    if entry.status != "PROPOSED":
        raise LookupError(f"proposal {pending_id} is already {entry.status.lower()}")
    cash.reject(conn, pending_id, reason.strip())


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["status"]: int(row["n"])
        for row in conn.execute("SELECT status, count(*) AS n FROM pending_entry GROUP BY status")
    }


#: Set by ``core.rebuild.run``. Compared against the newest decision to tell
#: whether the derived tables predate a confirmation.
LAST_REBUILD_KEY = "last_rebuild_at"


def stale_after_decision(conn: sqlite3.Connection) -> bool:
    """Whether a decision has been made that the derived tables predate.

    Confirming a contribution changes which lots a later sale consumes and every
    return figure downstream. Reporting the old numbers afterwards would be
    worse than refusing to answer, so callers are told to rebuild.

    Both sides are ISO-8601 UTC timestamps written by this process, so a string
    comparison is a chronological one.
    """
    decided = conn.execute(
        "SELECT max(decided_at) AS at FROM pending_entry WHERE status IN ('CONFIRMED', 'REJECTED')"
    ).fetchone()
    if decided is None or decided["at"] is None:
        return False
    built = conn.execute("SELECT value FROM setting WHERE key = ?", (LAST_REBUILD_KEY,)).fetchone()
    if built is None:
        return True
    return str(decided["at"]) > str(built["value"])
