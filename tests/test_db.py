"""Schema, migration and storage-layer behaviour.

The tests that matter most here are the ones asserting a *refusal*: the schema's
job is partly to make certain wrong states unrepresentable, and an unenforced
constraint reads exactly like an enforced one until the day it matters.
"""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect, transaction
from portfolio_tracker.money import DECIMAL_SQL_TYPE, plain, quantize_money, to_decimal


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    return conn


@pytest.fixture
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    db.execute("INSERT INTO person (id, name) VALUES (1, 'Test Person')")
    db.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    db.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency) "
        "VALUES (1, 'AAA', 'Test ETF', 'ETF', 'EUR')"
    )
    db.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency) "
        "VALUES (2, 'BBB', 'Lira Fund', 'FUND', 'TRY')"
    )
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Migration mechanics
# ---------------------------------------------------------------------------


def test_empty_database_reaches_head_in_one_call() -> None:
    conn = connect(":memory:")
    assert M.current_version(conn) == 0
    assert M.migrate(conn) == [1]
    assert M.current_version(conn) == 1


def test_second_migrate_is_a_no_op(db: sqlite3.Connection) -> None:
    assert M.migrate(db) == []
    assert M.current_version(db) == 1


def test_editing_an_applied_migration_is_refused(db: sqlite3.Connection, tmp_path: Path) -> None:
    """The guard against two databases sharing a version but not a shape."""
    original = M.discover()[0]
    edited_dir = tmp_path / "migrations"
    edited_dir.mkdir()
    target = edited_dir / original.path.name
    target.write_text(original.sql + "\n-- an innocuous trailing comment\n", encoding="utf-8")

    with pytest.raises(M.MigrationError, match="has changed since it was applied"):
        M.migrate(db, directory=edited_dir)


def test_database_ahead_of_checkout_is_refused(db: sqlite3.Connection, tmp_path: Path) -> None:
    empty = tmp_path / "migrations"
    empty.mkdir()
    with pytest.raises(M.MigrationError, match="newer than this checkout"):
        M.migrate(db, directory=empty)


def test_checksum_ignores_line_ending_differences(tmp_path: Path) -> None:
    """Otherwise the guard fires on every Windows checkout against Linux CI."""
    lf = tmp_path / "0001_x.sql"
    lf.write_bytes(b"CREATE TABLE a (b INTEGER);\n")
    crlf = tmp_path / "0002_x.sql"
    crlf.write_bytes(b"CREATE TABLE a (b INTEGER);\r\n")
    assert M.Migration(1, "x", lf).checksum == M.Migration(2, "x", crlf).checksum


@pytest.mark.parametrize("bad", ["1_missing_pad.sql", "0001-dashes.sql", "0001_Upper.sql"])
def test_malformed_migration_filenames_are_rejected(tmp_path: Path, bad: str) -> None:
    (tmp_path / bad).write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(M.MigrationError, match="filename"):
        M.discover(tmp_path)


def test_version_gaps_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(M.MigrationError, match="contiguous"):
        M.discover(tmp_path)


def test_user_version_pragma_mirrors_schema_version(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA user_version").fetchone()[0] == M.current_version(db)


# ---------------------------------------------------------------------------
# Decimal storage -- the affinity trap
# ---------------------------------------------------------------------------


def test_decimal_columns_are_stored_as_text_not_real(seeded: sqlite3.Connection) -> None:
    """A column typed `DECIMAL` would get NUMERIC affinity and become a float.

    That silently defeats the entire reason for using Decimal, and would only
    surface as a few cents of drift in a tax figure months later.
    """
    assert "TEXT" in DECIMAL_SQL_TYPE
    assert "INT" not in DECIMAL_SQL_TYPE

    seeded.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (1, '2026-01-02', ?, 'EUR', 'test', '2026-01-02T00:00:00Z')",
        (Decimal("0.1"),),
    )
    stored = seeded.execute("SELECT typeof(close) AS t FROM price").fetchone()["t"]
    assert stored == "text", f"stored as {stored}; Decimal precision is being lost"


def test_decimal_survives_a_round_trip_exactly(seeded: sqlite3.Connection) -> None:
    awkward = Decimal("376904.123456789012345678")
    seeded.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (1, '2026-01-05', ?, 'EUR', 'test', '2026-01-05T00:00:00Z')",
        (awkward,),
    )
    back = seeded.execute("SELECT close FROM price WHERE date = '2026-01-05'").fetchone()["close"]
    assert isinstance(back, Decimal)
    assert back == awkward
    assert back != float(awkward)


def test_summing_cents_is_exact(seeded: sqlite3.Connection) -> None:
    """The float version of this drifts; that is the whole argument."""
    for i in range(10):
        seeded.execute(
            "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
            f"VALUES (1, '2026-02-{i + 1:02d}', ?, 'EUR', 'test', '2026-02-01T00:00:00Z')",
            (Decimal("0.1"),),
        )
    rows = seeded.execute("SELECT close FROM price WHERE date LIKE '2026-02-%'").fetchall()
    assert sum((r["close"] for r in rows), Decimal(0)) == Decimal("1.0")


def test_to_decimal_refuses_floats() -> None:
    with pytest.raises(TypeError, match="precision is already lost"):
        to_decimal(0.1)  # type: ignore[arg-type]


def test_money_rounds_half_up_not_bankers() -> None:
    """Statements round half away from zero; Python's default does not."""
    assert quantize_money(Decimal("2.675")) == Decimal("2.68")
    assert quantize_money(Decimal("2.665")) == Decimal("2.67")


def test_plain_renders_without_exponent_or_trailing_zeros() -> None:
    assert plain(Decimal("376904.00")) == "376904"
    assert plain(Decimal("1E+3")) == "1000"
    assert plain(Decimal("0.3180")) == "0.318"


# ---------------------------------------------------------------------------
# Constraints that must actually be enforced
# ---------------------------------------------------------------------------


def test_foreign_keys_are_enforced(seeded: sqlite3.Connection) -> None:
    """Off by default in SQLite, per connection. Easy to omit, silently."""
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(
            "INSERT INTO account (person_id, name, kind, currency) "
            "VALUES (999, 'Ghost', 'DEPOT', 'EUR')"
        )


def test_price_currency_must_match_its_instrument(seeded: sqlite3.Connection) -> None:
    """Instrument 2 is lira-denominated; a euro price for it is a bug."""
    with pytest.raises(sqlite3.IntegrityError, match=re.escape("price.currency must equal")):
        seeded.execute(
            "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
            "VALUES (2, '2026-01-02', '1.0', 'EUR', 'test', '2026-01-02T00:00:00Z')"
        )


def test_fx_rate_direction_is_structurally_enforced(seeded: sqlite3.Connection) -> None:
    """Only EUR-based rates exist, so conversion is always division.

    This removes the single largest bug class in multi-currency systems: there
    is no way to write an inverted rate, because the inverted row cannot exist.
    """
    seeded.execute(
        "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
        "VALUES ('2026-01-02', 'EUR', 'TRY', '55.25', 'ecb', '2026-01-02T00:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(
            "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
            "VALUES ('2026-01-02', 'TRY', 'EUR', '0.0181', 'ecb', '2026-01-02T00:00:00Z')"
        )


def test_eur_cannot_be_its_own_quote(seeded: sqlite3.Connection) -> None:
    """EUR/EUR = 1 is short-circuited in code and must never reach the table."""
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(
            "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
            "VALUES ('2026-01-02', 'EUR', 'EUR', '1', 'ecb', '2026-01-02T00:00:00Z')"
        )


def test_external_id_makes_reimport_idempotent(seeded: sqlite3.Connection) -> None:
    stmt = (
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, "
        "currency, quantity, flow_class, external_id) "
        "VALUES (1, 1, 'BUY', '2026-01-02', 'EUR', '10', 'INVESTMENT', 'invcom:v1:BUY:AAA')"
    )
    seeded.execute(stmt)
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(stmt)


def test_flow_class_is_mandatory(seeded: sqlite3.Connection) -> None:
    """Without it a deposit is indistinguishable from a dividend, and every
    time-weighted return is wrong. It has no default for that reason."""
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(
            "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency) "
            "VALUES (1, 1, 'BUY', '2026-01-02', 'EUR')"
        )


def test_teilfreistellung_defaults_to_unresolved_not_zero_or_thirty(
    seeded: sqlite3.Connection,
) -> None:
    """Guessing this wrong is materially expensive in both directions, so the
    column is NULL until someone establishes the real figure."""
    row = seeded.execute("SELECT teilfreistellung_pct FROM instrument WHERE id = 1").fetchone()
    assert row["teilfreistellung_pct"] is None


def test_instrument_currency_has_no_default(db: sqlite3.Connection) -> None:
    """A silent 'EUR' default is how a lira fund gets valued as though euro."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO instrument (import_key, name, asset_class) VALUES ('X', 'X', 'ETF')"
        )


def test_isin_is_nullable_but_still_unique(seeded: sqlite3.Connection) -> None:
    """The source export has no ISINs at all, so NOT NULL would block import."""
    seeded.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency) "
        "VALUES ('C1', 'No ISIN 1', 'ETF', 'EUR')"
    )
    seeded.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency) "
        "VALUES ('C2', 'No ISIN 2', 'ETF', 'EUR')"
    )
    seeded.execute(
        "INSERT INTO instrument (import_key, isin, name, asset_class, currency) "
        "VALUES ('C3', 'IE00B4L5Y983', 'Has ISIN', 'ETF', 'EUR')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute(
            "INSERT INTO instrument (import_key, isin, name, asset_class, currency) "
            "VALUES ('C4', 'IE00B4L5Y983', 'Duplicate', 'ETF', 'EUR')"
        )


# ---------------------------------------------------------------------------
# reset_derived
# ---------------------------------------------------------------------------


def test_reset_derived_clears_derivations_and_keeps_facts(seeded: sqlite3.Connection) -> None:
    seeded.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, "
        "currency, quantity, flow_class) "
        "VALUES (1, 1, 1, 'BUY', '2026-01-02', 'EUR', '10', 'INVESTMENT')"
    )
    seeded.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (1, '2026-01-02', '100', 'EUR', 'test', '2026-01-02T00:00:00Z')"
    )
    seeded.execute(
        "INSERT INTO lot (id, account_id, instrument_id, open_txn_id, open_date, "
        "qty_opened, qty_remaining, currency, cost_total_local, cost_per_unit_local) "
        "VALUES (1, 1, 1, 1, '2026-01-02', '10', '10', 'EUR', '1000', '100')"
    )
    seeded.commit()

    deleted = M.reset_derived(seeded)

    assert deleted["lot"] == 1
    assert seeded.execute("SELECT count(*) n FROM lot").fetchone()["n"] == 0
    # Facts survive.
    assert seeded.execute("SELECT count(*) n FROM transaction_").fetchone()["n"] == 1
    assert seeded.execute("SELECT count(*) n FROM price").fetchone()["n"] == 1
    assert seeded.execute("SELECT count(*) n FROM tax_constant").fetchone()["n"] == 3
    assert seeded.execute("SELECT count(*) n FROM instrument").fetchone()["n"] == 2


def test_reset_derived_is_safe_to_repeat(seeded: sqlite3.Connection) -> None:
    M.reset_derived(seeded)
    assert all(count == 0 for count in M.reset_derived(seeded).values())


def test_derived_tables_are_ordered_for_foreign_keys(seeded: sqlite3.Connection) -> None:
    """lot_disposal references lot, so it must be emptied first."""
    assert M.DERIVED_TABLES.index("lot_disposal") < M.DERIVED_TABLES.index("lot")


def test_every_derived_table_actually_exists(db: sqlite3.Connection) -> None:
    """A typo here would silently skip a table during reset."""
    real = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(M.DERIVED_TABLES) <= real


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_on_error(seeded: sqlite3.Connection) -> None:
    with pytest.raises(ValueError), transaction(seeded):
        seeded.execute(
            "INSERT INTO instrument (import_key, name, asset_class, currency) "
            "VALUES ('ROLL', 'Rolled back', 'ETF', 'EUR')"
        )
        raise ValueError("boom")
    n = seeded.execute("SELECT count(*) n FROM instrument WHERE import_key = 'ROLL'").fetchone()
    assert n["n"] == 0


def test_connect_creates_the_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "portfolio.db"
    conn = connect(target)
    M.migrate(conn)
    conn.close()
    assert target.exists()


def test_a_file_database_persists_across_connections(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.db"
    first = connect(target)
    M.migrate(first)
    first.execute("INSERT INTO person (id, name) VALUES (1, 'Persisted')")
    first.commit()
    first.close()

    second = connect(target)
    assert M.migrate(second) == []
    assert second.execute("SELECT name FROM person").fetchone()["name"] == "Persisted"
    second.close()
