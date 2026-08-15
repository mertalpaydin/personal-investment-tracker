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
from portfolio_tracker.importers import overrides
from portfolio_tracker.importers.investing_com.importer import import_positions

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


@app.command("import")
def import_(
    open_csv: Annotated[Path, typer.Option("--open", help="Open-positions CSV.")],
    closed_csv: Annotated[Path, typer.Option("--closed", help="Closed-positions CSV.")],
    account_id: Annotated[int, typer.Option("--account", help="Target account id.")] = 1,
    db: DbPath = DEFAULT_DB_PATH,
    overrides_path: Annotated[
        Path, typer.Option("--overrides", help="Authoritative values for lossy fields.")
    ] = overrides.DEFAULT_PATH,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would happen, write nothing.")
    ] = False,
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat any warning as a failure.")
    ] = False,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write the report as JSON.")
    ] = None,
) -> None:
    """Import an Investing.com positions export.

    Idempotent: re-running against an unchanged export inserts nothing.
    """
    conn = connect(db)
    try:
        loaded = overrides.load(overrides_path)
        report = import_positions(
            conn,
            open_csv,
            closed_csv,
            account_id=account_id,
            overrides=loaded,
            dry_run=dry_run,
            strict=strict,
        )
    finally:
        conn.close()

    typer.echo(report.to_text())
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(report.to_json(), encoding="utf-8")
        typer.echo(f"\nreport written to {json_out}")

    if report.errors or (strict and report.warnings):
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Re-run data-quality identity checks against the database."""
    typer.echo("doctor: not implemented yet (lands with the importer, phase 3)")
    raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
