"""Command-line entry point.

Subcommands land as their phases do; this module stays a thin dispatch layer
with no business logic of its own.
"""

from __future__ import annotations

from typing import Annotated

import typer

from portfolio_tracker import __version__

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


@app.command()
def doctor() -> None:
    """Re-run data-quality identity checks against the database."""
    typer.echo("doctor: not implemented yet (lands with the importer, phase 3)")
    raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
