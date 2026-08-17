"""Measuring one instrument, and comparing it with another.

The tests that matter here are the ones guarding against a comparison that is
confidently wrong rather than obviously broken:

``test_a_lira_fund_is_measured_in_euros``
    A fund up 90% in a currency that halved has not returned 90% to somebody
    who spends euros. Measured locally it would top every ranking for a reason
    that is not performance.

``test_a_price_only_series_is_never_silently_compared``
    A distributing fund measured on closing prices against an accumulating one
    measured on total return understates the first by roughly its yield -- in
    the direction that makes the wrong fund look better.

``test_a_guessed_category_is_not_compared_across_the_portfolio``
    The fallback cannot tell a sector ETF from a world tracker. Answering
    anyway produces a precise number about two unlike things.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_tracker.core import peers, performance
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

AS_OF = date(2026, 6, 30)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.commit()
    return conn


def _instrument(
    conn: sqlite3.Connection,
    key: str,
    *,
    currency: str = "EUR",
    bucket: str | None = None,
    ter_bps: int | None = None,
    asset_class: str = "ETF",
    status: str = "WATCH",
) -> int:
    cursor = conn.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, is_fund, "
        "price_source, risk_bucket, ter_bps, status) "
        "VALUES (?, ?, ?, ?, 1, 'manual', ?, ?, ?)",
        (key, f"{key} Fund", asset_class, currency, bucket, ter_bps, status),
    )
    return int(cursor.lastrowid or 0)


def _prices(
    conn: sqlite3.Connection,
    instrument_id: int,
    *,
    currency: str = "EUR",
    start: date,
    days: int,
    first: Decimal,
    daily_growth: Decimal,
    adjusted: bool = True,
) -> None:
    level = first
    for offset in range(days):
        on = start + timedelta(days=offset)
        conn.execute(
            "INSERT INTO price (instrument_id, date, close, total_return_close, currency, "
            "source, fetched_at) VALUES (?, ?, ?, ?, ?, 'manual', '2026-01-01T00:00:00Z')",
            (
                instrument_id,
                on.isoformat(),
                str(level),
                str(level) if adjusted else None,
                currency,
            ),
        )
        level *= daily_growth
    conn.commit()


def _fx(
    conn: sqlite3.Connection,
    quote: str,
    *,
    start: date,
    days: int,
    first: Decimal,
    daily_growth: Decimal,
) -> None:
    rate = first
    for offset in range(days):
        on = start + timedelta(days=offset)
        conn.execute(
            "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
            "VALUES (?, 'EUR', ?, ?, 'ecb', '2026-01-01T00:00:00Z')",
            (on.isoformat(), quote, str(rate)),
        )
        rate *= daily_growth
    conn.commit()


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


def test_a_metric_with_too_little_history_is_none_not_zero(db: sqlite3.Connection) -> None:
    """Zero would say the holding went nowhere. It says nothing at all."""
    key = _instrument(db, "SHORT")
    # Four months of history, running right up to the measurement date.
    _prices(
        db,
        key,
        start=AS_OF - timedelta(days=119),
        days=120,
        first=Decimal(100),
        daily_growth=Decimal("1.0005"),
    )
    m = performance.metrics(db, key, import_key="SHORT", currency="EUR", as_of=AS_OF)
    assert m.ret_1m is not None
    assert m.ret_3y_ann is None, "three years cannot be measured from four months"
    assert m.ret_5y_ann is None


def test_a_lira_fund_is_measured_in_euros(db: sqlite3.Connection) -> None:
    """The single most consequential rule in this module.

    The fund doubles in lira while the lira halves against the euro, so the
    euro investor made nothing. Measured locally it would look like the best
    instrument in the database.
    """
    start = date(2025, 7, 1)
    days = 360
    key = _instrument(db, "TFND", currency="TRY")
    # Doubles over the window.
    _prices(
        db,
        key,
        currency="TRY",
        start=start,
        days=days,
        first=Decimal(100),
        daily_growth=Decimal("1.00193"),
    )
    # Lira per euro also doubles, i.e. the lira halves.
    _fx(db, "TRY", start=start, days=days, first=Decimal(30), daily_growth=Decimal("1.00193"))

    local = performance.metrics(
        db, key, import_key="TFND", currency="TRY", as_of=date(2026, 6, 20), in_eur=False
    )
    euro = performance.metrics(
        db, key, import_key="TFND", currency="TRY", as_of=date(2026, 6, 20), in_eur=True
    )
    assert local.ret_1y is not None and local.ret_1y > Decimal("0.7")
    assert euro.ret_1y is not None
    assert abs(euro.ret_1y) < Decimal("0.02"), "the euro investor made roughly nothing"
    assert any("in euros" in c for c in euro.caveats)


def test_a_feed_with_no_adjusted_close_is_labelled_price_only(db: sqlite3.Connection) -> None:
    key = _instrument(db, "NOADJ")
    _prices(
        db,
        key,
        start=date(2025, 1, 1),
        days=400,
        first=Decimal(100),
        daily_growth=Decimal("1.0004"),
        adjusted=False,
    )
    m = performance.metrics(db, key, import_key="NOADJ", currency="EUR", as_of=AS_OF)
    assert m.basis == "PRICE"
    assert any("dividend-adjusted" in c for c in m.caveats)


def test_a_partly_adjusted_series_is_treated_as_price_only(db: sqlite3.Connection) -> None:
    """A series adjusted for half its life has a step where the two meet.

    That step would be read as a return, so the whole series drops to the
    weaker basis rather than being silently spliced.
    """
    key = _instrument(db, "MIXED")
    _prices(
        db,
        key,
        start=date(2025, 1, 1),
        days=200,
        first=Decimal(100),
        daily_growth=Decimal("1.0004"),
        adjusted=True,
    )
    _prices(
        db,
        key,
        start=date(2025, 7, 20),
        days=200,
        first=Decimal(120),
        daily_growth=Decimal("1.0004"),
        adjusted=False,
    )
    m = performance.metrics(db, key, import_key="MIXED", currency="EUR", as_of=AS_OF)
    assert m.basis == "PRICE"


# ---------------------------------------------------------------------------
# Classifying
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("vol", "band"),
    [(Decimal("0.05"), "LOW"), (Decimal("0.15"), "MID"), (Decimal("0.40"), "HIGH"), (None, None)],
)
def test_volatility_bands_use_stated_cut_points(vol: Decimal | None, band: str | None) -> None:
    """Fixed, not fitted: two scans a day apart have to stay comparable."""
    assert peers.vol_band_for(vol) == band


def test_a_seeded_category_beats_a_guessed_one(db: sqlite3.Connection) -> None:
    _instrument(db, "GUESS")
    _instrument(db, "STATED", bucket="EQ_SECTOR")
    db.commit()
    guessed = db.execute("SELECT * FROM instrument WHERE import_key = 'GUESS'").fetchone()
    stated = db.execute("SELECT * FROM instrument WHERE import_key = 'STATED'").fetchone()
    assert peers.bucket_for(guessed)[1] == "DERIVED"
    assert peers.bucket_for(stated) == ("EQ_SECTOR", "SEEDED")


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def _pair(db: sqlite3.Connection, *, candidate_adjusted: bool = True) -> None:
    start = date(2023, 1, 1)
    days = 1200
    mine = _instrument(db, "MINE", bucket="EQ_GLOBAL_BROAD", ter_bps=25, status="OWNED")
    theirs = _instrument(db, "THEIRS", bucket="EQ_GLOBAL_BROAD", ter_bps=10)
    _prices(db, mine, start=start, days=days, first=Decimal(100), daily_growth=Decimal("1.0002"))
    _prices(
        db,
        theirs,
        start=start,
        days=days,
        first=Decimal(100),
        daily_growth=Decimal("1.0004"),
        adjusted=candidate_adjusted,
    )


def test_a_better_performing_peer_is_found_and_the_difference_stated(
    db: sqlite3.Connection,
) -> None:
    """The sentence this whole feature exists to produce."""
    _pair(db)
    scan = peers.scan(db, instrument="MINE", as_of=AS_OF)

    assert scan.bucket == "EQ_GLOBAL_BROAD"
    assert [c.import_key for c in scan.candidates] == ["THEIRS"]
    found = scan.candidates[0]
    assert found.ret_3y_ann is not None and scan.subject.ret_3y_ann is not None
    assert found.ret_3y_ann > scan.subject.ret_3y_ann
    assert found.ret_3y_ann_delta is not None and found.ret_3y_ann_delta > 0
    assert found.ter_bps_delta == -15, "cheaper by 15 basis points"
    assert found.comparable is True


def test_a_price_only_series_is_never_silently_compared(db: sqlite3.Connection) -> None:
    """The highest-severity trap in the feature.

    Price-only against total-return understates the price-only side by roughly
    its yield -- so it is flagged, ranked last, and says why.
    """
    _pair(db, candidate_adjusted=False)
    scan = peers.scan(db, instrument="MINE", as_of=AS_OF)
    found = scan.candidates[0]
    assert found.comparable is False
    assert any("not directly comparable" in c.lower() for c in found.caveats)


def test_a_different_category_is_not_offered_as_an_alternative(db: sqlite3.Connection) -> None:
    _pair(db)
    db.execute("UPDATE instrument SET risk_bucket = 'EQ_SECTOR' WHERE import_key = 'THEIRS'")
    db.commit()
    assert peers.scan(db, instrument="MINE", as_of=AS_OF).candidates == []


def test_an_instrument_with_too_little_history_is_not_offered(db: sqlite3.Connection) -> None:
    _pair(db)
    db.execute(
        "DELETE FROM price WHERE instrument_id = (SELECT id FROM instrument "
        "WHERE import_key = 'THEIRS') AND date < '2026-06-01'"
    )
    db.commit()
    assert peers.scan(db, instrument="MINE", as_of=AS_OF).candidates == []


def test_overlap_is_unknown_rather_than_zero_without_holdings_files(
    db: sqlite3.Connection,
) -> None:
    """ "They share nothing" and "nobody told me" are opposite conclusions."""
    _pair(db)
    scan = peers.scan(db, instrument="MINE", as_of=AS_OF)
    assert scan.candidates[0].overlap_pct is None
    assert any("Overlap is unknown" in c for c in scan.caveats)


def test_a_guessed_category_is_not_compared_across_the_portfolio(
    db: sqlite3.Connection,
) -> None:
    """Listed as needing a category rather than answered with a wrong one."""
    _pair(db)
    db.execute("UPDATE instrument SET risk_bucket = NULL WHERE import_key = 'MINE'")
    db.execute(
        "INSERT INTO position_daily (date, account_id, instrument_id, quantity, currency, "
        "price_local, price_asof, fx_rate, fx_asof, market_value_local, market_value_eur, "
        "cost_basis_local, cost_basis_eur, cost_basis_eur_at_spot, unrealized_eur, "
        "unrealized_local_eur, unrealized_fx_eur) "
        "SELECT ?, 1, id, '10', 'EUR', '100', ?, '1', ?, '1000', '1000', '900', '900', "
        "'900', '100', '100', '0' FROM instrument WHERE import_key = 'MINE'",
        (AS_OF.isoformat(), AS_OF.isoformat(), AS_OF.isoformat()),
    )
    db.commit()

    result = peers.scan_portfolio(db, as_of=AS_OF)
    assert result.scans == []
    assert result.needs_category == ["MINE"]
