"""Command-line entry point.

Subcommands land as their phases do; this module stays a thin dispatch layer
with no business logic of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker import __version__
from portfolio_tracker.db import migrate as migrations
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Personal investment tracker: FIFO tax lots, German CGT, daily FX revaluation.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Personal investment tracker."""


DbPath = Annotated[Path, typer.Option("--db", help="Database file.")]


@app.command("init-db")
def init_db(
    db: DbPath = DEFAULT_DB_PATH,
    reset_derived: Annotated[
        bool,
        typer.Option(
            "--reset-derived",
            help="Also empty every recomputable table, forcing a full rebuild.",
        ),
    ] = False,
) -> None:
    """Create or upgrade the database, then report its state.

    Safe to run repeatedly: migrations already applied are skipped. If an
    applied migration has been edited since, this refuses rather than leaving
    two databases with the same version number and different shapes.
    """
    conn = connect(db)
    try:
        try:
            applied = migrations.migrate(conn)
        except migrations.MigrationError as exc:
            typer.secho(f"migration refused: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

        if applied:
            names = ", ".join(f"{v:04d}" for v in applied)
            typer.echo(f"applied migration(s): {names}")
        else:
            typer.echo("schema already up to date")

        if reset_derived:
            deleted = migrations.reset_derived(conn)
            total = sum(deleted.values())
            typer.echo(f"cleared {total} row(s) from {len(deleted)} derived table(s)")

        typer.echo(f"{db} at schema version {migrations.current_version(conn)}")
    finally:
        conn.close()


@app.command()
def doctor() -> None:
    """Re-run data-quality identity checks against the database."""
    typer.echo("doctor: not implemented yet (lands with the importer, phase 3)")
    raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
