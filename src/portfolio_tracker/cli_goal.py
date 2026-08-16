"""``pt goal`` -- the target, and when it might be reached.

``show`` is the zero-return extrapolation: what contributions alone would do.
``project`` adds the two honest ways to answer the same question with growth in
it -- a sensitivity table across stated rates, and a resample of this
portfolio's own history.

Neither is a forecast, and the output says so in the places a reader is most
likely to skip. The spread across the table is the answer; a single date out of
it would be a number nobody chose being treated as one somebody did.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker.core import goal, projection
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect

app = typer.Typer(no_args_is_help=True, help="The savings target and when it might be reached.")

DbPath = Annotated[Path, typer.Option("--db", help="Database file.")]


def _decimal(raw: str, label: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        typer.secho(f"{label} must be a number, got {raw!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None


@app.command()
def show(
    db: DbPath = DEFAULT_DB_PATH,
    person_id: Annotated[int, typer.Option("--person", help="Whose portfolio.")] = 1,
) -> None:
    """Progress against the target, on contributions alone."""
    conn = connect(db, read_only=True)
    try:
        try:
            state = goal.progress(conn, person_id=person_id)
        except LookupError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"target      EUR {state.target_eur:>12,.2f}")
    typer.echo(f"now         EUR {state.current_eur:>12,.2f}   ({state.progress_pct:.1%})")
    typer.echo(f"remaining   EUR {state.remaining_eur:>12,.2f}")
    if state.projected_date:
        typer.echo(
            f"\nat EUR {state.monthly_contribution_eur:,.2f}/month and NO investment "
            f"return: {state.projected_date}"
        )
    typer.echo("")
    for caveat in state.caveats:
        typer.secho(f"  - {caveat}", fg=typer.colors.BLUE)
    typer.echo("\n  add investment return with:  pt goal project")


@app.command("set")
def set_target(
    amount: Annotated[str, typer.Argument(help="The new target, in euro.")],
    db: DbPath = DEFAULT_DB_PATH,
) -> None:
    """Set the target. A number nobody chose has no business driving a bar."""
    conn = connect(db)
    try:
        try:
            stored = goal.set_target(conn, _decimal(amount, "the target"))
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        conn.commit()
    finally:
        conn.close()
    typer.secho(f"target set to EUR {stored:,.2f}", fg=typer.colors.GREEN)


@app.command()
def project(
    db: DbPath = DEFAULT_DB_PATH,
    person_id: Annotated[int, typer.Option("--person", help="Whose portfolio.")] = 1,
    target: Annotated[
        str | None, typer.Option("--target", help="Override the stored target.")
    ] = None,
    monthly: Annotated[
        str | None,
        typer.Option("--monthly", help="Override the measured contribution rate."),
    ] = None,
    years: Annotated[
        int, typer.Option("--years", help="How far ahead to look.")
    ] = projection.HORIZON_YEARS,
    paths: Annotated[
        int, typer.Option("--paths", help="Bootstrap paths to simulate.")
    ] = projection.DEFAULT_PATHS,
) -> None:
    """When the target is reached, and what that answer depends on.

    The table is a sensitivity analysis, not five forecasts: the tool does not
    choose a rate, and the spread between the rows is the point.
    """
    conn = connect(db, read_only=True)
    try:
        try:
            result = projection.project(
                conn,
                person_id=person_id,
                target_eur=_decimal(target, "--target") if target else None,
                monthly_eur=_decimal(monthly, "--monthly") if monthly else None,
                horizon_years=years,
                paths=paths,
            )
            observed = projection.observed_annual_return(conn, person_id)
        except (LookupError, projection.ProjectionError) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(
        f"EUR {result.current_eur:,.2f} today, target EUR {result.target_eur:,.2f}, "
        f"EUR {result.monthly_contribution_eur:,.2f}/month  (as of {result.as_of})"
    )
    if result.already_reached:
        typer.secho("\n  the target has already been reached", fg=typer.colors.GREEN)
        return

    typer.echo("")
    typer.echo("  IF the portfolio returns...      then the target is reached")
    for scenario in result.scenarios:
        when = (
            scenario.reached_on.isoformat()
            if scenario.reached_on
            else f"not within {result.horizon_years} years"
        )
        typer.echo(f"    {scenario.annual_return:>5.0%} a year{'':<18}{when}")

    if observed is not None:
        typer.echo("")
        typer.secho(
            f"  For scale: this portfolio has actually returned {observed:.1%} a year so "
            "far. That is history, not a rate to plan on.",
            fg=typer.colors.YELLOW,
        )

    if result.bootstrap:
        b = result.bootstrap
        typer.echo("")
        typer.echo(f"  Resampling its own {b.sample_days} days of returns, {b.paths} paths:")
        typer.echo(f"    earlier than 9 in 10 paths   {b.earliest_decile}")
        typer.echo(f"    middle path                  {b.median}")
        typer.echo(f"    later than 9 in 10 paths     {b.latest_decile}")
        if b.never_reached_pct > 0:
            typer.secho(
                f"    never reached in {b.never_reached_pct:.1%} of paths",
                fg=typer.colors.YELLOW,
            )
        typer.echo("")
        for caveat in b.caveats:
            typer.secho(f"  - {caveat}", fg=typer.colors.BLUE)

    typer.echo("")
    for caveat in result.caveats:
        typer.secho(f"  - {caveat}", fg=typer.colors.BLUE)
