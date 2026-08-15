"""Parsing, lot reconstruction and import behaviour.

Reconstruction is tested against the generator's own oracle rather than against
hand-copied numbers, so the assertions cannot drift away from the corpus.
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
from portfolio_tracker.importers.investing_com import parse
from portfolio_tracker.importers.investing_com.importer import import_positions
from portfolio_tracker.importers.investing_com.normalize import load_closed, load_open
from portfolio_tracker.importers.investing_com.reconcile import (
    QtyConfidence,
    build_lots,
    lot_key,
    refine_quantity,
)
from portfolio_tracker.importers.overrides import OverrideError, Overrides
from portfolio_tracker.importers.overrides import load as load_overrides
from portfolio_tracker.importers.report import ImportReport

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "data" / "example"
OPEN_CSV = EXAMPLE / "open.csv"
CLOSED_CSV = EXAMPLE / "closed.csv"


@pytest.fixture(scope="module")
def truth() -> dict:  # type: ignore[type-arg]
    return json.loads((EXAMPLE / "truth.json").read_text(encoding="utf-8"))


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Scalar parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "amount", "currency"),
    [
        ("€ 507.08", Decimal("507.08"), "EUR"),
        ("€ 10,697.10", Decimal("10697.10"), "EUR"),
        ("-€ 2,087.72", Decimal("-2087.72"), "EUR"),
        ("€ 125", Decimal("125"), "EUR"),  # whole values lose the decimals
        ("-€ 161", Decimal("-161"), "EUR"),
        ("₺86,201.21", Decimal("86201.21"), "TRY"),  # lira takes no space
        ("₺258.51K", Decimal("258510"), "TRY"),
    ],
)
def test_parse_money(raw: str, amount: Decimal, currency: str) -> None:
    parsed = parse.parse_money(raw)
    assert parsed.amount == amount
    assert parsed.currency == currency


def test_money_sign_precedes_the_glyph_and_inverts_the_interval() -> None:
    parsed = parse.parse_money("-₺258.51K")
    assert parsed.amount == Decimal("-258510")
    assert parsed.interval.lo < parsed.amount < parsed.interval.hi


@pytest.mark.parametrize(
    ("raw", "value", "abbreviated", "width"),
    [
        ("35.9144", Decimal("35.9144"), False, Decimal(0)),
        ("19", Decimal("19"), False, Decimal(0)),
        ("0.04821637", Decimal("0.04821637"), False, Decimal(0)),
        ("412.3K", Decimal("412300"), True, Decimal(100)),  # one decimal -> +/-50
        ("137.48K", Decimal("137480"), True, Decimal(10)),  # two decimals -> +/-5
    ],
)
def test_parse_quantity_carries_its_own_uncertainty(
    raw: str, value: Decimal, abbreviated: bool, width: Decimal
) -> None:
    parsed = parse.parse_quantity(raw)
    assert parsed.value == value
    assert parsed.abbreviated is abbreviated
    assert parsed.interval.width == width


def test_us_dates_are_pinned_not_locale_driven() -> None:
    """`03/05/2026` is 5 March; a day-first parser would read it as 3 May."""
    assert parse.parse_us_date("03/05/2026") == date(2026, 3, 5)
    assert parse.parse_us_date("10/27/2025") == date(2025, 10, 27)


def test_a_day_first_date_raises_rather_than_falling_back() -> None:
    with pytest.raises(parse.ParseError):
        parse.parse_us_date("13/01/2025")


def test_prices_reject_a_currency_glyph() -> None:
    """A glyph here means the column layout changed, which must not pass."""
    assert parse.parse_number("91,204.70") == Decimal("91204.70")
    with pytest.raises(parse.ParseError, match="layout may have changed"):
        parse.parse_number("€ 91,204.70")


def test_percent() -> None:
    assert parse.parse_percent("-40.66%") == Decimal("-0.4066")
    assert parse.parse_percent("30.28%") == Decimal("0.3028")


def test_clean_text_removes_the_invisible_characters() -> None:
    assert parse.clean_text("Acme Robotics C" + chr(0xA0)) == "Acme Robotics C"
    assert parse.clean_text(chr(0xFEFF) + "Name") == "Name"


@pytest.mark.parametrize("bad", ["", "abc", "€", "1.2.3", "12/2025"])
def test_unparseable_values_raise(bad: str) -> None:
    with pytest.raises(parse.ParseError):
        parse.parse_money(bad)


# ---------------------------------------------------------------------------
# Quantity refinement
# ---------------------------------------------------------------------------


def test_exact_quantities_pass_through() -> None:
    result = refine_quantity(
        Decimal("35.9144"), parse.Interval(Decimal("35.9144"), Decimal("35.9144")), False
    )
    assert result.confidence is QtyConfidence.EXACT


def test_an_override_always_wins() -> None:
    result = refine_quantity(
        Decimal("412300"),
        parse.Interval(Decimal("412250"), Decimal("412350")),
        True,
        override=Decimal("412304"),
    )
    assert result.confidence is QtyConfidence.OVERRIDE
    assert result.value == Decimal("412304")


def test_derivation_recovers_the_exact_quantity(truth: dict) -> None:  # type: ignore[type-arg]
    """And the printed value it replaces is genuinely wrong.

    That is what makes preferring the derivation a correctness fix rather than
    a stylistic preference.
    """
    prices = {r.symbol: r.current_price for r in load_open(OPEN_CSV)}
    checked = 0
    for lot in truth["lots"]:
        if not (lot["abbreviated"] and lot["recoverable_from_market_value"]):
            continue
        rendered = lot["rendered_market_value"]
        market_value = Decimal(rendered.lstrip("₺€ ").replace(",", ""))
        printed = Decimal(lot["rendered_amount"].removesuffix("K")) * 1000

        result = refine_quantity(
            printed,
            parse.parse_quantity(lot["rendered_amount"]).interval,
            True,
            market_value=market_value,
            market_value_abbreviated=False,
            current_price=prices[lot["symbol"]],
        )
        assert result.confidence is QtyConfidence.DERIVED
        assert result.value == Decimal(lot["true_remaining_quantity"])
        assert result.value != printed, "the printed value should be wrong"
        checked += 1
    assert checked == 2


def test_unrecoverable_quantities_stay_estimated_with_a_narrowed_interval(
    truth: dict,  # type: ignore[type-arg]
) -> None:
    prices = {r.symbol: r.current_price for r in load_open(OPEN_CSV)}
    for lot in truth["lots"]:
        if not (lot["abbreviated"] and not lot["recoverable_from_market_value"]):
            continue
        printed_q = parse.parse_quantity(lot["rendered_amount"])
        market = parse.parse_money(lot["rendered_market_value"])
        result = refine_quantity(
            printed_q.value,
            printed_q.interval,
            True,
            market_value=market.amount,
            market_value_abbreviated=True,
            market_value_interval=market.interval,
            current_price=prices[lot["symbol"]],
        )
        assert result.confidence is QtyConfidence.ESTIMATED
        assert result.interval.contains(Decimal(lot["true_remaining_quantity"]))
        assert result.interval.width < printed_q.interval.width, "should have narrowed"


# ---------------------------------------------------------------------------
# Lot reconstruction -- the cases that silently corrupt a naive importer
# ---------------------------------------------------------------------------


@pytest.fixture
def lots():  # type: ignore[no-untyped-def]
    report = ImportReport()
    return build_lots(load_open(OPEN_CSV), load_closed(CLOSED_CSV), report), report


def test_43_rows_become_40_purchases_and_7_disposals(lots) -> None:  # type: ignore[no-untyped-def]
    built, _ = lots
    assert len(built) == 40
    assert sum(len(lot.disposals) for lot in built) == 7


def test_a_lot_split_across_both_files_is_summed(lots) -> None:  # type: ignore[no-untyped-def]
    built, report = lots
    lot = next(x for x in built if x.key == "ZOVN:2025-06-13:101.88")
    assert lot.quantity.value == Decimal("203.8874")
    assert lot.remaining == Decimal("131.4756")
    assert "PARTIAL_CLOSE" in report.codes


def test_one_lot_with_two_disposals_and_no_open_row(lots) -> None:  # type: ignore[no-untyped-def]
    """The case a naive importer gets wrong with no error anywhere."""
    built, report = lots
    lot = next(x for x in built if x.key == "ZGLD:2025-05-07:19.88")
    assert lot.quantity.value == Decimal("501.2193")
    assert lot.remaining == Decimal(0)
    assert len(lot.disposals) == 2
    assert "MULTI_DISPOSAL" in report.codes
    assert "FULLY_CLOSED" in report.codes


def test_currency_comes_from_another_lot_of_the_same_symbol(lots) -> None:  # type: ignore[no-untyped-def]
    """The closed file has no currency column, but GBEX also has an open lot."""
    built, report = lots
    gbex = [x for x in built if x.symbol == "GBEX"]
    assert {x.currency for x in gbex} == {"EUR"}
    assumed = {f.detail.get("key") for f in report.of("CURRENCY_ASSUMED")}
    assert not any(k and k.startswith("GBEX") for k in assumed)
    # ZEC/EUR appears nowhere in the open file, so it genuinely is a guess.
    assert any(k and k.startswith("ZEC/EUR") for k in assumed)


def test_lot_key_normalises_trailing_zeros() -> None:
    assert lot_key("X", date(2025, 1, 1), Decimal("19.880")) == lot_key(
        "X", date(2025, 1, 1), Decimal("19.88")
    )


def test_sell_external_ids_are_unique_within_a_multi_disposal_lot(lots) -> None:  # type: ignore[no-untyped-def]
    built, _ = lots
    lot = next(x for x in built if x.key == "ZGLD:2025-05-07:19.88")
    ids = {lot.sell_external_id(d) for d in lot.disposals}
    assert len(ids) == len(lot.disposals)


def test_buy_external_id_excludes_quantity(lots) -> None:  # type: ignore[no-untyped-def]
    """An open lot's printed quantity shrinks whenever it is partially sold, so
    a quantity-keyed id would mint a fresh row and duplicate the position."""
    built, _ = lots
    lot = next(x for x in built if x.key == "ZOVN:2025-06-13:101.88")
    assert "203.8874" not in lot.buy_external_id
    assert "131.4756" not in lot.buy_external_id


# ---------------------------------------------------------------------------
# End-to-end import
# ---------------------------------------------------------------------------


def _import(db: sqlite3.Connection, **kwargs) -> ImportReport:  # type: ignore[no-untyped-def]
    return import_positions(db, OPEN_CSV, CLOSED_CSV, account_id=1, **kwargs)


def test_import_writes_the_expected_rows(db: sqlite3.Connection) -> None:
    report = _import(db)
    assert report.ok
    assert report.buys_inserted == 40
    assert report.sells_inserted == 7
    assert db.execute("SELECT count(*) n FROM transaction_").fetchone()["n"] == 47


def test_reimport_is_idempotent(db: sqlite3.Connection) -> None:
    _import(db)
    before = db.execute("SELECT count(*) n FROM transaction_").fetchone()["n"]
    second = _import(db)
    assert second.buys_inserted == 0
    assert second.sells_inserted == 0
    assert second.unchanged == 40
    assert db.execute("SELECT count(*) n FROM transaction_").fetchone()["n"] == before


def test_dry_run_writes_nothing(db: sqlite3.Connection) -> None:
    report = _import(db, dry_run=True)
    assert report.buys_inserted == 40, "the report still says what would happen"
    assert db.execute("SELECT count(*) n FROM transaction_").fetchone()["n"] == 0


def test_strict_mode_rolls_back_on_warnings(db: sqlite3.Connection) -> None:
    report = _import(db, strict=True)
    assert report.warnings
    assert db.execute("SELECT count(*) n FROM transaction_").fetchone()["n"] == 0


def test_crypto_is_classified_as_a_private_disposal(db: sqlite3.Connection) -> None:
    """Section 23 EStG, not section 20. Its losses may only offset other
    private-disposal gains -- landing it in the ordinary loss pot would
    understate tax."""
    report = _import(db)
    row = db.execute(
        "SELECT asset_class, tax_regime FROM instrument WHERE import_key = 'ZEC/EUR'"
    ).fetchone()
    assert row["asset_class"] == "CRYPTO"
    assert row["tax_regime"] == "DE_PRIVATE_SALE"
    assert "ASSET_CLASS_OUT_OF_SCOPE" in report.codes


def test_foreign_funds_get_no_teilfreistellung_guess(db: sqlite3.Connection) -> None:
    report = _import(db)
    rows = db.execute(
        "SELECT teilfreistellung_pct FROM instrument WHERE currency = 'TRY'"
    ).fetchall()
    assert rows
    assert all(r["teilfreistellung_pct"] is None for r in rows)
    assert "TAX_CLASS_UNKNOWN" in report.codes


def test_foreign_rows_leave_eur_amounts_null_rather_than_fabricated(
    db: sqlite3.Connection,
) -> None:
    """A current spot rate on a two-year-old acquisition would invent a cost
    basis, and the tax engine cannot recover from that."""
    report = _import(db)
    rows = db.execute(
        "SELECT gross_amount_eur, fx_rate FROM transaction_ WHERE currency = 'TRY'"
    ).fetchall()
    assert rows
    assert all(r["gross_amount_eur"] is None and r["fx_rate"] is None for r in rows)
    assert "FX_BACKFILL_REQUIRED" in report.codes


def test_missing_fees_are_reported_not_silently_zero(db: sqlite3.Connection) -> None:
    report = _import(db)
    assert "FEES_UNKNOWN" in report.codes
    known = db.execute("SELECT count(*) n FROM transaction_ WHERE fees_known = 1").fetchone()["n"]
    assert known == 0


def test_overrides_are_applied_and_recorded(db: sqlite3.Connection) -> None:
    key = "ZTFB:2024-10-03:0.3105"
    report = _import(db, overrides=Overrides(quantities={key: Decimal("412304")}))
    assert report.ok
    row = db.execute(
        "SELECT t.quantity, t.quantity_confidence FROM transaction_ t "
        "JOIN instrument i ON i.id = t.instrument_id "
        "WHERE i.import_key = 'ZTFB' AND t.trade_date = '2024-10-03' AND t.type = 'BUY'"
    ).fetchone()
    assert row["quantity"] == Decimal("412304")
    assert row["quantity_confidence"] == "OVERRIDE"


def test_an_override_outside_the_derived_interval_is_flagged(db: sqlite3.Connection) -> None:
    """It is still applied -- the user has a statement and the importer does
    not -- but a silent disagreement would defeat the point of the report."""
    report = _import(
        db, overrides=Overrides(quantities={"ZTFB:2024-10-03:0.3105": Decimal("999999")})
    )
    assert "OVERRIDE_CONTRADICTS_DERIVED" in report.codes


def test_report_serialises_to_json(db: sqlite3.Connection) -> None:
    report = _import(db, dry_run=True)
    payload = json.loads(report.to_json())
    assert payload["lots_reconstructed"] == 40
    assert {f["code"] for f in payload["findings"]}


# ---------------------------------------------------------------------------
# Overrides file
# ---------------------------------------------------------------------------


def test_missing_override_file_is_not_an_error(tmp_path: Path) -> None:
    assert not load_overrides(tmp_path / "absent.yaml")


def test_override_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "o.yaml"
    path.write_text(
        "version: 1\nlots:\n  - key: 'A:2024-01-01:1.5'\n    quantity: 412304\n",
        encoding="utf-8",
    )
    loaded = load_overrides(path)
    assert loaded.quantities["A:2024-01-01:1.5"] == Decimal("412304")


def test_override_rejects_yaml_floats(tmp_path: Path) -> None:
    """`quantity: 0.1` parses as a float before this code ever sees it, so the
    precision is already gone. Better to refuse than to launder it."""
    path = tmp_path / "o.yaml"
    path.write_text(
        "version: 1\nlots:\n  - key: 'A:2024-01-01:1.5'\n    quantity: 0.1\n", encoding="utf-8"
    )
    with pytest.raises(OverrideError, match="quoted string"):
        load_overrides(path)


def test_override_rejects_an_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "o.yaml"
    path.write_text("version: 99\n", encoding="utf-8")
    with pytest.raises(OverrideError, match="unsupported version"):
        load_overrides(path)
