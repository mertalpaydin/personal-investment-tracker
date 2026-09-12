"""Command-line entry point.

Subcommands land as their phases do; this module stays a thin dispatch layer
with no business logic of its own.
"""

from __future__ import annotations

import csv
import json
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker import (
    __version__,
    cli_accounts,
    cli_flows,
    cli_goal,
    cli_look_through,
    cli_scenario,
    cli_vorabpauschale,
)
from portfolio_tracker import seeds as instrument_seeds
from portfolio_tracker.core import attribution, fx, returns, tax, valuation
from portfolio_tracker.core import doctor as doctor_checks
from portfolio_tracker.core import rebuild as rebuild_all
from portfolio_tracker.db import migrate as migrations
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect
from portfolio_tracker.importers import overrides
from portfolio_tracker.importers.investing_com.importer import import_positions
from portfolio_tracker.importers.report import Severity
from portfolio_tracker.ingest import fx as ingest_fx
from portfolio_tracker.ingest import prices as ingest_prices
from portfolio_tracker.ingest.providers.manual import ManualProvider

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
app.add_typer(cli_accounts.app, name="accounts")
app.add_typer(cli_look_through.app, name="look-through")
app.add_typer(cli_vorabpauschale.app, name="vorabpauschale")
app.add_typer(cli_scenario.app, name="scenario")
app.add_typer(cli_goal.app, name="goal")

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

        if migrations.seed_default_owner(conn):
            conn.commit()
            typer.echo(
                f"created person 1 ({migrations.DEFAULT_PERSON}) and account 1 "
                f"({migrations.DEFAULT_ACCOUNT}) -- rename them, or add your own"
            )

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
        bool, typer.Option("--offline", help="Use only the manual directory, no network.")
    ] = False,
    manual_dir: Annotated[
        Path | None,
        typer.Option("--manual-dir", help="Where to read manual rate CSVs from."),
    ] = None,
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
            observations = ingest_fx.load_manual(code, manual_dir or ingest_fx.MANUAL_DIR)
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
                            f"{manual_dir or ingest_fx.MANUAL_DIR}/{code}.csv",
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
        try:
            result = instrument_seeds.apply(conn, entries)
        except instrument_seeds.SeedError as exc:
            typer.secho(f"seed rejected: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
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
    manual_dir: Annotated[
        Path | None,
        typer.Option("--manual-dir", help="Where to read manual price CSVs from."),
    ] = None,
) -> None:
    """Fetch prices for every held instrument, then report coverage.

    A manual directory is not only a rescue: it is the one provider with no
    external dependency, and pointing at ``data/example/prices`` is how the
    synthetic corpus gets valued without touching the network.
    """
    conn = connect(db)
    try:
        providers = None
        if manual_dir is not None:
            providers = ingest_prices.default_providers(conn)
            providers["manual"] = ManualProvider(manual_dir)

        results = ingest_prices.fetch_all(
            conn,
            end=date.fromisoformat(end) if end else date.today(),
            start=date.fromisoformat(start) if start else None,
            providers=providers,
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


REPRICE_REPORT_PATH = Path("data/reprice_report.csv")


@app.command()
def reprice(
    db: DbPath = DEFAULT_DB_PATH,
    source: Annotated[
        str, typer.Option("--source", help="Provider to check every instrument on it against.")
    ] = "onvista",
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Actually replace mismatched history. Default is dry-run.",
        ),
    ] = False,
    tolerance: Annotated[
        float,
        typer.Option("--tolerance", help="Relative close difference that counts as a mismatch."),
    ] = 0.005,
    only_mismatched: Annotated[
        bool,
        typer.Option(
            "--only-mismatched/--all", help="List every instrument, not only REPLACE/UNRESOLVED."
        ),
    ] = True,
    report_path: Annotated[
        Path, typer.Option("--report", help="Where to write the CSV report.")
    ] = REPRICE_REPORT_PATH,
) -> None:
    """Compare a source's history against what is stored, and optionally fix it.

    Built for onvista replacing Yahoo Finance as the price source for German
    Xetra ETFs and stocks: Yahoo's history for roughly half of them turned out
    to be wrong (USD figures stored against EUR instruments, among other
    mismatches). Dry-run by default -- pass ``--apply`` to actually delete the
    mismatched ``yfinance`` rows (never a manually-entered one) and replace
    them, then rebuild the derived layer once.
    """
    conn = connect(db)
    try:
        rows, stopped_early = ingest_prices.reprice(
            conn,
            source=source,
            tolerance=Decimal(str(tolerance)),
            only_mismatched=only_mismatched,
            apply=apply,
        )
    finally:
        conn.close()

    if not rows:
        typer.echo(f"no instrument on source {source!r}, or none mismatched")
        return

    typer.echo(
        f"{'import_key':<12} {'verdict':<11} {'stored':>7} {'new':>7} "
        f"{'overlap':>8} {'max_diff':>9}  reason"
    )
    for r in sorted(rows, key=lambda x: (x.verdict, x.import_key)):
        colour = {
            "MATCH": None,
            "REPLACE": typer.colors.YELLOW,
            "UNRESOLVED": typer.colors.RED,
        }[r.verdict]
        diff = f"{r.max_rel_diff * 100:.2f}%" if r.max_rel_diff is not None else "-"
        typer.secho(
            f"{r.import_key:<12} {r.verdict:<11} {r.stored_rows:>7} {r.new_rows:>7} "
            f"{r.overlap:>8} {diff:>9}  {r.reason or ''}",
            fg=colour,
        )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "import_key",
                "verdict",
                "stored_rows",
                "new_rows",
                "overlap",
                "max_rel_diff",
                "reason",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r.import_key,
                    r.verdict,
                    r.stored_rows,
                    r.new_rows,
                    r.overlap,
                    r.max_rel_diff,
                    r.reason or "",
                ]
            )
    typer.echo(f"\nreport written to {report_path}")

    counts = {v: sum(1 for r in rows if r.verdict == v) for v in ("MATCH", "REPLACE", "UNRESOLVED")}
    typer.echo(
        f"MATCH {counts['MATCH']}, REPLACE {counts['REPLACE']}, UNRESOLVED {counts['UNRESOLVED']}"
    )
    if stopped_early:
        typer.secho(
            "stopped early: rate-limited. Run again to continue from here.",
            fg=typer.colors.YELLOW,
        )


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
        try:
            result = rebuild_all.run(conn, end=as_of, strict=strict)
        except valuation.StalePriceError as exc:
            typer.secho(f"valuation refused: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except fx.FxUnavailable as exc:
            # A traceback here reads as a crash. It is a missing prerequisite:
            # a non-euro holding cannot be valued in euros without a rate, and
            # inventing one is the failure this whole design exists to avoid.
            typer.secho(f"cannot value the portfolio: {exc}", fg=typer.colors.RED, err=True)
            typer.secho("run `pt fetch-fx` first", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(code=1) from exc

        typer.echo(f"lots: {result.lots_created} open, {result.disposals_created} disposals")
        for problem in result.oversold:
            typer.secho(f"  oversold: {problem}", fg=typer.colors.RED)
        if result.missing_fx:
            typer.secho(
                f"  {result.missing_fx} lot(s) with no trade-date FX rate",
                fg=typer.colors.YELLOW,
            )
        typer.echo(
            f"valuation: {result.position_days} position-days over {result.grid_days} grid days"
        )
        typer.echo(f"cash: {result.cash_rows} daily balance row(s)")
        for problem in result.breaks:
            typer.secho(f"  {problem}", fg=typer.colors.RED)
        if not result.breaks:
            typer.echo("returns: unit series rebuilt")
        for problem in result.stale[:5]:
            typer.secho(f"  stale: {problem}", fg=typer.colors.YELLOW)
        for problem in result.unvalued[:5]:
            typer.secho(f"  unvalued: {problem}", fg=typer.colors.YELLOW)
        if len(result.unvalued) > 5:
            typer.secho(
                f"  ... and {len(result.unvalued) - 5} more unvalued position-days",
                fg=typer.colors.YELLOW,
            )

        if not result.ok:
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

        # Where that return came from. The two legs of the headline claim, at
        # portfolio level: a lira book can be up sharply and down in euros.
        split = attribution.summarise(conn, person_id=person_id)
        if split is not None:
            typer.echo("")
            typer.echo("  attributed to:")
            typer.echo(f"    asset prices         {pct(split.local):>10}")
            typer.echo(f"    currency             {pct(split.currency):>10}")
            typer.echo(f"    income               {pct(split.income):>10}")
            typer.echo(f"    trading              {pct(split.unexplained):>10}")
            typer.secho(f"    {'-' * 30}", dim=True)
            typer.echo(f"    total                {pct(split.total):>10}")

            # Which holdings produced those points. Nobody can sell "currency";
            # the per-holding view is the one decisions are made at.
            per_holding = attribution.by_holding(conn, person_id=person_id)
            if per_holding is not None:
                typer.echo("")
                typer.echo(f"  by holding:  {'prices':>10} {'currency':>10} {'total':>10}")
                for holding in per_holding.holdings:
                    typer.echo(
                        f"    {holding.instrument:<18} {pct(holding.local):>8} "
                        f"{pct(holding.currency):>10} {pct(holding.total):>10}"
                    )

        typer.echo("")
        typer.echo(f"  contributions          EUR {summary.external_flows_eur:>12,.2f}")

        last = conn.execute(
            "SELECT * FROM portfolio_daily WHERE person_id = ? ORDER BY date DESC LIMIT 1",
            (person_id,),
        ).fetchone()
        if last is not None:
            typer.echo(f"  holdings               EUR {last['market_value_eur']:>12,.2f}")
            typer.echo(f"  deposits               EUR {last['deposits_eur']:>12,.2f}")
            typer.secho(
                f"  TOTAL                  EUR {last['total_value_eur']:>12,.2f}", bold=True
            )
            typer.echo("")
            # Deliberately outside the total: the tracker cannot know whether
            # this money is still in the account or has been spent.
            typer.echo(
                f"  uninvested cash        EUR {last['cash_eur']:>12,.2f}"
                "   (sale proceeds and distributions -- not counted above)"
            )

        if summary.caveats:
            typer.echo("")
            typer.echo("caveats:")
            for caveat in summary.caveats:
                typer.secho(f"  - {caveat}", fg=typer.colors.YELLOW)
    finally:
        conn.close()


@app.command("tax")
def tax_report(
    db: DbPath = DEFAULT_DB_PATH,
    year: Annotated[int, typer.Option("--year", help="Tax year.")] = 2026,
    person_id: Annotated[int, typer.Option("--person", help="Whose return.")] = 1,
) -> None:
    """German capital-gains position for a tax year.

    A planning worksheet. The bank's Steuerbescheinigung is authoritative.
    """
    conn = connect(db)
    try:
        excluded = tax.out_of_scope_report(conn, tax_year=year, as_of=date.today())
        try:
            state = tax.allowance_state(conn, person_id=person_id, tax_year=year)
        except tax.TaxDataMissing as exc:
            typer.secho(f"cannot compute: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

        typer.echo(f"tax year {year}, person {person_id}")
        typer.echo("")
        typer.echo(f"  gains in scope             EUR {state.gross_gains_eur:>10,.2f}")
        if state.distributions_eur:
            typer.echo(f"    of which distributions   EUR {state.distributions_eur:>10,.2f}")
        if state.vorabpauschale_eur:
            # Called out separately because it is the one line where money was
            # taxed without arriving, and the one a reader will not expect.
            typer.echo(
                f"    of which Vorabpauschale  EUR {state.vorabpauschale_eur:>10,.2f}"
                "   (no cash received)"
            )
        typer.echo(f"  losses offset              EUR {state.losses_offset_eur:>10,.2f}")
        typer.echo(f"  after losses               EUR {state.after_losses_eur:>10,.2f}")
        cap = f"{state.statutory_cap_eur:,.0f}"
        typer.echo(f"  allowance (cap {cap})       EUR {state.consumed_eur:>10,.2f} used")
        typer.echo(f"    remaining                EUR {state.remaining_eur:>10,.2f}")
        typer.echo(f"  taxable                    EUR {state.taxable_after_allowance_eur:>10,.2f}")
        typer.echo(f"  estimated tax (26.375%)    EUR {state.estimated_tax_eur:>10,.2f}")
        if state.carried_forward.aktien or state.carried_forward.sonstige:
            typer.echo("")
            typer.echo(
                f"  losses carried forward: shares EUR {state.carried_forward.aktien:,.2f}, "
                f"other EUR {state.carried_forward.sonstige:,.2f}"
            )

        rows = tax.forecast_vorabpauschale(conn, tax_year=year)
        charged = [r for r in rows if r.taxable_eur > 0]
        if charged:
            typer.echo("")
            typer.echo(f"  Vorabpauschale for {year} (taxable {year + 1}-01-02):")
            for row in charged:
                typer.echo(
                    f"    {row.instrument:<8} basisertrag {row.basisertrag:>9,.2f} "
                    f"-> taxable {row.taxable_eur:>9,.2f}"
                )

        if not excluded.is_empty:
            typer.echo("")
            typer.secho("  NOT INCLUDED IN THE ABOVE:", fg=typer.colors.YELLOW, bold=True)
            for key in excluded.instruments:
                typer.secho(f"    {key:<12} {excluded.reasons[key]}", fg=typer.colors.YELLOW)
            if excluded.value_eur:
                typer.secho(
                    f"    holdings worth EUR {excluded.value_eur:,.2f} are outside this figure",
                    fg=typer.colors.YELLOW,
                )

        typer.echo("")
        typer.secho(
            "  Planning worksheet only. The bank's Steuerbescheinigung is authoritative.",
            fg=typer.colors.BLUE,
        )
    finally:
        conn.close()


@app.command()
def serve(
    db: DbPath = DEFAULT_DB_PATH,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
) -> None:
    """Serve the app: the page, the JSON API, and the debug page at /debug.

    The HTTP surface writes. Every write either records a decision the person
    made on the page, or recomputes something derived -- and each one names the
    change it would make before making it. This reverses an earlier decision
    that all writes go through a reviewed CLI command; see ADR 0010.
    """
    if not Path(db).exists():
        # Better here than as a 503 on the first request: the person who typed
        # this command is looking at the terminal right now.
        typer.secho(f"no database at {db}; run `pt init-db` first", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        import uvicorn

        from portfolio_tracker.api import app as api
        from portfolio_tracker.api import deps
    except ImportError as exc:
        typer.secho(
            "the API extras are not installed; run `uv sync --extra api`",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    # The page's drop folder sits beside the database, so a second database in
    # another directory does not quietly share the first one's incoming files.
    api.configure(db_path=Path(db))
    api.warm_metrics_cache()
    typer.echo(f"serving {db} at http://{host}:{port}/  (debug page at /debug)")
    typer.echo(f"drop holdings files in {deps.INCOMING_DIR}")
    uvicorn.run(api.app, host=host, port=port, log_level="warning")


@app.command()
def doctor(
    db: DbPath = DEFAULT_DB_PATH,
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit non-zero on warnings too, not just errors.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Check the derived data against the identities it must satisfy.

    Errors are inconsistencies: a market value that is not quantity times
    price, a gain split whose parts do not sum, contributions in the daily
    series that do not match the ledger. Each is a bug, not a surprise.

    Warnings and notes are gaps rather than faults -- unresolved sale
    dispositions, missing fees, prices carried forward. They bound what the
    numbers mean, which is why they are printed instead of filtered out.
    """
    if not Path(db).exists():
        typer.secho(f"no database at {db}; run `pt init-db` first", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    with closing(connect(db, read_only=True)) as conn:
        report = doctor_checks.run(conn)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "ok": report.ok,
                    "checks_run": report.checks_run,
                    "rows_examined": report.rows_examined,
                    "findings": [
                        {
                            "code": f.code,
                            "severity": str(f.severity),
                            "message": f.message,
                            "detail": f.detail,
                        }
                        for f in report.findings
                    ],
                },
                indent=2,
            )
        )
    else:
        colours = {
            Severity.ERROR: typer.colors.RED,
            Severity.WARNING: typer.colors.YELLOW,
            Severity.INFO: typer.colors.CYAN,
        }
        # Errors first: a wrong number matters more than a missing one.
        order = (Severity.ERROR, Severity.WARNING, Severity.INFO)
        for finding in sorted(report.findings, key=lambda f: order.index(f.severity)):
            typer.secho(str(finding), fg=colours[finding.severity])
        typer.echo(
            f"\n{report.checks_run} check group(s), {report.rows_examined:,} row(s) examined: "
            f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"
        )
        if report.ok and not report.warnings:
            typer.secho("every identity holds", fg=typer.colors.GREEN)
        elif report.ok:
            typer.secho(
                "every identity holds; the warnings are gaps in the source, not faults",
                fg=typer.colors.GREEN,
            )

    if report.errors or (strict and report.warnings):
        raise typer.Exit(code=1)


@app.command("sync-fundamentals")
def sync_fundamentals(
    db: DbPath = DEFAULT_DB_PATH,
    workers: Annotated[
        int, typer.Option("--workers", "-w", help="Number of concurrent worker threads.")
    ] = 4,
    rescore_only: Annotated[
        bool,
        typer.Option(
            "--rescore-only",
            help="Recompute value scores from the stored data without calling Yahoo.",
        ),
    ] = False,
) -> None:
    """Fetch and update fundamental valuation indicators for all single stocks."""
    from portfolio_tracker.core.stock_indicators import (
        rescore_stock_fundamentals,
        sync_all_stock_fundamentals,
    )

    conn = connect(db)
    try:
        if rescore_only:
            n = rescore_stock_fundamentals(conn)
            typer.secho(
                f"Re-scored {n} stocks and recorded today's score snapshot.",
                fg=typer.colors.GREEN,
            )
            return
        typer.echo(f"Fetching stock fundamentals from Yahoo Finance ({workers} workers)...")

        def on_progress(done: int, total: int, key: str) -> None:
            if done % 10 == 0 or done == total:
                typer.echo(f"  [{done}/{total}] Last: {key}")

        success, total = sync_all_stock_fundamentals(
            conn, max_workers=workers, progress_cb=on_progress
        )
        typer.secho(
            f"Successfully updated fundamentals for {success}/{total} stocks.",
            fg=typer.colors.GREEN,
        )
        if success:
            from portfolio_tracker.core.score_snapshots import snapshot_summary

            summary = snapshot_summary(conn)
            typer.echo(
                f"Score snapshots on file: {summary['snapshots']} rows over "
                f"{summary['days']} days (latest {summary['last_date']})."
            )
    finally:
        conn.close()


@app.command("score-backtest")
def score_backtest(
    db: DbPath = DEFAULT_DB_PATH,
    score: Annotated[
        str, typer.Option("--score", help="Which score to test: value or quality.")
    ] = "value",
    horizon: Annotated[
        list[str] | None,
        typer.Option("--horizon", help="Horizon(s): 1m, 3m, 6m, 12m. Default all."),
    ] = None,
    formula_version: Annotated[
        str | None, typer.Option("--formula-version", help="Only this formula version.")
    ] = None,
    sample: Annotated[
        str,
        typer.Option("--sample", help="Snapshot days to use: all, weekly or monthly."),
    ] = "monthly",
    benchmark: Annotated[
        str | None,
        typer.Option("--benchmark", help="Import key of the benchmark instrument for this run."),
    ] = None,
    set_benchmark: Annotated[
        str | None,
        typer.Option("--set-benchmark", help="Store this import key as the default benchmark."),
    ] = None,
    max_age: Annotated[
        int,
        typer.Option("--max-age", help="Ignore snapshots whose fundamentals are older (days)."),
    ] = 120,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Measure whether the stock scores predicted forward returns.

    Reads the dated score snapshots, computes each stock's return since the
    snapshot from the stored prices, and reports rank correlation, bucket
    returns and hit rate against the benchmark. Diagnostic, not proof.
    """
    from portfolio_tracker.core import settings
    from portfolio_tracker.core.score_backtest import (
        BENCHMARK_SETTING_KEY,
        SAMPLES,
        SCORE_KINDS,
        evaluate,
    )

    if score not in SCORE_KINDS:
        typer.secho(f"--score must be one of {', '.join(SCORE_KINDS)}", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if sample not in SAMPLES:
        typer.secho(f"--sample must be one of {', '.join(SAMPLES)}", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    conn = connect(db)
    try:
        if set_benchmark:
            row = conn.execute(
                "SELECT 1 FROM instrument WHERE import_key = ?", (set_benchmark,)
            ).fetchone()
            if row is None:
                typer.secho(f"No instrument with key {set_benchmark!r}.", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            settings.set_value(conn, BENCHMARK_SETTING_KEY, set_benchmark)
            conn.commit()
            typer.secho(f"Benchmark set to {set_benchmark}.", fg=typer.colors.GREEN)

        try:
            report = evaluate(
                conn,
                score=score,
                horizons=horizon or None,
                formula_version=formula_version,
                sample=sample,
                benchmark=benchmark,
                max_fundamentals_age_days=max_age,
            )
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=2) from exc
    finally:
        conn.close()

    if as_json:
        typer.echo(json.dumps(report.to_dict(), indent=2, default=str))
        return

    cov = report.coverage
    if not cov["snapshots"]:
        typer.echo(
            "No score snapshots yet. Run `pt sync-fundamentals` or the daily update; "
            "snapshots are recorded from then on."
        )
        return

    bench = report.benchmark if report.benchmark_mode == "instrument" else "universe median"
    typer.echo(
        f"{report.score.title()} score: {cov['snapshots']} snapshots, {cov['instruments']} stocks, "
        f"{cov['days']} days ({cov['first_date']} to {cov['last_date']}); "
        f"benchmark: {bench}; sample: {report.sample}."
    )
    mature = cov["mature_by_horizon"]
    if not any(mature.values()):
        first = cov["first_reading_by_horizon"]
        typer.echo(
            "Collecting. Nothing is old enough to measure yet; "
            f"the first 1-month reading is possible from {first.get('1m')}."
        )
        return

    def f(x: float | None, digits: int = 2, pct: bool = False) -> str:
        if x is None:
            return "-"
        return f"{x * 100:.{digits}f}%" if pct else f"{x:.{digits}f}"

    for version, stats in report.versions.items():
        typer.echo("")
        typer.secho(f"Formula {version}", bold=True)
        typer.echo(
            f"{'horizon':>8} {'n':>5} {'days':>5} {'IC':>6} {'t':>6} {'IC>0':>6} "
            f"{'hit':>6} {'top':>8} {'bottom':>8} {'spread':>8}"
        )
        for st in stats:
            top = st.buckets[0].mean_rel if st.buckets else None
            bottom = st.buckets[-1].mean_rel if st.buckets else None
            typer.echo(
                f"{st.horizon:>8} {st.n_obs:>5} {st.n_dates:>5} {f(st.mean_ic):>6} "
                f"{f(st.ic_tstat, 1):>6} {f(st.ic_positive_share, 0, True):>6} "
                f"{f(st.hit_rate, 0, True):>6} {f(top, 1, True):>8} {f(bottom, 1, True):>8} "
                f"{f(st.spread_top_minus_bottom, 1, True):>8}"
            )
        typer.echo("")
        typer.echo("Which block predicts (mean IC per horizon):")
        components = list(stats[0].component_ic) if stats else []
        typer.echo(f"{'block':>16} " + " ".join(f"{st.horizon:>7}" for st in stats))
        for c in components:
            typer.echo(f"{c:>16} " + " ".join(f"{f(st.component_ic.get(c)):>7}" for st in stats))
        for st in stats:
            for note in st.caveats:
                typer.echo(f"  {st.horizon}: {note}")
    typer.echo("")
    for note in report.caveats:
        typer.echo(f"Note: {note}")


if __name__ == "__main__":  # pragma: no cover
    app()
