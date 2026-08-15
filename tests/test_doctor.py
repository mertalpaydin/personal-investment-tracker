"""The identity checks.

A checker that only ever passes is indistinguishable from one that does
nothing, so every check here is tested twice: once against consistent data, and
once against data with a specific violation planted in it. The planted values
are the shapes of real bugs this project already hit -- a market value that
stopped being quantity times price, a gain split whose parts stopped summing,
and a contribution that fell off the daily series and was reported as growth.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from portfolio_tracker.core import doctor
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.importers.report import Severity

D = Decimal
DAY = "2026-06-01"


@pytest.fixture
def db() -> sqlite3.Connection:
    """A tiny, entirely consistent portfolio: one EUR lot, one TRY lot."""
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'D', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, "
        "teilfreistellung_pct, is_fund, tax_regime) "
        "VALUES (1, 'AAA', 'A', 'ETF', 'EUR', '0.30', 1, 'DE_INVESTMENT')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, tax_regime) "
        "VALUES (2, 'TTT', 'T', 'FUND', 'TRY', 'OUT_OF_SCOPE')"
    )
    conn.execute(
        "INSERT INTO transaction_ (id, account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, gross_amount_eur, fx_rate, flow_class, fees_known) "
        "VALUES (1, 1, NULL, 'DEPOSIT', '2026-01-02', 'EUR', NULL, 5000, 5000, 1, "
        "'EXTERNAL_IN', 1)"
    )
    conn.execute(
        "INSERT INTO lot (id, account_id, instrument_id, open_txn_id, open_date, qty_opened, "
        "qty_remaining, currency, cost_total_local, cost_per_unit_local, fx_rate_at_open, "
        "cost_total_eur, cost_per_unit_eur) "
        "VALUES (1, 1, 1, 1, '2026-01-02', 100, 100, 'EUR', 5000, 50, 1, 5000, 50)"
    )
    conn.execute(
        "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
        "VALUES (?, 'EUR', 'TRY', '50', 'manual', '2026-06-01T00:00:00Z')",
        (DAY,),
    )
    _position(conn, 1, "EUR", qty="100", price="60", rate="1", cost_eur="5000", cost_spot="5000")
    _position(conn, 2, "TRY", qty="1000", price="200", rate="50", cost_eur="3800", cost_spot="3000")
    _portfolio(conn)
    conn.commit()
    return conn


def _position(
    conn: sqlite3.Connection,
    instrument_id: int,
    currency: str,
    *,
    qty: str,
    price: str,
    rate: str,
    cost_eur: str,
    cost_spot: str,
) -> None:
    """Insert a row that satisfies every identity, computed rather than typed."""
    mv_local = D(qty) * D(price)
    mv_eur = mv_local / D(rate)
    fx_effect = D(cost_spot) - D(cost_eur)
    local_gain = mv_eur - D(cost_spot)
    conn.execute(
        """
        INSERT INTO position_daily
            (date, account_id, instrument_id, quantity, currency, price_local, price_asof,
             fx_rate, fx_asof, market_value_local, market_value_eur, cost_basis_local,
             cost_basis_eur, cost_basis_eur_at_spot, unrealized_eur, unrealized_local_eur,
             unrealized_fx_eur, valuation_status)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OK')
        """,
        (
            DAY,
            instrument_id,
            D(qty),
            currency,
            D(price),
            DAY,
            D(rate),
            DAY,
            mv_local,
            mv_eur,
            mv_local,
            D(cost_eur),
            D(cost_spot),
            local_gain + fx_effect,
            local_gain,
            fx_effect,
        ),
    )


def _portfolio(conn: sqlite3.Connection, **override: Decimal) -> None:
    values: dict[str, Decimal] = {
        "market_value_eur": D(6000) + D(200000) / D(50),
        "deposits_eur": D(0),
        "cash_eur": D(0),
        "income_cash_eur": D(0),
        "net_external_flow_eur": D(5000),
    }
    values.update(override)
    total = values["market_value_eur"] + values["deposits_eur"]
    basis = total + values["cash_eur"]
    conn.execute("DELETE FROM portfolio_daily WHERE date = ?", (DAY,))
    conn.execute(
        """
        INSERT INTO portfolio_daily
            (date, person_id, market_value_eur, total_value_eur, cost_basis_eur, cash_eur,
             deposits_eur, valuation_basis_eur, income_cash_eur, income_received_eur,
             net_external_flow_eur, units_outstanding, unit_price, completeness)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FULL')
        """,
        (
            DAY,
            values["market_value_eur"],
            total,
            D(8800),
            values["cash_eur"],
            values["deposits_eur"],
            basis,
            values["income_cash_eur"],
            D(0),
            values["net_external_flow_eur"],
            D(100),
            basis / D(100),
        ),
    )


# ---------------------------------------------------------------------------
# The baseline: consistent data produces no errors
# ---------------------------------------------------------------------------


def test_consistent_data_raises_no_errors(db: sqlite3.Connection) -> None:
    report = doctor.run(db)
    assert report.errors == [], [str(f) for f in report.errors]
    assert report.ok
    assert report.rows_examined > 0
    assert report.checks_run == len(doctor.CHECKS)


def test_it_examines_every_row_it_claims_to(db: sqlite3.Connection) -> None:
    """A checker that silently skips rows passes for the wrong reason."""
    seeded = sum(
        db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("lot", "position_daily", "fx_rate", "portfolio_daily")
    )
    # `lot` is read by two separate queries, so the count exceeds the row count.
    assert doctor.run(db).rows_examined >= seeded


# ---------------------------------------------------------------------------
# Planted violations, one per identity
# ---------------------------------------------------------------------------


def test_a_market_value_that_is_not_quantity_times_price_is_caught(
    db: sqlite3.Connection,
) -> None:
    db.execute("UPDATE position_daily SET market_value_local = '9999' WHERE instrument_id = 1")
    assert "MV_NOT_QTY_TIMES_PRICE" in doctor.run(db).codes


def test_a_euro_value_that_does_not_follow_from_the_rate_is_caught(
    db: sqlite3.Connection,
) -> None:
    db.execute("UPDATE position_daily SET market_value_eur = '1' WHERE instrument_id = 2")
    assert "MV_EUR_NOT_LOCAL_OVER_RATE" in doctor.run(db).codes


def test_a_gain_split_whose_parts_do_not_sum_is_caught(db: sqlite3.Connection) -> None:
    """The API shows all three numbers; two of them must make the third."""
    db.execute("UPDATE position_daily SET unrealized_eur = '12345' WHERE instrument_id = 2")
    assert "GAIN_SPLIT_DOES_NOT_SUM" in doctor.run(db).codes


def test_a_currency_effect_that_is_not_the_cost_revaluation_is_caught(
    db: sqlite3.Connection,
) -> None:
    db.execute(
        "UPDATE position_daily SET unrealized_fx_eur = '0', unrealized_local_eur = "
        "unrealized_eur WHERE instrument_id = 2"
    )
    assert "FX_EFFECT_NOT_COST_REVALUATION" in doctor.run(db).codes


def test_a_lot_whose_parts_do_not_balance_is_caught(db: sqlite3.Connection) -> None:
    db.execute("UPDATE lot SET qty_remaining = '40' WHERE id = 1")
    assert "LOT_QUANTITY_UNBALANCED" in doctor.run(db).codes


def test_a_lot_holding_more_than_was_bought_is_caught(db: sqlite3.Connection) -> None:
    db.execute("UPDATE lot SET qty_remaining = '500' WHERE id = 1")
    assert "LOT_QTY_GREW" in doctor.run(db).codes


def test_the_schema_itself_refuses_a_non_euro_base(db: sqlite3.Connection) -> None:
    """The check is a backstop; this is the constraint that makes it redundant.

    Conversion in this system is always division, which only works because
    every stored rate is quote-per-EUR. The CHECK is the real guarantee; the
    doctor rule exists in case a future migration relaxes it.
    """
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
            "VALUES ('2026-05-01', 'TRY', 'EUR', '0.02', 'manual', '2026-06-01T00:00:00Z')"
        )


def test_cash_folded_into_the_total_is_caught(db: sqlite3.Connection) -> None:
    """The rule the owner asked for, enforced rather than trusted."""
    _portfolio(db, cash_eur=D(500))
    db.execute(
        "UPDATE portfolio_daily SET total_value_eur = market_value_eur + deposits_eur + cash_eur"
    )
    codes = doctor.run(db).codes
    assert "CASH_LEAKED_INTO_TOTAL" in codes
    assert "TOTAL_IS_NOT_HOLDINGS_PLUS_DEPOSITS" in codes


def test_income_exceeding_the_balance_it_sits_in_is_caught(db: sqlite3.Connection) -> None:
    _portfolio(db, cash_eur=D(100), income_cash_eur=D(400))
    assert "INCOME_EXCEEDS_CASH" in doctor.run(db).codes


def test_a_broken_unit_identity_is_caught(db: sqlite3.Connection) -> None:
    """The check that caught an 877% return."""
    db.execute("UPDATE portfolio_daily SET unit_price = '250'")
    assert "UNIT_IDENTITY_BROKEN" in doctor.run(db).codes


def test_a_contribution_missing_from_the_series_is_caught(db: sqlite3.Connection) -> None:
    """The check that caught a 1326% return.

    A flow dropped on its way into the daily series does not vanish; it is
    reported as investment growth, which is the most flattering way to be wrong.
    """
    db.execute("UPDATE portfolio_daily SET net_external_flow_eur = '0'")
    assert "FLOWS_DO_NOT_RECONCILE" in doctor.run(db).codes


def test_an_fx_gap_on_the_valuation_grid_is_reported(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM fx_rate WHERE quote = 'TRY'")
    report = doctor.run(db)
    assert "FX_GRID_INCOMPLETE" in report.codes
    # A gap is a warning, not an error: the value is carried forward, which is
    # a stated approximation rather than a broken identity.
    assert report.ok


# ---------------------------------------------------------------------------
# Severity is load-bearing
# ---------------------------------------------------------------------------


def test_a_missing_teilfreistellung_is_an_error_not_a_note(db: sqlite3.Connection) -> None:
    """Defaulting it to 30% would produce a confident, wrong tax number."""
    db.execute("UPDATE instrument SET teilfreistellung_pct = NULL WHERE id = 1")
    report = doctor.run(db)
    assert "TAX_CLASS_UNKNOWN" in report.codes
    assert not report.ok


def test_missing_fees_are_a_note_not_an_error(db: sqlite3.Connection) -> None:
    """The source never had them. That bounds the numbers; it does not break them."""
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, gross_amount_eur, fx_rate, flow_class, fees_known) "
        "VALUES (1, 1, 'BUY', '2026-01-02', 'EUR', 10, 500, 500, 1, 'INVESTMENT', 0)"
    )
    report = doctor.run(db)
    finding = next(f for f in report.findings if f.code == "FEES_UNKNOWN")
    assert finding.severity is Severity.INFO
    assert report.ok


def test_an_unresolved_sale_disposition_is_a_warning(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, gross_amount_local, gross_amount_eur, fx_rate, flow_class) "
        "VALUES (1, 1, 'SELL', '2026-02-02', 'EUR', 10, 700, 700, 1, 'INVESTMENT')"
    )
    report = doctor.run(db)
    finding = next(f for f in report.findings if f.code == "SALE_DISPOSITION_UNRESOLVED")
    assert finding.severity is Severity.WARNING
    assert report.ok


def test_checks_are_independent_so_one_failure_does_not_hide_another(
    db: sqlite3.Connection,
) -> None:
    db.execute("UPDATE position_daily SET market_value_local = '1' WHERE instrument_id = 1")
    db.execute("UPDATE portfolio_daily SET net_external_flow_eur = '0'")
    codes = doctor.run(db).codes
    assert {"MV_NOT_QTY_TIMES_PRICE", "FLOWS_DO_NOT_RECONCILE"} <= codes
