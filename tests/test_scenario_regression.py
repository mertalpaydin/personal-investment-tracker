"""Pins `scenario.sell()` before it gains the kwargs trade entry needs.

`POST /api/transactions/validate` has to show which lots a sale would consume
and what it would cost in tax -- which is exactly what `sell()` already
computes, except that it insists on pricing at the last observed close. Trade
entry needs it priced at the figure the user typed instead.

Adding two optional arguments is the right change, and the risk is that it
quietly alters the existing answer for everyone who does not pass them. The
harvest planner and `GET /api/scenario/sell` both depend on that answer, and a
drift of a few cents in a tax figure is not the kind of bug that announces
itself.

So: this file records the output of the unmodified function, field by field,
and asserts the defaults reproduce it exactly.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from portfolio_tracker.core import scenario
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "teilfreistellung_pct, tax_regime, price_source) "
        "VALUES (1, 'ZGEQ', 'Global Equity', 'ETF', 'EUR', 1, '0.30', 'DE_INVESTMENT', 'manual')"
    )
    # Two lots at different costs, so FIFO order is observable in the result.
    for lot_id, when, qty, cost in (
        (1, "2024-01-15", "100", "5000"),
        (2, "2024-09-10", "50", "3250"),
    ):
        conn.execute(
            "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, "
            "currency, quantity, price_per_unit_local, gross_amount_local, gross_amount_eur, "
            "flow_class) VALUES (?, 1, 1, 'BUY', ?, 'EUR', ?, '50', ?, ?, 'INVESTMENT')",
            (lot_id, when, qty, cost, cost),
        )
        conn.execute(
            "INSERT INTO lot (id, account_id, instrument_id, open_txn_id, open_date, "
            "qty_opened, qty_remaining, currency, cost_total_local, cost_per_unit_local, "
            "fx_rate_at_open, cost_total_eur, cost_per_unit_eur) "
            "VALUES (?, 1, 1, ?, ?, ?, ?, 'EUR', ?, ?, '1', ?, ?)",
            (
                lot_id,
                lot_id,
                when,
                qty,
                qty,
                cost,
                str(Decimal(cost) / Decimal(qty)),
                cost,
                str(Decimal(cost) / Decimal(qty)),
            ),
        )
    conn.execute(
        "INSERT INTO price (instrument_id, date, close, currency, source, fetched_at) "
        "VALUES (1, '2026-06-30', '72.50', 'EUR', 'manual', '2026-06-30T00:00:00Z')"
    )
    conn.commit()
    return conn


AS_OF = date(2026, 6, 30)


def _snapshot(result: scenario.SaleScenario) -> dict[str, Any]:
    """Every field that reaches a caller, flattened for comparison."""
    return {
        "instrument": result.instrument,
        "price_local": result.price_local,
        "price_asof": result.price_asof,
        "price_stale_days": result.price_stale_days,
        "quantity": result.quantity,
        "quantity_held": result.quantity_held,
        "proceeds_eur": result.proceeds_eur,
        "cost_basis_eur": result.cost_basis_eur,
        "gross_gain_eur": result.gross_gain_eur,
        "vap_offset_eur": result.vap_offset_eur,
        "teilfreistellung_eur": result.teilfreistellung_eur,
        "taxable_gain_eur": result.taxable_gain_eur,
        "marginal_tax_eur": result.marginal_tax_eur,
        "net_proceeds_eur": result.net_proceeds_eur,
        "allowance_remaining_before_eur": result.allowance_remaining_before_eur,
        "allowance_remaining_after_eur": result.allowance_remaining_after_eur,
        "consumed": [
            (c.lot_id, c.open_date, c.quantity, c.cost_basis_eur, c.proceeds_eur, c.gross_gain_eur)
            for c in result.consumed
        ],
        "caveats": list(result.caveats),
    }


#: The answer as of the commit that introduced trade entry. Written out rather
#: than recomputed so that a change to `sell()` shows up here as a diff of
#: numbers, not as two implementations agreeing with each other.
EXPECTED_FULL_SALE = {
    "price_local": Decimal("72.50"),
    "quantity": Decimal("150"),
    "proceeds_eur": Decimal("10875.00"),
    "cost_basis_eur": Decimal("8250.00"),
    "gross_gain_eur": Decimal("2625.00"),
    "teilfreistellung_eur": Decimal("787.50"),
    "taxable_gain_eur": Decimal("1837.50"),
}


def test_a_full_sale_prices_as_it_always_has(db: sqlite3.Connection) -> None:
    result = scenario.sell(db, instrument="ZGEQ", as_of=AS_OF, tax_year=2026)
    got = _snapshot(result)
    for key, want in EXPECTED_FULL_SALE.items():
        assert got[key] == want, f"{key}: {got[key]} != {want}"


def test_a_partial_sale_takes_the_oldest_lot_first(db: sqlite3.Connection) -> None:
    result = scenario.sell(
        db, instrument="ZGEQ", quantity=Decimal("120"), as_of=AS_OF, tax_year=2026
    )
    assert [c.lot_id for c in result.consumed] == [1, 2]
    assert [c.quantity for c in result.consumed] == [Decimal("100"), Decimal("20")]
    # 100 units at cost 50, 20 units at cost 65.
    assert result.cost_basis_eur == Decimal("6300.00")


def test_passing_no_new_arguments_reproduces_the_old_answer(db: sqlite3.Connection) -> None:
    """The guard on the additive change itself.

    Both calls must be byte-identical: the defaults are the only thing keeping
    `harvest()` and `/api/scenario/sell` on their previous behaviour.
    """
    plain = scenario.sell(db, instrument="ZGEQ", quantity=Decimal("60"), as_of=AS_OF, tax_year=2026)
    explicit = scenario.sell(
        db,
        instrument="ZGEQ",
        quantity=Decimal("60"),
        as_of=AS_OF,
        tax_year=2026,
        price_local=None,
        fees_local=Decimal("0"),
    )
    assert _snapshot(plain) == _snapshot(explicit)


# ---------------------------------------------------------------------------
# What the new arguments are for
# ---------------------------------------------------------------------------


def test_a_supplied_price_is_used_instead_of_the_last_close(db: sqlite3.Connection) -> None:
    result = scenario.sell(
        db,
        instrument="ZGEQ",
        quantity=Decimal("100"),
        as_of=AS_OF,
        tax_year=2026,
        price_local=Decimal("80"),
    )
    assert result.price_local == Decimal("80")
    assert result.proceeds_eur == Decimal("8000.00")
    # Not stale: it is the figure being entered, not an observation carried
    # forward, so reporting an age would be false.
    assert result.price_stale_days == 0
    assert result.price_asof == AS_OF
    assert any("figure you entered" in c for c in result.caveats)


def test_fees_reduce_proceeds_and_the_gain(db: sqlite3.Connection) -> None:
    without = scenario.sell(
        db, instrument="ZGEQ", quantity=Decimal("100"), as_of=AS_OF, tax_year=2026
    )
    with_fee = scenario.sell(
        db,
        instrument="ZGEQ",
        quantity=Decimal("100"),
        as_of=AS_OF,
        tax_year=2026,
        fees_local=Decimal("9.90"),
    )
    assert with_fee.proceeds_eur == without.proceeds_eur - Decimal("9.90")
    assert with_fee.gross_gain_eur == without.gross_gain_eur - Decimal("9.90")
    assert with_fee.fees_eur == Decimal("9.90")


def test_fees_are_spread_across_the_lots_a_sale_consumes(db: sqlite3.Connection) -> None:
    """The per-lot rows must still add up to the totals shown above them.

    A fee subtracted only from the total would leave the lot table summing to a
    different number than the summary, which reads as an arithmetic error even
    though the total is right.
    """
    result = scenario.sell(
        db,
        instrument="ZGEQ",
        quantity=Decimal("150"),
        as_of=AS_OF,
        tax_year=2026,
        fees_local=Decimal("10"),
    )
    assert sum(c.proceeds_eur for c in result.consumed) == result.proceeds_eur
    assert sum(c.gross_gain_eur for c in result.consumed) == result.gross_gain_eur
