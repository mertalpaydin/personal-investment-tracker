"""Orchestration and the only database writes in this package.

Idempotent by construction: every row carries a deterministic ``external_id``,
and re-importing an unchanged export inserts nothing.

The one subtle rule is that a reconstructed purchase quantity may **grow but
never shrink**. If the upstream site ever prunes old closed rows, the sum of
disposals falls and the reconstructed purchase would silently shrink with it,
retroactively deleting history. A genuine partial sale moves quantity from the
open side to the closed side and leaves the sum flat, so a falling sum is never
legitimate.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from portfolio_tracker.importers.investing_com.normalize import load_closed, load_open
from portfolio_tracker.importers.investing_com.parse import ParseError
from portfolio_tracker.importers.investing_com.reconcile import (
    QtyConfidence,
    ReconstructedLot,
    build_lots,
)
from portfolio_tracker.importers.overrides import Overrides
from portfolio_tracker.importers.report import ImportReport, Severity
from portfolio_tracker.money import plain

# Instruments the source data contains that need a decision before the tax
# engine can touch them. Guessing any of these is expensive in both directions,
# so the importer records what it does not know rather than picking a default.
_CRYPTO_HINT = "/"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _instrument_id(
    conn: sqlite3.Connection, lot: ReconstructedLot, report: ImportReport
) -> tuple[int, bool]:
    row = conn.execute("SELECT id FROM instrument WHERE import_key = ?", (lot.symbol,)).fetchone()
    if row is not None:
        return int(row["id"]), False

    # A slash in the symbol marks a currency pair, which in this data means
    # crypto. That is section 23 EStG (private disposal), not section 20 --
    # its losses may only offset other private-disposal gains, never capital
    # income. Left on the default path it would land in the wrong loss pot.
    is_crypto = _CRYPTO_HINT in lot.symbol
    asset_class = "CRYPTO" if is_crypto else ("FUND" if lot.currency != "EUR" else "ETF")
    tax_regime = "DE_PRIVATE_SALE" if is_crypto else "DE_INVESTMENT"
    note = None

    if is_crypto:
        tax_scope_note = "crypto: section 23 EStG private disposal, not section 20"
        report.add(
            "ASSET_CLASS_OUT_OF_SCOPE",
            Severity.WARNING,
            f"{lot.symbol}: treated as a private disposal (section 23 EStG); "
            "its losses must not offset capital income",
            symbol=lot.symbol,
        )
    elif lot.currency != "EUR":
        tax_regime = "OUT_OF_SCOPE"
        tax_scope_note = f"{lot.currency} fund; tax treatment not established"
        report.add(
            "TAX_CLASS_UNKNOWN",
            Severity.WARNING,
            f"{lot.symbol}: Teilfreistellung unresolved; left NULL rather than defaulting",
            symbol=lot.symbol,
        )
    else:
        tax_scope_note = None
        note = "teilfreistellung_pct unresolved"

    cursor = conn.execute(
        """
        INSERT INTO instrument
            (import_key, name, asset_class, currency, tax_regime, tax_scope_note,
             price_source, trading_calendar, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OWNED', ?)
        """,
        (
            lot.symbol,
            lot.name,
            asset_class,
            lot.currency,
            tax_regime,
            tax_scope_note,
            "tefas" if lot.currency == "TRY" else "yfinance",
            "TEFAS" if lot.currency == "TRY" else "XETRA",
            note,
        ),
    )
    return int(cursor.lastrowid or 0), True


def _upsert_buy(
    conn: sqlite3.Connection,
    lot: ReconstructedLot,
    instrument_id: int,
    account_id: int,
    batch_id: int | None,
    fees_eur: Decimal | None,
    report: ImportReport,
) -> str:
    """Insert or update the purchase. Returns 'inserted' | 'updated' | 'unchanged'."""
    external_id = lot.buy_external_id
    existing = conn.execute(
        "SELECT id, quantity FROM transaction_ WHERE external_id = ?", (external_id,)
    ).fetchone()

    quantity = lot.quantity.value
    price = lot.open_price
    gross_local = quantity * price
    is_eur = lot.currency == "EUR"

    if existing is not None:
        previous = Decimal(str(existing["quantity"]))
        if quantity < previous:
            report.add(
                "BUY_QTY_SHRANK",
                Severity.ERROR,
                f"{lot.key}: reconstructed purchase fell from {plain(previous)} to "
                f"{plain(quantity)}. A partial sale keeps the sum flat, so this means "
                "closed rows were dropped upstream. Not applied.",
                key=lot.key,
            )
            return "unchanged"
        if quantity == previous:
            return "unchanged"
        conn.execute(
            "UPDATE transaction_ SET quantity = ?, gross_amount_local = ? WHERE id = ?",
            (quantity, gross_local, existing["id"]),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO transaction_
            (account_id, instrument_id, type, trade_date, currency, quantity,
             price_per_unit_local, gross_amount_local, fees_eur, fees_known,
             fx_rate, gross_amount_eur,
             source, confidence, quantity_confidence, quantity_lo, quantity_hi,
             basis_quality, flow_class, external_id, import_batch_id, note)
        VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'IMPORT', ?, ?, ?, ?, ?,
                'INVESTMENT', ?, ?, ?)
        """,
        (
            account_id,
            instrument_id,
            lot.open_date.isoformat(),
            lot.currency,
            quantity,
            price,
            gross_local,
            fees_eur,
            1 if fees_eur is not None else 0,
            Decimal(1) if is_eur else None,
            lot.cost_basis_eur if is_eur else None,
            "RECONSTRUCTED",
            str(lot.quantity.confidence),
            lot.quantity.interval.lo,
            lot.quantity.interval.hi,
            lot.basis_quality,
            external_id,
            batch_id,
            lot.quantity.note or None,
        ),
    )
    return "inserted"


def _insert_sells(
    conn: sqlite3.Connection,
    lot: ReconstructedLot,
    instrument_id: int,
    account_id: int,
    batch_id: int | None,
) -> int:
    inserted = 0
    for disposal in lot.disposals:
        external_id = lot.sell_external_id(disposal)
        exists = conn.execute(
            "SELECT 1 FROM transaction_ WHERE external_id = ?", (external_id,)
        ).fetchone()
        if exists:
            continue
        is_eur = lot.currency == "EUR"
        conn.execute(
            """
            INSERT INTO transaction_
                (account_id, instrument_id, type, trade_date, currency, quantity,
                 price_per_unit_local, gross_amount_local, fees_known,
                 fx_rate, gross_amount_eur,
                 source, confidence, basis_quality, flow_class, external_id,
                 import_batch_id)
            VALUES (?, ?, 'SELL', ?, ?, ?, ?, ?, 0, ?, ?, 'IMPORT', 'RECONSTRUCTED',
                    ?, 'INVESTMENT', ?, ?)
            """,
            (
                account_id,
                instrument_id,
                disposal.close_date.isoformat(),
                lot.currency,
                disposal.qty,
                disposal.close_price,
                disposal.qty * disposal.close_price,
                Decimal(1) if is_eur else None,
                (disposal.qty * disposal.close_price) if is_eur else None,
                lot.basis_quality,
                external_id,
                batch_id,
            ),
        )
        inserted += 1
    return inserted


def import_positions(
    conn: sqlite3.Connection,
    open_csv: Path,
    closed_csv: Path,
    *,
    account_id: int,
    overrides: Overrides | None = None,
    dry_run: bool = False,
    strict: bool = False,
) -> ImportReport:
    """Import a positions export. Idempotent; wraps everything in one transaction.

    ``dry_run`` rolls back and returns the report that *would* have resulted.
    ``strict`` promotes any warning to a failure, which is what CI runs.
    """
    report = ImportReport(dry_run=dry_run)
    overrides = overrides or Overrides()

    report.source_files = {open_csv.name: _sha256(open_csv), closed_csv.name: _sha256(closed_csv)}

    try:
        open_rows = load_open(open_csv)
        closed_rows = load_closed(closed_csv)
    except ParseError as exc:
        report.add("PARSE_FAILED", Severity.ERROR, str(exc))
        return report

    report.rows_read = len(open_rows) + len(closed_rows)
    report.rows_parsed = report.rows_read

    lots = build_lots(open_rows, closed_rows, report, overrides.quantities)
    report.lots_reconstructed = len(lots)
    report.instruments_seen = len({lot.symbol for lot in lots})

    conn.execute("BEGIN")
    try:
        batch_id: int | None = None
        if not dry_run:
            cursor = conn.execute(
                """
                INSERT INTO import_batch (filename, source, source_sha256, imported_at, rows_total)
                VALUES (?, 'investing_com', ?, ?, ?)
                """,
                (
                    open_csv.name,
                    report.source_files[open_csv.name],
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    report.rows_read,
                ),
            )
            batch_id = int(cursor.lastrowid or 0)

        for lot in lots:
            instrument_id, created = _instrument_id(conn, lot, report)
            if created:
                report.instruments_created += 1

            outcome = _upsert_buy(
                conn,
                lot,
                instrument_id,
                account_id,
                batch_id,
                overrides.fees_eur.get(lot.key),
                report,
            )
            if outcome == "inserted":
                report.buys_inserted += 1
            elif outcome == "updated":
                report.buys_updated += 1
            else:
                report.unchanged += 1

            report.sells_inserted += _insert_sells(conn, lot, instrument_id, account_id, batch_id)

        _report_gaps(lots, report)

        if dry_run or (strict and report.warnings) or not report.ok:
            conn.rollback()
        else:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise

    return report


def _report_gaps(lots: list[ReconstructedLot], report: ImportReport) -> None:
    """State what the source could not provide, rather than letting it pass."""
    without_fees = len(lots)
    if without_fees:
        report.add(
            "FEES_UNKNOWN",
            Severity.WARNING,
            f"{without_fees} lot(s) have no fee data; the source export contains none. "
            "Cost basis is understated, so every computed taxable gain is biased high.",
            lots=without_fees,
        )

    foreign = [lot for lot in lots if lot.currency != "EUR"]
    if foreign:
        report.add(
            "FX_BACKFILL_REQUIRED",
            Severity.WARNING,
            f"{len(foreign)} lot(s) are not euro-denominated and have no trade-date rate. "
            "gross_amount_eur left NULL rather than fabricated from a current spot rate.",
            lots=len(foreign),
        )

    estimated = [lot for lot in lots if lot.quantity.confidence is QtyConfidence.ESTIMATED]
    if estimated:
        report.add(
            "QUANTITY_UNRESOLVED",
            Severity.WARNING,
            f"{len(estimated)} lot(s) still have an estimated quantity; supply overrides",
            keys=[lot.key for lot in estimated],
        )
