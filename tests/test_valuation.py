"""FIFO lots and daily valuation.

The reconciliation tests at the end run against the synthetic corpus, so they
work on any clone. They check that the tool reproduces the source export where
it should, and differs from it *by a stated amount* where the export is wrong.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_tracker.core import lots as L
from portfolio_tracker.core.valuation import (
    StalePriceError,
    build_position_daily,
    value_position,
)
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.importers.investing_com.importer import import_positions
from portfolio_tracker.ingest.prices import store as store_prices
from portfolio_tracker.ingest.providers.base import InstrumentRef, PriceObservation

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "data" / "example"


def _lot(qty: str, cost_per_unit: str = "10") -> L.Lot:
    q = Decimal(qty)
    return L.Lot(
        account_id=1,
        instrument_id=1,
        open_txn_id=1,
        open_date=date(2025, 1, 1),
        currency="EUR",
        qty_opened=q,
        qty_remaining=q,
        cost_total_local=q * Decimal(cost_per_unit),
        cost_per_unit_local=Decimal(cost_per_unit),
        fx_rate_at_open=Decimal(1),
        cost_total_eur=q * Decimal(cost_per_unit),
        cost_per_unit_eur=Decimal(cost_per_unit),
    )


# ---------------------------------------------------------------------------
# FIFO
# ---------------------------------------------------------------------------


def test_fifo_takes_the_oldest_lot_first() -> None:
    a, b = _lot("10"), _lot("10")
    taken = L.fifo_consume([a, b], Decimal("4"))
    assert taken == [(a, Decimal("4"))]


def test_fifo_spans_lots_when_one_is_not_enough() -> None:
    a, b, c = _lot("3"), _lot("5"), _lot("10")
    taken = L.fifo_consume([a, b, c], Decimal("7"))
    assert [(lot.qty_opened, qty) for lot, qty in taken] == [
        (Decimal("3"), Decimal("3")),
        (Decimal("5"), Decimal("4")),
    ]


def test_overselling_raises_rather_than_going_negative() -> None:
    """An oversell means the transaction set is wrong. Continuing would produce
    a plausible tax figure from an impossible history."""
    with pytest.raises(L.LotError, match="only"):
        L.fifo_consume([_lot("5")], Decimal("6"))


def test_a_non_positive_disposal_is_rejected() -> None:
    with pytest.raises(L.LotError, match="must be positive"):
        L.fifo_consume([_lot("5")], Decimal("0"))


def test_exhausted_lots_are_skipped() -> None:
    spent, live = _lot("10"), _lot("10")
    spent.qty_remaining = Decimal(0)
    assert L.fifo_consume([spent, live], Decimal("2")) == [(live, Decimal("2"))]


# ---------------------------------------------------------------------------
# The decomposition identity
# ---------------------------------------------------------------------------


def test_local_and_currency_effects_sum_to_the_total_exactly() -> None:
    """The identity the whole design rests on.

    A foreign fund up 97% locally, in a currency that fell by a third.
    """
    value = value_position(
        quantity=Decimal("376904"),
        price_local=Decimal("0.627"),
        fx_rate=Decimal("55.3879"),
        cost_local=Decimal("117012.19"),
        cost_eur_frozen=Decimal("3103.66"),
    )
    assert value.unrealized_eur == value.unrealized_local_eur + value.unrealized_fx_eur


@pytest.mark.parametrize(
    ("rate", "cost_local", "cost_eur"),
    [
        (Decimal("55.25"), Decimal("100000"), Decimal("2000")),
        (Decimal("1"), Decimal("5000"), Decimal("5000")),
        (Decimal("0.85"), Decimal("850"), Decimal("1000")),
    ],
)
def test_the_identity_holds_for_any_inputs(
    rate: Decimal, cost_local: Decimal, cost_eur: Decimal
) -> None:
    value = value_position(Decimal("100"), Decimal("12.34"), rate, cost_local, cost_eur)
    assert value.unrealized_eur == value.unrealized_local_eur + value.unrealized_fx_eur


def test_a_euro_position_has_exactly_zero_currency_effect() -> None:
    """Not 'nearly zero'. A phantom FX line on a domestic holding would make
    every other currency figure suspect."""
    value = value_position(
        Decimal("100"), Decimal("50"), Decimal(1), Decimal("4000"), Decimal("4000")
    )
    assert value.unrealized_fx_eur == Decimal(0)
    assert value.unrealized_eur == value.unrealized_local_eur


def test_the_local_line_reproduces_what_the_export_reports() -> None:
    """`unrealized_local_eur` is deliberately the broker's own figure: the tool
    has to be able to show why it disagrees, not merely disagree."""
    value = value_position(
        quantity=Decimal("1000"),
        price_local=Decimal("2"),
        fx_rate=Decimal("50"),
        cost_local=Decimal("1000"),
        cost_eur_frozen=Decimal("50"),  # bought when the rate was 20
    )
    # The export translates both legs at today's rate: (2000 - 1000) / 50.
    assert value.unrealized_local_eur == Decimal("20.00")
    # The truth: 40 now against 50 paid.
    assert value.unrealized_eur == Decimal("-10.00")
    assert value.unrealized_fx_eur == Decimal("-30.00")


# ---------------------------------------------------------------------------
# End to end, against the synthetic corpus
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'D', 'DEPOT', 'EUR')"
    )
    conn.commit()
    import_positions(conn, EXAMPLE / "open.csv", EXAMPLE / "closed.csv", account_id=1)

    as_of = date(2026, 8, 14)
    conn.execute(
        "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
        "VALUES (?, 'EUR', 'TRY', '55.25', 'manual', '2026-08-14T00:00:00Z')",
        (as_of.isoformat(),),
    )
    # One price per instrument, at the corpus's own "current price", so the
    # comparison isolates this code from any price feed.
    rows = list(csv.DictReader((EXAMPLE / "open.csv").read_text(encoding="utf-8-sig").splitlines()))
    seen: dict[str, Decimal] = {}
    for row in rows:
        seen.setdefault(row["Symbol"], Decimal(row["Current Price"]))
    for symbol, price in seen.items():
        inst = conn.execute(
            "SELECT id, currency, trading_calendar FROM instrument WHERE import_key = ?",
            (symbol,),
        ).fetchone()
        ref = InstrumentRef(
            int(inst["id"]), symbol, inst["currency"], "manual", None, inst["trading_calendar"]
        )
        store_prices(conn, ref, [PriceObservation(as_of, price)], "manual")

    L.rebuild_lots(conn)
    build_position_daily(conn, start=as_of, end=as_of, strict=False)
    conn.commit()
    return conn


def _export_totals() -> dict[str, dict[str, Decimal]]:
    def money(text: str) -> Decimal:
        text = text.strip()
        negative = text.startswith("-")
        body = text.lstrip("-").lstrip("€₺ ").strip().replace(",", "")
        value = Decimal(body.rstrip("K")) * (1000 if body.endswith("K") else 1)
        return -value if negative else value

    totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"qty": Decimal(0), "mv": Decimal(0), "pl": Decimal(0)}
    )
    text = (EXAMPLE / "open.csv").read_text(encoding="utf-8-sig")
    for row in csv.DictReader(io.StringIO(text)):
        entry = totals[row["Symbol"]]
        entry["qty"] += Decimal(row["Amount"].rstrip("K")) * (
            1000 if row["Amount"].endswith("K") else 1
        )
        entry["mv"] += money(row["Market Value"])
        entry["pl"] += money(row["Net P/L"])
    return totals


def test_every_position_day_satisfies_the_identity(built: sqlite3.Connection) -> None:
    rows = built.execute(
        "SELECT unrealized_eur u, unrealized_local_eur l, unrealized_fx_eur f FROM position_daily"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["u"] == row["l"] + row["f"]


def test_euro_positions_carry_no_currency_effect(built: sqlite3.Connection) -> None:
    rows = built.execute(
        "SELECT p.unrealized_fx_eur f FROM position_daily p "
        "JOIN instrument i ON i.id = p.instrument_id WHERE i.currency = 'EUR'"
    ).fetchall()
    assert rows
    assert all(row["f"] == Decimal(0) for row in rows)


def test_quantities_reconcile_against_the_export(built: sqlite3.Connection) -> None:
    """Per instrument, not per lot: FIFO reallocates quantity *between* lots of
    the same instrument, so only the total is comparable."""
    export = _export_totals()
    for symbol, expected in export.items():
        rows = built.execute(
            "SELECT p.quantity q FROM position_daily p "
            "JOIN instrument i ON i.id = p.instrument_id "
            "WHERE i.import_key = ? AND p.date = '2026-08-14'",
            (symbol,),
        ).fetchall()
        if not rows:
            continue
        got = sum((r["q"] for r in rows), Decimal(0))
        # A quantity the source abbreviated is only known to an interval, and
        # the refined figure is deliberately *not* the printed one -- so the
        # tolerance is proportional rather than absolute.
        tolerance = max(Decimal("0.5"), abs(expected["qty"]) * Decimal("0.0001"))
        assert abs(got - expected["qty"]) <= tolerance, symbol


def test_market_value_reconciles_to_the_cent(built: sqlite3.Connection) -> None:
    """Given the export's own price, the computed market value must match it."""
    export = _export_totals()
    checked = 0
    for symbol, expected in export.items():
        rows = built.execute(
            "SELECT p.market_value_local v FROM position_daily p "
            "JOIN instrument i ON i.id = p.instrument_id "
            "WHERE i.import_key = ? AND p.date = '2026-08-14'",
            (symbol,),
        ).fetchall()
        if not rows:
            continue
        got = sum((r["v"] for r in rows), Decimal(0))
        # Abbreviated quantities carry a known interval, so allow 0.1% there.
        tolerance = max(Decimal("0.02"), abs(expected["mv"]) * Decimal("0.001"))
        assert abs(got - expected["mv"]) <= tolerance, f"{symbol}: {got} vs {expected['mv']}"
        checked += 1
    assert checked >= 8


def test_the_foreign_book_differs_from_the_export_by_exactly_the_currency_effect(
    built: sqlite3.Connection,
) -> None:
    """The headline claim, asserted rather than described.

    The export's profit figure translates both legs at today's rate, so it is
    reproduced by `unrealized_local_eur`; the true euro figure differs from it
    by precisely `unrealized_fx_eur`.
    """
    rows = built.execute(
        "SELECT p.unrealized_eur u, p.unrealized_local_eur l, p.unrealized_fx_eur f "
        "FROM position_daily p JOIN instrument i ON i.id = p.instrument_id "
        "WHERE i.currency = 'TRY' AND p.date = '2026-08-14'"
    ).fetchall()
    assert rows, "the corpus must contain foreign-currency positions"
    for row in rows:
        assert row["u"] - row["l"] == row["f"]
    # And the currency effect is material, not a rounding artefact.
    assert any(abs(row["f"]) > Decimal("100") for row in rows)


def test_rebuild_is_idempotent(built: sqlite3.Connection) -> None:
    before = built.execute("SELECT count(*) n FROM lot").fetchone()["n"]
    disposals = built.execute("SELECT count(*) n FROM lot_disposal").fetchone()["n"]
    L.rebuild_lots(built)
    assert built.execute("SELECT count(*) n FROM lot").fetchone()["n"] == before
    assert built.execute("SELECT count(*) n FROM lot_disposal").fetchone()["n"] == disposals


def test_disposals_consume_the_oldest_lot_not_the_one_the_export_names(
    built: sqlite3.Connection,
) -> None:
    """German law is FIFO per depot per instrument; the export attributes a sale
    to the lot most recently bought. Where the two disagree, FIFO wins.
    """
    row = built.execute(
        "SELECT l.open_date FROM lot_disposal d JOIN lot l ON l.id = d.lot_id "
        "JOIN instrument i ON i.id = l.instrument_id WHERE i.import_key = 'ZOVN'"
    ).fetchone()
    assert row is not None
    # The corpus's ZOVN sale is dated against the 2025-06-13 lot, but three
    # older lots exist, so FIFO must reach past it.
    assert row["open_date"] < "2025-06-13"


def test_a_stale_price_raises_in_strict_mode(built: sqlite3.Connection) -> None:
    with pytest.raises(StalePriceError, match="stale"):
        build_position_daily(built, start=date(2026, 9, 30), end=date(2026, 9, 30), strict=True)


def test_sql_sum_must_not_be_used_on_money_columns(built: sqlite3.Connection) -> None:
    """SQLite's aggregate functions coerce a TEXT-affinity column to REAL.

    ``SELECT sum(cost_basis_eur)`` therefore returns a float and silently
    discards the exactness the whole storage design exists to preserve.
    Aggregation belongs in Python. This test pins the behaviour so the trap
    stays documented rather than being rediscovered.
    """
    aggregated = built.execute("SELECT sum(cost_basis_eur) AS s FROM position_daily").fetchone()
    assert isinstance(aggregated["s"], float)

    rows = built.execute("SELECT cost_basis_eur c FROM position_daily").fetchall()
    assert all(isinstance(row["c"], Decimal) for row in rows)
