"""Command-line entry point.

Subcommands land as their phases do; this module stays a thin dispatch layer
with no business logic of its own.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker import __version__, cli_flows
from portfolio_tracker import seeds as instrument_seeds
from portfolio_tracker.core import cash, fx, lots, returns, valuation
from portfolio_tracker.db import migrate as migrations
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect
from portfolio_tracker.importers import overrides
from portfolio_tracker.importers.investing_com.importer import import_positions
from portfolio_tracker.ingest import fx as ingest_fx
from portfolio_tracker.ingest import prices as ingest_prices

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


app.add_typer(cli_flows.app, name="flows")

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


@app.command("fetch-fx")
def fetch_fx(
    db: DbPath = DEFAULT_DB_PATH,
    start: Annotated[
        str, typer.Option("--start", help="Earliest date to fetch (YYYY-MM-DD).")
    ] = "2024-07-01",
    quote: Annotated[
        str | None, typer.Option("--quote", help="Single currency; default is all needed.")
    ] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use only data/fx_manual/, no network.")
    ] = False,
) -> None:
    """Fetch daily reference rates, then report coverage against the grid."""
    start_date = date.fromisoformat(start)
    conn = connect(db)
    try:
        quotes = {quote} if quote else fx.required_quotes(conn)
        if not quotes:
            typer.echo("no non-euro currencies in this database; nothing to fetch")
            return

        for code in sorted(quotes):
            observations = ingest_fx.load_manual(code)
            source = "manual"
            if not offline:
                try:
                    observations = ingest_fx.fetch_ecb(code, start_date) + observations
                    source = "ecb + manual" if observations else "ecb"
                except ingest_fx.FxIngestError as exc:
                    typer.secho(f"{code}: {exc}", fg=typer.colors.YELLOW, err=True)
                    if not observations:
                        typer.secho(
                            f"{code}: no rates available; drop a CSV in "
                            f"{ingest_fx.MANUAL_DIR}/{code}.csv",
                            fg=typer.colors.RED,
                            err=True,
                        )
                        raise typer.Exit(code=1) from exc
            written = ingest_fx.store(conn, observations)
            conn.commit()
            typer.echo(f"{code}: {written} rate(s) stored from {source}")

        today = date.today()
        for gap in fx.coverage_report(conn, start_date, today, quotes=quotes):
            if gap.is_complete:
                typer.echo(
                    f"{gap.quote}: complete from {start_date} to {today} "
                    f"(observed {gap.first_available} .. {gap.last_available})"
                )
            else:
                typer.secho(
                    f"{gap.quote}: {len(gap.missing)} grid day(s) missing, first {gap.missing[0]}",
                    fg=typer.colors.YELLOW,
                )
    finally:
        conn.close()


@app.command()
def seed(
    db: DbPath = DEFAULT_DB_PATH,
    path: Annotated[
        Path, typer.Option("--file", help="Instrument metadata YAML.")
    ] = instrument_seeds.DEFAULT_PATH,
) -> None:
    """Apply instrument metadata the export does not carry."""
    conn = connect(db)
    try:
        try:
            entries = instrument_seeds.load(path)
        except instrument_seeds.SeedError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        if not entries:
            typer.echo(f"no seed data at {path}")
            return
        result = instrument_seeds.apply(conn, entries)
        conn.commit()
        typer.echo(f"updated {len(result.updated)} instrument(s), {result.fields_written} field(s)")
        for missing in result.unknown_instruments:
            typer.secho(
                f"  {missing}: not in the database; import it first", fg=typer.colors.YELLOW
            )
    finally:
        conn.close()


@app.command("fetch-prices")
def fetch_prices(
    db: DbPath = DEFAULT_DB_PATH,
    end: Annotated[str | None, typer.Option("--end", help="Last date (YYYY-MM-DD).")] = None,
    start: Annotated[
        str | None, typer.Option("--start", help="First date; default is each first trade.")
    ] = None,
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit non-zero if any instrument is under 98%.")
    ] = False,
) -> None:
    """Fetch prices for every held instrument, then report coverage."""
    conn = connect(db)
    try:
        results = ingest_prices.fetch_all(
            conn,
            end=date.fromisoformat(end) if end else date.today(),
            start=date.fromisoformat(start) if start else None,
        )
    finally:
        conn.close()

    if not results:
        typer.echo("no held instruments; import a positions export first")
        return

    typer.echo(f"{'instrument':<10} {'source':<10} {'coverage':>9}  {'window':<25} notes")
    for r in sorted(results, key=lambda x: (x.ok, x.import_key)):
        colour = None if r.ok else typer.colors.YELLOW
        window = f"{r.start} .. {r.end}"
        note = f"{r.observed}/{r.expected} on {r.calendar}"
        pct = f"{r.pct}%"
        source = r.source or "-"
        typer.secho(
            f"{r.import_key:<10} {source:<10} {pct:>9}  {window:<25} {note}",
            fg=colour,
        )
        for lo, hi, length in r.gaps:
            typer.secho(f"    gap: {lo} .. {hi} ({length} days)", fg=typer.colors.YELLOW)
        if r.error:
            typer.secho(f"    {r.error}", fg=typer.colors.RED)

    failed = [r for r in results if not r.ok]
    if failed:
        typer.echo("")
        typer.echo(f"{len(failed)} instrument(s) below 98% coverage")
        if strict:
            raise typer.Exit(code=1)


@app.command()
def rebuild(
    db: DbPath = DEFAULT_DB_PATH,
    end: Annotated[str | None, typer.Option("--end", help="Last valuation date.")] = None,
    strict: Annotated[
        bool, typer.Option("--strict", help="Fail on a stale price instead of flagging it.")
    ] = False,
) -> None:
    """Recompute FIFO lots and the daily valuation from the ledger.

    Everything here is derived, so this is always safe to run: it deletes and
    recomputes rather than patching. An edit anywhere in the history changes
    which lots a later sale consumes, which is why nothing is updated in place.
    """
    as_of = date.fromisoformat(end) if end else date.today()
    conn = connect(db)
    try:
        lot_report = lots.rebuild_lots(conn)
        conn.commit()
        typer.echo(
            f"lots: {lot_report.lots_created} open, {lot_report.disposals_created} disposals"
        )
        for problem in lot_report.oversold:
            typer.secho(f"  oversold: {problem}", fg=typer.colors.RED)
        if lot_report.missing_fx:
            typer.secho(
                f"  {len(lot_report.missing_fx)} lot(s) with no trade-date FX rate",
                fg=typer.colors.YELLOW,
            )

        try:
            value_report = valuation.build_position_daily(conn, end=as_of, strict=strict)
        except valuation.StalePriceError as exc:
            typer.secho(f"valuation refused: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        conn.commit()

        typer.echo(
            f"valuation: {value_report.rows} position-days over {value_report.days} grid days"
        )

        cash_rows = cash.build_cash_daily(conn, end=as_of)
        conn.commit()
        typer.echo(f"cash: {cash_rows} daily balance row(s)")

        for person in conn.execute("SELECT id FROM person ORDER BY id"):
            breaks = returns.build_portfolio_daily(conn, person_id=int(person["id"]), end=as_of)
            conn.commit()
            for problem in breaks:
                typer.secho(f"  {problem}", fg=typer.colors.RED)
            if breaks:
                raise typer.Exit(code=1)
        typer.echo("returns: unit series rebuilt")
        for problem in value_report.stale[:5]:
            typer.secho(f"  stale: {problem}", fg=typer.colors.YELLOW)
        for problem in value_report.missing[:5]:
            typer.secho(f"  unvalued: {problem}", fg=typer.colors.YELLOW)
        if len(value_report.missing) > 5:
            typer.secho(
                f"  ... and {len(value_report.missing) - 5} more unvalued position-days",
                fg=typer.colors.YELLOW,
            )

        if not lot_report.ok:
            raise typer.Exit(code=1)
    finally:
        conn.close()


@app.command()
def report(
    db: DbPath = DEFAULT_DB_PATH,
    person_id: Annotated[int, typer.Option("--person", help="Whose portfolio.")] = 1,
) -> None:
    """Headline performance, with the caveats that qualify it."""
    conn = connect(db)
    try:
        summary = returns.summarise(conn, person_id=person_id)
        if summary.days == 0:
            typer.echo("no portfolio history; run `pt rebuild` first")
            raise typer.Exit(code=1)

        def pct(value: object) -> str:
            return f"{value:.2%}" if isinstance(value, Decimal) else "n/a"

        typer.echo(f"{summary.start} .. {summary.end}   ({summary.days} days)")
        typer.echo("")
        typer.echo(f"  time-weighted return   {pct(summary.twr):>10}")
        typer.echo(f"    annualised           {pct(summary.twr_annualised):>10}")
        typer.echo(
            f"  money-weighted (XIRR)  {pct(summary.xirr):>10}"
            f"   [reliability: {summary.xirr_reliability}]"
        )
        typer.echo(f"    timing gap           {pct(summary.timing_gap):>10}")
        typer.echo(f"  volatility             {pct(summary.volatility):>10}")
        typer.echo(f"  max drawdown           {pct(summary.max_drawdown):>10}")
        typer.echo("")
        typer.echo(f"  contributions          EUR {summary.external_flows_eur:>12,.2f}")

        last = conn.execute(
            "SELECT * FROM portfolio_daily WHERE person_id = ? ORDER BY date DESC LIMIT 1",
            (person_id,),
        ).fetchone()
        if last is not None:
            typer.echo(f"  positions              EUR {last['market_value_eur']:>12,.2f}")
            typer.echo(f"  cash                   EUR {last['cash_eur']:>12,.2f}")
            typer.echo(f"  total value            EUR {last['total_value_eur']:>12,.2f}")

        if summary.caveats:
            typer.echo("")
            typer.echo("caveats:")
            for caveat in summary.caveats:
                typer.secho(f"  - {caveat}", fg=typer.colors.YELLOW)
    finally:
        conn.close()


@app.command()
def doctor() -> None:
    """Re-run data-quality identity checks against the database."""
    typer.echo("doctor: not implemented yet (lands with the importer, phase 3)")
    raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
