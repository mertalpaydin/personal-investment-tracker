"""``pt scenario`` -- what a sale would cost, before making it.

Read-only by construction. Every command here prices a hypothetical against the
last observed close and writes nothing, so running one can never be mistaken
for having done something.

There is deliberately no command that assumes a future return. ``pt goal``
already projects on contributions alone for the same reason: a number with the
shape of a forecast and none of the substance is worse than no number.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker.core import fx, scenario, tax
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect

app = typer.Typer(
    no_args_is_help=True,
    help="Price a sale before making it. Today's prices only; nothing is written.",
)

DbPath = Annotated[Path, typer.Option("--db", help="Database file.")]


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command()
def sell(
    instrument: Annotated[
        str, typer.Option("--instrument", "-i", help="Import key, ISIN or symbol.")
    ],
    quantity: Annotated[
        str | None,
        typer.Option("--qty", help="Units to sell. Default is the whole holding."),
    ] = None,
    year: Annotated[
        int | None, typer.Option("--year", help="Tax year to charge it to. Default: this year.")
    ] = None,
    as_of: Annotated[str | None, typer.Option("--as-of", help="Price it as at this date.")] = None,
    person_id: Annotated[int, typer.Option("--person", help="Whose portfolio.")] = 1,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """What selling this today would realise, and what it would cost in tax."""
    try:
        qty = Decimal(quantity) if quantity else None
    except InvalidOperation:
        _fail(f"--qty must be a number, got {quantity!r}")
        return

    conn = connect(db, read_only=True)
    try:
        try:
            result = scenario.sell(
                conn,
                instrument=instrument,
                quantity=qty,
                person_id=person_id,
                as_of=date.fromisoformat(as_of) if as_of else None,
                tax_year=year,
            )
        except (scenario.ScenarioError, tax.TaxDataMissing, fx.FxUnavailable) as exc:
            _fail(str(exc))
            return
    finally:
        conn.close()

    portion = "the whole holding" if result.quantity == result.quantity_held else "part of it"
    typer.echo(
        f"Selling {result.quantity:,.4f} of {result.instrument} ({portion}) "
        f"at {result.price_local:,.4f} {result.currency} as of {result.price_asof}"
    )
    typer.echo("")
    typer.echo(f"  proceeds                 EUR {result.proceeds_eur:>12,.2f}")
    typer.echo(f"  cost basis               EUR {result.cost_basis_eur:>12,.2f}")
    colour = typer.colors.GREEN if result.gross_gain_eur >= 0 else typer.colors.RED
    typer.secho(f"  gross gain               EUR {result.gross_gain_eur:>12,.2f}", fg=colour)
    if result.vap_offset_eur:
        typer.echo(
            f"  less Vorabpauschale       EUR {result.vap_offset_eur:>12,.2f}   (already taxed)"
        )
    if result.teilfreistellung_eur:
        typer.echo(f"  less Teilfreistellung    EUR {result.teilfreistellung_eur:>12,.2f}")
    typer.echo(f"  taxable gain             EUR {result.taxable_gain_eur:>12,.2f}")
    typer.echo("")

    # The marginal figure is the whole point: the same gain costs nothing or
    # 26.375% depending only on what the allowance has already absorbed.
    typer.echo(f"  allowance left before    EUR {result.allowance_remaining_before_eur:>12,.2f}")
    typer.echo(f"  allowance left after     EUR {result.allowance_remaining_after_eur:>12,.2f}")
    typer.secho(
        f"  TAX THIS SALE ADDS       EUR {result.marginal_tax_eur:>12,.2f}",
        bold=True,
        fg=typer.colors.YELLOW if result.marginal_tax_eur > 0 else typer.colors.GREEN,
    )
    typer.echo(f"  proceeds after that      EUR {result.net_proceeds_eur:>12,.2f}")

    typer.echo("")
    typer.echo("  FIFO would consume:")
    typer.echo(f"    {'opened':<12} {'units':>12} {'cost':>11} {'proceeds':>11} {'gain':>11}")
    for lot in result.consumed:
        typer.echo(
            f"    {lot.open_date.isoformat():<12} {lot.quantity:>12,.4f} "
            f"{lot.cost_basis_eur:>11,.2f} {lot.proceeds_eur:>11,.2f} "
            f"{lot.gross_gain_eur:>11,.2f}"
        )

    typer.echo("")
    for caveat in result.caveats:
        typer.secho(f"  - {caveat}", fg=typer.colors.BLUE)
    typer.secho(
        "\n  Nothing was written. This is a planning figure, not a decision.",
        fg=typer.colors.BLUE,
    )


@app.command()
def harvest(
    year: Annotated[
        int | None, typer.Option("--year", help="Tax year. Default: this year.")
    ] = None,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Price everything as at this date.")
    ] = None,
    person_id: Annotated[int, typer.Option("--person", help="Whose portfolio.")] = 1,
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """What could be sold to use up this year's tax-free allowance.

    The Sparerpauschbetrag does not carry forward. Whatever is unused on 31
    December is gone, so this is a question with a deadline.
    """
    conn = connect(db, read_only=True)
    try:
        try:
            result = scenario.harvest(
                conn,
                person_id=person_id,
                tax_year=year,
                as_of=date.fromisoformat(as_of) if as_of else None,
            )
        except (scenario.ScenarioError, tax.TaxDataMissing) as exc:
            _fail(str(exc))
            return
    finally:
        conn.close()

    typer.echo(f"tax year {result.tax_year}, priced as of {result.as_of}")
    typer.echo("")
    typer.echo(f"  allowance          EUR {result.allowance_cap_eur:>10,.2f}")
    typer.echo(f"  already used       EUR {result.allowance_used_eur:>10,.2f}")
    typer.secho(
        f"  still free         EUR {result.allowance_remaining_eur:>10,.2f}",
        bold=True,
        fg=typer.colors.GREEN if result.allowance_remaining_eur > 0 else None,
    )

    if result.gains:
        typer.echo("")
        typer.echo("  Selling the whole holding would realise a GAIN:")
        typer.echo(f"    {'fund':<8} {'value':>12} {'taxable gain':>13}  pot")
        for c in result.gains:
            typer.echo(
                f"    {c.instrument:<8} {c.market_value_eur:>12,.2f} "
                f"{c.taxable_gain_eur:>13,.2f}  {c.pot}"
            )

    if result.losses:
        typer.echo("")
        typer.echo("  ...or a LOSS, which offsets gains in the same pot:")
        typer.echo(f"    {'fund':<8} {'value':>12} {'taxable loss':>13}  pot")
        for c in result.losses:
            typer.secho(
                f"    {c.instrument:<8} {c.market_value_eur:>12,.2f} "
                f"{c.taxable_gain_eur:>13,.2f}  {c.pot}",
                fg=typer.colors.RED,
            )

    if not result.candidates:
        typer.echo("\n  no in-scope holdings could be priced")

    typer.echo("")
    for caveat in result.caveats:
        typer.secho(f"  - {caveat}", fg=typer.colors.BLUE)
    typer.echo("")
    typer.echo("  price one exactly with:  pt scenario sell -i <fund> --qty <units>")
