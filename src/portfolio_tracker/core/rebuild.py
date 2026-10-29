"""Recompute everything derived, from the ledger down.

The whole derived layer -- lots, disposals, daily positions, daily cash, the
unit series -- is a pure function of the transaction table. So this deletes and
recomputes rather than patching: an edit anywhere in the history changes which
lots a later sale consumes, and a patched row would leave the FIFO chain
disagreeing with itself in a way nothing would surface.

This lived in the CLI until the HTTP surface needed it too. Two implementations
of "rebuild" is how they drift, and drift here means two answers to the same
question depending on which button was pressed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from portfolio_tracker.core import cash, inbox, lots, returns, savings, valuation


@dataclass
class RebuildResult:
    lots_created: int = 0
    disposals_created: int = 0
    oversold: list[str] = field(default_factory=list)
    missing_fx: int = 0
    position_days: int = 0
    grid_days: int = 0
    cash_rows: int = 0
    savings_periods: int = 0
    transfers_paired: int = 0
    #: Withdrawals matching more than one deposit. Left for a person, because
    #: guessing would erase a real contribution or invent one.
    ambiguous_transfers: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    unvalued: list[str] = field(default_factory=list)
    #: Reconciliation failures in the unit series. Non-empty means a
    #: contribution went missing and is being reported as investment return.
    breaks: list[str] = field(default_factory=list)
    as_of: date | None = None

    @property
    def ok(self) -> bool:
        return not self.breaks and not self.oversold


def default_end(conn: sqlite3.Connection, *, today: date | None = None) -> date:
    """Latest date with actual market prices or non-future transactions.

    Rebuilding before today's market close should value the portfolio up to the
    most recent priced day (or a trade entered today), rather than appending a
    synthetic flat valuation row for today using yesterday's carried-forward
    prices.
    """
    ref = today or date.today()
    price_row = conn.execute(
        """
        SELECT max(p.date) AS d
        FROM price p
        JOIN instrument i ON i.id = p.instrument_id
        WHERE i.is_cash_proxy = 0 AND i.price_source <> 'synthetic'
        """
    ).fetchone()
    txn_row = conn.execute(
        "SELECT max(trade_date) AS d FROM transaction_ WHERE trade_date <= ?",
        (ref.isoformat(),),
    ).fetchone()

    candidates: list[date] = []
    if price_row and price_row["d"]:
        candidates.append(min(date.fromisoformat(price_row["d"]), ref))
    if txn_row and txn_row["d"]:
        candidates.append(date.fromisoformat(txn_row["d"]))

    return max(candidates) if candidates else ref


def run(
    conn: sqlite3.Connection,
    *,
    end: date,
    strict: bool = False,
    commit: bool = True,
) -> RebuildResult:
    """Rebuild the derived layer up to ``end``.

    ``strict`` makes a price older than the staleness limit an error rather than
    a flag. That is right for a nightly job and wrong for a multi-year backfill,
    where one Bayram gap would abort the whole history -- hence a parameter
    rather than a constant.
    """
    result = RebuildResult(as_of=end)

    lot_report = lots.rebuild_lots(conn)
    result.lots_created = lot_report.lots_created
    result.disposals_created = lot_report.disposals_created
    result.oversold = list(lot_report.oversold)
    result.missing_fx = len(lot_report.missing_fx)
    if commit:
        conn.commit()

    value_report = valuation.build_position_daily(conn, end=end, strict=strict)
    result.position_days = value_report.rows
    result.grid_days = value_report.days
    result.stale = list(value_report.stale)
    result.unvalued = list(value_report.missing)
    if commit:
        conn.commit()

    result.cash_rows = cash.build_cash_daily(conn, end=end)
    if commit:
        conn.commit()

    for person in conn.execute("SELECT id FROM person ORDER BY id"):
        person_id = int(person["id"])

        # Before the unit series, not after. Classification decides which
        # movements are external, and unit accounting issues units against
        # exactly those -- so a transfer recognised afterwards would leave a
        # return series that had already counted it as new money.
        report = savings.classify(conn, person_id=person_id)
        result.transfers_paired += report.paired
        result.ambiguous_transfers.extend(report.ambiguous)
        if commit:
            conn.commit()

        result.breaks.extend(returns.build_portfolio_daily(conn, person_id=person_id, end=end))
        if commit:
            conn.commit()

        # After, because a month's market growth is the residual once the
        # closing value is known.
        result.savings_periods += savings.build_periods(conn, person_id=person_id)
        if commit:
            conn.commit()

    # Stamped last, and only on success: a rebuild that failed to reconcile has
    # not brought the derived tables up to date, so it must not be recorded as
    # though it had.
    if result.ok:
        conn.execute(
            "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (inbox.LAST_REBUILD_KEY,) + (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),) * 2,
        )
        if commit:
            conn.commit()

    return result
