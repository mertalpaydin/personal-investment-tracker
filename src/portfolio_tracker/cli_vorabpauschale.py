"""``pt vorabpauschale`` -- the January charge, forecast and proposed.

Two commands and a deliberate gap between them. ``forecast`` computes and
prints; ``propose`` computes, stores and queues for review. Neither books
anything: a Vorabpauschale reaches the ledger only when a person confirms it,
like every other inference here.

That rule earns its keep more on this kind than on any other. The figure rests
on a Basiszins published once a year by the Bundesfinanzministerium and on a
Teilfreistellung classification the tool cannot verify -- it follows the fund's
Anlagebedingungen, not what the fund happens to hold. Both are things a person
should look at before the number reaches a tax return.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker.core import tax, vorabpauschale
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect
from portfolio_tracker.money import ZERO

app = typer.Typer(
    no_args_is_help=True,
    help="Section 18 InvStG: the deemed distribution charged each January.",
)

DbPath = Annotated[Path, typer.Option("--db", help="Database file.")]
Year = Annotated[int, typer.Option("--year", help="The year the charge accrues FOR.")]


def _table(charges: list[vorabpauschale.Charge]) -> None:
    typer.echo(f"  {'fund':<8} {'qty':>12} {'basisertrag':>12} {'gross':>10} {'taxable':>10}  lots")
    for charge in charges:
        typer.echo(
            f"  {charge.import_key:<8} {charge.quantity:>12,.4f} "
            f"{charge.basisertrag_eur:>12,.2f} {charge.gross_eur:>10,.2f} "
            f"{charge.taxable_eur:>10,.2f}  {charge.lots}"
        )


@app.command()
def forecast(
    year: Year = 2025,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """Compute the charge and print it. Writes nothing."""
    conn = connect(db, read_only=True)
    try:
        try:
            charges = vorabpauschale.compute(conn, tax_year=year)
        except tax.TaxDataMissing as exc:
            # A refusal, not a zero. A Vorabpauschale of EUR 0.00 is a number
            # somebody will put on an Anlage KAP.
            typer.secho(f"cannot compute: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    if not charges:
        typer.echo(f"no in-scope funds held at the end of {year}")
        return

    typer.echo(f"Vorabpauschale for {year}, taxable {year + 1}-01-02")
    typer.echo("")
    _table(charges)
    total = sum((c.taxable_eur for c in charges), ZERO)
    typer.echo("")
    typer.echo(f"  total taxable EUR {total:,.2f}")
    typer.secho(
        "  No cash arrives. Confirming raises each lot's cost basis by the gross, "
        "so the same amount is not taxed again on sale.",
        fg=typer.colors.BLUE,
    )


@app.command()
def propose(
    year: Year = 2025,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """Store the forecast and queue it for review.

    Idempotent: re-running against unchanged inputs queues nothing new.
    """
    conn = connect(db)
    try:
        try:
            result = vorabpauschale.propose(conn, tax_year=year)
        except tax.TaxDataMissing as exc:
            typer.secho(f"cannot compute: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        conn.commit()
    finally:
        conn.close()

    if not result.charges:
        typer.echo(f"no in-scope funds held at the end of {year}")
        return

    typer.echo(f"Vorabpauschale for {year}, taxable {year + 1}-01-02")
    typer.echo("")
    _table(result.charges)
    typer.echo("")
    typer.echo(f"  {result.created} proposal(s) queued, total taxable "
               f"EUR {result.total_taxable_eur:,.2f}")  # fmt: skip
    if result.already_pending:
        typer.echo(f"  {result.already_pending} already awaiting a decision")
    if result.skipped_zero:
        typer.echo(
            f"  {result.skipped_zero} fund(s) owe nothing -- they did not rise, "
            "or distributed more than the Basisertrag"
        )
    typer.echo("")
    typer.echo("  review with:  pt flows list      confirm with:  pt flows confirm <id>")
    typer.secho(
        "  Check the Basiszins and each fund's Teilfreistellung before confirming. "
        "The rate follows the fund's Anlagebedingungen, not its holdings.",
        fg=typer.colors.YELLOW,
    )


@app.command("list")
def list_stored(
    year: Year = 2025,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """What has been computed and stored, and whether it has been booked."""
    conn = connect(db, read_only=True)
    try:
        rows = conn.execute(
            "SELECT v.*, i.import_key FROM vorabpauschale v "
            "JOIN instrument i ON i.id = v.instrument_id "
            "WHERE v.tax_year = ? ORDER BY i.import_key",
            (year,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo(f"nothing stored for {year}; run `pt vorabpauschale propose --year {year}`")
        return

    typer.echo(f"{'fund':<8} {'basisertrag':>12} {'taxable':>10}  status    computed")
    for row in rows:
        colour = typer.colors.GREEN if row["status"] == "ACTUAL" else None
        typer.secho(
            f"{row['import_key']:<8} {row['basisertrag_eur']:>12,.2f} "
            f"{row['taxable_eur']:>10,.2f}  {row['status']:<9} {(row['computed_at'] or '')[:10]}",
            fg=colour,
        )
    typer.echo("")
    typer.echo("FORECAST: computed, not in the ledger.  ACTUAL: confirmed and booked.")


def report_section(conn: object, *, tax_year: int) -> None:  # pragma: no cover - CLI glue
    """Print the booked charge inside `pt tax`, if there is one."""
    booked = vorabpauschale.taxable_income(conn, tax_year=tax_year)  # type: ignore[arg-type]
    if not booked:
        return
    typer.echo("")
    typer.echo(f"  Vorabpauschale booked as {tax_year} income:")
    for key, amount in booked:
        typer.echo(f"    {key:<8} EUR {amount:>9,.2f}  (after Teilfreistellung)")


@app.command()
def check(
    year: Year = 2025,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """How much relief is sitting on the lots, unclaimed until they are sold.

    The figure that proves the double-taxation guard is doing something: every
    euro here is a euro of gain that will not be taxed a second time.
    """
    conn = connect(db, read_only=True)
    try:
        rows = conn.execute(
            # Only lots actually carrying relief. Counting every open lot would
            # imply the charge reached holdings bought after it was raised,
            # which is precisely what the chronological pass prevents.
            "SELECT i.import_key, count(*) AS lots, total(l.vap_accumulated_eur) AS relief "
            "FROM lot l JOIN instrument i ON i.id = l.instrument_id "
            "WHERE l.qty_remaining > 0 AND l.vap_accumulated_eur > 0 "
            "GROUP BY i.import_key ORDER BY relief DESC"
        ).fetchall()
        claimed = conn.execute("SELECT total(vap_offset_eur) AS used FROM lot_disposal").fetchone()[
            "used"
        ]
    finally:
        conn.close()

    if not rows:
        typer.echo("no Vorabpauschale relief attached to any open lot")
    else:
        typer.echo(f"{'fund':<8} {'lots':>5} {'relief carried':>15}")
        for row in rows:
            typer.echo(f"{row['import_key']:<8} {row['lots']:>5} {row['relief']:>15,.2f}")
        typer.echo("")
        typer.echo(
            "  Deducted from the gain when these lots are sold, so the same euros "
            "are not taxed twice."
        )
    if claimed:
        typer.echo(f"\n  EUR {claimed:,.2f} already claimed against past disposals")
