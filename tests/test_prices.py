"""Price providers, storage, coverage, and instrument seeding.

No test reaches the network. Providers separate fetching from parsing, so the
parsers run against captured payloads and the orchestration runs against fakes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_tracker import seeds
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.ingest import prices
from portfolio_tracker.ingest.providers.base import (
    InstrumentRef,
    PriceObservation,
    ProviderUnavailable,
    RateLimited,
)
from portfolio_tracker.ingest.providers.manual import ManualProvider, parse_csv
from portfolio_tracker.ingest.providers.market import SyntheticProvider, TefasProvider


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'D', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, "
        "price_source, price_symbol, trading_calendar) "
        "VALUES (1, 'AAA', 'Fund A', 'ETF', 'EUR', 'manual', NULL, 'TARGET2')"
    )
    conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, flow_class) VALUES (1, 1, 'BUY', '2026-08-03', 'EUR', '10', 'INVESTMENT')"
    )
    conn.commit()
    return conn


def ref(**kwargs: object) -> InstrumentRef:
    base = {
        "instrument_id": 1,
        "import_key": "AAA",
        "currency": "EUR",
        "price_source": "manual",
        "price_symbol": None,
        "trading_calendar": "TARGET2",
    }
    base.update(kwargs)
    return InstrumentRef(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Manual provider -- the one with no external dependency
# ---------------------------------------------------------------------------


def test_manual_csv_parses() -> None:
    observations = parse_csv("date,close\n2026-08-03,0.627\n2026-08-04,0.629\n")
    assert [o.on for o in observations] == [date(2026, 8, 3), date(2026, 8, 4)]
    assert observations[0].close == Decimal("0.627")
    assert observations[0].total_return_close is None


def test_manual_csv_accepts_an_optional_total_return_column() -> None:
    observations = parse_csv("date,close,total_return_close\n2026-08-03,10,10.5\n")
    assert observations[0].total_return_close == Decimal("10.5")


def test_manual_csv_rejects_a_missing_column() -> None:
    with pytest.raises(ProviderUnavailable, match="header row"):
        parse_csv("day,value\n2026-08-03,1\n")


def test_manual_csv_rejects_a_non_positive_price() -> None:
    """A zero price would silently value a whole position at nothing."""
    with pytest.raises(ProviderUnavailable, match="non-positive"):
        parse_csv("date,close\n2026-08-03,0\n")


def test_manual_provider_names_the_file_it_wants(tmp_path: Path) -> None:
    """The failure message has to be actionable: this is the path every other
    provider falls back to."""
    provider = ManualProvider(tmp_path)
    with pytest.raises(ProviderUnavailable, match=r"Create .*AAA\.csv"):
        provider.fetch(ref(), date(2026, 8, 1), date(2026, 8, 14))


def test_manual_provider_filters_to_the_window(tmp_path: Path) -> None:
    (tmp_path / "AAA.csv").write_text(
        "date,close\n2026-07-01,1\n2026-08-03,2\n2026-09-01,3\n", encoding="utf-8"
    )
    observations = ManualProvider(tmp_path).fetch(ref(), date(2026, 8, 1), date(2026, 8, 14))
    assert [o.on for o in observations] == [date(2026, 8, 3)]


# ---------------------------------------------------------------------------
# TEFAS -- the rebuilt site's JSON price endpoint
# ---------------------------------------------------------------------------


def _tefas_payload(*rows: tuple[str, float]) -> str:
    return json.dumps(
        {
            "errorCode": None,
            "errorMessage": None,
            "resultList": [{"fonKodu": "TTA", "tarih": d, "fiyat": p} for d, p in rows],
        }
    )


def test_tefas_parses_the_price_list() -> None:
    observations = TefasProvider.parse(_tefas_payload(("2026-09-25", 0.614713)), "TTA")
    assert observations[0].on == date(2026, 9, 25)
    assert observations[0].close == Decimal("0.614713")
    # NAV only: no distribution-adjusted series is published, and faking one
    # from NAV would overstate a distributing fund's performance.
    assert observations[0].total_return_close is None


def test_tefas_error_message_points_at_the_manual_path() -> None:
    """The service answers HTTP 200 with an error text for a request it rejects."""
    payload = json.dumps({"errorCode": None, "errorMessage": "Sistem Hatasi!!", "resultList": None})
    with pytest.raises(ProviderUnavailable, match=r"prices_manual/TTA\.csv"):
        TefasProvider.parse(payload, "TTA")


def test_tefas_non_json_response_points_at_the_manual_path() -> None:
    with pytest.raises(ProviderUnavailable, match="endpoint has changed"):
        TefasProvider.parse("<html>Request Rejected</html>", "TTA")


@pytest.mark.parametrize(
    ("start", "period"),
    [(date(2026, 9, 20), 1), (date(2026, 3, 1), 12), (date(2021, 10, 1), 60)],
)
def test_tefas_asks_for_the_smallest_look_back_that_reaches_start(start: date, period: int) -> None:
    seen: list[dict[str, object]] = []

    def fake(url: str, payload: bytes) -> str:
        seen.append(json.loads(payload))
        return _tefas_payload(("2026-09-24", 0.61), ("2026-09-25", 0.62))

    provider = TefasProvider(fetcher=fake, today=lambda: date(2026, 9, 27))
    rows = provider.fetch(ref(import_key="TTA", price_source="tefas"), start, date(2026, 9, 24))
    assert seen[0] == {"fonKodu": "TTA", "dil": "TR", "periyod": period}
    assert [r.on for r in rows] == [date(2026, 9, 24)], "trimmed to [start, end]"


# ---------------------------------------------------------------------------
# Synthetic -- the cash proxy
# ---------------------------------------------------------------------------


def test_synthetic_prices_a_deposit_at_one_unit_every_grid_day() -> None:
    """Its price never moves; the FX leg does all the work, which is the point."""
    observations = SyntheticProvider().fetch(
        ref(price_source="synthetic"), date(2026, 8, 3), date(2026, 8, 7)
    )
    assert len(observations) == 5
    assert all(o.close == Decimal(1) for o in observations)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_store_is_idempotent(db: sqlite3.Connection) -> None:
    observations = [PriceObservation(date(2026, 8, 3), Decimal("10.5"))]
    assert prices.store(db, ref(), observations, "manual") == 1
    assert prices.store(db, ref(), observations, "manual") == 0


def test_store_does_not_overwrite_an_existing_observation(db: sqlite3.Connection) -> None:
    """A re-fetch that disagrees is a signal worth investigating, not something
    to silently replace."""
    prices.store(db, ref(), [PriceObservation(date(2026, 8, 3), Decimal("10"))], "manual")
    prices.store(db, ref(), [PriceObservation(date(2026, 8, 3), Decimal("99"))], "yfinance")
    row = db.execute("SELECT close, source FROM price").fetchone()
    assert row["close"] == Decimal("10")
    assert row["source"] == "manual"


def test_stored_price_currency_must_match_the_instrument(db: sqlite3.Connection) -> None:
    """The schema trigger, reached through the storage path."""
    with pytest.raises(sqlite3.IntegrityError):
        prices.store(
            db, ref(currency="TRY"), [PriceObservation(date(2026, 8, 3), Decimal("1"))], "manual"
        )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_coverage_counts_against_the_instruments_own_calendar(db: sqlite3.Connection) -> None:
    for day in ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"):
        prices.store(db, ref(), [PriceObservation(date.fromisoformat(day), Decimal("1"))], "manual")
    db.commit()
    report = prices.coverage(db, ref(), date(2026, 8, 3), date(2026, 8, 7))
    assert report.expected == 5  # Mon-Fri; the weekend is not expected
    assert report.observed == 5
    assert report.pct == Decimal("100.0")
    assert report.ok


def test_long_gaps_are_listed_individually(db: sqlite3.Connection) -> None:
    """Runs beyond the fill tolerance are named, because past that point
    forward-filling stops being a stand-in and starts being an invention."""
    prices.store(db, ref(), [PriceObservation(date(2026, 8, 3), Decimal("1"))], "manual")
    prices.store(db, ref(), [PriceObservation(date(2026, 9, 30), Decimal("1"))], "manual")
    db.commit()
    report = prices.coverage(db, ref(), date(2026, 8, 3), date(2026, 9, 30))
    assert not report.ok
    assert len(report.gaps) == 1
    start, _end, length = report.gaps[0]
    assert start == date(2026, 8, 4)
    assert length > 5


def test_short_gaps_are_not_reported(db: sqlite3.Connection) -> None:
    """A two-day gap is within the fill tolerance and is not an anomaly."""
    for day in ("2026-08-03", "2026-08-06", "2026-08-07"):
        prices.store(db, ref(), [PriceObservation(date.fromisoformat(day), Decimal("1"))], "manual")
    db.commit()
    report = prices.coverage(db, ref(), date(2026, 8, 3), date(2026, 8, 7))
    assert report.gaps == []


def test_coverage_below_the_threshold_is_not_ok(db: sqlite3.Connection) -> None:
    prices.store(db, ref(), [PriceObservation(date(2026, 8, 3), Decimal("1"))], "manual")
    db.commit()
    report = prices.coverage(db, ref(), date(2026, 8, 3), date(2026, 8, 7))
    assert report.pct == Decimal("20.0")
    assert not report.ok


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class _BrokenProvider:
    """Stands in for TEFAS, whose endpoint currently 404s."""

    name = "tefas"

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        raise ProviderUnavailable("endpoint is gone")


def test_a_dead_provider_recovers_from_the_manual_file(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """The designed behaviour when an undocumented endpoint disappears."""
    (tmp_path / "AAA.csv").write_text("date,close\n2026-08-03,1.5\n", encoding="utf-8")
    db.execute("UPDATE instrument SET price_source = 'tefas' WHERE id = 1")
    db.commit()
    results = prices.fetch_all(
        db,
        end=date(2026, 8, 7),
        providers={"manual": ManualProvider(tmp_path), "tefas": _BrokenProvider()},
    )
    assert db.execute("SELECT count(*) n FROM price").fetchone()["n"] == 1
    assert results[0].source == "manual"
    assert results[0].error is not None and "recovered from manual" in results[0].error


def test_a_dead_provider_with_no_manual_file_reports_rather_than_raising(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """One broken endpoint must not stop the other instruments being priced."""
    db.execute("UPDATE instrument SET price_source = 'tefas' WHERE id = 1")
    db.commit()
    results = prices.fetch_all(
        db,
        end=date(2026, 8, 7),
        providers={"manual": ManualProvider(tmp_path), "tefas": _BrokenProvider()},
    )
    assert results[0].error is not None
    assert results[0].observed == 0


def test_the_window_starts_at_the_first_trade(db: sqlite3.Connection) -> None:
    assert prices.first_trade_date(db, 1) == date(2026, 8, 3)


# ---------------------------------------------------------------------------
# Instrument seeding
# ---------------------------------------------------------------------------


def test_seed_applies_only_the_keys_present(db: sqlite3.Connection) -> None:
    """A partial file must not blank a column that was already correct."""
    db.execute("UPDATE instrument SET isin = 'IE00TEST0001' WHERE id = 1")
    seeds.apply(db, {"AAA": {"price_symbol": "AAA.DE"}})
    row = db.execute("SELECT isin, price_symbol FROM instrument WHERE id = 1").fetchone()
    assert row["price_symbol"] == "AAA.DE"
    assert row["isin"] == "IE00TEST0001"


def test_seed_cannot_override_currency(tmp_path: Path) -> None:
    """Currency comes from the export's own glyph and is authoritative.

    Letting a seed change it would reintroduce exactly the "valued as though
    euro" failure the schema is built to prevent.
    """
    path = tmp_path / "s.yaml"
    path.write_text("version: 1\ninstruments:\n  AAA:\n    currency: EUR\n", encoding="utf-8")
    with pytest.raises(seeds.SeedError, match="cannot be set from a seed"):
        seeds.load(path)


def test_seed_rejects_an_unknown_field(tmp_path: Path) -> None:
    """A silently ignored typo is worse than a failure: the user believes it
    took effect."""
    path = tmp_path / "s.yaml"
    path.write_text("version: 1\ninstruments:\n  AAA:\n    prise_symbol: X\n", encoding="utf-8")
    with pytest.raises(seeds.SeedError, match="not a known instrument field"):
        seeds.load(path)


def test_seed_rejects_a_float_teilfreistellung(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        "version: 1\ninstruments:\n  AAA:\n    teilfreistellung_pct: 0.30\n", encoding="utf-8"
    )
    with pytest.raises(seeds.SeedError, match="quoted string"):
        seeds.load(path)


def test_seed_reports_instruments_it_could_not_find(db: sqlite3.Connection) -> None:
    result = seeds.apply(db, {"NOPE": {"price_symbol": "X"}})
    assert result.unknown_instruments == ["NOPE"]
    assert result.updated == []


def test_committed_example_seed_file_is_valid() -> None:
    """It is what a visitor runs first, so it must actually load."""
    entries = seeds.load(Path("seeds/instruments.example.yaml"))
    assert entries
    assert all(set(fields) <= seeds.SETTABLE for fields in entries.values())


# ---------------------------------------------------------------------------
# Fintables -- the working route to Turkish fund NAVs
# ---------------------------------------------------------------------------


FINTABLES_SAMPLE = json.dumps(
    {
        "price": 0.622452,
        "day": "2026-08-14",
        "prev_price": 0.626853,
        "market_cap": 26638859070.558926,
    }
)


def test_fintables_parses_the_dated_price() -> None:
    from portfolio_tracker.ingest.providers.market import FintablesProvider

    observations = FintablesProvider.parse(FINTABLES_SAMPLE, "TTA")
    assert len(observations) == 1
    assert observations[0].on == date(2026, 8, 14)
    assert observations[0].close == Decimal("0.622452")


def test_fintables_ignores_prev_price_because_its_date_is_unstated() -> None:
    """The payload carries a previous price but not the day it belongs to.

    Inferring it would mean guessing around Turkish public holidays, which move
    against the Gregorian calendar. A price filed under the wrong date is worse
    than a missing one.
    """
    from portfolio_tracker.ingest.providers.market import FintablesProvider

    observations = FintablesProvider.parse(FINTABLES_SAMPLE, "TTA")
    assert all(o.close != Decimal("0.626853") for o in observations)


def test_fintables_publishes_nav_only() -> None:
    from portfolio_tracker.ingest.providers.market import FintablesProvider

    assert FintablesProvider.parse(FINTABLES_SAMPLE, "TTA")[0].total_return_close is None


def test_fintables_missing_price_points_at_the_manual_path() -> None:
    from portfolio_tracker.ingest.providers.market import FintablesProvider

    with pytest.raises(ProviderUnavailable, match=r"prices_manual/TTA\.csv"):
        FintablesProvider.parse(json.dumps({"day": "2026-08-14"}), "TTA")


def test_fintables_uses_the_injected_fetcher() -> None:
    from portfolio_tracker.ingest.providers.market import FintablesProvider

    seen: list[str] = []

    def fake(url: str) -> str:
        seen.append(url)
        return FINTABLES_SAMPLE

    provider = FintablesProvider(fetcher=fake)
    observations = provider.fetch(
        ref(import_key="TTA", price_source="fintables"), date(2026, 8, 1), date(2026, 8, 31)
    )
    assert "funds/TTA/price/" in seen[0]
    assert len(observations) == 1


class _LatestOnlyProvider:
    """Stands in for a feed that serves only today's observation."""

    name = "fintables"

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        return [PriceObservation(end, Decimal("2.0"))]


def test_a_manual_file_supplements_a_working_feed(db: sqlite3.Connection, tmp_path: Path) -> None:
    """History from a file and today's figure from a feed must coexist.

    A feed that only serves the latest price cannot backfill, and a user who
    supplied a figure should not have it discarded because the feed answered.
    """
    (tmp_path / "AAA.csv").write_text(
        "date,close\n2026-08-03,1.0\n2026-08-04,1.1\n", encoding="utf-8"
    )
    db.execute("UPDATE instrument SET price_source = 'fintables' WHERE id = 1")
    db.commit()

    results = prices.fetch_all(
        db,
        end=date(2026, 8, 5),
        providers={"manual": ManualProvider(tmp_path), "fintables": _LatestOnlyProvider()},
    )
    stored = {
        date.fromisoformat(r["date"]): r["source"]
        for r in db.execute("SELECT date, source FROM price")
    }
    assert stored == {
        date(2026, 8, 3): "manual",
        date(2026, 8, 4): "manual",
        date(2026, 8, 5): "fintables",
    }
    assert results[0].source == "fintables+manual"
    assert results[0].error is None


# ---------------------------------------------------------------------------
# The TEFAS export dialect
#
# Accepted verbatim, because requiring the user to reformat a download is how
# transcription errors get introduced into a financial record.
# ---------------------------------------------------------------------------


TEFAS_EXPORT = (
    "D\u0131\u015fa Aktar\u0131m Tarihi:,15.08.2026 08:15:41\r\n"
    "Toplam Kay\u0131t Say\u0131s\u0131:,753\r\n"
    "\r\n"
    "Tarih,Fiyat\r\n"
    '14.08.2023,"0,178578"\r\n'
    '15.08.2023,"0,179386"\r\n'
    '14.08.2026,"0,622452"\r\n'
)


def test_tefas_export_preamble_is_skipped() -> None:
    observations = parse_csv(TEFAS_EXPORT)
    assert len(observations) == 3


def test_tefas_export_dates_are_day_first() -> None:
    """The broker export is month-first and this one is day-first.

    Both formats appear in this project, which is exactly why each parser is
    pinned rather than sniffing: 14.08 would be nonsense as a month.
    """
    assert parse_csv(TEFAS_EXPORT)[0].on == date(2023, 8, 14)


def test_tefas_export_uses_comma_as_the_decimal_separator() -> None:
    assert parse_csv(TEFAS_EXPORT)[0].close == Decimal("0.178578")
    assert parse_csv(TEFAS_EXPORT)[-1].close == Decimal("0.622452")


def test_dialects_are_chosen_by_header_never_guessed() -> None:
    """`0.627` and `0,627` must never be confused for one another."""
    native = parse_csv("date,close\n2026-08-14,0.627\n")
    assert native[0].close == Decimal("0.627")
    assert native[0].on == date(2026, 8, 14)


def test_a_file_with_no_recognisable_header_is_rejected() -> None:
    with pytest.raises(ProviderUnavailable, match="header row"):
        parse_csv("some,other\n1,2\n", label="x.csv")


def test_turkish_thousands_separator_is_handled() -> None:
    """A dot means thousands in this dialect, not a decimal point."""
    observations = parse_csv('Tarih,Fiyat\n14.08.2026,"1.234,56"\n')
    assert observations[0].close == Decimal("1234.56")


# ---------------------------------------------------------------------------
# Being told to slow down
# ---------------------------------------------------------------------------


class ThrottlingProvider:
    """Refuses the first `refusals` calls with RateLimited, then answers."""

    name = "yfinance"

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.calls = 0
        self.weekly_asked: list[bool] = []

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        self.calls += 1
        self.weekly_asked.append(weekly)
        if self.calls <= self.refusals:
            raise RateLimited("slow down")
        return [PriceObservation(on=date(2026, 8, 3), close=Decimal("10"))]


def test_it_waits_and_tries_again_before_giving_up(db: sqlite3.Connection) -> None:
    """A short throttle should cost a pause, not the run."""
    db.execute(
        "UPDATE instrument SET price_source = 'yfinance', "
        "price_symbol = 'AAA.DE', status = 'OWNED' WHERE id = 1"
    )
    db.commit()
    provider = ThrottlingProvider(refusals=2)
    slept: list[float] = []

    run = prices.run_fetch(
        db,
        end=date(2026, 8, 4),
        start=date(2026, 8, 1),
        providers={"yfinance": provider},
        allow_manual_fallback=False,
        sleeper=slept.append,
    )
    assert run.stopped_early is False
    assert provider.calls == 3, "two refusals, then the answer"
    # Doubling, and waiting *before* the retry rather than after the failure.
    assert slept == [prices.RATE_LIMIT_BACKOFF_SECONDS, prices.RATE_LIMIT_BACKOFF_SECONDS * 2]
    assert run.written > 0


def test_a_source_that_keeps_refusing_stops_the_run(db: sqlite3.Connection) -> None:
    """Continuing would turn a pause into a block.

    Everything fetched before the refusal stays stored -- each instrument is
    committed as it lands -- so the next run resumes from there rather than
    starting again.
    """
    db.execute(
        "UPDATE instrument SET price_source = 'yfinance', "
        "price_symbol = 'AAA.DE', status = 'OWNED' WHERE id = 1"
    )
    db.commit()
    provider = ThrottlingProvider(refusals=99)
    run = prices.run_fetch(
        db,
        end=date(2026, 8, 4),
        start=date(2026, 8, 1),
        providers={"yfinance": provider},
        allow_manual_fallback=False,
        sleeper=lambda _: None,
    )
    assert run.stopped_early is True
    assert run.reason and "slow down" in run.reason
    assert run.remaining >= 1
    assert provider.calls == prices.RATE_LIMIT_RETRIES, "it stopped asking"


def test_what_is_owned_is_fetched_daily_and_the_rest_weekly(
    db: sqlite3.Connection,
) -> None:
    """A weekly series answers a three-year-return question at a fraction of
    the bandwidth; a valuation needs every day."""
    db.execute(
        "UPDATE instrument SET price_source = 'yfinance', "
        "price_symbol = 'AAA.DE', status = 'OWNED' WHERE id = 1"
    )
    db.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, "
        "price_source, price_symbol, status) VALUES "
        "('WATCHED', 'Watched', 'ETF', 'EUR', 'yfinance', 'WATCHED.DE', 'UNIVERSE')"
    )
    db.commit()

    provider = ThrottlingProvider(refusals=0)
    prices.run_fetch(
        db,
        end=date(2026, 8, 4),
        start=date(2026, 8, 1),
        providers={"yfinance": provider},
        allow_manual_fallback=False,
        weekly_for_watched=True,
        sleeper=lambda _: None,
    )
    assert True in provider.weekly_asked, "the watched one was asked for weekly"
    assert False in provider.weekly_asked, "the owned one was not"
