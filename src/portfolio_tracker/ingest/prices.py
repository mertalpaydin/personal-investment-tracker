"""Routing instruments to providers, storing prices, and reporting coverage.

Coverage is reported on *observations*, never on what a forward-fill would
produce. Filling is a valuation-time decision made against an explicit
tolerance; letting it also hide the gaps it is covering would leave nobody
able to tell a quiet market from a broken downloader.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.core.calendars import grid_days
from portfolio_tracker.ingest.providers.base import (
    InstrumentRef,
    PriceObservation,
    PriceProvider,
    ProviderUnavailable,
)
from portfolio_tracker.ingest.providers.manual import ManualProvider
from portfolio_tracker.ingest.providers.market import (
    FintablesProvider,
    SyntheticProvider,
    TefasProvider,
    YFinanceProvider,
)

#: Calendar days of price history to fetch before the first trade.
PRICE_LEAD_DAYS = 10

#: Years of history fetched for an instrument that has never been traded --
#: a watchlist entry. Five, because a three-year annualised return is the
#: figure a comparison turns on and it needs three full years to exist, with
#: room to spare. A newly watched ticker is therefore one slow fetch, once.
WATCHLIST_BACKFILL_YEARS = 5

#: Days of already-stored history re-requested on an incremental run.
#: Providers revise a provisional close for a day or two after the fact, and
#: `store` never overwrites, so the cost of asking again is one small request.
REFETCH_OVERLAP_DAYS = 5


def default_providers() -> dict[str, PriceProvider]:
    return {
        "yfinance": YFinanceProvider(),
        "tefas": TefasProvider(),
        "fintables": FintablesProvider(),
        "synthetic": SyntheticProvider(),
        "manual": ManualProvider(),
    }


def priced_instruments(
    conn: sqlite3.Connection, *, include_watched: bool = True
) -> list[InstrumentRef]:
    """Instruments the price layer is responsible for keeping current.

    Anything with at least one transaction, plus anything being watched or
    held in the comparison universe. A watched instrument has no lots, so it
    never reaches ``core/valuation.py`` and cannot affect a single figure
    about the portfolio -- it exists to have a price history worth comparing
    against, which is the whole point of watching it.

    This used to be an inner join on ``transaction_``, which meant a watchlist
    entry could be added and would then never be priced.
    """
    watched = ("WATCH", "UNIVERSE") if include_watched else ()
    placeholders = ",".join("?" for _ in watched)
    clause = f" OR i.status IN ({placeholders})" if watched else ""
    rows = conn.execute(
        f"""
        SELECT i.id, i.import_key, i.currency, i.price_source,
               i.price_symbol, i.trading_calendar
        FROM instrument i
        WHERE EXISTS (SELECT 1 FROM transaction_ t WHERE t.instrument_id = i.id){clause}
        ORDER BY i.import_key
        """,
        watched,
    ).fetchall()
    return [
        InstrumentRef(
            instrument_id=int(r["id"]),
            import_key=r["import_key"],
            currency=r["currency"],
            price_source=r["price_source"],
            price_symbol=r["price_symbol"],
            trading_calendar=r["trading_calendar"],
        )
        for r in rows
    ]


def last_price_date(conn: sqlite3.Connection, instrument_id: int) -> date | None:
    """The most recent observation stored for an instrument."""
    row = conn.execute(
        "SELECT max(date) AS d FROM price WHERE instrument_id = ?", (instrument_id,)
    ).fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def first_trade_date(conn: sqlite3.Connection, instrument_id: int) -> date | None:
    row = conn.execute(
        "SELECT min(trade_date) AS d FROM transaction_ WHERE instrument_id = ?",
        (instrument_id,),
    ).fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def store(
    conn: sqlite3.Connection,
    instrument: InstrumentRef,
    observations: list[PriceObservation],
    source: str,
) -> int:
    """Insert prices, leaving any existing row alone.

    Prices are immutable observations; a re-fetch that disagrees with a stored
    value is a signal worth investigating, not something to overwrite quietly.
    """
    if not observations:
        return 0
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    for obs in observations:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO price
                (instrument_id, date, close, total_return_close, currency, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instrument.instrument_id,
                obs.on.isoformat(),
                obs.close,
                obs.total_return_close,
                instrument.currency,
                source,
                now,
            ),
        )
        written += cursor.rowcount
    return written


@dataclass
class InstrumentCoverage:
    import_key: str
    calendar: str
    start: date
    end: date
    expected: int
    observed: int
    gaps: list[tuple[date, date, int]] = field(default_factory=list)
    source: str | None = None
    error: str | None = None
    #: Rows this run actually inserted. Counted here rather than inferred from
    #: dates afterwards: a backfill writes five years of history, all of it
    #: older than the newest price already stored, so "rows newer than before"
    #: reports a successful backfill as having done nothing.
    written: int = 0

    @property
    def pct(self) -> Decimal:
        if self.expected == 0:
            return Decimal(100)
        return (Decimal(self.observed) / Decimal(self.expected) * 100).quantize(Decimal("0.1"))

    @property
    def ok(self) -> bool:
        return self.error is None and self.pct >= Decimal("98.0")


def coverage(
    conn: sqlite3.Connection,
    instrument: InstrumentRef,
    start: date,
    end: date,
    *,
    max_gap_days: int = 5,
) -> InstrumentCoverage:
    """Observed prices against the instrument's own trading calendar.

    Runs of consecutive missing days longer than ``max_gap_days`` are listed
    individually, because that is the length at which forward-filling stops
    being a reasonable stand-in and starts being an invention.
    """
    expected = grid_days(start, end, instrument.trading_calendar)
    rows = conn.execute(
        "SELECT date FROM price WHERE instrument_id = ? AND date BETWEEN ? AND ?",
        (instrument.instrument_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    have = {date.fromisoformat(r["date"]) for r in rows}

    gaps: list[tuple[date, date, int]] = []
    run: list[date] = []
    for day in expected:
        if day in have:
            if len(run) > max_gap_days:
                gaps.append((run[0], run[-1], len(run)))
            run = []
        else:
            run.append(day)
    if len(run) > max_gap_days:
        gaps.append((run[0], run[-1], len(run)))

    source_row = conn.execute(
        "SELECT source, count(*) AS n FROM price WHERE instrument_id = ? "
        "GROUP BY source ORDER BY n DESC LIMIT 1",
        (instrument.instrument_id,),
    ).fetchone()

    return InstrumentCoverage(
        import_key=instrument.import_key,
        calendar=instrument.trading_calendar,
        start=start,
        end=end,
        expected=len(expected),
        observed=len([d for d in expected if d in have]),
        gaps=gaps,
        source=source_row["source"] if source_row else None,
    )


def window_for(
    conn: sqlite3.Connection,
    instrument: InstrumentRef,
    *,
    end: date,
    incremental: bool,
) -> date:
    """The first date to request for this instrument.

    On a full run, reach back before the first trade: a market can be shut on
    the day a position is opened, and without an earlier observation that day
    cannot be valued at all. An instrument that has never been traded is a
    watchlist entry, and reaches back years instead -- there is no first trade
    to anchor to, and ten days of history compares against nothing.

    On an incremental run, start a few days before the last stored price. That
    is what makes "pick up everything missing" true regardless of *when* the
    last run happened -- a laptop closed for a fortnight resumes from the last
    observation, not from yesterday. An instrument with nothing stored falls
    back to the full window, so a newly watched ticker still gets its history.
    """
    traded_from = first_trade_date(conn, instrument.instrument_id)
    full = (
        traded_from - timedelta(days=PRICE_LEAD_DAYS)
        if traded_from is not None
        else end - timedelta(days=365 * WATCHLIST_BACKFILL_YEARS)
    )
    if not incremental:
        return full
    stored = last_price_date(conn, instrument.instrument_id)
    return min(stored - timedelta(days=REFETCH_OVERLAP_DAYS), end) if stored else full


def fetch_all(
    conn: sqlite3.Connection,
    *,
    end: date,
    start: date | None = None,
    providers: dict[str, PriceProvider] | None = None,
    allow_manual_fallback: bool = True,
    incremental: bool = False,
    only: list[int] | None = None,
) -> list[InstrumentCoverage]:
    """Fetch prices for every priced instrument, then report coverage.

    A provider failure is recorded and the run continues. One dead endpoint must
    not prevent the other eleven instruments from being priced.

    ``incremental`` asks each provider only for what is missing, which is what
    the daily job uses; without it every run re-requests two years of history
    from every provider on every run. ``only`` restricts to given instrument
    ids, for backfilling a single newly added ticker.
    """
    registry = providers or default_providers()
    manual = registry.get("manual")
    results: list[InstrumentCoverage] = []

    for instrument in priced_instruments(conn):
        if only is not None and instrument.instrument_id not in only:
            continue
        # Coverage is reported from the first trade for something owned, and
        # from whatever was actually requested for something merely watched --
        # a watchlist entry has no trade date to be measured against.
        traded_from = first_trade_date(conn, instrument.instrument_id)
        window_start = start or window_for(conn, instrument, end=end, incremental=incremental)
        coverage_from = max(window_start, traded_from) if traded_from else window_start
        provider = registry.get(instrument.price_source)
        error: str | None = None
        used: str | None = None
        added_any = 0

        if provider is None:
            error = f"no provider registered for source {instrument.price_source!r}"
        else:
            try:
                observations = provider.fetch(instrument, window_start, end)
                added_any += store(conn, instrument, observations, provider.name)
                used = provider.name
            except ProviderUnavailable as exc:
                error = str(exc)

            # A manual file is a *supplement*, not only a rescue. Some feeds
            # serve only the latest observation, so history has to come from a
            # file alongside them; and a user who has taken the trouble to
            # supply a figure should not have it ignored because the feed
            # happened to answer. Storage is keyed by (instrument, date) and
            # never overwrites, so merging the two is safe.
            if allow_manual_fallback and manual is not None and provider is not manual:
                try:
                    supplement = manual.fetch(instrument, window_start, end)
                    added = store(conn, instrument, supplement, manual.name)
                    added_any += added
                    if added:
                        used = f"{used}+manual" if used else "manual"
                        if error:
                            error = f"{error}  [recovered from manual file]"
                except ProviderUnavailable:
                    pass

        conn.commit()
        report = coverage(conn, instrument, coverage_from, end)
        report.error = error
        # `coverage` reports the source most rows actually came from. Only
        # override it when this run contributed something, so a run that
        # inserted one new price does not claim credit for a history someone
        # else supplied.
        if used and added_any:
            report.source = used
        report.written = added_any
        results.append(report)

    return results
