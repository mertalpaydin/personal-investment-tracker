"""Routing instruments to providers, storing prices, and reporting coverage.

Coverage is reported on *observations*, never on what a forward-fill would
produce. Filling is a valuation-time decision made against an explicit
tolerance; letting it also hide the gaps it is covering would leave nobody
able to tell a quiet market from a broken downloader.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.core.calendars import grid_days, is_business_day
from portfolio_tracker.ingest.onvista_cache import NotationCache
from portfolio_tracker.ingest.providers.base import (
    InstrumentRef,
    PriceObservation,
    PriceProvider,
    ProviderUnavailable,
    RateLimited,
)
from portfolio_tracker.ingest.providers.manual import ManualProvider
from portfolio_tracker.ingest.providers.market import (
    FintablesProvider,
    OnvistaProvider,
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


#: When the primary source for an instrument fails with `ProviderUnavailable`
#: (not `RateLimited`), the run tries this one before giving up on it. Yahoo
#: is unreliable for Xetra ETFs, which is why onvista is primary now, but it
#: is a public undocumented endpoint too -- one that stops answering should
#: not leave an instrument unpriced when the very feed it replaced still works.
FALLBACK_PROVIDER: dict[str, str] = {"onvista": "yfinance"}


def default_providers(conn: sqlite3.Connection | None = None) -> dict[str, PriceProvider]:
    """The standard provider registry.

    ``conn`` backs onvista's notation cache -- a resolved instrument/market
    lookup is written there once and read on every later call, so a caller
    with no connection at hand (a one-off `describe()`, a unit test building
    providers in isolation) still gets a working, if uncached, provider.
    """
    return {
        "yfinance": YFinanceProvider(),
        "tefas": TefasProvider(),
        "fintables": FintablesProvider(),
        "onvista": OnvistaProvider(
            notation_cache=NotationCache(conn) if conn is not None else None
        ),
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
               i.price_symbol, i.trading_calendar, i.status, i.isin
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
            status=str(r["status"] or "OWNED"),
            isin=r["isin"],
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
    #: Set when the primary provider failed but a fallback recovered it. Kept
    #: separate from `error`, which stays `None` on a successful run -- this
    #: is informational, not something that should make the run look failed.
    note: str | None = None

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


#: How many times to wait and try again when a source says we are asking too
#: often, and how long the first wait is. Doubling from there: 5s, 10s, 20s.
#: Three attempts is enough to ride out a short throttle without turning a
#: batch of two hundred into an afternoon.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 5.0


@dataclass
class FetchRun:
    """What a fetch achieved, and whether it finished.

    ``stopped_early`` is the field that matters. A run cut short by throttling
    has still stored everything it got -- each instrument is committed as it
    lands -- so the honest report is "this much, and there is more to come",
    not a failure and not a success.
    """

    coverage: list[InstrumentCoverage] = field(default_factory=list)
    stopped_early: bool = False
    reason: str | None = None
    remaining: int = 0

    @property
    def written(self) -> int:
        return sum(c.written for c in self.coverage)


def fetch_all(
    conn: sqlite3.Connection,
    *,
    end: date,
    start: date | None = None,
    providers: dict[str, PriceProvider] | None = None,
    allow_manual_fallback: bool = True,
    incremental: bool = False,
    only: list[int] | None = None,
    weekly_for_watched: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[InstrumentCoverage]:
    """Fetch prices for every priced instrument, then report coverage.

    A provider failure is recorded and the run continues. One dead endpoint must
    not prevent the other eleven instruments from being priced.

    ``incremental`` asks each provider only for what is missing, which is what
    the daily job uses; without it every run re-requests two years of history
    from every provider on every run. ``only`` restricts to given instrument
    ids, for backfilling a single newly added ticker.
    """
    return run_fetch(
        conn,
        end=end,
        start=start,
        providers=providers,
        allow_manual_fallback=allow_manual_fallback,
        incremental=incremental,
        only=only,
        weekly_for_watched=weekly_for_watched,
        sleeper=sleeper,
    ).coverage


def _fetch_with_backoff(
    provider: PriceProvider,
    instrument: InstrumentRef,
    window_start: date,
    end: date,
    *,
    weekly: bool,
    sleeper: Callable[[float], None],
) -> list[PriceObservation]:
    """One instrument, waiting and retrying if the source is throttling.

    Backs off before retrying rather than after failing, because the whole
    point is to stop asking for a moment. If it is still refusing after the
    last wait, the `RateLimited` escapes and the caller stops the run -- there
    is nothing to be gained by putting the same question to a source that has
    said no four times.
    """
    delay = RATE_LIMIT_BACKOFF_SECONDS
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            return provider.fetch(instrument, window_start, end, weekly=weekly)
        except RateLimited:
            if attempt == RATE_LIMIT_RETRIES - 1:
                raise
            sleeper(delay)
            delay *= 2
    return []  # unreachable; the loop either returns or raises


def run_fetch(
    conn: sqlite3.Connection,
    *,
    end: date,
    start: date | None = None,
    providers: dict[str, PriceProvider] | None = None,
    allow_manual_fallback: bool = True,
    incremental: bool = False,
    only: list[int] | None = None,
    weekly_for_watched: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> FetchRun:
    """Fetch prices, and report whether throttling cut the run short.

    ``weekly_for_watched`` asks for one observation a week for instruments
    that are only ever compared against, never valued. Two hundred candidates
    at daily resolution is a great deal of somebody else's bandwidth to answer
    a question about three-year returns.
    """
    registry = providers or default_providers(conn)
    manual = registry.get("manual")
    run = FetchRun()
    results = run.coverage

    queue = [i for i in priced_instruments(conn) if only is None or i.instrument_id in only]
    for position, instrument in enumerate(queue):
        # Only what is owned is valued daily. Everything else exists to be
        # compared against, and a weekly series answers that question.
        weekly = weekly_for_watched and instrument.status != "OWNED"
        # Coverage is reported from the first trade for something owned, and
        # from whatever was actually requested for something merely watched --
        # a watchlist entry has no trade date to be measured against.
        traded_from = first_trade_date(conn, instrument.instrument_id)
        window_start = start or window_for(conn, instrument, end=end, incremental=incremental)
        coverage_from = max(window_start, traded_from) if traded_from else window_start
        provider = registry.get(instrument.price_source)
        error: str | None = None
        note: str | None = None
        used: str | None = None
        added_any = 0

        if provider is None:
            error = f"no provider registered for source {instrument.price_source!r}"
        else:
            try:
                observations = _fetch_with_backoff(
                    provider, instrument, window_start, end, weekly=weekly, sleeper=sleeper
                )
                added_any += store(conn, instrument, observations, provider.name)
                used = provider.name
            except RateLimited as exc:
                # Stop the whole run rather than work through the rest. Being
                # throttled means the next request is more likely to fail than
                # the last, and continuing turns a pause into a block. What is
                # already stored stays stored, and `incremental` picks up here.
                conn.commit()
                run.stopped_early = True
                run.reason = str(exc)
                run.remaining = len(queue) - position
                return run
            except ProviderUnavailable as exc:
                error = str(exc)

            # A source with a registered fallback (currently onvista ->
            # yfinance) gets one try at it before the instrument is reported
            # as failed. onvista is a public, undocumented endpoint too --
            # one that stops answering should not leave an instrument unpriced
            # when the feed it replaced still works.
            fallback_name = FALLBACK_PROVIDER.get(provider.name)
            fallback = registry.get(fallback_name) if fallback_name else None
            if error is not None and fallback is not None and fallback is not provider:
                try:
                    fallback_observations = _fetch_with_backoff(
                        fallback, instrument, window_start, end, weekly=weekly, sleeper=sleeper
                    )
                except RateLimited as exc:
                    conn.commit()
                    run.stopped_early = True
                    run.reason = str(exc)
                    run.remaining = len(queue) - position
                    return run
                except ProviderUnavailable as fb_exc:
                    error = f"{error}  [{fallback.name} fallback also failed: {fb_exc}]"
                else:
                    fallback_added = store(conn, instrument, fallback_observations, fallback.name)
                    added_any += fallback_added
                    if fallback_added:
                        used = fallback.name
                        note = f"[{provider.name} failed: {error}; used {fallback.name}]"
                        error = None
                    else:
                        error = f"{error}  [{fallback.name} fallback returned nothing]"

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
        report.note = note
        # `coverage` reports the source most rows actually came from. Only
        # override it when this run contributed something, so a run that
        # inserted one new price does not claim credit for a history someone
        # else supplied.
        if used and added_any:
            report.source = used
        report.written = added_any
        results.append(report)

    return run


# ---------------------------------------------------------------------------
# Reprice: compare a source's history against what is stored, and optionally
# replace it. Built for the onvista migration (Yahoo's Xetra ETF history is
# wrong for roughly half the .DE ETFs in this database), but not specific to
# it: any `--source` registered in the provider map can be checked this way.
# ---------------------------------------------------------------------------

#: How far back to compare. Five years, matching `WATCHLIST_BACKFILL_YEARS`:
#: long enough to catch a scaling error or a wrong-fund mixup that only shows
#: up outside the last year or two, short enough to stay one request.
REPRICE_HISTORY_YEARS = 5

#: A coverage gap longer than this many business days is what "the new
#: source covers dates the old one lacks" means -- short of that, a handful
#: of extra days is exactly what a backfill is expected to add incrementally,
#: not a sign the old series is missing history.
REPRICE_COVERAGE_GAP_DAYS = 3


@dataclass
class RepriceRow:
    import_key: str
    instrument_id: int
    stored_rows: int
    new_rows: int
    overlap: int
    max_rel_diff: Decimal | None
    verdict: str  # MATCH / REPLACE / UNRESOLVED
    reason: str | None = None


def _reprice_classify(
    stored: dict[date, tuple[Decimal, str]],
    new: dict[date, PriceObservation],
    *,
    tolerance: Decimal,
    calendar: str,
) -> tuple[Decimal | None, bool]:
    """Max relative difference on overlapping dates, and whether it flags a gap."""
    overlap = set(stored) & set(new)
    max_rel_diff: Decimal | None = None
    for day in overlap:
        old_close, _source = stored[day]
        new_close = new[day].close
        if old_close == 0:
            continue
        rel = abs(new_close - old_close) / abs(old_close)
        if max_rel_diff is None or rel > max_rel_diff:
            max_rel_diff = rel

    stored_from_yfinance = any(src == "yfinance" for _close, src in stored.values())
    gap = False
    if stored_from_yfinance:
        extra = [d for d in new if d not in stored]
        extra_business_days = [d for d in extra if is_business_day(d, calendar)]
        gap = len(extra_business_days) > REPRICE_COVERAGE_GAP_DAYS
    return max_rel_diff, gap


def reprice(
    conn: sqlite3.Connection,
    *,
    source: str = "onvista",
    tolerance: Decimal = Decimal("0.005"),
    only_mismatched: bool = True,
    apply: bool = False,
    providers: dict[str, PriceProvider] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[list[RepriceRow], bool]:
    """Compare ``source``'s history against what is stored, per instrument.

    An instrument is "mismatched" (verdict ``REPLACE``) if any overlapping
    close differs by more than ``tolerance`` relative, or if the stored series
    has ``yfinance`` rows and the new one covers more than
    ``REPRICE_COVERAGE_GAP_DAYS`` business days the stored one lacks.

    With ``apply``, every ``REPLACE`` instrument has its ``yfinance`` rows
    deleted (manual rows are never touched) and the new rows inserted, then
    `core.rebuild.run` is called once after the loop -- once, not per
    instrument, because it recomputes the whole derived layer regardless of
    how many prices changed.

    Returns the rows and whether a `RateLimited` response stopped the run
    before every instrument was checked -- what is already classified (and,
    under ``apply``, already replaced) stays as it is; the run resumes from
    there next time.
    """
    registry = providers or default_providers(conn)
    provider = registry.get(source)
    rows: list[RepriceRow] = []
    if provider is None:
        return rows, False

    instruments = [i for i in priced_instruments(conn) if i.price_source == source]
    end = date.today()
    start = end - timedelta(days=365 * REPRICE_HISTORY_YEARS)
    stopped_early = False

    for instrument in instruments:
        stored_rows = conn.execute(
            "SELECT date, close, source FROM price WHERE instrument_id = ? ORDER BY date",
            (instrument.instrument_id,),
        ).fetchall()
        stored = {
            date.fromisoformat(r["date"]): (Decimal(str(r["close"])), r["source"])
            for r in stored_rows
        }

        try:
            observations = _fetch_with_backoff(
                provider, instrument, start, end, weekly=False, sleeper=sleeper
            )
        except RateLimited:
            stopped_early = True
            break
        except ProviderUnavailable as exc:
            rows.append(
                RepriceRow(
                    import_key=instrument.import_key,
                    instrument_id=instrument.instrument_id,
                    stored_rows=len(stored),
                    new_rows=0,
                    overlap=0,
                    max_rel_diff=None,
                    verdict="UNRESOLVED",
                    reason=str(exc),
                )
            )
            continue

        new = {o.on: o for o in observations}
        max_rel_diff, gap = _reprice_classify(
            stored, new, tolerance=tolerance, calendar=instrument.trading_calendar
        )
        mismatched = (max_rel_diff is not None and max_rel_diff > tolerance) or gap
        verdict = "REPLACE" if mismatched else "MATCH"
        row = RepriceRow(
            import_key=instrument.import_key,
            instrument_id=instrument.instrument_id,
            stored_rows=len(stored),
            new_rows=len(new),
            overlap=len(set(stored) & set(new)),
            max_rel_diff=max_rel_diff,
            verdict=verdict,
        )

        if verdict == "REPLACE" and apply:
            conn.execute(
                "DELETE FROM price WHERE instrument_id = ? AND source = 'yfinance'",
                (instrument.instrument_id,),
            )
            written = store(conn, instrument, observations, provider.name)
            conn.commit()
            row.reason = f"replaced; {written} row(s) inserted"

        if not only_mismatched or verdict != "MATCH":
            rows.append(row)

    if apply and any(r.verdict == "REPLACE" for r in rows):
        from portfolio_tracker.core import rebuild as rebuild_all

        rebuild_all.run(conn, end=date.today(), strict=False)

    return rows, stopped_early
