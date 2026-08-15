"""``pt flows`` -- reviewing the cash movements the source data never recorded.

Inferences are proposals, never entries. The rule is the one the original brief
set: nothing auto-generated writes to the ledger, because a plausible number
that nobody checked is exactly how a tracker starts lying convincingly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker.core import cash, inbox
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect

app = typer.Typer(no_args_is_help=True, help="Review inferred contributions and withdrawals.")

DbPath = Annotated[Path, typer.Option("--db", help="Database file.")]


@app.command("list")
def list_flows(db: DbPath = DEFAULT_DB_PATH, limit: int = 40) -> None:
    """Show what is waiting for a decision, and why."""
    conn = connect(db)
    try:
        report = cash.compute_flows(conn)

        pending = conn.execute(
            "SELECT id, proposed_date, currency, amount_local, basis, source_txn_id "
            "FROM pending_entry WHERE status = 'PROPOSED' AND kind = 'EXTERNAL_IN' "
            "ORDER BY proposed_date LIMIT ?",
            (limit,),
        ).fetchall()

        if pending:
            typer.echo(f"{len(pending)} inferred contribution(s) awaiting confirmation:\n")
            typer.echo(f"  {'id':>4}  {'date':<11} {'amount':>14}  basis")
            for row in pending:
                typer.echo(
                    f"  {row['id']:>4}  {row['proposed_date']:<11} "
                    f"{row['currency']} {row['amount_local']:>10}  {row['basis']}"
                )
        else:
            typer.echo("no contributions awaiting confirmation")

        unresolved = conn.execute(
            """
            SELECT t.id, t.trade_date, i.import_key, t.currency, t.gross_amount_local
            FROM transaction_ t JOIN instrument i ON i.id = t.instrument_id
            WHERE t.type = 'SELL' AND t.proceeds_disposition IS NULL
            ORDER BY t.trade_date
            """
        ).fetchall()
        if unresolved:
            typer.echo(
                f"\n{len(unresolved)} sale(s) not yet classified "
                "(treated as reinvestment until you say otherwise):\n"
            )
            typer.echo(f"  {'id':>4}  {'date':<11} {'symbol':<8} {'proceeds':>14}")
            for row in unresolved:
                typer.echo(
                    f"  {row['id']:>4}  {row['trade_date']:<11} {row['import_key']:<8} "
                    f"{row['currency']} {row['gross_amount_local']:>10}"
                )
            typer.echo("\n  mark one with:  pt flows sale <id> --withdraw")

        if report.idle_periods:
            typer.echo(f"\ncash left idle beyond {cash.IDLE_FLAG_DAYS} days:")
            for start, end, balance, days in report.idle_periods:
                typer.secho(
                    f"  {start} .. {end}  {balance:,.2f} untouched for {days} days "
                    "-- was it actually withdrawn?",
                    fg=typer.colors.YELLOW,
                )
    finally:
        conn.close()


@app.command()
def propose(db: DbPath = DEFAULT_DB_PATH) -> None:
    """Infer the contributions the ledger implies, and queue them for review."""
    conn = connect(db)
    try:
        created, report = cash.propose_deposits(conn)
        conn.commit()
        typer.echo(f"queued {created} new proposal(s)")
        for currency, amount in sorted(report.total_inferred.items()):
            typer.echo(f"  inferred contributions in {currency}: {amount:,.2f}")
        typer.echo("\nreview with:  pt flows list")
    finally:
        conn.close()


@app.command()
def confirm(
    ids: Annotated[list[int] | None, typer.Argument(help="Proposal ids.")] = None,
    db: DbPath = DEFAULT_DB_PATH,
    all_: Annotated[bool, typer.Option("--all", help="Confirm every proposal.")] = False,
    amount: Annotated[
        str | None,
        typer.Option("--amount", help="Book a corrected amount; the inferred one is kept."),
    ] = None,
) -> None:
    """Turn proposals into ledger entries.

    Routing goes through ``core.inbox`` -- the same code the web review page
    calls -- so that confirming from a terminal and confirming from a browser
    cannot come to mean two different things.
    """
    conn = connect(db)
    try:
        targets = list(ids or [])
        if all_:
            targets = [entry.id for entry in inbox.listing(conn)]
        if not targets:
            typer.echo("nothing to confirm; pass ids or --all")
            return
        if amount is not None and len(targets) != 1:
            typer.secho("--amount applies to exactly one proposal", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

        corrected = Decimal(amount) if amount is not None else None
        for pending_id in targets:
            try:
                txn_id = inbox.confirm(conn, pending_id, amount_local=corrected)
            except (LookupError, inbox.NotBookable, ValueError) as exc:
                typer.secho(f"  proposal {pending_id}: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc
            typer.echo(f"  proposal {pending_id} -> transaction {txn_id}")
        conn.commit()
        typer.echo(f"\nconfirmed {len(targets)}; re-run `pt rebuild` to fold them in")
    finally:
        conn.close()


@app.command()
def reject(
    pending_id: int,
    reason: Annotated[str, typer.Option("--reason", help="Why it is wrong.")],
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """Dismiss a proposal, recording why."""
    conn = connect(db)
    try:
        try:
            inbox.reject(conn, pending_id, reason)
        except (LookupError, ValueError) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        conn.commit()
        typer.echo(f"rejected {pending_id}: {reason}")
    finally:
        conn.close()


@app.command()
def sale(
    txn_id: int,
    db: DbPath = DEFAULT_DB_PATH,
    withdraw: Annotated[
        bool, typer.Option("--withdraw", help="Proceeds left the portfolio.")
    ] = False,
    reinvest: Annotated[
        bool, typer.Option("--reinvest", help="Proceeds stayed, to be reinvested.")
    ] = False,
) -> None:
    """Say whether a sale's proceeds were reinvested or taken out."""
    if withdraw == reinvest:
        typer.secho("choose exactly one of --withdraw or --reinvest", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    conn = connect(db)
    try:
        cash.set_disposition(conn, txn_id, "WITHDRAW" if withdraw else "REINVEST")
        conn.commit()
        typer.echo(
            f"sale {txn_id} marked as {'withdrawal' if withdraw else 'reinvestment'}; "
            "re-run `pt flows propose` so the downstream inference follows"
        )
    finally:
        conn.close()


@app.command("cash")
def cash_series(
    db: DbPath = DEFAULT_DB_PATH,
    end: Annotated[str | None, typer.Option("--end", help="Last date.")] = None,
) -> None:
    """Materialise the daily cash balance, so idle proceeds count as held."""
    conn = connect(db)
    try:
        written = cash.build_cash_daily(conn, end=date.fromisoformat(end) if end else date.today())
        conn.commit()
        typer.echo(f"wrote {written} daily cash row(s)")
    finally:
        conn.close()
