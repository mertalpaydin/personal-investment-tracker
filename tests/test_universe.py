"""The comparison set, and what loading it must never touch.

`core/peers.py` shipped complete with a candidate pool of zero, which renders
as a blank panel that reads as a broken feature. This is the catalogue that
fills it, and these tests guard the three ways filling it could do harm:

``test_an_owned_instrument_is_never_touched``
    The catalogue and a real portfolio overlap by design. Adopting a row with
    lots against it, or rewriting its metadata from a generic entry, would
    corrupt actual holdings in order to populate a comparison.

``test_every_entry_states_a_risk_category``
    `peers.scan_portfolio` compares only where the category was *stated*,
    because the fallback cannot tell a semiconductor fund from a world
    tracker. A catalogue without categories is a catalogue that can never be
    compared against, which is the whole point of having one.

``test_unloading_keeps_what_you_own_or_watch``
    Removing the set must not remove five years of downloaded prices for
    something the owner chose.
"""

from __future__ import annotations

import sqlite3

import pytest

from portfolio_tracker.core import peers, universe, watchlist
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# The catalogue itself
# ---------------------------------------------------------------------------


def test_the_catalogue_is_readable_and_not_empty() -> None:
    entries = universe.catalogue()
    assert len(entries) > 150, "a comparison set this small is not worth the price fetches"


def test_every_entry_states_a_risk_category() -> None:
    missing = [k for k, v in universe.catalogue().items() if not v.get("risk_bucket")]
    assert not missing, (
        "peers.scan_portfolio only compares where the category was stated, so an "
        f"entry without one can never be a candidate: {missing[:10]}"
    )


def test_every_category_is_one_the_engine_knows() -> None:
    unknown = {v["risk_bucket"] for v in universe.catalogue().values()} - set(peers.BUCKETS)
    assert not unknown, f"peers.BUCKETS does not contain {sorted(unknown)}"


def test_every_entry_carries_what_pricing_needs() -> None:
    for key, spec in universe.catalogue().items():
        assert spec.get("currency"), key
        assert spec.get("price_symbol"), key
        assert spec.get("name"), key


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_loading_inserts_the_whole_catalogue(db: sqlite3.Connection) -> None:
    report = universe.load(db)
    db.commit()
    assert report.added == len(universe.catalogue())

    state = universe.status(db)
    assert state["loaded"] is True
    assert state["present"] == state["catalogue_size"]
    # Instruments land at once; their prices arrive with the next fetch, and
    # the two are reported separately so the wait does not look like a fault.
    assert state["priced"] == 0


def test_loading_twice_changes_nothing(db: sqlite3.Connection) -> None:
    universe.load(db)
    db.commit()
    assert universe.load(db).added == 0


def test_an_owned_instrument_is_never_touched(db: sqlite3.Connection) -> None:
    """The catalogue overlaps a real portfolio, and yours wins."""
    key = next(iter(universe.catalogue()))
    db.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, "
        "price_source, status) VALUES (?, 'My own name', 'ETF', 'EUR', 'manual', 'OWNED')",
        (key,),
    )
    db.commit()

    report = universe.load(db, refresh=True)
    db.commit()
    assert key in report.owned

    row = db.execute(
        "SELECT name, status, price_source FROM instrument WHERE import_key = ?", (key,)
    ).fetchone()
    assert row["name"] == "My own name"
    assert row["status"] == "OWNED"
    assert row["price_source"] == "manual"


def test_a_watched_instrument_keeps_its_own_status(db: sqlite3.Connection) -> None:
    """Watching is a choice; the universe is furniture. They do not merge."""
    key = next(iter(universe.catalogue()))
    watchlist.add(db, import_key=key, name="Chosen by me", currency="EUR")
    db.commit()

    report = universe.load(db)
    db.commit()
    assert key in report.watched
    row = db.execute("SELECT status, name FROM instrument WHERE import_key = ?", (key,)).fetchone()
    assert row["status"] == watchlist.WATCHED
    assert row["name"] == "Chosen by me"


# ---------------------------------------------------------------------------
# Unloading
# ---------------------------------------------------------------------------


def test_unloading_keeps_what_you_own_or_watch(db: sqlite3.Connection) -> None:
    keys = list(universe.catalogue())
    owned, watched = keys[0], keys[1]
    db.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, "
        "price_source, status) VALUES (?, 'Held', 'ETF', 'EUR', 'manual', 'OWNED')",
        (owned,),
    )
    watchlist.add(db, import_key=watched, name="Followed", currency="EUR")
    universe.load(db)
    db.commit()

    removed = universe.unload(db)
    db.commit()
    assert removed == len(keys) - 2

    survivors = {str(r["import_key"]) for r in db.execute("SELECT import_key FROM instrument")}
    assert survivors == {owned, watched}
