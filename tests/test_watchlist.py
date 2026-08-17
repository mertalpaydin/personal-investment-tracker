"""Watching an instrument you do not own.

The properties worth pinning down are about identity and about not losing
data:

``test_the_key_is_the_bare_symbol_so_a_later_purchase_adopts_it``
    The importer matches on the bare symbol. If a watchlist entry were keyed
    on the Yahoo ticker, buying the thing would create a *second* instrument
    with a disjoint price series, and both would look correct.

``test_removing_keeps_the_price_history``
    Deleting would throw away years of downloaded prices to save a table row.
"""

from __future__ import annotations

import sqlite3

import pytest

from portfolio_tracker.core import watchlist
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.ingest import prices


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


def _own(conn: sqlite3.Connection, key: str) -> int:
    cursor = conn.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, price_source, status) "
        "VALUES (?, ?, 'ETF', 'EUR', 'yfinance', 'OWNED')",
        (key, f"{key} Fund"),
    )
    instrument_id = int(cursor.lastrowid or 0)
    conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, flow_class) VALUES (1, ?, 'BUY', '2025-01-02', 'EUR', '10', 'INVESTMENT')",
        (instrument_id,),
    )
    conn.commit()
    return instrument_id


# ---------------------------------------------------------------------------
# Adding
# ---------------------------------------------------------------------------


def test_adding_records_a_watched_instrument(db: sqlite3.Connection) -> None:
    watchlist.add(
        db,
        import_key="IWDA",
        name="iShares Core MSCI World",
        currency="EUR",
        price_symbol="IWDA.AS",
    )
    row = db.execute("SELECT * FROM instrument WHERE import_key = 'IWDA'").fetchone()
    assert row["status"] == "WATCH"
    assert row["price_symbol"] == "IWDA.AS"
    assert row["is_fund"] == 1


def test_the_key_is_the_bare_symbol_so_a_later_purchase_adopts_it(
    db: sqlite3.Connection,
) -> None:
    """The identity decision this module turns on.

    The importer looks instruments up by `import_key` against the bare symbol
    in a positions export. A `.AS`-suffixed key would mean buying the fund
    creates a second row with its own, disjoint price history.
    """
    watchlist.add(
        db,
        import_key="IWDA",
        name="iShares Core MSCI World",
        currency="EUR",
        price_symbol="IWDA.AS",
    )
    keys = [r["import_key"] for r in db.execute("SELECT import_key FROM instrument")]
    assert keys == ["IWDA"], "the stored key must be what an import would match"


def test_a_tax_rate_is_never_guessed_for_something_unowned(db: sqlite3.Connection) -> None:
    """Nothing is taxed on an instrument nobody owns.

    Writing a plausible 30% here would put an unverified rate in the table,
    ready to be believed on the day it is actually bought.
    """
    watchlist.add(
        db, import_key="VWCE", name="Vanguard All-World", currency="EUR", price_symbol="VWCE.DE"
    )
    row = db.execute(
        "SELECT teilfreistellung_pct FROM instrument WHERE import_key='VWCE'"
    ).fetchone()
    assert row["teilfreistellung_pct"] is None


def test_watching_something_already_owned_is_refused_with_a_reason(
    db: sqlite3.Connection,
) -> None:
    _own(db, "SEC0")
    with pytest.raises(watchlist.WatchlistError, match="already hold"):
        watchlist.add(
            db, import_key="SEC0", name="whatever", currency="EUR", price_symbol="SEC0.DE"
        )


def test_re_adding_a_removed_instrument_reuses_the_row(db: sqlite3.Connection) -> None:
    first = watchlist.add(
        db, import_key="EQQQ", name="Invesco EQQQ", currency="EUR", price_symbol="EQQQ.DE"
    )
    watchlist.remove(db, "EQQQ")
    second = watchlist.add(
        db, import_key="EQQQ", name="Invesco EQQQ", currency="EUR", price_symbol="EQQQ.DE"
    )
    assert first == second


# ---------------------------------------------------------------------------
# Removing
# ---------------------------------------------------------------------------


def test_removing_keeps_the_price_history(db: sqlite3.Connection) -> None:
    instrument_id = watchlist.add(
        db, import_key="EQQQ", name="Invesco EQQQ", currency="EUR", price_symbol="EQQQ.DE"
    )
    db.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (?, '2026-01-02', '100', 'EUR', 'yfinance', '2026-01-02T00:00:00Z')",
        (instrument_id,),
    )
    db.commit()

    watchlist.remove(db, "EQQQ")
    row = db.execute("SELECT status FROM instrument WHERE import_key = 'EQQQ'").fetchone()
    assert row["status"] == "UNIVERSE", "kept, not deleted"
    assert db.execute("SELECT count(*) c FROM price").fetchone()["c"] == 1


def test_removing_something_owned_is_refused(db: sqlite3.Connection) -> None:
    _own(db, "SEC0")
    with pytest.raises(watchlist.WatchlistError, match="hold"):
        watchlist.remove(db, "SEC0")


# ---------------------------------------------------------------------------
# Details that cannot be fetched
# ---------------------------------------------------------------------------


def test_the_fee_and_category_can_be_recorded(db: sqlite3.Connection) -> None:
    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    watchlist.set_details(db, "IWDA", ter_bps=20, risk_bucket="eq_global_broad")
    row = db.execute(
        "SELECT ter_bps, risk_bucket FROM instrument WHERE import_key='IWDA'"
    ).fetchone()
    assert row["ter_bps"] == 20
    assert row["risk_bucket"] == "EQ_GLOBAL_BROAD"


def test_editing_one_detail_leaves_the_other_alone(db: sqlite3.Connection) -> None:
    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    watchlist.set_details(db, "IWDA", ter_bps=20, risk_bucket="EQ_GLOBAL_BROAD")
    watchlist.set_details(db, "IWDA", ter_bps=18)
    row = db.execute(
        "SELECT ter_bps, risk_bucket FROM instrument WHERE import_key='IWDA'"
    ).fetchone()
    assert (row["ter_bps"], row["risk_bucket"]) == (18, "EQ_GLOBAL_BROAD")


def test_a_percentage_typed_as_a_fee_is_refused(db: sqlite3.Connection) -> None:
    """0.2 is not 0.2%: the field is basis points, and 20 means 0.20% a year."""
    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    with pytest.raises(watchlist.WatchlistError, match="basis points"):
        watchlist.set_details(db, "IWDA", ter_bps=5000)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_a_watched_instrument_is_priced_even_with_no_transactions(
    db: sqlite3.Connection,
) -> None:
    """The bug that made a watchlist impossible.

    The price fetcher inner-joined `transaction_`, so an instrument nobody had
    traded was never fetched -- and a watchlist entry is exactly that.
    """
    _own(db, "SEC0")
    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    db.commit()

    keys = {i.import_key for i in prices.priced_instruments(db)}
    assert keys == {"SEC0", "IWDA"}


def test_the_watchlist_can_be_excluded_from_pricing(db: sqlite3.Connection) -> None:
    _own(db, "SEC0")
    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    db.commit()
    keys = {i.import_key for i in prices.priced_instruments(db, include_watched=False)}
    assert keys == {"SEC0"}


def test_a_watched_instrument_gets_years_of_history_not_ten_days(
    db: sqlite3.Connection,
) -> None:
    """It has no first trade to anchor to, and ten days compares against nothing."""
    from datetime import date, timedelta

    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    db.commit()
    ref = prices.priced_instruments(db)[0]
    start = prices.window_for(db, ref, end=date(2026, 6, 30), incremental=False)
    assert start < date(2026, 6, 30) - timedelta(days=365 * 4)


def test_a_watched_instrument_never_reaches_the_derived_layer(
    db: sqlite3.Connection,
) -> None:
    """It has no lots, so it cannot move a single figure about the portfolio."""
    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    db.commit()
    assert db.execute("SELECT count(*) c FROM lot").fetchone()["c"] == 0
    assert db.execute("SELECT count(*) c FROM transaction_").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_the_listing_says_when_prices_have_not_arrived(db: sqlite3.Connection) -> None:
    watchlist.add(db, import_key="IWDA", name="World", currency="EUR", price_symbol="IWDA.AS")
    db.commit()
    entry = watchlist.listing(db)[0]
    assert entry.prices == 0
    assert any("No prices yet" in c for c in entry.caveats)
    assert any("ongoing charge" in c for c in entry.caveats)
