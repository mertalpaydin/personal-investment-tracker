"""``pt accounts`` -- the accounts a portfolio is held in.

A depot arrives with the first import, because the export names its holdings.
A deposit account does not: no CSV anywhere mentions it, so until now the only
way to have one was to write the INSERT by hand. That is a poor answer for the
account that is often the largest single line in a portfolio.

The interest terms are first-class columns rather than notes, and one of them
earns its place loudly: ``withholding_rate``. Turkish *stopaj* is taken at
source, so a model that accrues gross drifts above the real balance on every
single payment date and never corrects itself.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker.core import accounts as core_accounts
from portfolio_tracker.core import interest as core_interest
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect, transaction
from portfolio_tracker.importers import deposits
from portfolio_tracker.money import ZERO

app = typer.Typer(no_args_is_help=True, help="Depots and deposit accounts.")

DbPath = Annotated[Path, typer.Option("--db", help="Database file.")]

DEPOSIT_KINDS = ("TAGESGELD", "FESTGELD")


def _rate(raw: str | None, *, label: str) -> Decimal | None:
    """Accept ``2.5`` or ``0.025`` and mean the same thing.

    A rate typed as a percentage and stored as a fraction is off by a hundred,
    which compounds silently for a year before anyone notices. Anything at or
    above 1 is read as a percentage, because no deposit pays 100% -- except
    Turkish ones, which is why the threshold is stated rather than guessed.
    """
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise typer.BadParameter(f"{label} is not a number: {raw!r}") from exc
    if value < ZERO:
        raise typer.BadParameter(f"{label} cannot be negative")
    return value / Decimal(100) if value >= Decimal(1) else value


@app.command("list")
def list_accounts(db: DbPath = DEFAULT_DB_PATH) -> None:
    """Every account, with the terms that drive its accrual."""
    conn = connect(db, read_only=True)
    try:
        rows = conn.execute(
            "SELECT a.*, p.name AS owner FROM account a JOIN person p ON p.id = a.person_id "
            "ORDER BY a.id"
        ).fetchall()
        if not rows:
            typer.echo("no accounts; run `pt init-db`")
            return
        typer.echo(f"{'id':>3}  {'name':<24} {'kind':<10} {'ccy':<4} {'rate':>7}  terms")
        for row in rows:
            rate = f"{Decimal(str(row['interest_rate'])):.2%}" if row["interest_rate"] else "-"
            terms = []
            if row["kind"] in DEPOSIT_KINDS:
                terms.append(row["interest_day_count"] or "ACT/365")
                terms.append((row["interest_frequency"] or "?").lower())
                if row["withholding_rate"] and Decimal(str(row["withholding_rate"])) > ZERO:
                    terms.append(f"{Decimal(str(row['withholding_rate'])):.3%} withheld at source")
                if row["maturity_date"]:
                    terms.append(f"matures {row['maturity_date']}")
            typer.echo(
                f"{row['id']:>3}  {row['name'][:24]:<24} {row['kind']:<10} "
                f"{row['currency']:<4} {rate:>7}  {', '.join(terms)}"
            )
    finally:
        conn.close()


@app.command()
def add(
    name: Annotated[str, typer.Option("--name", help="What you call it.")],
    kind: Annotated[
        str, typer.Option("--kind", help="DEPOT | CASH | TAGESGELD | FESTGELD.")
    ] = "TAGESGELD",
    currency: Annotated[str, typer.Option("--currency", help="ISO code.")] = "EUR",
    person_id: Annotated[int, typer.Option("--person", help="Owner.")] = 1,
    broker: Annotated[str | None, typer.Option("--broker")] = None,
    rate: Annotated[
        str | None, typer.Option("--rate", help="Annual interest, e.g. 2.5 or 0.025.")
    ] = None,
    day_count: Annotated[str, typer.Option("--day-count")] = "ACT/365",
    frequency: Annotated[
        str, typer.Option("--frequency", help="MONTHLY | QUARTERLY | ANNUAL | AT_MATURITY.")
    ] = "MONTHLY",
    withholding: Annotated[
        str | None,
        typer.Option("--withholding", help="Tax taken at source, e.g. 17.5 for Turkish stopaj."),
    ] = None,
    first_accrual: Annotated[
        str | None, typer.Option("--first-accrual", help="Value date interest starts (YYYY-MM-DD).")
    ] = None,
    maturity: Annotated[str | None, typer.Option("--maturity", help="YYYY-MM-DD.")] = None,
    rollover: Annotated[
        str, typer.Option("--rollover", help="CAPITALIZE | PAYOUT | ROLL_PRINCIPAL.")
    ] = "CAPITALIZE",
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """Create an account. Prints its id, which the other commands take."""
    # Parsed before the transaction opens: a bad --rate is a usage error, and
    # `typer.BadParameter` must not be mistaken for a database failure below.
    interest = _rate(rate, label="--rate")
    tax_at_source = _rate(withholding, label="--withholding") or ZERO

    # The schema enforces this, but its CHECK text names a column rather than
    # the thing that is missing. Interest cannot be accrued from an unknown
    # value date, and guessing one silently misstates the first period.
    if interest is not None and first_accrual is None:
        typer.secho(
            "--rate needs --first-accrual: the value date interest starts from. "
            "Without it the first period's interest cannot be computed.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    conn = connect(db)
    try:
        with transaction(conn):
            cursor = conn.execute(
                """
                INSERT INTO account
                    (person_id, name, broker, kind, currency, interest_rate,
                     interest_day_count, interest_frequency, interest_first_accrual,
                     withholding_rate, maturity_date, rollover_policy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    name,
                    broker,
                    kind.upper(),
                    currency.upper(),
                    interest,
                    day_count,
                    frequency.upper(),
                    first_accrual,
                    tax_at_source,
                    maturity,
                    rollover.upper(),
                ),
            )
            account_id = int(cursor.lastrowid or 0)
            if kind.upper() in DEPOSIT_KINDS:
                # The synthetic instrument that makes a balance a position, so
                # daily FX revaluation needs no special case for cash.
                core_accounts.ensure_cash_instrument(conn, account_id)
    except sqlite3.IntegrityError as exc:
        # A CHECK rejection: an unknown kind, frequency or rollover policy. The
        # constraint text names the column, which is the useful half.
        typer.secho(f"could not create the account: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"created account {account_id} ({name})")
    if kind.upper() in DEPOSIT_KINDS and rate is None:
        typer.secho(
            "  no --rate given, so nothing will accrue. Set one with `pt accounts set`.",
            fg=typer.colors.YELLOW,
        )


@app.command("set")
def set_terms(
    account_id: Annotated[int, typer.Argument(help="Account id.")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    rate: Annotated[str | None, typer.Option("--rate")] = None,
    withholding: Annotated[str | None, typer.Option("--withholding")] = None,
    frequency: Annotated[str | None, typer.Option("--frequency")] = None,
    first_accrual: Annotated[str | None, typer.Option("--first-accrual")] = None,
    maturity: Annotated[str | None, typer.Option("--maturity")] = None,
    rollover: Annotated[str | None, typer.Option("--rollover")] = None,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """Change an account's terms. Only the options given are written."""
    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    if rate is not None:
        updates["interest_rate"] = _rate(rate, label="--rate")
    if withholding is not None:
        updates["withholding_rate"] = _rate(withholding, label="--withholding")
    if frequency is not None:
        updates["interest_frequency"] = frequency.upper()
    if first_accrual is not None:
        updates["interest_first_accrual"] = first_accrual
    if maturity is not None:
        updates["maturity_date"] = maturity
    if rollover is not None:
        updates["rollover_policy"] = rollover.upper()

    if not updates:
        typer.echo("nothing to change; pass at least one option")
        raise typer.Exit(code=1)

    conn = connect(db)
    try:
        assignments = ", ".join(f"{column} = ?" for column in updates)
        with transaction(conn):
            changed = conn.execute(
                f"UPDATE account SET {assignments} WHERE id = ?",  # columns are from the map above
                (*updates.values(), account_id),
            ).rowcount
    except sqlite3.IntegrityError as exc:
        typer.secho(f"refused: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    if not changed:
        typer.secho(f"no account {account_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"account {account_id}: updated {', '.join(updates)}")


@app.command("import")
def import_statement(
    path: Annotated[Path, typer.Argument(help="Interest statement or term-deposit CSV.")],
    account_id: Annotated[int, typer.Option("--account", help="Target deposit account.")],
    db: DbPath = DEFAULT_DB_PATH,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would happen, write nothing.")
    ] = False,
) -> None:
    """Import a deposit statement: capital added, and interest paid.

    Idempotent, keyed on the figures the statement states. Every row is checked
    against its own arithmetic before it is booked -- a statement is a
    transcription of a bank's numbers, and a transcription error that gets
    imported silently becomes a wrong tax return.
    """
    if not path.exists():
        typer.secho(f"no such file: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        parsed = deposits.load(path)
    except deposits.DepositParseError as exc:
        typer.secho(f"could not read {path}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    conn = connect(db)
    try:
        try:
            conn.execute("BEGIN")
            result = deposits.book(conn, parsed, account_id=account_id)
        except (LookupError, ValueError) as exc:
            conn.rollback()
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    verb = "would book" if dry_run else "booked"
    typer.echo(
        f"{verb} {result.deposits} capital movement(s) and {result.interest} interest "
        f"payment(s); {result.unchanged} already present"
    )
    for finding in result.findings:
        typer.secho(f"  {finding}", fg=typer.colors.YELLOW)
    if not dry_run and (result.deposits or result.interest or result.account_updated):
        typer.echo("\nre-run `pt rebuild` to fold them in")


@app.command()
def accrue(
    account_id: Annotated[int, typer.Option("--account", help="Deposit account.")],
    end: Annotated[str | None, typer.Option("--end", help="Last day (YYYY-MM-DD).")] = None,
    start: Annotated[
        str | None, typer.Option("--start", help="First day; default is the account's value date.")
    ] = None,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """Compute daily interest between payments, and say what is owed but unpaid.

    Accrual is not a transaction. It is what the account has *earned* and not
    yet been credited with, which is a real amount the balance does not show
    and no statement reports between payment dates.
    """
    as_of = date.fromisoformat(end) if end else date.today()
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT name, currency, interest_first_accrual FROM account WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            typer.secho(f"no account {account_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        first = start or row["interest_first_accrual"]
        if first is None:
            typer.secho(
                f"account {account_id} has no --first-accrual date; interest cannot be "
                "accrued from an unknown value date",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        try:
            report = core_accounts.accrue_interest(
                conn, account_id=account_id, start=date.fromisoformat(first), end=as_of
            )
        except core_accounts.DepositError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        conn.commit()
    finally:
        conn.close()

    typer.echo(
        f"{row['name']}: {report.days} day(s) accrued to {as_of}; "
        f"{report.gross_accrued:,.2f} {row['currency']} earned and not yet paid"
    )
    for note in report.notes:
        typer.secho(f"  {note}", fg=typer.colors.YELLOW)


@app.command()
def propose_interest(
    account_id: Annotated[int, typer.Option("--account", help="Deposit account.")],
    as_of: Annotated[str | None, typer.Option("--as-of", help="YYYY-MM-DD.")] = None,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """Queue interest the schedule says is due but no statement has confirmed.

    A proposal, never an entry. An accrual is a model -- a rate, a day-count
    convention and an opening balance -- and banks round differently and move
    instant-access rates without announcing it twice. A modelled payment that
    wrote itself into the ledger would be indistinguishable from the bank's own
    figure once it was there.
    """
    when = date.fromisoformat(as_of) if as_of else date.today()
    conn = connect(db)
    try:
        try:
            due = core_interest.due_payments(conn, account_id=account_id, as_of=when)
            created = core_interest.propose(conn, account_id=account_id, as_of=when)
        except LookupError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        conn.commit()
        currency = conn.execute(
            "SELECT currency FROM account WHERE id = ?", (account_id,)
        ).fetchone()["currency"]
    finally:
        conn.close()

    if not due:
        typer.echo("no interest payments are due and unbooked")
        return
    typer.echo(f"{len(due)} payment(s) due, {created} newly queued:" + chr(10))
    typer.echo(f"  {'date':<11} {'gross':>12} {'withheld':>12} {'net':>12}")
    for payment_date, gross, withheld, net in due:
        typer.echo(
            f"  {payment_date.isoformat():<11} {gross:>12,.2f} {withheld:>12,.2f} {net:>12,.2f}"
            f"  {currency}"
        )
    typer.echo(chr(10) + "review with:  pt flows list      confirm with:  pt flows confirm <id>")
    typer.echo("If you have the bank's own figure, confirm with --amount; it outranks the model.")
