"""Distributions.

A positions export records what was bought and sold. It does not record income,
so a distributing fund is invisible twice over -- and the second way is worse
than the first.

**Returns.** A fund's price falls on its ex-date by roughly the amount it paid.
The price feed records the fall; nothing records the payment. The portfolio's
value therefore drops with no offsetting entry, and the drop is booked as a
loss. Across this portfolio that is EUR 2,549 of return simply deleted.

**Tax.** A distribution is section 20 EStG income in the year it is paid. It
consumes the same allowance as a realised gain, and it reduces the
Vorabpauschale for the same year. Omitting it understates taxable income and
overstates remaining allowance -- which then corrupts any harvesting decision
made against that headroom.

Amounts are computed from the per-share distribution and the quantity actually
held on the ex-date, not from a current holding: a position that grew during
the year did not receive the larger figure on the earlier payment.

Nothing here writes to the ledger. Distributions become proposals carrying
their own arithmetic, and only a confirmation books one.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from portfolio_tracker.money import ZERO, quantize_money

#: Per-share amounts come from the price feed, which publishes them gross.
#: German withholding is applied by the bank and is not visible here, so it is
#: recorded as unknown rather than assumed to be zero.
WITHHOLDING_UNKNOWN = None

DividendFetcher = Callable[[str], list[tuple[date, Decimal]]]


@dataclass(frozen=True)
class Distribution:
    instrument_id: int
    import_key: str
    ex_date: date
    per_share: Decimal
    quantity: Decimal
    gross_eur: Decimal
    currency: str


@dataclass
class DividendReport:
    distributions: list[Distribution] = field(default_factory=list)
    proposed: int = 0
    skipped_no_holding: int = 0
    findings: list[str] = field(default_factory=list)

    @property
    def total_gross(self) -> Decimal:
        return sum((d.gross_eur for d in self.distributions), ZERO)


def _yfinance_dividends(symbol: str) -> list[tuple[date, Decimal]]:  # pragma: no cover - network
    import yfinance

    series = yfinance.Ticker(symbol).dividends
    if series is None or len(series) == 0:
        return []
    return [
        (stamp.date(), Decimal(str(round(float(value), 6))))
        for stamp, value in series.items()
        if float(value) > 0
    ]


def quantity_on(conn: sqlite3.Connection, instrument_id: int, when: date) -> Decimal:
    """Units held on a date, from the daily position series."""
    row = conn.execute(
        "SELECT quantity FROM position_daily WHERE instrument_id = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 1",
        (instrument_id, when.isoformat()),
    ).fetchone()
    return Decimal(str(row["quantity"])) if row else ZERO


def discover(
    conn: sqlite3.Connection,
    *,
    since: date,
    fetcher: DividendFetcher | None = None,
) -> DividendReport:
    """Find distributions on held instruments. Reads only; proposes nothing."""
    fetch = fetcher or _yfinance_dividends
    report = DividendReport()

    for instrument in conn.execute(
        """
        SELECT DISTINCT i.id, i.import_key, i.price_symbol, i.currency
        FROM instrument i JOIN transaction_ t ON t.instrument_id = i.id
        WHERE i.price_symbol IS NOT NULL AND i.is_cash_proxy = 0
        ORDER BY i.import_key
        """
    ).fetchall():
        try:
            events = fetch(instrument["price_symbol"])
        except Exception as exc:
            report.findings.append(f"{instrument['import_key']}: dividend lookup failed ({exc})")
            continue

        for ex_date, per_share in events:
            if ex_date < since:
                continue
            held = quantity_on(conn, int(instrument["id"]), ex_date)
            if held <= ZERO:
                report.skipped_no_holding += 1
                continue
            report.distributions.append(
                Distribution(
                    instrument_id=int(instrument["id"]),
                    import_key=instrument["import_key"],
                    ex_date=ex_date,
                    per_share=per_share,
                    quantity=held,
                    gross_eur=quantize_money(held * per_share),
                    currency=instrument["currency"],
                )
            )
    return report


def propose(
    conn: sqlite3.Connection, *, since: date, fetcher: DividendFetcher | None = None
) -> DividendReport:
    """Queue discovered distributions for review.

    Idempotent: the uniqueness constraint on ``pending_entry`` means re-running
    does not duplicate a proposal already waiting.
    """
    report = discover(conn, since=since, fetcher=fetcher)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    for item in report.distributions:
        account = conn.execute(
            "SELECT account_id FROM transaction_ WHERE instrument_id = ? LIMIT 1",
            (item.instrument_id,),
        ).fetchone()
        if account is None:
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO pending_entry
                (kind, account_id, proposed_date, currency, amount_local,
                 basis, derivation, created_at)
            VALUES ('DIVIDEND', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(account["account_id"]),
                item.ex_date.isoformat(),
                item.currency,
                item.gross_eur,
                f"{item.import_key}: {item.per_share} per share x {item.quantity} held "
                f"on {item.ex_date}",
                json.dumps(
                    {
                        "instrument_id": item.instrument_id,
                        "import_key": item.import_key,
                        "per_share": str(item.per_share),
                        "quantity": str(item.quantity),
                        "withholding": "unknown -- the feed publishes gross amounts",
                    }
                ),
                now,
            ),
        )
        report.proposed += cursor.rowcount

    if report.distributions:
        report.findings.append(
            "Amounts are gross. German withholding tax is applied by the bank and is "
            "not visible in the price feed, so it is recorded as unknown rather than zero."
        )
    return report


def confirm(conn: sqlite3.Connection, pending_id: int) -> int:
    """Book a distribution. Returns the new transaction id.

    ``flow_class='INCOME'`` is the load-bearing part: income is internal to the
    portfolio, so unit accounting counts it as return rather than issuing units
    for it. A distribution booked as a contribution would cancel the very
    return it represents.
    """
    row = conn.execute(
        "SELECT * FROM pending_entry WHERE id = ? AND status = 'PROPOSED' AND kind = 'DIVIDEND'",
        (pending_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no distribution proposal {pending_id} awaiting a decision")

    detail = json.loads(row["derivation"] or "{}")
    amount = Decimal(str(row["amount_local"]))

    cursor = conn.execute(
        """
        INSERT INTO transaction_
            (account_id, instrument_id, type, trade_date, currency, quantity,
             gross_amount_local, gross_amount_eur, fx_rate,
             source, confidence, flow_class, external_id, note)
        VALUES (?, ?, 'DIVIDEND', ?, ?, 0, ?, ?, 1, 'AUTO_CONFIRMED', 'ESTIMATE',
                'INCOME', ?, ?)
        """,
        (
            int(row["account_id"]),
            detail.get("instrument_id"),
            row["proposed_date"],
            row["currency"],
            amount,
            amount,
            f"div:v1:{detail.get('import_key')}:{row['proposed_date']}",
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


def distributions_for(conn: sqlite3.Connection, *, instrument_id: int, tax_year: int) -> Decimal:
    """Distributions booked on an instrument in a tax year.

    Used by the Vorabpauschale calculation, which is reduced by what the fund
    actually paid out: the charge exists only to top a fund up to a notional
    minimum return, so a fund that distributed more owes nothing.
    """
    total = ZERO
    for row in conn.execute(
        "SELECT gross_amount_eur FROM transaction_ WHERE instrument_id = ? AND type = 'DIVIDEND' "
        "AND trade_date BETWEEN ? AND ?",
        (instrument_id, f"{tax_year}-01-01", f"{tax_year}-12-31"),
    ):
        if row["gross_amount_eur"] is not None:
            total += row["gross_amount_eur"]
    return quantize_money(total)
