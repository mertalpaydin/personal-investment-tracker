"""German capital-gains treatment, against hand-computed cases.

Every expected figure below was worked out by hand from the statute before the
code existed, and is asserted to the cent. These are the twelve fixtures the
original brief specified, plus the ones the real data turned out to need.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.core import tax
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'Single', 0)")
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (2, 'Joint', 1)")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'D', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (2, 2, 'D2', 'DEPOT', 'EUR')"
    )
    conn.commit()
    return conn


def instrument(
    conn: sqlite3.Connection,
    key: str,
    *,
    asset_class: str = "ETF",
    tf: str | None = "0.30",
    is_fund: int = 1,
    regime: str = "DE_INVESTMENT",
) -> int:
    cursor = conn.execute(
        "INSERT INTO instrument (import_key, name, asset_class, currency, "
        "teilfreistellung_pct, is_fund, tax_regime) VALUES (?, ?, ?, 'EUR', ?, ?, ?)",
        (key, key, asset_class, tf, is_fund, regime),
    )
    return int(cursor.lastrowid or 0)


def disposal(
    conn: sqlite3.Connection,
    instrument_id: int,
    *,
    gain: str,
    on: str = "2026-06-01",
    vap_offset: str = "0",
    account_id: int = 1,
    holding_days: int = 400,
) -> None:
    txn = conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "quantity, flow_class) VALUES (?, ?, 'SELL', ?, 'EUR', 1, 'INVESTMENT')",
        (account_id, instrument_id, on),
    )
    lot = conn.execute(
        "INSERT INTO lot (account_id, instrument_id, open_txn_id, open_date, qty_opened, "
        "qty_remaining, currency, cost_total_local, cost_per_unit_local) "
        "VALUES (?, ?, ?, '2024-01-01', 1, 0, 'EUR', 1, 1)",
        (account_id, instrument_id, txn.lastrowid),
    )
    conn.execute(
        "INSERT INTO lot_disposal (sell_txn_id, lot_id, qty, currency, proceeds_local, "
        "cost_basis_local, gross_gain_eur, vap_offset_eur, taxable_gain_eur, holding_days) "
        "VALUES (?, ?, 1, 'EUR', 0, 0, ?, ?, NULL, ?)",
        (txn.lastrowid, lot.lastrowid, gain, vap_offset, holding_days),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixture 2 and 3 -- Teilfreistellung, symmetric
# ---------------------------------------------------------------------------


def test_equity_fund_gain_is_exempt_by_thirty_percent(db: sqlite3.Connection) -> None:
    """EUR 1,000 gross gain, 30% partial exemption -> EUR 700 taxable."""
    fund = instrument(db, "EQ", tf="0.30")
    disposal(db, fund, gain="1000")
    result = tax.compute_disposal_tax(db)[0]
    assert result.teilfreistellung_eur == D("300.00")
    assert result.taxable_gain_eur == D("700.00")


def test_equity_fund_loss_is_reduced_by_the_same_thirty_percent(
    db: sqlite3.Connection,
) -> None:
    """The rule cuts both ways.

    Exempting 30% of a gain while deducting 100% of a loss would be a pleasant
    arithmetic error and an incorrect return.
    """
    fund = instrument(db, "EQ", tf="0.30")
    disposal(db, fund, gain="-1000")
    result = tax.compute_disposal_tax(db)[0]
    assert result.taxable_gain_eur == D("-700.00")
    assert result.pot == "SONSTIGE"


@pytest.mark.parametrize(
    ("rate", "expected"),
    [("0.30", "700.00"), ("0.15", "850.00"), ("0.00", "1000.00")],
)
def test_each_exemption_band(db: sqlite3.Connection, rate: str, expected: str) -> None:
    fund = instrument(db, "F", tf=rate)
    disposal(db, fund, gain="1000")
    assert tax.compute_disposal_tax(db)[0].taxable_gain_eur == D(expected)


def test_an_unresolved_exemption_refuses_to_compute(db: sqlite3.Connection) -> None:
    """0.30 on a bond fund and 0.00 on an equity fund are both wrong, and both
    produce a confident number."""
    fund = instrument(db, "UNKNOWN", tf=None)
    disposal(db, fund, gain="1000")
    with pytest.raises(tax.TaxDataMissing, match="Teilfreistellung is unresolved"):
        tax.compute_disposal_tax(db)


# ---------------------------------------------------------------------------
# Fixtures 4 to 7 -- Vorabpauschale
# ---------------------------------------------------------------------------


def test_vorabpauschale_on_a_fund_that_rose() -> None:
    """EUR 50,000 on 1 January, Basiszins 3.20%, accumulating, no distributions.

    Basisertrag = 50,000 x 0.0320 x 0.7 = 1,120; taxable at 30% exemption = 784.
    """
    _raw, _cap, basisertrag, gross, taxable = tax.vorabpauschale_for(
        jan_first_price=D("100"),
        year_end_price=D("130"),
        quantity=D("500"),
        basiszins_rate=D("0.0320"),
        teilfreistellung_pct=D("0.30"),
    )
    assert basisertrag == D("1120.00")
    assert gross == D("1120.00")
    assert taxable == D("784.00")


def test_a_fund_that_fell_owes_nothing() -> None:
    """However high the reference rate: the charge is capped at the rise."""
    *_, gross, taxable = tax.vorabpauschale_for(
        jan_first_price=D("100"),
        year_end_price=D("90"),
        quantity=D("500"),
        basiszins_rate=D("0.0320"),
        teilfreistellung_pct=D("0.30"),
    )
    assert gross == D("0.00")
    assert taxable == D("0.00")


def test_the_cap_binds_when_the_rise_is_smaller_than_the_basisertrag() -> None:
    """Up only EUR 300 on a 50,000 holding: the charge is the 300, not 1,120."""
    raw, cap, basisertrag, gross, _taxable = tax.vorabpauschale_for(
        jan_first_price=D("100"),
        year_end_price=D("100.60"),
        quantity=D("500"),
        basiszins_rate=D("0.0320"),
        teilfreistellung_pct=D("0.00"),
    )
    assert raw == D("1120.00")
    assert cap == D("300.00")
    assert basisertrag == D("300.00")
    assert gross == D("300.00")


def test_a_part_year_holding_is_pro_rated_by_whole_months() -> None:
    """Bought 10 July: six whole months elapsed first, so six twelfths."""
    *_, gross, _taxable = tax.vorabpauschale_for(
        jan_first_price=D("100"),
        year_end_price=D("130"),
        quantity=D("500"),
        basiszins_rate=D("0.0320"),
        teilfreistellung_pct=D("0.00"),
        full_months_before=6,
    )
    assert gross == D("560.00")  # 1,120 x 6/12


def test_a_distribution_reduces_the_charge() -> None:
    """The Vorabpauschale only tops up to the notional minimum return."""
    *_, gross, _taxable = tax.vorabpauschale_for(
        jan_first_price=D("100"),
        year_end_price=D("130"),
        quantity=D("500"),
        basiszins_rate=D("0.0320"),
        teilfreistellung_pct=D("0.00"),
        distributions_eur=D("400"),
    )
    assert gross == D("720.00")  # 1,120 - 400


def test_a_distribution_exceeding_the_basisertrag_leaves_nothing() -> None:
    *_, gross, _taxable = tax.vorabpauschale_for(
        jan_first_price=D("100"),
        year_end_price=D("130"),
        quantity=D("500"),
        basiszins_rate=D("0.0320"),
        teilfreistellung_pct=D("0.00"),
        distributions_eur=D("2000"),
    )
    assert gross == D("0.00")


# ---------------------------------------------------------------------------
# Fixture 8 -- the Vorabpauschale offset on sale
# ---------------------------------------------------------------------------


def test_previously_taxed_vorabpauschale_is_deducted_from_the_eventual_gain(
    db: sqlite3.Connection,
) -> None:
    """A lot carrying EUR 784 of taxed Vorabpauschale, later sold at EUR 2,000.

    Without the offset the same 784 is taxed twice -- the field most home-built
    trackers omit.
    """
    fund = instrument(db, "EQ", tf="0.30")
    disposal(db, fund, gain="2000", vap_offset="784")
    result = tax.compute_disposal_tax(db)[0]
    assert result.vap_offset_eur == D("784.00")
    # 2,000 - 784 = 1,216, then 30% exempt.
    assert result.taxable_gain_eur == D("851.20")


def test_the_offset_never_turns_a_gain_into_a_loss(db: sqlite3.Connection) -> None:
    fund = instrument(db, "EQ", tf="0.00")
    disposal(db, fund, gain="500", vap_offset="800")
    result = tax.compute_disposal_tax(db)[0]
    assert result.vap_offset_eur == D("500.00")
    assert result.taxable_gain_eur == D("0.00")


def test_the_offset_never_deepens_a_loss(db: sqlite3.Connection) -> None:
    fund = instrument(db, "EQ", tf="0.00")
    disposal(db, fund, gain="-500", vap_offset="800")
    result = tax.compute_disposal_tax(db)[0]
    assert result.vap_offset_eur == D("0.00")
    assert result.taxable_gain_eur == D("-500.00")


# ---------------------------------------------------------------------------
# Fixture 9 -- loss pots are segregated
# ---------------------------------------------------------------------------


def test_a_share_loss_cannot_offset_a_fund_gain(db: sqlite3.Connection) -> None:
    """§20 VI EStG. Merging the pots would understate tax."""
    share = instrument(db, "SHARE", asset_class="STOCK", tf="0.00", is_fund=0)
    fund = instrument(db, "FUND", tf="0.00")
    disposal(db, share, gain="-1000")
    disposal(db, fund, gain="1000")

    state = tax.allowance_state(db, person_id=1, tax_year=2026)
    assert state.gross_gains_eur == D("1000.00")
    assert state.losses_offset_eur == D("0.00")  # the share loss cannot be used here
    assert state.carried_forward.aktien == D("1000.00")


def test_a_share_loss_does_offset_a_share_gain(db: sqlite3.Connection) -> None:
    share = instrument(db, "S1", asset_class="STOCK", tf="0.00", is_fund=0)
    other = instrument(db, "S2", asset_class="STOCK", tf="0.00", is_fund=0)
    disposal(db, share, gain="-400")
    disposal(db, other, gain="1000")

    state = tax.allowance_state(db, person_id=1, tax_year=2026)
    assert state.losses_offset_eur == D("400.00")
    assert state.after_losses_eur == D("600.00")


def test_a_fund_loss_offsets_a_fund_gain(db: sqlite3.Connection) -> None:
    a = instrument(db, "F1", tf="0.00")
    b = instrument(db, "F2", tf="0.00")
    disposal(db, a, gain="-300")
    disposal(db, b, gain="1000")
    assert tax.allowance_state(db, person_id=1, tax_year=2026).after_losses_eur == D("700.00")


# ---------------------------------------------------------------------------
# Fixtures 10 and 12 -- allowance
# ---------------------------------------------------------------------------


def test_the_allowance_is_applied_after_losses_not_before(db: sqlite3.Connection) -> None:
    """Applying it first would waste it against income the losses cancel anyway."""
    a = instrument(db, "F1", tf="0.00")
    b = instrument(db, "F2", tf="0.00")
    disposal(db, a, gain="-500")
    disposal(db, b, gain="2000")

    state = tax.allowance_state(db, person_id=1, tax_year=2026)
    assert state.after_losses_eur == D("1500.00")
    assert state.consumed_eur == D("1000.00")  # single person
    assert state.taxable_after_allowance_eur == D("500.00")


def test_a_jointly_assessed_couple_get_twice_the_allowance(db: sqlite3.Connection) -> None:
    fund = instrument(db, "F", tf="0.00")
    disposal(db, fund, gain="3000", account_id=2)
    state = tax.allowance_state(db, person_id=2, tax_year=2026)
    assert state.statutory_cap_eur == D("2000")
    assert state.taxable_after_allowance_eur == D("1000.00")


def test_the_allowance_is_never_exceeded(db: sqlite3.Connection) -> None:
    fund = instrument(db, "F", tf="0.00")
    disposal(db, fund, gain="200")
    state = tax.allowance_state(db, person_id=1, tax_year=2026)
    assert state.consumed_eur == D("200.00")
    assert state.remaining_eur == D("800.00")
    assert state.taxable_after_allowance_eur == D("0.00")
    assert state.estimated_tax_eur == D("0.00")


def test_tax_is_charged_at_the_combined_rate(db: sqlite3.Connection) -> None:
    """25% plus 5.5% solidarity surcharge = 26.375%."""
    fund = instrument(db, "F", tf="0.00")
    disposal(db, fund, gain="2000")
    state = tax.allowance_state(db, person_id=1, tax_year=2026)
    assert state.taxable_after_allowance_eur == D("1000.00")
    assert state.estimated_tax_eur == D("263.75")


def test_losses_carry_forward_when_unused(db: sqlite3.Connection) -> None:
    fund = instrument(db, "F", tf="0.00")
    disposal(db, fund, gain="-1500")
    state = tax.allowance_state(db, person_id=1, tax_year=2026)
    assert state.carried_forward.sonstige == D("1500.00")


def test_a_carried_loss_offsets_a_later_year(db: sqlite3.Connection) -> None:
    fund = instrument(db, "F", tf="0.00")
    disposal(db, fund, gain="1000", on="2026-03-01")
    state = tax.allowance_state(
        db, person_id=1, tax_year=2026, carried_in=tax.LossPots(sonstige=D("600"))
    )
    assert state.losses_offset_eur == D("600.00")
    assert state.after_losses_eur == D("400.00")


def test_allowances_never_cross_between_people(db: sqlite3.Connection) -> None:
    fund = instrument(db, "F", tf="0.00")
    disposal(db, fund, gain="5000", account_id=1)
    other = tax.allowance_state(db, person_id=2, tax_year=2026)
    assert other.gross_gains_eur == D("0.00")
    assert other.remaining_eur == D("2000.00")


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_scope_is_decided_by_regime_alone(db: sqlite3.Connection) -> None:
    """No currency test exists in this module, so a foreign-currency holding is
    in scope if its regime says so."""
    foreign = instrument(db, "OUT", regime="OUT_OF_SCOPE")
    crypto = instrument(db, "CRYPTO", regime="DE_PRIVATE_SALE", asset_class="CRYPTO", is_fund=0)
    normal = instrument(db, "IN")
    assert not tax.in_scope(db, foreign)
    assert not tax.in_scope(db, crypto)
    assert tax.in_scope(db, normal)


def test_out_of_scope_disposals_are_excluded_from_the_figure(
    db: sqlite3.Connection,
) -> None:
    crypto = instrument(db, "BTC", regime="DE_PRIVATE_SALE", asset_class="CRYPTO", is_fund=0)
    disposal(db, crypto, gain="-2000")
    state = tax.allowance_state(db, person_id=1, tax_year=2026)
    # A private-disposal loss may only offset other private-disposal gains, so
    # it must not appear in the capital-income pots at all.
    assert state.carried_forward.sonstige == D("0.00")
    assert state.carried_forward.aktien == D("0.00")


def test_out_of_scope_holdings_are_reported_not_hidden(db: sqlite3.Connection) -> None:
    """A tax figure that silently omits part of the portfolio is more dangerous
    than no figure, because it looks complete."""
    instrument(db, "TRY1", regime="OUT_OF_SCOPE")
    instrument(db, "BTC", regime="DE_PRIVATE_SALE", asset_class="CRYPTO", is_fund=0)
    instrument(db, "UNRESOLVED", tf=None)

    report = tax.out_of_scope_report(db, tax_year=2026, as_of=date(2026, 8, 14))
    assert set(report.instruments) == {"TRY1", "BTC", "UNRESOLVED"}
    assert "unresolved" in report.reasons["UNRESOLVED"]
    assert not report.is_empty


def test_a_fully_in_scope_portfolio_reports_nothing_excluded(
    db: sqlite3.Connection,
) -> None:
    instrument(db, "F", tf="0.30")
    assert tax.out_of_scope_report(db, tax_year=2026, as_of=date(2026, 8, 14)).is_empty


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_the_seeded_reference_rates_are_present(db: sqlite3.Connection) -> None:
    assert tax.basiszins(db, 2026) == D("0.0320")
    assert tax.basiszins(db, 2025) == D("0.0253")


def test_a_missing_reference_rate_refuses_rather_than_guessing(
    db: sqlite3.Connection,
) -> None:
    with pytest.raises(tax.TaxDataMissing, match="no Basiszins"):
        tax.basiszins(db, 2099)


def test_the_combined_rate_is_correct() -> None:
    assert D("0.26375") == tax.COMBINED_RATE
