"""``pt look-through`` -- what the funds actually hold.

The import command is the only way constituent data enters this system. There
is no fetcher, and adding one would be a mistake in the same way an automatic
price fetcher for Turkish funds would be: every issuer publishes its own file,
on its own schedule, and none of them promises the format will be there next
quarter. A manual file that somebody chose to place is a contract; a scraper is
a dependency that fails quietly on a Sunday.

The import prints what it decided -- which column it read as the weight, what
unit it concluded, what the weights summed to before scaling -- because those
three decisions are where a plausible, wrong answer comes from.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from portfolio_tracker.core import look_through
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect
from portfolio_tracker.ingest import look_through as ingest

app = typer.Typer(
    no_args_is_help=True,
    help="Fund constituents, and the economic exposure they reveal.",
)

DbPath = Annotated[Path, typer.Option("--db", help="Database file.")]


def _instrument_id(conn: object, key: str) -> int:
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT id FROM instrument WHERE import_key = ? OR isin = ? OR symbol = ?",
        (key, key.upper(), key),
    ).fetchone()
    if row is None:
        typer.secho(
            f"no instrument matches {key!r}; import a positions export first, "
            "or check `pt look-through list`",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return int(row["id"])


@app.command("import")
def import_holdings(
    instrument: Annotated[
        str, typer.Option("--instrument", "-i", help="Import key, ISIN or symbol.")
    ],
    file: Annotated[Path, typer.Option("--file", "-f", help="Holdings CSV from the issuer.")],
    as_of: Annotated[
        str, typer.Option("--as-of", help="The date the FUND published this composition.")
    ],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Where the file came from. Defaults to its path."),
    ] = None,
    weights: Annotated[
        str,
        typer.Option(
            "--weights",
            help="Unit of the weight column: auto, percent or fraction.",
        ),
    ] = "auto",
    tolerance: Annotated[
        str,
        typer.Option("--tolerance", help="How far the weights may sum from 1.0."),
    ] = str(ingest.DEFAULT_TOLERANCE),
    db: DbPath = DEFAULT_DB_PATH,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Parse and report; write nothing.")
    ] = False,
) -> None:
    """Load one fund's constituent list.

    Idempotent per (fund, as-of date): re-importing replaces that composition
    wholesale rather than merging, so half of an old file can never sit beside
    half of a new one and produce a fund nobody published.
    """
    if weights not in {"auto", "percent", "fraction"}:
        typer.secho("--weights must be auto, percent or fraction", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if not file.exists():
        typer.secho(f"no such file: {file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    published = date.fromisoformat(as_of)
    try:
        parsed, digest = ingest.load(
            file,
            unit=weights,  # type: ignore[arg-type]
            tolerance=Decimal(tolerance),
        )
    except ingest.LookThroughError as exc:
        typer.secho(f"refused: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    # Printed before the write, and printed even on --dry-run: these are the
    # three decisions that turn a good file into a wrong answer.
    typer.echo(f"{file}")
    typer.echo(f"  weight column   {parsed.columns['weight']!r} read as {parsed.unit}")
    mapped = ", ".join(f"{k}={v!r}" for k, v in parsed.columns.items() if k != "weight")
    typer.echo(f"  other columns   {mapped or 'none'}")
    typer.echo(f"  holdings        {len(parsed.lines)} ({parsed.skipped} row(s) skipped)")
    drift = parsed.drift
    colour = None if abs(drift) < Decimal("0.001") else typer.colors.YELLOW
    typer.secho(f"  weights sum to  {parsed.weight_sum:.6f}  (drift {drift:+.6f})", fg=colour)
    if parsed.negative:
        typer.secho(
            f"  {parsed.negative} negative weight(s) -- a short or a futures offset; "
            "stored as published",
            fg=typer.colors.YELLOW,
        )

    if dry_run:
        typer.echo("\ndry run: nothing written")
        return

    conn = connect(db)
    try:
        target = _instrument_id(conn, instrument)
        result = ingest.store(
            conn,
            instrument_id=target,
            as_of=published,
            parsed=parsed,
            source=source or str(file),
            file_sha256=digest,
        )
        conn.commit()
    finally:
        conn.close()

    verb = "replaced" if result.replaced else "stored"
    typer.secho(
        f"\n{verb} {result.holdings} holding(s) for {instrument} as of {published}",
        fg=typer.colors.GREEN,
    )
    typer.echo("exposure is derived on read; no rebuild is needed")


@app.command("list")
def list_coverage(
    db: DbPath = DEFAULT_DB_PATH,
    person_id: Annotated[int, typer.Option("--person", help="Whose portfolio.")] = 1,
) -> None:
    """Which held instruments have a composition, and how old it is."""
    conn = connect(db, read_only=True)
    try:
        try:
            funds = look_through.fund_coverage(conn, person_id=person_id)
        except look_through.NoValuation as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"{'instrument':<10} {'value (EUR)':>13}  {'composition':<12} {'lines':>6}  source")
    for fund in funds:
        if fund.covered:
            colour = typer.colors.YELLOW if fund.stale else None
            age = f"{fund.composition_date} ({fund.stale_days}d)"
            typer.secho(
                f"{fund.import_key:<10} {fund.value_eur:>13,.2f}  {fund.composition_date!s:<12} "
                f"{fund.holdings:>6}  {fund.source}",
                fg=colour,
            )
            if fund.stale:
                typer.secho(f"    stale: {age}", fg=typer.colors.YELLOW)
        else:
            note = "no composition" if fund.is_fund else "not a fund; it is its own exposure"
            typer.secho(
                f"{fund.import_key:<10} {fund.value_eur:>13,.2f}  {note}",
                fg=typer.colors.YELLOW if fund.is_fund else None,
            )

    missing = [f for f in funds if f.is_fund and not f.covered]
    held = sum((f.value_eur for f in funds), Decimal(0))
    if missing and held > 0:
        share = sum((f.value_eur for f in missing), Decimal(0)) / held
        typer.echo("")
        typer.secho(
            f"{share:.1%} of holdings has no composition and is reported on the wrapper",
            fg=typer.colors.YELLOW,
        )


@app.command("show")
def show(
    instrument: Annotated[str, typer.Option("--instrument", "-i", help="Import key or ISIN.")],
    db: DbPath = DEFAULT_DB_PATH,
    limit: Annotated[int, typer.Option("--limit", help="Rows to print.")] = 20,
) -> None:
    """One fund's stored composition, largest first."""
    conn = connect(db, read_only=True)
    try:
        target = _instrument_id(conn, instrument)
        header = conn.execute(
            "SELECT * FROM fund_holding_set WHERE instrument_id = ? ORDER BY as_of DESC LIMIT 1",
            (target,),
        ).fetchone()
        if header is None:
            typer.secho(f"no composition stored for {instrument}", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1)

        typer.echo(f"{instrument} as of {header['as_of']}  ({header['source']})")
        typer.echo(
            f"  {header['holdings']} holding(s), published weights sum to "
            f"{header['weight_sum']:.6f} as {header['unit']}"
        )
        typer.echo("")
        rows = conn.execute(
            "SELECT name, identifier, weight, currency, country, sector FROM fund_holding "
            "WHERE instrument_id = ? AND as_of = ? ORDER BY weight DESC LIMIT ?",
            (target, header["as_of"], limit),
        ).fetchall()
        typer.echo(f"  {'weight':>8}  {'ccy':<4} {'country':<14} {'sector':<22} name")
        for row in rows:
            typer.echo(
                f"  {row['weight']:>8.4%}  {(row['currency'] or '-'):<4} "
                f"{(row['country'] or '-')[:14]:<14} {(row['sector'] or '-')[:22]:<22} "
                f"{row['name'][:40]}"
            )
        if header["holdings"] > limit:
            typer.echo(f"  ... and {header['holdings'] - limit} more")
    finally:
        conn.close()


@app.command("exposure")
def exposure(
    db: DbPath = DEFAULT_DB_PATH,
    dim: Annotated[
        str, typer.Option("--dim", help="currency, country, sector or asset_class.")
    ] = "currency",
    person_id: Annotated[int, typer.Option("--person", help="Whose portfolio.")] = 1,
    compare: Annotated[
        bool,
        typer.Option("--compare", help="Show the quote-basis figure beside it."),
    ] = True,
) -> None:
    """Economic exposure: what the portfolio holds, not what it is quoted in.

    ``--compare`` puts the v1 answer next to the v2 one. For a portfolio of
    euro-listed global trackers the two are wildly different, and seeing the gap
    is the entire reason this feature exists.
    """
    conn = connect(db, read_only=True)
    try:
        try:
            result = look_through.exposure(conn, person_id=person_id, dimension=dim)
        except look_through.NoValuation as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

        quote: dict[str, Decimal] = {}
        if compare:
            column = {
                "currency": "i.currency",
                "country": "i.domicile",
                "sector": "i.sector",
                "asset_class": "i.asset_class",
            }[dim]
            for row in conn.execute(
                f"SELECT {column} AS bucket, p.market_value_eur FROM position_daily p "
                "JOIN instrument i ON i.id = p.instrument_id "
                "JOIN account a ON a.id = p.account_id "
                "WHERE p.date = ? AND a.person_id = ?",  # column is from the fixed map above
                (result.as_of.isoformat(), person_id),
            ):
                key = row["bucket"] or look_through.UNKNOWN
                quote[key] = quote.get(key, Decimal(0)) + row["market_value_eur"]
    finally:
        conn.close()

    typer.echo(f"{dim} exposure as of {result.as_of}")
    typer.echo("")
    if compare:
        typer.echo(f"  {'bucket':<24} {'look-through':>14} {'weight':>8}   {'quoted':>14}")
    else:
        typer.echo(f"  {'bucket':<24} {'value (EUR)':>14} {'weight':>8}")
    for bucket in result.buckets:
        line = f"  {bucket.name[:24]:<24} {bucket.value_eur:>14,.2f} {bucket.weight:>8.1%}"
        if compare:
            quoted = quote.get(bucket.name)
            line += f"   {quoted:>14,.2f}" if quoted is not None else f"   {'-':>14}"
        typer.echo(line)

    covered = result.coverage.look_through_pct
    typer.echo("")
    typer.secho(
        f"  {covered:.1%} of holdings attributed on evidence (constituents or a direct holding)",
        fg=typer.colors.GREEN if covered > Decimal("0.9") else typer.colors.YELLOW,
    )
    for caveat in result.caveats:
        typer.secho(f"  - {caveat}", fg=typer.colors.YELLOW)


@app.command("concentration")
def concentration(
    db: DbPath = DEFAULT_DB_PATH,
    top: Annotated[int, typer.Option("--top", help="How many issuers to show.")] = 20,
    person_id: Annotated[int, typer.Option("--person", help="Whose portfolio.")] = 1,
) -> None:
    """The largest underlying issuers, across every fund and direct holding.

    Overlap between funds is invisible at the instrument level and is the
    reason this exists: three global trackers plus a semiconductor fund can
    easily be a bigger single-issuer bet than anything held outright.
    """
    conn = connect(db, read_only=True)
    try:
        try:
            result = look_through.concentration(conn, person_id=person_id, top=top)
        except look_through.NoValuation as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"top {top} underlying issuers as of {result.as_of}")
    typer.echo("")
    typer.echo(f"  {'issuer':<34} {'value (EUR)':>13} {'weight':>8}  via")
    for u in result.underlyings:
        via = ", ".join(f"{key} {value:,.0f}" for key, value in u.via)
        typer.echo(f"  {u.name[:34]:<34} {u.value_eur:>13,.2f} {u.weight:>8.2%}  {via}")

    typer.echo("")
    typer.echo(
        f"  EUR {result.resolved_eur:,.2f} of EUR {result.total_eur:,.2f} holdings "
        "resolved to an issuer"
    )
    for caveat in result.caveats:
        typer.secho(f"  - {caveat}", fg=typer.colors.YELLOW)
