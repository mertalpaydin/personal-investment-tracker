"""Cash, and the purchases the source data cannot account for.

A positions export says what was bought and sold. It does not say where the
money came from, so a purchase can exceed every euro this tool knows about.

**That shortfall is not a deposit.** An earlier version of this module assumed
it was -- "you bought EUR 7,926 with EUR 0 of recorded cash, so EUR 7,926 must
have arrived" -- and proposed a contribution for it. On the owner's real data
that produced EUR 131,479 of invented savings against EUR 23,367 of recorded
ones, which is not a small error at the edges: it is 85% of the figure. It
also lumped a year of accumulated saving into whichever month a purchase
happened, and the projection then took that as a monthly rate.

The money may have been a deposit. It may equally have come from an account
this tool does not see, or from selling something it has no record of. Those
are different facts with different consequences, and nothing here can tell
them apart -- so the shortfall is recorded as **unfunded**, surfaced for a
person to explain, and counted as savings only once somebody says it was.

The model is a running cash balance per account:

* a sale credits it,
* a purchase debits it,
* a purchase exceeding the balance is recorded as unfunded, and the balance
  resets to zero rather than going negative,
* a sale marked as a withdrawal debits it instead of leaving the money to be
  reinvested.

Proceeds waiting to be reinvested stay as cash, which is the point: money that
sat in the account for three days really was held for three days, and dropping
it would show as a loss followed by an identical gain.

Nothing here writes to ``transaction_``.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

from portfolio_tracker.core import fx
from portfolio_tracker.core.calendars import TARGET2, grid_days
from portfolio_tracker.money import ZERO, quantize_money

#: A balance untouched for longer than this is surfaced at review time. It is
#: not converted to a withdrawal: inventing one would understate contributions
#: and misstate return. The user is told, and decides.
IDLE_FLAG_DAYS = 90

#: Below this a balance is rounding residue rather than real money.
DUST = Decimal("0.01")


@dataclass(frozen=True)
class FlowEvent:
    """One movement of cash, in ledger order."""

    on: date
    account_id: int
    currency: str
    kind: str  # SELL_PROCEEDS | PURCHASE | WITHDRAWAL | UNFUNDED
    amount: Decimal  # signed: positive credits the balance
    balance_after: Decimal
    txn_id: int | None = None
    note: str = ""
    #: The part of ``balance_after`` that is realised income rather than
    #: capital awaiting investment. See ``INCOME_KINDS`` and the drawdown rule.
    income_after: Decimal = ZERO
    #: Cumulative income credited to this account up to and including this
    #: event, whether or not it is still sitting in cash. Reinvested income
    #: leaves the balance but does not stop having been earned.
    income_received: Decimal = ZERO


#: Credits that are *return* rather than money entering the portfolio. The
#: distinction is the same one ``flow_class`` makes in the ledger, restated
#: here because the cash balance needs it per event rather than per row.
INCOME_KINDS = frozenset({"DISTRIBUTION", "INTEREST"})


@dataclass
class FlowReport:
    events: list[FlowEvent] = field(default_factory=list)
    #: Purchases larger than any cash this tool knows about. A question,
    #: never an answer -- see the module docstring.
    unfunded: list[FlowEvent] = field(default_factory=list)
    unresolved_sales: list[int] = field(default_factory=list)
    idle_periods: list[tuple[date, date, Decimal, int]] = field(default_factory=list)

    @property
    def total_unfunded(self) -> dict[str, Decimal]:
        """Per currency. Summing across them would add lira to euro."""
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for event in self.unfunded:
            totals[event.currency] += event.amount
        return dict(totals)


def _amount(row: sqlite3.Row) -> Decimal:
    """A transaction's cash effect in its own currency, net of fees."""
    gross = Decimal(str(row["gross_amount_local"] or 0))
    fees = Decimal(str(row["fees_local"] or 0))
    # A purchase costs its gross plus fees; a sale returns its gross less fees.
    if row["type"] == "BUY":
        return gross + fees
    return gross - fees


def compute_flows(conn: sqlite3.Connection, *, account_id: int | None = None) -> FlowReport:
    """Replay the ledger as a cash balance, inferring the deposits it implies.

    Pure with respect to the database: reads only, writes nothing. Proposing
    and confirming are separate steps on purpose.
    """
    report = FlowReport()

    scope = "WHERE t.type IN ('BUY', 'SELL', 'DIVIDEND', 'DEPOSIT', 'WITHDRAWAL', 'INTEREST')"
    params: list[object] = []
    if account_id is not None:
        scope += " AND t.account_id = ?"
        params.append(account_id)

    rows = conn.execute(
        f"""
        SELECT t.id, t.account_id, t.type, t.trade_date, t.currency,
               t.gross_amount_local, t.fees_local, t.quantity, t.proceeds_disposition,
               i.import_key, i.name AS instrument_name
        FROM transaction_ t
        LEFT JOIN instrument i ON i.id = t.instrument_id
        {scope}
        ORDER BY t.account_id, t.currency, t.trade_date,
                 CASE t.type WHEN 'DEPOSIT' THEN 0 WHEN 'SELL' THEN 1
                             WHEN 'DIVIDEND' THEN 2 WHEN 'INTEREST' THEN 3
                             WHEN 'BUY' THEN 4 ELSE 5 END,
                 t.id
        """,
        tuple(params),
    ).fetchall()

    # Seeded with whatever was in each account before this ledger begins.
    # Without it every purchase funded by money that was already there is
    # unexplained for ever, and there is no earlier data to go and fetch.
    balances: dict[tuple[int, str], Decimal] = opening_balances(conn)
    # The part of each balance that is realised income, and the running total
    # of income ever credited. The second is not derivable from the first:
    # reinvested income leaves the balance without ceasing to have been earned.
    income_pots: dict[tuple[int, str], Decimal] = {}
    income_totals: dict[tuple[int, str], Decimal] = {}

    for row in rows:
        key = (int(row["account_id"]), row["currency"])
        balance = balances.get(key, ZERO)
        income = income_pots.get(key, ZERO)
        received = income_totals.get(key, ZERO)
        when = date.fromisoformat(row["trade_date"])
        value = _amount(row)

        def record(
            kind: str,
            amount: Decimal,
            balance_after: Decimal,
            income_after: Decimal,
            received_after: Decimal,
            note: str = "",
            *,
            when: date = when,
            key: tuple[int, str] = key,
            row: sqlite3.Row = row,
        ) -> FlowEvent:
            event = FlowEvent(
                when,
                key[0],
                key[1],
                kind,
                amount,
                balance_after,
                int(row["id"]),
                note,
                income_after=income_after,
                income_received=received_after,
            )
            report.events.append(event)
            return event

        if row["type"] in ("DEPOSIT", "WITHDRAWAL"):
            # Money crossing the boundary, already recorded. It credits (or
            # debits) the balance, and it is NOT re-inferred: a confirmed
            # contribution funding a later purchase is exactly what the
            # inference was proposing in the first place.
            if row["type"] == "DEPOSIT":
                signed, balance = value, balance + value
            else:
                signed = -value
                balance, income = _spend(balance, income, value)
            record(row["type"], signed, max(balance, ZERO), income, received)
            balances[key], income_pots[key] = max(balance, ZERO), income
            continue

        if row["type"] == "INTEREST":
            # Only the capitalised part changes the balance; interest paid away
            # leaves the account. `quantity` carries what was retained.
            retained = Decimal(str(row["quantity"] or 0))
            balance += retained
            income += retained
            received += retained
            record("INTEREST", retained, balance, income, received)
            balances[key], income_pots[key], income_totals[key] = balance, income, received
            continue

        if row["type"] == "DIVIDEND":
            # Income, not a contribution: it credits cash so the ex-date price
            # fall is offset, but it is return rather than new money.
            balance += value
            income += value
            received += value
            record("DISTRIBUTION", value, balance, income, received)
            balances[key], income_pots[key], income_totals[key] = balance, income, received
            continue

        if row["type"] == "SELL":
            disposition = row["proceeds_disposition"]
            if disposition is None:
                report.unresolved_sales.append(int(row["id"]))
            if disposition == "WITHDRAW":
                # The money left the portfolio: it neither stays as cash nor
                # funds anything later.
                record(
                    "WITHDRAWAL",
                    -value,
                    balance,
                    income,
                    received,
                    "proceeds taken out of the portfolio",
                )
                continue
            balance += value
            record("SELL_PROCEEDS", value, balance, income, received)
        else:
            shortfall = value - balance
            if shortfall > DUST:
                # Recorded, not explained. Where this money came from is a
                # question for the owner: it may have been a deposit, or an
                # account this tool cannot see, or a sale it has no record of.
                # Guessing "deposit" was how 85% of the savings figure came to
                # be invented.
                # Written to be read. "purchase of 40015.36 against a cash
                # balance of 0.00" said neither what was bought nor in which
                # currency -- and on a lira fund that figure sat next to a
                # euro one, unlabelled, which reads as nonsense.
                bought = row["import_key"] or "something"
                held = (
                    f"{quantize_money(balance):,.2f} {key[1]} of recorded cash"
                    if balance > DUST
                    else "no recorded cash"
                )
                unfunded = record(
                    "UNFUNDED",
                    quantize_money(shortfall),
                    ZERO,
                    ZERO,
                    received,
                    f"buying {quantize_money(value):,.2f} {key[1]} of {bought} against {held}",
                )
                report.unfunded.append(unfunded)
                balance, income = ZERO, ZERO
            else:
                balance, income = _spend(balance, income, value)
            record("PURCHASE", -value, max(balance, ZERO), income, received)

        balances[key], income_pots[key] = max(balance, ZERO), income

    report.idle_periods = _idle_periods(report.events)
    return report


def opening_balances(conn: sqlite3.Connection) -> dict[tuple[int, str], Decimal]:
    """What each account already held, per currency, before the ledger starts."""
    return {
        (int(r["account_id"]), str(r["currency"])): Decimal(str(r["amount_local"]))
        for r in conn.execute("SELECT account_id, currency, amount_local FROM opening_balance")
    }


@dataclass(frozen=True)
class OpeningSuggestion:
    """The smallest opening balance that would explain the ledger."""

    account_id: int
    account: str
    currency: str
    #: The largest shortfall reached at any point, which is the least the
    #: account can have held for every purchase to be funded.
    needed: Decimal
    on_date: date
    unfunded_count: int
    current: Decimal


def suggest_opening_balances(conn: sqlite3.Connection) -> list[OpeningSuggestion]:
    """How much each account must have held for its purchases to make sense.

    Computed as the deepest the running balance would go if nothing were
    assumed: that low point is exactly what had to be there already. It is a
    *floor*, not a guess at the real figure -- the account may well have held
    more -- which is why it is offered as a starting number to correct rather
    than applied on anybody's behalf.
    """
    report = compute_flows(conn)
    existing = opening_balances(conn)

    worst: dict[tuple[int, str], Decimal] = {}
    counts: dict[tuple[int, str], int] = {}
    running: dict[tuple[int, str], Decimal] = {}

    # Dated at the portfolio's own first day, not at the stream's. An opening
    # balance means "this was already here"; one dated later is not an opening
    # balance at all, it is a contribution, and treating it as the former makes
    # the money appear as return on the day it shows up.
    inception = min((e.on for e in report.events), default=date.today())

    for event in report.events:
        key = (event.account_id, event.currency)
        if event.kind == "UNFUNDED":
            # The shortfall is money the balance went below zero by.
            running[key] = running.get(key, ZERO) - event.amount
            counts[key] = counts.get(key, 0) + 1
            worst[key] = min(worst.get(key, ZERO), running[key])

    names = {int(r["id"]): str(r["name"]) for r in conn.execute("SELECT id, name FROM account")}
    return [
        OpeningSuggestion(
            account_id=key[0],
            account=names.get(key[0], str(key[0])),
            currency=key[1],
            needed=quantize_money(-low),
            on_date=inception,
            unfunded_count=counts.get(key, 0),
            current=existing.get(key, ZERO),
        )
        for key, low in sorted(worst.items())
        if low < ZERO
    ]


def set_opening_balance(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    currency: str,
    amount: Decimal,
    on: date,
    source: str | None = None,
) -> None:
    """Record what an account already held. Replaces any previous figure."""
    if amount < ZERO:
        raise ValueError("an opening balance cannot be negative")
    conn.execute(
        "INSERT INTO opening_balance (account_id, currency, on_date, amount_local, source, set_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (account_id, currency) DO UPDATE SET "
        "  on_date = excluded.on_date, amount_local = excluded.amount_local, "
        "  source = excluded.source, set_at = excluded.set_at",
        (
            account_id,
            currency,
            on.isoformat(),
            str(quantize_money(amount)),
            source,
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )


def clear_opening_balance(conn: sqlite3.Connection, *, account_id: int, currency: str) -> None:
    conn.execute(
        "DELETE FROM opening_balance WHERE account_id = ? AND currency = ?",
        (account_id, currency),
    )


def _spend(balance: Decimal, income: Decimal, amount: Decimal) -> tuple[Decimal, Decimal]:
    """Draw ``amount`` out of a balance, spending capital before income.

    A bank account is fungible: no record says which euro funded which
    purchase, so this is a stated convention rather than an observation. It is
    the one that matches intent -- money deposited in order to buy something is
    spent on it -- and it is the conservative direction. Spending income first
    would report the dividends as reinvested and leave the owner believing a
    balance is capital, which is the more misleading of the two errors.
    """
    capital = balance - income
    from_capital = min(capital, amount)
    return balance - amount, income - (amount - from_capital)


def _idle_periods(events: list[FlowEvent]) -> list[tuple[date, date, Decimal, int]]:
    """Stretches where a positive balance went untouched beyond the threshold."""
    periods: list[tuple[date, date, Decimal, int]] = []
    streams: dict[tuple[int, str], list[FlowEvent]] = defaultdict(list)
    for event in events:
        streams[(event.account_id, event.currency)].append(event)
    for stream in streams.values():
        for earlier, later in pairwise(stream):
            if earlier.balance_after <= DUST:
                continue
            gap = (later.on - earlier.on).days
            if gap > IDLE_FLAG_DAYS:
                periods.append((earlier.on, later.on, earlier.balance_after, gap))
    return periods


def unfunded_purchases(
    conn: sqlite3.Connection, *, account_id: int | None = None
) -> list[FlowEvent]:
    """Purchases this tool cannot account for, for a person to explain.

    Replaces `propose_deposits`, which turned each one into a proposed
    contribution. It is the same list; what changed is that it is now a
    question rather than an answer. Marking one as new money is a decision
    somebody makes on the Trades page, and only then does it become savings.
    """
    return compute_flows(conn, account_id=account_id).unfunded


def confirm(conn: sqlite3.Connection, pending_id: int) -> int:
    """Turn a proposal into a ledger entry. Returns the new transaction id."""
    row = conn.execute(
        "SELECT * FROM pending_entry WHERE id = ? AND status = 'PROPOSED'", (pending_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no proposal {pending_id} awaiting a decision")

    is_in = row["kind"] == "EXTERNAL_IN"
    amount = Decimal(str(row["amount_local"]))
    account_id = int(row["account_id"])
    when = date.fromisoformat(row["proposed_date"])

    rate = Decimal(1)
    if row["currency"] != fx.BASE:
        rate = fx.get_rate(conn, row["currency"], when, strict=False).rate

    cursor = conn.execute(
        """
        INSERT INTO transaction_
            (account_id, instrument_id, type, trade_date, currency, quantity,
             gross_amount_local, gross_amount_eur, fx_rate,
             source, confidence, flow_class, external_id, note)
        VALUES (?, NULL, ?, ?, ?, NULL, ?, ?, ?, 'AUTO_CONFIRMED', 'RECONSTRUCTED', ?, ?, ?)
        """,
        (
            account_id,
            "DEPOSIT" if is_in else "WITHDRAWAL",
            when.isoformat(),
            row["currency"],
            amount,
            quantize_money(fx.to_eur(amount, rate)),
            rate,
            "EXTERNAL_IN" if is_in else "EXTERNAL_OUT",
            f"flow:v1:{row['kind']}:{row['id']}",
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


def reject(conn: sqlite3.Connection, pending_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE pending_entry SET status = 'REJECTED', decided_at = ?, note = ? WHERE id = ?",
        (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), reason, pending_id),
    )


def set_disposition(conn: sqlite3.Connection, txn_id: int, disposition: str) -> None:
    """Record whether a sale's proceeds were reinvested or taken out."""
    if disposition not in ("REINVEST", "WITHDRAW"):
        raise ValueError(f"disposition must be REINVEST or WITHDRAW, got {disposition!r}")
    updated = conn.execute(
        "UPDATE transaction_ SET proceeds_disposition = ? WHERE id = ? AND type = 'SELL'",
        (disposition, txn_id),
    ).rowcount
    if not updated:
        raise LookupError(f"no sale with id {txn_id}")


def build_cash_daily(conn: sqlite3.Connection, *, end: date, start: date | None = None) -> int:
    """Materialise the daily cash balance so idle proceeds are a held position."""
    report = compute_flows(conn)
    if not report.events:
        return 0

    first = min(event.on for event in report.events)
    window_start = start or first
    days = grid_days(window_start, end, TARGET2)

    conn.execute(
        "DELETE FROM cash_daily WHERE date BETWEEN ? AND ?",
        (window_start.isoformat(), end.isoformat()),
    )

    openings = opening_balances(conn)
    keys = {(e.account_id, e.currency) for e in report.events} | set(openings)
    written = 0
    for account_id, currency in keys:
        relevant = [
            e for e in report.events if (e.account_id, e.currency) == (account_id, currency)
        ]
        rates = fx.get_rate_series(conn, currency, days, strict=False)
        # From the first day of the grid, not from this stream's first event.
        #
        # Starting at zero and picking the balance up when the stream begins
        # makes an opening balance appear out of nowhere mid-series: on the
        # real ledger the valuation basis jumped from EUR 7,042 to EUR 112,301
        # in a day and the unit price with it, reporting a 16x return for
        # money that had been sitting there all along.
        balance = openings.get((account_id, currency), ZERO)
        income = ZERO
        idle = 0
        cursor = 0
        for day in days:
            moved = False
            while cursor < len(relevant) and relevant[cursor].on <= day:
                balance = relevant[cursor].balance_after
                income = relevant[cursor].income_after
                cursor += 1
                moved = True
            idle = 0 if moved else (idle + 1 if balance > DUST else 0)
            rate = rates.get(day)
            conn.execute(
                """
                INSERT INTO cash_daily
                    (date, account_id, currency, balance_local, balance_eur, fx_rate,
                     idle_days, income_local, income_eur)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day.isoformat(),
                    account_id,
                    currency,
                    quantize_money(balance),
                    quantize_money(fx.to_eur(balance, rate.rate)) if rate else None,
                    rate.rate if rate else None,
                    idle,
                    quantize_money(income),
                    quantize_money(fx.to_eur(income, rate.rate)) if rate else None,
                ),
            )
            written += 1
    return written
