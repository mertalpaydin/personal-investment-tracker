"""The example corpus is the parser's test surface, so its bytes are the fixture.

These tests exist because the corpus can be destroyed silently. An editor that
strips trailing whitespace, a `.gitattributes` line that goes missing, a
well-meaning `dos2unix` -- any of them leaves every other test in the suite
passing while the thing under test has quietly become ordinary, well-behaved
CSV. Asserting on raw bytes is the only way to notice.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "data" / "example"
OPEN_CSV = EXAMPLE_DIR / "open.csv"
CLOSED_CSV = EXAMPLE_DIR / "closed.csv"
TRUTH_JSON = EXAMPLE_DIR / "truth.json"

BOM = b"\xef\xbb\xbf"
NBSP = chr(0xA0)  # U+00A0, not U+0020 -- written as chr() so it stays visible


def _load_generator():  # type: ignore[no-untyped-def]
    path = REPO_ROOT / "scripts" / "make_example_data.py"
    spec = importlib.util.spec_from_file_location("make_example_data", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_example_data"] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


@pytest.fixture(scope="module")
def truth() -> dict:  # type: ignore[type-arg]
    return json.loads(TRUTH_JSON.read_text(encoding="utf-8"))


def rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# Byte-level properties -- these are the ones that vanish silently
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "crlf"), [(OPEN_CSV, 37), (CLOSED_CSV, 8)])
def test_bom_and_crlf_survived_the_git_round_trip(path: Path, crlf: int) -> None:
    raw = path.read_bytes()
    assert raw.startswith(BOM), "UTF-8 BOM lost; `.gitattributes -text` may have been dropped"
    assert raw.count(b"\r\n") == crlf, "CRLF endings normalised to LF"
    assert b"\n" in raw and raw.count(b"\n") == crlf, "mixed or stripped line endings"


@pytest.mark.parametrize(("path", "count"), [(OPEN_CSV, 36), (CLOSED_CSV, 7)])
def test_every_name_ends_with_a_non_breaking_space(path: Path, count: int) -> None:
    """U+00A0, not a regular space. `strip(' ')` and SQL TRIM() both miss it."""
    data = rows(path)
    assert len(data) == count
    assert all(row["Name"].endswith(NBSP) for row in data)


def test_utf8_sig_is_required_to_read_the_header() -> None:
    """Plain utf-8 leaves a BOM glued to the first column name."""
    naive = OPEN_CSV.read_text(encoding="utf-8")
    assert naive.startswith("﻿Name")
    assert OPEN_CSV.read_text(encoding="utf-8-sig").startswith("Name,")


def test_turkish_characters_are_intact() -> None:
    names = {row["Name"] for row in rows(OPEN_CSV)}
    assert any("Işık" in n for n in names), "U+0130/U+0131 mangled"
    assert any("Zümrüt" in n and "Altın" in n for n in names), "U+00FC/U+0131 mangled"


# ---------------------------------------------------------------------------
# Formatting conventions
# ---------------------------------------------------------------------------


def test_euro_takes_a_space_and_lira_never_does() -> None:
    """A single regex matching glyph-then-whitespace drops every lira value.

    Codepoints are built with chr() on purpose. The distinction under test is
    U+0020 versus U+00A0 after the glyph, and written literally those two are
    indistinguishable on screen -- the test would read as two identical lines
    that a later reader "tidies up", silently deleting the assertion.
    """
    raw = OPEN_CSV.read_text(encoding="utf-8-sig")
    euro, lira = chr(0x20AC), chr(0x20BA)
    space, nbsp = chr(0x20), chr(0xA0)

    euro_at = [i for i, ch in enumerate(raw) if ch == euro]
    lira_at = [i for i, ch in enumerate(raw) if ch == lira]
    assert euro_at and lira_at, "corpus must contain both currencies"

    assert all(raw[i + 1] == space for i in euro_at), "euro glyph must be followed by U+0020"
    assert all(raw[i + 1] not in (space, nbsp) for i in lira_at), "lira takes no separator"


def test_minus_sign_precedes_the_currency_glyph() -> None:
    raw = CLOSED_CSV.read_text(encoding="utf-8-sig")
    assert "-€ " in raw
    assert "€ -" not in raw


def test_whole_amounts_are_rendered_without_decimals() -> None:
    """`€ 125` means 125.00; a fixed-precision parser must not assume 2dp."""
    values = {row["Net P/L"] for row in rows(CLOSED_CSV)}
    assert "€ 125" in values
    assert "-€ 161" in values


def test_prices_carry_no_currency_glyph_but_do_use_thousands_separators() -> None:
    crypto = next(r for r in rows(CLOSED_CSV) if r["Symbol"] == "ZEC/EUR")
    assert crypto["Open Price"] == "91,204.70"
    assert crypto["Close Price"] == "54,118.30"
    assert "€" not in crypto["Open Price"]


def test_a_symbol_containing_a_slash_is_present() -> None:
    assert any(r["Symbol"] == "ZEC/EUR" for r in rows(CLOSED_CSV))


def test_an_ambiguous_us_date_is_present() -> None:
    """Day <= 12, so a dayfirst parser shifts it by two months without erroring."""
    dates = {r["Open Date"] for r in rows(OPEN_CSV)}
    assert "03/05/2026" in dates


def test_type_is_always_buy_even_for_closed_positions() -> None:
    """Closed rows are lot states, not events. There is no sell side to read."""
    assert {r["Type"] for r in rows(OPEN_CSV)} == {"BUY"}
    assert {r["Type"] for r in rows(CLOSED_CSV)} == {"BUY"}


# ---------------------------------------------------------------------------
# Internal identities -- if these fail, tests asserting on them test noise
# ---------------------------------------------------------------------------


def money(text: str) -> Decimal:
    negative = text.lstrip().startswith("-")
    body = text.lstrip("-").lstrip("€₺").strip().replace(",", "")
    value = Decimal(body.removesuffix("K")) * (1000 if body.endswith("K") else 1)
    return -value if negative else value


def test_market_value_equals_quantity_times_current_price() -> None:
    """Holds for every row whose quantity was printed in full."""
    checked = 0
    for row in rows(OPEN_CSV):
        if row["Amount"].endswith("K") or row["Market Value"].rstrip().endswith("K"):
            continue
        expected = Decimal(row["Amount"]) * Decimal(row["Current Price"])
        assert abs(money(row["Market Value"]) - expected) <= Decimal("0.01"), row["Symbol"]
        checked += 1
    assert checked >= 25


def test_gain_percent_matches_open_and_close_prices() -> None:
    for row in rows(CLOSED_CSV):
        open_p = Decimal(row["Open Price"].replace(",", ""))
        close_p = Decimal(row["Close Price"].replace(",", ""))
        expected = (close_p - open_p) / open_p * 100
        actual = Decimal(row["Gain%"].rstrip("%"))
        assert abs(actual - expected) <= Decimal("0.01"), row["Symbol"]


def test_foreign_currency_profit_ignores_the_trade_date_rate(truth: dict) -> None:  # type: ignore[type-arg]
    """The defining flaw the tracker exists to correct.

    Every lira lot implies the same EUR/TRY rate regardless of when it was
    bought, which is only possible if the profit column carries no currency
    effect whatsoever.
    """
    fx = Decimal(truth["fx_try_per_eur"])
    implied: list[Decimal] = []
    for row in rows(OPEN_CSV):
        if not row["Market Value"].lstrip().startswith("₺") or row["Amount"].endswith("K"):
            continue
        qty = Decimal(row["Amount"])
        gain_local = qty * (Decimal(row["Current Price"]) - Decimal(row["Open Price"]))
        implied.append(gain_local / money(row["Net P/L"]))
    assert len(implied) >= 8
    for rate in implied:
        assert abs(rate - fx) < Decimal("0.05"), f"implied {rate} != spot {fx}"


# ---------------------------------------------------------------------------
# The K-abbreviation landmine, against the generator's own oracle
# ---------------------------------------------------------------------------


def test_four_lots_are_abbreviated_two_recoverable_two_not(truth: dict) -> None:  # type: ignore[type-arg]
    abbreviated = [lot for lot in truth["lots"] if lot["abbreviated"]]
    assert len(abbreviated) == 4
    recoverable = [lot for lot in abbreviated if lot["recoverable_from_market_value"]]
    assert len(recoverable) == 2, "both recoverable and unrecoverable cases must be present"


def test_declared_intervals_contain_the_true_quantity(truth: dict) -> None:  # type: ignore[type-arg]
    for lot in truth["lots"]:
        if not lot["abbreviated"]:
            continue
        lo, hi = (Decimal(x) for x in lot["amount_interval"])
        assert lo <= Decimal(lot["true_remaining_quantity"]) <= hi, lot["key"]


def test_recoverable_lots_recover_exactly_and_beat_the_printed_value(truth: dict) -> None:  # type: ignore[type-arg]
    """Recovery is not merely better than the printed value -- it is exact.

    And the printed value is genuinely wrong, which is why preferring it would
    be a real error rather than a stylistic one.
    """
    prices = {r["Symbol"]: Decimal(r["Current Price"]) for r in rows(OPEN_CSV)}
    checked = 0
    for lot in truth["lots"]:
        if not (lot["abbreviated"] and lot["recoverable_from_market_value"]):
            continue
        true_q = Decimal(lot["true_remaining_quantity"])
        market_value = money(lot["rendered_market_value"])
        recovered = (market_value / prices[lot["symbol"]]).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        assert recovered == true_q, lot["key"]

        printed = Decimal(lot["rendered_amount"].removesuffix("K")) * 1000
        assert printed != true_q, "printed value should be wrong, else the case is uninteresting"
        checked += 1
    assert checked == 2


def test_unrecoverable_lots_have_an_abbreviated_market_value(truth: dict) -> None:  # type: ignore[type-arg]
    """The reason recovery fails, stated as a property rather than a claim."""
    for lot in truth["lots"]:
        if lot["abbreviated"] and not lot["recoverable_from_market_value"]:
            assert lot["rendered_market_value"].rstrip().endswith("K")


# ---------------------------------------------------------------------------
# Lot reconstruction -- the case that silently corrupts everything downstream
# ---------------------------------------------------------------------------


def test_a_lot_split_across_both_files_is_present() -> None:
    """ZOVN 06/13/2025: the real purchase is open + closed, in neither file alone."""
    open_row = next(
        r for r in rows(OPEN_CSV) if r["Symbol"] == "ZOVN" and r["Open Date"] == "06/13/2025"
    )
    closed_row = next(
        r for r in rows(CLOSED_CSV) if r["Symbol"] == "ZOVN" and r["Open Date"] == "06/13/2025"
    )
    assert open_row["Open Price"] == closed_row["Open Price"]
    total = Decimal(open_row["Amount"]) + Decimal(closed_row["Amount"])
    assert total == Decimal("203.8874")


def test_one_lot_with_two_disposals_appears_only_in_the_closed_file() -> None:
    """ZGLD 05/07/2025: two closed rows, no open row, one purchase of 501.2193."""
    key = ("ZGLD", "05/07/2025")
    closed = [r for r in rows(CLOSED_CSV) if (r["Symbol"], r["Open Date"]) == key]
    assert len(closed) == 2, "the multi-disposal case is the one that breaks naive importers"
    assert len({r["Open Price"] for r in closed}) == 1
    assert not [r for r in rows(OPEN_CSV) if (r["Symbol"], r["Open Date"]) == key]
    assert sum(Decimal(r["Amount"]) for r in closed) == Decimal("501.2193")


def test_fully_closed_positions_are_absent_from_the_open_file() -> None:
    open_symbols = {r["Symbol"] for r in rows(OPEN_CSV)}
    assert "ZEC/EUR" not in open_symbols
    gbex_closed = next(r for r in rows(CLOSED_CSV) if r["Symbol"] == "GBEX")
    assert gbex_closed["Open Date"] not in {
        r["Open Date"] for r in rows(OPEN_CSV) if r["Symbol"] == "GBEX"
    }


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_regeneration_is_byte_identical() -> None:
    """Same input, same bytes -- the property CI's data-drift job relies on."""
    artefacts = gen.build()
    for name, expected in artefacts.items():
        assert (EXAMPLE_DIR / name).read_bytes() == expected, f"{name} is stale or hand-edited"


def test_check_mode_reports_success() -> None:
    assert gen.main(["--check"]) == 0


def test_truth_symbols_cover_every_symbol_in_both_files(truth: dict) -> None:  # type: ignore[type-arg]
    """The PII guard's allowlist is this list, so a gap would weaken the guard."""
    declared = set(truth["symbols"])
    used = {r["Symbol"] for r in rows(OPEN_CSV)} | {r["Symbol"] for r in rows(CLOSED_CSV)}
    assert used <= declared
