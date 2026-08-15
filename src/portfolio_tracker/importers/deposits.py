"""Importing deposit statements.

Two shapes, because the two accounts publish different things.

**Instant-access statement** -- one row per interest payment::

    date, added capital, interest gain, tax, net
    01/08/2026, 0, 43.06, 11.36, 31.70
    01/04/2026, 20186.08, 0, 0, 0

**Term deposit** -- one row describing the whole contract::

    start date, capital, interest rate, return date, total return, tax, net return
    14/08/2026, 176177, 41.5, 15/09/2026, 182586.95, 1121.74, 181465.21

Both are read as stated and then **checked against their own arithmetic** rather
than trusted. A statement is a transcription of a bank's figures, and a
transcription error that is silently imported becomes a wrong tax return.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from portfolio_tracker.core.accounts import day_count_fraction, ensure_cash_instrument
from portfolio_tracker.money import ZERO, quantize_money

#: Tolerance when checking a statement against its own arithmetic. Wide enough
#: for the bank's own rounding, narrow enough to catch a typed digit.
CHECK_TOLERANCE = Decimal("0.05")


class DepositParseError(ValueError):
    pass


@dataclass(frozen=True)
class InterestPayment:
    on: date
    added_capital: Decimal
    gross_interest: Decimal
    tax_withheld: Decimal
    net_interest: Decimal
    stated_net: str
    net_was_corrected: bool = False


@dataclass(frozen=True)
class TermDeposit:
    start: date
    maturity: date
    principal: Decimal
    annual_rate: Decimal
    gross_interest: Decimal
    tax_withheld: Decimal
    #: Interest alone, net of withholding -- NOT the maturity proceeds. The
    #: statement's "net return" column includes the principal, and conflating
    #: the two would book the whole balance as income.
    net_interest: Decimal
    net_proceeds: Decimal


@dataclass
class DepositImport:
    payments: list[InterestPayment] = field(default_factory=list)
    term: TermDeposit | None = None
    findings: list[str] = field(default_factory=list)


def _decimal(raw: str, *, field_name: str, line: int) -> Decimal:
    text = raw.strip().replace(",", "")
    if text in ("", "-", "--"):
        return ZERO
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise DepositParseError(f"line {line}: {field_name} is not a number: {raw!r}") from exc


def _day_first(raw: str, *, line: int) -> date:
    """``01/08/2026`` is 1 August.

    Day-first, unlike the broker export's month-first dates. Both appear in
    this project, so each parser is pinned rather than sniffing.
    """
    try:
        day, month, year = (int(part) for part in raw.strip().split("/"))
        return date(year, month, day)
    except (ValueError, TypeError) as exc:
        raise DepositParseError(f"line {line}: not a DD/MM/YYYY date: {raw!r}") from exc


def parse_interest_statement(text: str) -> DepositImport:
    """Read an instant-access statement, verifying each row against itself."""
    result = DepositImport()
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise DepositParseError("empty statement")

    header = [cell.strip().lower() for cell in rows[0]]
    if "date" not in header[0]:
        raise DepositParseError(f"expected a 'date' column first, got {header}")

    for offset, row in enumerate(rows[1:], start=2):
        if len(row) < 5:
            raise DepositParseError(f"line {offset}: expected 5 columns, got {len(row)}")
        when = _day_first(row[0], line=offset)
        added = _decimal(row[1], field_name="added capital", line=offset)
        gross = _decimal(row[2], field_name="interest gain", line=offset)
        tax = _decimal(row[3], field_name="tax", line=offset)

        stated_net_raw = row[4].strip()
        implied_net = gross - tax
        corrected = False

        if stated_net_raw in ("", "-", "--"):
            # No net stated: the payment has not settled yet.
            net = implied_net
            if gross > ZERO:
                result.findings.append(
                    f"{when}: interest of {gross} has no tax or net figure yet -- "
                    "treated as accrued but unpaid"
                )
        else:
            stated_net = _decimal(stated_net_raw, field_name="net", line=offset)
            if abs(stated_net - implied_net) > CHECK_TOLERANCE:
                # The statement disagrees with its own columns. Trust the
                # arithmetic over the transcription, and say so loudly -- this
                # is exactly how a mistyped digit becomes a wrong tax figure.
                corrected = True
                net = implied_net
                result.findings.append(
                    f"{when}: stated net {stated_net_raw!r} does not match "
                    f"{gross} - {tax} = {implied_net}; using the computed figure"
                )
            else:
                net = stated_net

        if tax > ZERO and gross > ZERO:
            withheld = tax / gross
            if abs(withheld - Decimal("0.26375")) > Decimal("0.005"):
                result.findings.append(
                    f"{when}: withholding is {withheld:.3%}, not the expected 26.375% "
                    "(Kapitalertragsteuer plus Solidaritätszuschlag)"
                )

        result.payments.append(
            InterestPayment(
                on=when,
                added_capital=added,
                gross_interest=gross,
                tax_withheld=tax,
                net_interest=quantize_money(net),
                stated_net=stated_net_raw,
                net_was_corrected=corrected,
            )
        )

    result.payments.sort(key=lambda payment: payment.on)
    return result


def parse_term_deposit(text: str, *, day_count: str = "ACT/365") -> DepositImport:
    """Read a term-deposit contract, verifying the interest it states."""
    result = DepositImport()
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise DepositParseError("a term deposit needs a header and at least one row")

    row = rows[1]
    if len(row) < 7:
        raise DepositParseError(f"expected 7 columns, got {len(row)}")

    start = _day_first(row[0], line=2)
    principal = _decimal(row[1], field_name="capital", line=2)
    # Stated as a percentage, not a fraction.
    annual_rate = _decimal(row[2], field_name="interest rate", line=2) / 100
    maturity = _day_first(row[3], line=2)
    total_return = _decimal(row[4], field_name="total return", line=2)
    tax = _decimal(row[5], field_name="tax", line=2)
    net_return = _decimal(row[6], field_name="net return", line=2)

    if maturity <= start:
        raise DepositParseError(f"maturity {maturity} is not after start {start}")

    stated_interest = total_return - principal
    fraction = day_count_fraction(start, maturity, day_count)
    computed = quantize_money(principal * annual_rate * fraction)

    if abs(computed - stated_interest) > CHECK_TOLERANCE:
        result.findings.append(
            f"stated interest {stated_interest} does not match {principal} x "
            f"{annual_rate:.4f} x {fraction:.6f} = {computed} on {day_count}; "
            "the day count or rate basis may differ"
        )
    else:
        result.findings.append(
            f"interest verified: {principal} x {annual_rate:.1%} over "
            f"{(maturity - start).days} days on {day_count} = {computed}"
        )

    if abs((net_return + tax) - total_return) > CHECK_TOLERANCE:
        result.findings.append(
            f"net {net_return} plus tax {tax} does not equal total {total_return}"
        )

    if stated_interest > ZERO:
        result.findings.append(
            f"withholding is {tax / stated_interest:.2%} of gross interest (Turkish stopaj)"
        )

    result.term = TermDeposit(
        start=start,
        maturity=maturity,
        principal=principal,
        annual_rate=annual_rate,
        gross_interest=quantize_money(stated_interest),
        tax_withheld=tax,
        net_interest=quantize_money(stated_interest - tax),
        net_proceeds=net_return,
    )
    return result


def load(path: Path) -> DepositImport:
    """Read either shape, choosing by the header row."""
    text = path.read_text(encoding="utf-8-sig")
    first = text.splitlines()[0].lower() if text.strip() else ""
    if "interest rate" in first and "return date" in first:
        return parse_term_deposit(text)
    return parse_interest_statement(text)


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------


@dataclass
class DepositBookResult:
    deposits: int = 0
    interest: int = 0
    unchanged: int = 0
    account_updated: bool = False
    findings: list[str] = field(default_factory=list)


def _external_id(account_id: int, kind: str, on: date, amount: Decimal) -> str:
    """Stable across re-imports of the same statement.

    Keyed on the figures the statement states rather than on a row number, so
    re-importing a file that gained rows at the top does not duplicate history.
    """
    return f"deposit:v1:{account_id}:{kind}:{on.isoformat()}:{quantize_money(amount)}"


def book(
    conn: sqlite3.Connection,
    parsed: DepositImport,
    *,
    account_id: int,
) -> DepositBookResult:
    """Turn a parsed statement into ledger entries. Idempotent.

    Two rules do the real work here.

    ``flow_class = 'INCOME'`` on interest. Interest is *return*, not money the
    owner saved, so unit accounting must issue no units for it. Booking it as a
    contribution would make the tool report the owner as having saved his own
    interest -- and would cancel exactly the return the interest represents.

    ``quantity`` carries the **capitalised** part only. Interest paid away
    leaves the account and must not raise the balance; interest left in place
    does. The statement's own columns decide which, and where they disagree
    with their arithmetic the parser has already said so.
    """
    result = DepositBookResult(findings=list(parsed.findings))
    account = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise LookupError(f"no account {account_id}")
    if account["kind"] not in ("TAGESGELD", "FESTGELD"):
        raise ValueError(
            f"account {account_id} is a {account['kind']}, not a deposit account; "
            "booking interest against a depot would misstate both balances"
        )
    currency = account["currency"]
    instrument_id = ensure_cash_instrument(conn, account_id)

    def insert(
        kind: str, on: date, gross: Decimal, *, quantity: Decimal, tax: Decimal, note: str
    ) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO transaction_
                (account_id, instrument_id, type, trade_date, currency, quantity,
                 gross_amount_local, tax_withheld_local, fx_rate, source, confidence,
                 fees_known, flow_class, external_id, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'MANUAL', 'ACTUAL', 1, ?, ?, ?)
            """,
            (
                account_id,
                instrument_id,
                kind,
                on.isoformat(),
                currency,
                quantity,
                quantize_money(gross),
                quantize_money(tax),
                "INCOME" if kind == "INTEREST" else "EXTERNAL_IN",
                _external_id(account_id, kind, on, gross),
                note,
            ),
        )
        return bool(cursor.rowcount)

    for payment in parsed.payments:
        if payment.added_capital > ZERO:
            added = insert(
                "DEPOSIT",
                payment.on,
                payment.added_capital,
                quantity=payment.added_capital,
                tax=ZERO,
                note="capital added, from the account statement",
            )
            result.deposits += added
            result.unchanged += not added
        if payment.gross_interest > ZERO:
            # A statement that shows a net figure and no capital movement has
            # capitalised it; one whose balance did not move paid it away. The
            # statement cannot distinguish these, so the account's own policy
            # does -- it is a property of the contract, not of the row.
            capitalised = (
                payment.net_interest if account["rollover_policy"] == "CAPITALIZE" else ZERO
            )
            added = insert(
                "INTEREST",
                payment.on,
                payment.gross_interest,
                quantity=capitalised,
                tax=payment.tax_withheld,
                note=("capitalised" if capitalised > ZERO else "paid out, not capitalised"),
            )
            result.interest += added
            result.unchanged += not added

    term = parsed.term
    if term is not None:
        added = insert(
            "DEPOSIT",
            term.start,
            term.principal,
            quantity=term.principal,
            tax=ZERO,
            note="term deposit principal",
        )
        result.deposits += added
        result.unchanged += not added
        # The contract's own terms, taken from the statement rather than typed
        # twice. Interest is projected from these until the payout is imported.
        # `annual_rate` is already a fraction -- the parser divides the
        # statement's "41.5" by 100 on the way in. Dividing a second time here
        # stored 0.415% and made a 41.5% term deposit accrue like a current
        # account, which is a hundredfold error that still looks like a number.
        conn.execute(
            "UPDATE account SET interest_rate = ?, maturity_date = ?, "
            "interest_first_accrual = coalesce(interest_first_accrual, ?) WHERE id = ?",
            (
                term.annual_rate,
                term.maturity.isoformat(),
                term.start.isoformat(),
                account_id,
            ),
        )
        result.account_updated = True
        result.findings.append(
            f"account {account_id}: rate set to {term.annual_rate:.2%} and maturity to "
            f"{term.maturity} from the statement"
        )

    return result
