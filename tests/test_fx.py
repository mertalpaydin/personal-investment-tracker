"""Calendars, conversion, and rate ingestion.

No test here touches the network: fetching and parsing are separate functions,
so the parser is exercised against a captured response.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_tracker.core import calendars as cal
from portfolio_tracker.core import fx
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.ingest import fx as ingest

# A real ECB csvdata response, trimmed to a handful of rows. Column order is
# preserved from the wire because the parser must select by name, not position.
ECB_SAMPLE = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "EXR.D.TRY.EUR.SP00.A,D,TRY,EUR,SP00,A,2026-08-03,54.8333,A\n"
    "EXR.D.TRY.EUR.SP00.A,D,TRY,EUR,SP00,A,2026-08-04,54.7606,A\n"
    "EXR.D.TRY.EUR.SP00.A,D,TRY,EUR,SP00,A,2026-08-05,54.9691,A\n"
    "EXR.D.TRY.EUR.SP00.A,D,TRY,EUR,SP00,A,2026-08-06,,A\n"  # holiday: no value
)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    return conn


@pytest.fixture
def with_rates(db: sqlite3.Connection) -> sqlite3.Connection:
    ingest.store(db, ingest.parse_ecb_csv(ECB_SAMPLE, "TRY"))
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5))],
)
def test_easter_is_computed_not_tabulated(year: int, expected: date) -> None:
    """Good Friday and Easter Monday are TARGET2 holidays and move every year."""
    assert cal.easter_sunday(year) == expected


@pytest.mark.parametrize(
    "holiday",
    [
        date(2026, 1, 1),  # New Year
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 6),  # Easter Monday
        date(2026, 5, 1),  # Labour Day
        date(2026, 12, 25),
        date(2026, 12, 26),
    ],
)
def test_target2_holidays_are_closed(holiday: date) -> None:
    assert not cal.is_business_day(holiday, cal.TARGET2)


def test_weekends_are_closed() -> None:
    assert not cal.is_business_day(date(2026, 8, 15))  # Saturday
    assert not cal.is_business_day(date(2026, 8, 16))  # Sunday
    assert cal.is_business_day(date(2026, 8, 14))  # Friday


def test_xetra_closes_two_days_target2_does_not() -> None:
    """A real divergence: euro settlement runs on 24 and 31 December."""
    for day in (date(2026, 12, 24), date(2026, 12, 31)):
        assert cal.is_business_day(day, cal.TARGET2)
        assert not cal.is_business_day(day, cal.XETRA)


def test_grid_excludes_weekends_and_holidays() -> None:
    days = cal.grid_days(date(2026, 12, 24), date(2026, 12, 31))
    assert date(2026, 12, 25) not in days
    assert date(2026, 12, 26) not in days
    assert date(2026, 12, 24) in days


def test_staleness_is_counted_in_business_days_not_calendar_days() -> None:
    """Over a weekend, three calendar days are only one business day."""
    friday, monday = date(2026, 8, 14), date(2026, 8, 17)
    assert (monday - friday).days == 3
    assert cal.business_days_between(friday, monday) == 1


# ---------------------------------------------------------------------------
# The euro short-circuit
# ---------------------------------------------------------------------------


class _RefusingConnection:
    """Any query at all is a failure."""

    def execute(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError("get_rate('EUR') must not touch the database")


def test_euro_short_circuits_without_touching_the_database() -> None:
    """Not an optimisation.

    It is what guarantees a euro-only portfolio reports *exactly* zero currency
    contribution rather than a rounding artefact from dividing by a stored 1.0.
    """
    rate = fx.get_rate(_RefusingConnection(), "EUR", date(2026, 8, 14))  # type: ignore[arg-type]
    assert rate.rate == Decimal(1)
    assert rate.is_identity
    assert rate.stale_days == 0


def test_euro_series_also_short_circuits() -> None:
    days = cal.grid_days(date(2026, 8, 10), date(2026, 8, 14))
    series = fx.get_rate_series(_RefusingConnection(), "EUR", days)  # type: ignore[arg-type]
    assert set(series) == set(days)
    assert all(r.rate == Decimal(1) for r in series.values())


def test_converting_euro_at_the_identity_rate_is_exact() -> None:
    amount = Decimal("10697.10")
    assert fx.to_eur(amount, Decimal(1)) == amount


# ---------------------------------------------------------------------------
# Lookup and forward-fill
# ---------------------------------------------------------------------------


def test_exact_date_lookup(with_rates: sqlite3.Connection) -> None:
    rate = fx.get_rate(with_rates, "TRY", date(2026, 8, 4))
    assert rate.rate == Decimal("54.7606")
    assert rate.stale_days == 0
    assert rate.source == "ecb"


def test_forward_fill_carries_the_previous_observation(with_rates: sqlite3.Connection) -> None:
    rate = fx.get_rate(with_rates, "TRY", date(2026, 8, 6))
    assert rate.asof == date(2026, 8, 5)
    assert rate.stale_days == 1


def test_a_rate_beyond_tolerance_raises_rather_than_defaulting(
    with_rates: sqlite3.Connection,
) -> None:
    """Silently reusing a weeks-old rate for a fast-moving currency produces a
    materially wrong number, not a rounding error."""
    with pytest.raises(fx.FxUnavailable, match="stale"):
        fx.get_rate(with_rates, "TRY", date(2026, 9, 30))


def test_non_strict_mode_returns_the_stale_rate_but_says_so(
    with_rates: sqlite3.Connection,
) -> None:
    rate = fx.get_rate(with_rates, "TRY", date(2026, 9, 30), strict=False)
    assert rate.stale_days > fx.DEFAULT_MAX_STALE_DAYS


def test_a_date_before_any_observation_raises(with_rates: sqlite3.Connection) -> None:
    with pytest.raises(fx.FxUnavailable, match="no EUR/TRY rate"):
        fx.get_rate(with_rates, "TRY", date(2020, 1, 1))


def test_series_matches_pointwise_lookup(with_rates: sqlite3.Connection) -> None:
    days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]
    series = fx.get_rate_series(with_rates, "TRY", days)
    for day in days:
        assert series[day].rate == fx.get_rate(with_rates, "TRY", day).rate


# ---------------------------------------------------------------------------
# Conversion direction
# ---------------------------------------------------------------------------


def test_conversion_is_division_and_a_weak_currency_shrinks() -> None:
    """The rate is quote-per-euro, so a large rate means a weak currency."""
    assert fx.to_eur(Decimal("55250"), Decimal("55.25")) == Decimal(1000)


def test_a_non_positive_rate_is_rejected() -> None:
    for bad in (Decimal(0), Decimal(-1)):
        with pytest.raises(ValueError, match="must be positive"):
            fx.to_eur(Decimal(100), bad)


def test_only_modules_permitted_to_convert_contain_fx_arithmetic() -> None:
    """ADR 0004 rule 3, enforced rather than trusted.

    If this fails, a module that should be currency-agnostic has grown its own
    conversion, and the single-direction guarantee no longer holds.
    """
    root = Path(__file__).resolve().parent.parent / "src" / "portfolio_tracker"
    allowed = {"core/fx.py", "ingest/fx.py"}
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "to_eur(" in text and "def to_eur" not in text:
            offenders.append(rel)
    assert offenders == [], f"unexpected FX conversion in: {offenders}"


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def test_ecb_parser_selects_columns_by_name() -> None:
    observations = ingest.parse_ecb_csv(ECB_SAMPLE, "TRY")
    assert [o.on for o in observations] == [
        date(2026, 8, 3),
        date(2026, 8, 4),
        date(2026, 8, 5),
    ]
    assert observations[0].rate == Decimal("54.8333")


def test_rows_without_a_value_are_skipped_not_zeroed() -> None:
    """An empty OBS_VALUE is a holiday. Storing zero would be catastrophic."""
    observations = ingest.parse_ecb_csv(ECB_SAMPLE, "TRY")
    assert date(2026, 8, 6) not in {o.on for o in observations}
    assert all(o.rate > 0 for o in observations)


def test_an_unexpected_response_shape_raises() -> None:
    with pytest.raises(ingest.FxIngestError, match="TIME_PERIOD"):
        ingest.parse_ecb_csv("something,else\n1,2\n", "TRY")


def test_fetch_uses_the_injected_fetcher() -> None:
    seen: list[str] = []

    def fake(url: str) -> str:
        seen.append(url)
        return ECB_SAMPLE

    observations = ingest.fetch_ecb("TRY", date(2024, 7, 1), fetcher=fake)
    assert len(observations) == 3
    assert "D.TRY.EUR.SP00.A" in seen[0]
    assert "startPeriod=2024-07-01" in seen[0]


def test_store_is_idempotent(db: sqlite3.Connection) -> None:
    observations = ingest.parse_ecb_csv(ECB_SAMPLE, "TRY")
    ingest.store(db, observations)
    ingest.store(db, observations)
    assert db.execute("SELECT count(*) n FROM fx_rate").fetchone()["n"] == 3


def test_a_better_source_wins_regardless_of_arrival_order(db: sqlite3.Connection) -> None:
    """A manually supplied rate outranks the feed, because someone chose it."""
    ingest.store(db, ingest.parse_ecb_csv(ECB_SAMPLE, "TRY"))
    ingest.store(db, [ingest.Observation(date(2026, 8, 3), "TRY", Decimal("99.9999"), "manual")])
    row = db.execute("SELECT rate, source FROM fx_rate WHERE date = '2026-08-03'").fetchone()
    assert row["source"] == "manual"
    assert row["rate"] == Decimal("99.9999")

    # And the reverse does not happen.
    ingest.store(db, [ingest.Observation(date(2026, 8, 3), "TRY", Decimal("1"), "yfinance")])
    assert (
        db.execute("SELECT source FROM fx_rate WHERE date = '2026-08-03'").fetchone()["source"]
        == "manual"
    )


def test_manual_csv_fallback(tmp_path: Path) -> None:
    (tmp_path / "TRY.csv").write_text("date,rate\n2026-08-03,54.83\n", encoding="utf-8")
    observations = ingest.load_manual("TRY", tmp_path)
    assert observations[0].rate == Decimal("54.83")


def test_missing_manual_file_is_not_an_error(tmp_path: Path) -> None:
    assert ingest.load_manual("XXX", tmp_path) == []


def test_manual_file_needs_the_right_columns() -> None:
    with pytest.raises(ingest.FxIngestError, match="needs 'date' and 'rate'"):
        ingest.parse_manual_csv("day,value\n2026-01-01,1\n", "TRY")


def test_cross_source_divergence_is_detected() -> None:
    """The one failure a single source cannot self-report: a convention change."""
    primary = ingest.parse_ecb_csv(ECB_SAMPLE, "TRY")
    agreeing = [
        ingest.Observation(o.on, "TRY", o.rate * Decimal("1.0005"), "yfinance") for o in primary
    ]
    assert ingest.compare(primary, agreeing) == []

    inverted = [ingest.Observation(o.on, "TRY", Decimal(1) / o.rate, "yfinance") for o in primary]
    assert len(ingest.compare(primary, inverted)) == len(primary)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_coverage_reports_missing_grid_days(with_rates: sqlite3.Connection) -> None:
    gaps = fx.coverage_report(with_rates, date(2026, 8, 3), date(2026, 8, 7), quotes={"TRY"})
    gap = gaps[0]
    # 3rd to 7th August 2026 is Mon-Fri; three observed, so two are missing.
    assert not gap.is_complete
    assert gap.missing == [date(2026, 8, 6), date(2026, 8, 7)]


def test_coverage_is_complete_when_every_grid_day_is_observed(
    with_rates: sqlite3.Connection,
) -> None:
    gaps = fx.coverage_report(with_rates, date(2026, 8, 3), date(2026, 8, 5), quotes={"TRY"})
    assert gaps[0].is_complete


def test_required_quotes_ignores_euro(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    db.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'A', 'DEPOT', 'EUR')"
    )
    db.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency) "
        "VALUES ('X', 'X', 'ETF', 'EUR')"
    )
    db.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency) "
        "VALUES ('Y', 'Y', 'FUND', 'TRY')"
    )
    assert fx.required_quotes(db) == {"TRY"}


# ---------------------------------------------------------------------------
# The Turkish trading calendar
#
# Derived from three years of observed fund NAVs rather than copied from a
# reference: every weekday with no published price was checked, and after
# removing the fixed national holidays exactly these dates remained.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "holiday",
    [
        date(2026, 1, 1),  # Yilbasi
        date(2026, 4, 23),  # Ulusal Egemenlik
        date(2026, 5, 1),  # Emek ve Dayanisma
        date(2026, 5, 19),  # Genclik ve Spor
        date(2026, 7, 15),  # Demokrasi
        date(2024, 8, 30),  # Zafer -- a Friday, so the weekday check is real
        date(2026, 10, 29),  # Cumhuriyet
    ],
)
def test_turkish_fixed_holidays_are_closed(holiday: date) -> None:
    assert not cal.is_business_day(holiday, cal.TEFAS)
    # TARGET2 does not observe them, which is why it is the better grid spine.
    # TARGET2 observes only 1 January and 1 May of these.
    if (holiday.month, holiday.day) not in ((1, 1), (5, 1)):
        assert cal.is_business_day(holiday, cal.TARGET2)


@pytest.mark.parametrize(
    "holiday",
    [date(2025, 3, 31), date(2025, 6, 6), date(2026, 3, 20), date(2026, 5, 27)],
)
def test_turkish_religious_holidays_are_closed(holiday: date) -> None:
    """These move by ~11 days a year, so they cannot be computed from a rule."""
    assert not cal.is_business_day(holiday, cal.TEFAS)


def test_an_ordinary_turkish_weekday_is_open() -> None:
    assert cal.is_business_day(date(2026, 8, 14), cal.TEFAS)


def test_the_religious_holiday_table_declares_how_far_it_reaches() -> None:
    """It will go stale, so the limit is explicit rather than implied."""
    assert cal.TURKEY_HOLIDAYS_KNOWN_THROUGH >= 2026
    assert max(d.year for d in cal.TURKEY_RELIGIOUS_HOLIDAYS) == (cal.TURKEY_HOLIDAYS_KNOWN_THROUGH)
