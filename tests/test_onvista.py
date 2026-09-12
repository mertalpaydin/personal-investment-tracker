"""onvista: search/snapshot/history parsing, notation caching, and reprice.

No test reaches the network -- the provider takes an injected fetcher, exactly
like Fintables and TEFAS, and the parsers run against fixture payloads captured
inline.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.ingest import prices
from portfolio_tracker.ingest.onvista_cache import NotationCache, OnvistaNotation
from portfolio_tracker.ingest.providers.base import InstrumentRef, PriceObservation
from portfolio_tracker.ingest.providers.market import OnvistaProvider


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
        "price_source, price_symbol, trading_calendar, isin) "
        "VALUES (1, 'AAA', 'Fund A', 'ETF', 'EUR', 'onvista', 'AAA.DE', 'XETRA', 'LU1812092168')"
    )
    conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, flow_class) VALUES (1, 1, 'BUY', '2026-08-03', 'EUR', '10', 'INVESTMENT')"
    )
    conn.commit()
    return conn


def ref(**kwargs: object) -> InstrumentRef:
    base: dict[str, object] = {
        "instrument_id": 1,
        "import_key": "AAA",
        "currency": "EUR",
        "price_source": "onvista",
        "price_symbol": "AAA.DE",
        "trading_calendar": "XETRA",
        "isin": "LU1812092168",
    }
    base.update(kwargs)
    return InstrumentRef(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

SEARCH_PAYLOAD = json.dumps(
    {
        "list": [
            {"entityType": "STOCK", "entityValue": "82849", "isin": "US0000000000", "name": "X"},
            {
                "entityType": "FUND",
                "entityValue": "142129752",
                "isin": "LU1812092168",
                "name": "Fund A",
            },
        ]
    }
)


def test_search_matches_on_isin_when_present() -> None:
    entity_type, entity_id = OnvistaProvider.parse_search(SEARCH_PAYLOAD, "AAA", "LU1812092168")
    assert (entity_type, entity_id) == ("fund", "142129752")


def test_search_falls_back_to_the_first_fund_or_stock_hit_with_no_isin() -> None:
    """Owned ETFs with no ISIN on file (EGV2, FLXD, ...) resolve this way."""
    entity_type, entity_id = OnvistaProvider.parse_search(SEARCH_PAYLOAD, "AAA", None)
    assert (entity_type, entity_id) == ("stock", "82849")


def test_search_with_no_match_names_the_query() -> None:
    from portfolio_tracker.ingest.providers.base import ProviderUnavailable

    with pytest.raises(ProviderUnavailable, match="AAA"):
        OnvistaProvider.parse_search(json.dumps({"list": []}), "AAA", None)


# ---------------------------------------------------------------------------
# Snapshot: notation preference and the currency check
# ---------------------------------------------------------------------------


def _snapshot(*quotes: dict[str, object]) -> str:
    return json.dumps({"quoteList": {"list": list(quotes)}})


def test_snapshot_prefers_xetra_over_tradegate() -> None:
    payload = _snapshot(
        {
            "isoCurrency": "EUR",
            "market": {"codeExchange": "BSX", "name": "Tradegate BSX", "idNotation": "1"},
        },
        {
            "isoCurrency": "EUR",
            "market": {"codeExchange": "GER", "name": "Xetra", "idNotation": "2"},
        },
    )
    notation = OnvistaProvider.parse_snapshot(payload, "AAA", "EUR")
    assert notation.id_notation == "2"
    assert notation.market == "Xetra"


def test_snapshot_falls_back_to_gettex_then_stuttgart_by_name() -> None:
    payload = _snapshot(
        {
            "isoCurrency": "EUR",
            "market": {"codeExchange": None, "name": "Boerse Stuttgart", "idNotation": "9"},
        }
    )
    notation = OnvistaProvider.parse_snapshot(payload, "AAA", "EUR")
    assert notation.id_notation == "9"


def test_snapshot_rejects_a_currency_mismatch() -> None:
    """Load-bearing, not decorative: a wrong-currency price misvalues the position."""
    from portfolio_tracker.ingest.providers.base import ProviderUnavailable

    payload = _snapshot(
        {
            "isoCurrency": "USD",
            "market": {"codeExchange": "GER", "name": "Xetra", "idNotation": "2"},
        }
    )
    with pytest.raises(ProviderUnavailable, match="currency"):
        OnvistaProvider.parse_snapshot(payload, "AAA", "EUR")


def test_snapshot_with_no_recognised_market_is_refused() -> None:
    from portfolio_tracker.ingest.providers.base import ProviderUnavailable

    payload = _snapshot(
        {
            "isoCurrency": "EUR",
            "market": {"codeExchange": "XYZ", "name": "Somewhere", "idNotation": "1"},
        }
    )
    with pytest.raises(ProviderUnavailable, match="notation"):
        OnvistaProvider.parse_snapshot(payload, "AAA", "EUR")


# ---------------------------------------------------------------------------
# EOD history
# ---------------------------------------------------------------------------


def _history_payload() -> str:
    return json.dumps(
        {
            "datetimeLast": [1723075200, 1723161600, 1723248000],  # 2024-08-08/09/10 UTC
            "last": [10.5, 10.6, 10.7],
            "volume": [100, 200, None],
        }
    )


def test_history_parses_dates_and_closes() -> None:
    observations = OnvistaProvider.parse_history(_history_payload(), "AAA")
    assert len(observations) == 3
    assert observations[0].close == Decimal("10.5")
    assert observations[0].on == date(2024, 8, 8)
    assert observations[0].total_return_close is None, "onvista gives no adjusted close"
    assert observations[0].volume == Decimal("100")
    assert observations[2].volume is None


def test_history_with_no_rows_is_refused() -> None:
    from portfolio_tracker.ingest.providers.base import ProviderUnavailable

    with pytest.raises(ProviderUnavailable, match="no rows"):
        OnvistaProvider.parse_history(json.dumps({"datetimeLast": [], "last": []}), "AAA")


# ---------------------------------------------------------------------------
# Weekly sampling
# ---------------------------------------------------------------------------


def test_weekly_sampling_keeps_the_last_trading_day_of_each_week() -> None:
    def fetcher(url: str) -> str:
        if "query" in url:
            return SEARCH_PAYLOAD
        if "snapshot" in url:
            return _snapshot(
                {
                    "isoCurrency": "EUR",
                    "market": {"codeExchange": "GER", "name": "Xetra", "idNotation": "2"},
                }
            )
        return json.dumps(
            {
                "datetimeLast": [
                    1723075200,  # Thu 2024-08-08
                    1723161600,  # Fri 2024-08-09 (last of that ISO week)
                    1723420800,  # Mon 2024-08-12
                    1723507200,  # Tue 2024-08-13 (last requested)
                ],
                "last": [1, 2, 3, 4],
            }
        )

    provider = OnvistaProvider(fetcher=fetcher)
    observations = provider.fetch(ref(), date(2024, 8, 8), date(2024, 8, 13), weekly=True)
    assert [o.on for o in observations] == [date(2024, 8, 9), date(2024, 8, 13)]


# ---------------------------------------------------------------------------
# Notation cache: a hit skips search and snapshot entirely
# ---------------------------------------------------------------------------


def test_cache_hit_skips_search_and_snapshot(db: sqlite3.Connection) -> None:
    cache = NotationCache(db)
    cache.put(
        1,
        OnvistaNotation(
            entity_type="fund",
            entity_id="142129752",
            id_notation="2",
            market="Xetra",
            currency="EUR",
        ),
    )

    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        assert "eod_history" in url, "search/snapshot must be skipped on a cache hit"
        return _history_payload()

    provider = OnvistaProvider(fetcher=fetcher, notation_cache=cache)
    observations = provider.fetch(ref(), date(2024, 8, 8), date(2024, 8, 10))
    assert len(observations) == 3
    assert len(calls) == 1


def test_a_resolved_notation_is_written_to_the_cache(db: sqlite3.Connection) -> None:
    def fetcher(url: str) -> str:
        if "query" in url:
            return SEARCH_PAYLOAD
        if "snapshot" in url:
            return _snapshot(
                {
                    "isoCurrency": "EUR",
                    "market": {"codeExchange": "GER", "name": "Xetra", "idNotation": "2"},
                }
            )
        return _history_payload()

    cache = NotationCache(db)
    provider = OnvistaProvider(fetcher=fetcher, notation_cache=cache)
    provider.fetch(ref(), date(2024, 8, 8), date(2024, 8, 10))

    cached = cache.get(1)
    assert cached is not None
    assert cached.id_notation == "2"
    assert cached.entity_type == "fund"


# ---------------------------------------------------------------------------
# run_fetch fallback: onvista -> yfinance
# ---------------------------------------------------------------------------


class _BrokenOnvista:
    name = "onvista"

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        from portfolio_tracker.ingest.providers.base import ProviderUnavailable

        raise ProviderUnavailable("onvista snapshot has no quotes")


class _WorkingYFinance:
    name = "yfinance"

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        return [PriceObservation(date(2026, 8, 3), Decimal("10"))]


def test_run_fetch_falls_back_from_onvista_to_yfinance(db: sqlite3.Connection) -> None:
    run = prices.run_fetch(
        db,
        end=date(2026, 8, 4),
        start=date(2026, 8, 1),
        providers={"onvista": _BrokenOnvista(), "yfinance": _WorkingYFinance()},
        allow_manual_fallback=False,
    )
    assert run.coverage[0].source == "yfinance"
    assert run.coverage[0].error is None
    assert run.coverage[0].note and "onvista failed" in run.coverage[0].note
    assert run.coverage[0].note and "used yfinance" in run.coverage[0].note


class _BrokenYFinance:
    name = "yfinance"

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        from portfolio_tracker.ingest.providers.base import ProviderUnavailable

        raise ProviderUnavailable("Yahoo has nothing either")


def test_run_fetch_reports_the_error_when_the_fallback_also_fails(db: sqlite3.Connection) -> None:
    run = prices.run_fetch(
        db,
        end=date(2026, 8, 4),
        start=date(2026, 8, 1),
        providers={"onvista": _BrokenOnvista(), "yfinance": _BrokenYFinance()},
        allow_manual_fallback=False,
    )
    assert run.coverage[0].error is not None
    assert "fallback also failed" in run.coverage[0].error


# ---------------------------------------------------------------------------
# Migration 0025
# ---------------------------------------------------------------------------


def test_migration_flips_only_de_etf_and_stock_yfinance_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    migrations = M.discover()
    pre_0025 = [m for m in migrations if m.version < 25]
    for migration in pre_0025:
        for statement in M.split_statements(migration.sql):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (migration.version, migration.name, migration.checksum),
        )
    conn.commit()

    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'D', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, "
        "price_source, price_symbol) VALUES "
        "('DEETF', 'A German ETF', 'ETF', 'EUR', 'yfinance', 'DEETF.DE')"
    )
    conn.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, "
        "price_source, price_symbol) VALUES "
        "('USSTOCK', 'A US stock', 'STOCK', 'USD', 'yfinance', 'USSTOCK')"
    )
    conn.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, "
        "price_source, price_symbol) VALUES "
        "('MANUALDE', 'Manual DE fund', 'FUND', 'EUR', 'manual', 'MANUALDE.DE')"
    )
    conn.commit()

    M.migrate(conn)

    rows = {
        r["import_key"]: r["price_source"]
        for r in conn.execute("SELECT import_key, price_source FROM instrument")
    }
    assert rows["DEETF"] == "onvista"
    assert rows["USSTOCK"] == "yfinance", "not a .DE symbol"
    assert rows["MANUALDE"] == "manual", "already on manual, untouched"

    # The table rebuild carried the CHECK constraint, the indexes and the
    # currency trigger, and added the notation cache table.
    conn.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "SELECT id, '2026-08-01', 1.0, 'EUR', 'onvista', datetime('now') FROM instrument "
        "WHERE import_key = 'DEETF'"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
            "SELECT id, '2026-08-02', 1.0, 'USD', 'onvista', datetime('now') FROM instrument "
            "WHERE import_key = 'DEETF'"
        )
    assert conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='onvista_notation'"
    ).fetchone()


def test_migration_applies_cleanly_on_a_fresh_db() -> None:
    conn = connect(":memory:")
    applied = M.migrate(conn)
    assert 25 in applied
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='instrument'"
    ).fetchone()
    assert "'onvista'" in row["sql"]


# ---------------------------------------------------------------------------
# reprice: dry-run classification
# ---------------------------------------------------------------------------


class _FakeOnvistaHistory:
    name = "onvista"

    def __init__(self, series: dict[str, list[PriceObservation]]) -> None:
        self.series = series

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        return self.series.get(instrument.import_key, [])


def _reprice_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'D', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, "
        "price_source, price_symbol, trading_calendar) VALUES "
        "(1, 'MATCH1', 'Matches', 'ETF', 'EUR', 'onvista', 'MATCH1.DE', 'XETRA')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, "
        "price_source, price_symbol, trading_calendar) VALUES "
        "(2, 'WRONG1', 'Wrong prices', 'ETF', 'EUR', 'onvista', 'WRONG1.DE', 'XETRA')"
    )
    for iid, _key in ((1, "MATCH1"), (2, "WRONG1")):
        conn.execute(
            "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
            "quantity, flow_class) VALUES (1, ?, 'BUY', '2026-08-03', 'EUR', '10', 'INVESTMENT')",
            (iid,),
        )
    conn.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (1, '2026-08-03', 10.0, 'EUR', 'yfinance', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (2, '2026-08-03', 100.0, 'EUR', 'yfinance', datetime('now'))"
    )
    conn.commit()
    return conn


def test_reprice_classifies_match_and_replace(tmp_path: Path) -> None:
    conn = _reprice_db(tmp_path)
    fake = _FakeOnvistaHistory(
        {
            "MATCH1": [PriceObservation(date(2026, 8, 3), Decimal("10.0"))],
            # 14.4%-ish gap, well over the default 0.5% tolerance.
            "WRONG1": [PriceObservation(date(2026, 8, 3), Decimal("87.0"))],
        }
    )
    rows, stopped_early = prices.reprice(conn, providers={"onvista": fake}, only_mismatched=False)
    assert not stopped_early
    by_key = {r.import_key: r for r in rows}
    assert by_key["MATCH1"].verdict == "MATCH"
    assert by_key["WRONG1"].verdict == "REPLACE"
    assert by_key["WRONG1"].max_rel_diff is not None
    assert by_key["WRONG1"].max_rel_diff > Decimal("0.1")


def test_reprice_only_mismatched_hides_matches_by_default(tmp_path: Path) -> None:
    conn = _reprice_db(tmp_path)
    fake = _FakeOnvistaHistory(
        {
            "MATCH1": [PriceObservation(date(2026, 8, 3), Decimal("10.0"))],
            "WRONG1": [PriceObservation(date(2026, 8, 3), Decimal("87.0"))],
        }
    )
    rows, _ = prices.reprice(conn, providers={"onvista": fake})
    assert [r.import_key for r in rows] == ["WRONG1"]


def test_reprice_apply_replaces_yfinance_rows_only(tmp_path: Path) -> None:
    conn = _reprice_db(tmp_path)
    conn.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (2, '2026-08-04', 101.0, 'EUR', 'manual', datetime('now'))"
    )
    conn.commit()
    fake = _FakeOnvistaHistory(
        {
            "MATCH1": [PriceObservation(date(2026, 8, 3), Decimal("10.0"))],
            "WRONG1": [PriceObservation(date(2026, 8, 3), Decimal("87.0"))],
        }
    )
    rows, _ = prices.reprice(conn, providers={"onvista": fake}, apply=True)
    assert rows[0].verdict == "REPLACE"

    stored = {
        (r["date"], r["source"]): r["close"]
        for r in conn.execute("SELECT date, close, source FROM price WHERE instrument_id = 2")
    }
    assert (str(date(2026, 8, 3)), "onvista") in stored
    assert (str(date(2026, 8, 3)), "yfinance") not in stored, "the wrong yfinance row is gone"
    assert (str(date(2026, 8, 4)), "manual") in stored, "a manual row is never deleted"


def test_reprice_unresolved_when_the_provider_is_unavailable(tmp_path: Path) -> None:
    conn = _reprice_db(tmp_path)

    class _AlwaysBroken:
        name = "onvista"

        def supports(self, instrument: InstrumentRef) -> bool:
            return True

        def fetch(
            self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
        ) -> list[PriceObservation]:
            from portfolio_tracker.ingest.providers.base import ProviderUnavailable

            raise ProviderUnavailable("no notation found")

    rows, stopped_early = prices.reprice(
        conn, providers={"onvista": _AlwaysBroken()}, only_mismatched=False
    )
    assert not stopped_early
    assert all(r.verdict == "UNRESOLVED" for r in rows)
    assert all(r.reason and "no notation found" in r.reason for r in rows)


def test_history_is_paged_forward_from_start_until_end_is_covered() -> None:
    """``range`` alone returns one month; the window is [startDate, startDate + range].

    A five-year request must therefore page: one Y5 window from ``start``, then a
    second window for the days after it, with duplicates dropped.
    """
    from datetime import UTC, date, datetime

    from portfolio_tracker.ingest.onvista_cache import OnvistaNotation
    from portfolio_tracker.ingest.providers.base import InstrumentRef
    from portfolio_tracker.ingest.providers.market import OnvistaProvider

    seen: list[str] = []

    def fetcher(url: str) -> str:
        seen.append(url)
        params = dict(p.split("=") for p in url.split("?")[1].split("&"))
        start = date.fromisoformat(params["startDate"])
        days = {"M1": 31, "Y1": 365, "Y5": 5 * 365}[params["range"]]
        stamps = [
            int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp()) + 86400 * i
            for i in range(0, days, 7)
        ]
        return json.dumps({"datetimeLast": stamps, "last": [1.0] * len(stamps)})

    class Cache:
        def get(self, instrument_id: int) -> OnvistaNotation:
            return OnvistaNotation("FUND", "1", "2", "Xetra", "EUR")

        def put(self, instrument_id: int, notation: OnvistaNotation) -> None: ...

    provider = OnvistaProvider(fetcher=fetcher, notation_cache=Cache())  # type: ignore[arg-type]
    ref = InstrumentRef(1, "X", "EUR", "onvista", "X.DE", "XETRA")
    rows = provider.fetch(ref, date(2021, 1, 1), date(2026, 6, 30))

    assert len(seen) == 2
    assert "range=Y5" in seen[0] and "startDate=2021-01-01" in seen[0]
    assert "startDate=2026-01-01" in seen[1] or "startDate=2025-12-31" in seen[1]
    assert rows[0].on >= date(2021, 1, 1) and rows[-1].on <= date(2026, 6, 30)
    assert len({r.on for r in rows}) == len(rows)
