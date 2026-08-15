#!/usr/bin/env python
"""Generate the synthetic example corpus under ``data/example/``.

The project's real input is an Investing.com positions export, which cannot be
committed. This generator produces a fictional stand-in with **identical
structural properties**, so the parser can be developed and tested in public.

The method is: define the truth, then *render* it through a formatter that
reimplements the source UI's display quirks. Never hand-write the CSVs. Two
consequences matter:

* Internal identities hold by construction -- ``market_value == qty x price``,
  ``gain_pct == (close - open) / open``. Tests that assert against them are
  therefore testing the parser, not asserting against noise.
* The generator knows the true quantities *including for the rows it is about
  to abbreviate*, and writes them to ``truth.json``. That is what lets a test
  say "the importer recovered 137,482 exactly, and correctly refused to guess
  412,300 +/- 50".

Determinism is a hard requirement: the same input always produces byte-identical
output, and CI runs ``--check`` to prove the committed files were not
hand-edited. There is no randomness anywhere in this file.

    python scripts/make_example_data.py            # write data/example/
    python scripts/make_example_data.py --check    # verify, write nothing

Every landmine carried by the corpus is catalogued in ``LANDMINES`` below and
rendered into ``data/example/README.md``.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from portfolio_tracker.core.calendars import grid_days

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "example"

# Single spot rate, applied uniformly regardless of trade date. This reproduces
# the defining flaw of the real export: its EUR profit column for foreign-currency
# positions translates at *today's* rate, so it contains no currency effect at
# all. The tracker exists partly to stop relying on that number.
FX_TRY_PER_EUR = Decimal("55.25")

AS_OF = date(2026, 8, 14)

CURRENCY_SYMBOL = {"EUR": "€", "TRY": "₺"}

# A value at or above this magnitude is abbreviated by the source UI, losing
# precision irreversibly. Confirmed empirically against the real export.
K_THRESHOLD = Decimal(100_000)

NBSP = " "

OPEN_HEADER = [
    "Name", "Symbol", "Open Date", "Type", "Amount",
    "Open Price", "Current Price", "Market Value", "Daily P/L", "Net P/L",
]  # fmt: skip

CLOSED_HEADER = [
    "Name", "Symbol", "Open Date", "Type", "Amount",
    "Open Price", "Close Date", "Close Price", "Gain%", "Net P/L",
]  # fmt: skip


# ---------------------------------------------------------------------------
# The fictional universe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    currency: str
    current_price: Decimal


INSTRUMENTS: dict[str, Instrument] = {
    i.symbol: i
    for i in [
        Instrument("ZGEQ", "Meridian Global Equity UCITS Acc", "EUR", Decimal("17.48")),
        Instrument("ZSEM", "Vantage Semiconductor Index UCITS Acc", "EUR", Decimal("17.85")),
        Instrument("ZBNK", "Northwind Eurozone Banks C-EUR", "EUR", Decimal("406.45")),
        Instrument("ZGLD", "Aurum Miners Select UCITS", "EUR", Decimal("35.57")),
        Instrument("ZDIV", "Harbour European Dividend UCITS", "EUR", Decimal("36.91")),
        Instrument("ZOVN", "Solstice Overnight Return UCITS", "EUR", Decimal("101.71")),
        # Turkish diacritics: U+0130, U+015F, U+0131, U+00FC, U+00F6.
        Instrument("ZTFA", "Işık Portföy Teknoloji Fonu", "TRY", Decimal("0.9933")),
        Instrument("ZTFB", "Zümrüt Portföy Altın Fonu", "TRY", Decimal("0.627")),
        Instrument("ACRO", "Acme Robotics C", "EUR", Decimal("297.85")),
        Instrument("GBEX", "Globex Retail", "EUR", Decimal("229.35")),
        # Slash in the symbol; four-digit price. Fully closed, so no current price
        # is ever rendered for it.
        Instrument("ZEC/EUR", "Zcoin Euro", "EUR", Decimal("54118.30")),
    ]
}


@dataclass(frozen=True)
class Disposal:
    qty: Decimal
    close_date: date
    close_price: Decimal


@dataclass(frozen=True)
class Lot:
    """A purchase. ``quantity`` is the ORIGINAL size, before any disposal.

    The source export never states this number. It reports the *remaining*
    quantity in the open file and each disposed quantity in the closed file,
    which is why reconstructing it is the importer's central problem.
    """

    symbol: str
    open_date: date
    quantity: Decimal
    open_price: Decimal
    disposals: tuple[Disposal, ...] = field(default_factory=tuple)

    @property
    def remaining(self) -> Decimal:
        return self.quantity - sum((d.qty for d in self.disposals), Decimal(0))


def _d(value: str) -> Decimal:
    return Decimal(value)


LOTS: list[Lot] = [
    # -- ZGEQ: plain baseline, three lots ---------------------------------
    Lot("ZGEQ", date(2025, 8, 8), _d("221.2074"), _d("14.01")),
    # Ambiguous date: day <= 12, so a dayfirst parser silently shifts it.
    Lot("ZGEQ", date(2026, 3, 5), _d("140"), _d("16.02")),
    Lot("ZGEQ", date(2026, 6, 24), _d("30.5"), _d("17.30")),
    # -- ZSEM: highly fractional quantity ---------------------------------
    Lot("ZSEM", date(2025, 6, 11), _d("440.5993"), _d("6.81")),
    # -- ZBNK: integer quantity, price above 100 --------------------------
    Lot("ZBNK", date(2025, 8, 4), _d("19"), _d("261.25")),
    Lot("ZBNK", date(2026, 7, 15), _d("5.311"), _d("376.8")),
    # -- ZGLD: four open lots, plus two lots that are fully closed --------
    Lot("ZGLD", date(2025, 11, 12), _d("315.68847"), _d("31.27")),
    Lot("ZGLD", date(2026, 3, 24), _d("45.4583"), _d("33.08")),
    Lot("ZGLD", date(2025, 12, 17), _d("43.99"), _d("33.06")),
    Lot("ZGLD", date(2026, 2, 12), _d("90.1393"), _d("38.83")),
    # ONE lot, TWO disposals, and it appears in NEITHER file as open. The
    # importer must sum both closed rows to recover a buy of 501.2193.
    Lot(
        "ZGLD",
        date(2025, 5, 7),
        _d("501.2193"),
        _d("19.88"),
        (
            Disposal(_d("251.2193"), date(2025, 10, 27), _d("25.90")),
            # Whole-number profit, so the UI renders it without decimals.
            Disposal(_d("250"), date(2025, 7, 29), _d("20.38")),
        ),
    ),
    Lot(
        "ZGLD",
        date(2025, 9, 15),
        _d("131.85"),
        _d("27.06"),
        (Disposal(_d("131.85"), date(2025, 10, 27), _d("25.90")),),
    ),
    # -- ZDIV: many lots of one instrument --------------------------------
    Lot("ZDIV", date(2025, 6, 23), _d("162.4471"), _d("31.55")),
    Lot("ZDIV", date(2025, 5, 18), _d("174.9302"), _d("32.87")),
    Lot("ZDIV", date(2025, 3, 23), _d("118.2036"), _d("30.94")),
    Lot("ZDIV", date(2026, 6, 24), _d("14.2258"), _d("36.28")),
    Lot("ZDIV", date(2026, 4, 14), _d("71.06"), _d("37.14")),
    # -- ZOVN: the partial close that spans BOTH files --------------------
    Lot("ZOVN", date(2025, 3, 13), _d("322.4471"), _d("101.05")),
    Lot("ZOVN", date(2025, 5, 4), _d("81.2265"), _d("101.38")),
    Lot("ZOVN", date(2025, 5, 5), _d("21.447"), _d("101.29")),
    Lot(
        "ZOVN",
        date(2025, 6, 13),
        _d("203.8874"),
        _d("101.88"),
        (Disposal(_d("72.4118"), date(2025, 6, 14), _d("101.82")),),
    ),
    # -- ACRO: a stock, also partially closed across both files -----------
    Lot("ACRO", date(2025, 5, 9), _d("38.2617"), _d("138.94")),
    Lot(
        "ACRO",
        date(2025, 5, 27),
        _d("21.8104"),
        _d("151.22"),
        (Disposal(_d("18"), date(2025, 8, 22), _d("172.60")),),
    ),
    # -- GBEX: one open lot, one entirely separate lot fully closed -------
    Lot("GBEX", date(2025, 10, 6), _d("16"), _d("188.20")),
    Lot(
        "GBEX",
        date(2025, 8, 26),
        _d("14"),
        _d("192.50"),
        # Whole-number loss: renders as "-EUR 161", no decimals.
        (Disposal(_d("14"), date(2025, 10, 15), _d("181.00")),),
    ),
    # -- ZTFB: lira fund. Three K-abbreviated quantities, two recoverable --
    # Market value also >= 100k, so it is abbreviated too and the quantity
    # CANNOT be recovered from it.
    Lot("ZTFB", date(2024, 10, 3), _d("412300"), _d("0.3105")),
    # Market value below the threshold, printed exactly -> quantity recoverable.
    Lot("ZTFB", date(2024, 7, 22), _d("137482"), _d("0.2634")),
    # Below the threshold: printed in full, no abbreviation at all.
    Lot("ZTFB", date(2024, 9, 2), _d("91206"), _d("0.2891")),
    Lot("ZTFB", date(2025, 12, 22), _d("118394"), _d("0.6012")),
    # -- ZTFA: lira fund, ten lots ----------------------------------------
    # Both quantity and market value abbreviated -> not recoverable.
    Lot("ZTFA", date(2025, 8, 31), _d("115627"), _d("0.6702")),
    Lot("ZTFA", date(2026, 3, 5), _d("94218"), _d("0.8241")),
    Lot("ZTFA", date(2025, 10, 3), _d("76140"), _d("0.7934")),
    Lot("ZTFA", date(2025, 11, 4), _d("64007"), _d("0.8288")),
    Lot("ZTFA", date(2026, 1, 4), _d("82911"), _d("0.8655")),
    Lot("ZTFA", date(2025, 10, 7), _d("46893"), _d("0.7981")),
    Lot("ZTFA", date(2025, 12, 4), _d("40872"), _d("0.8302")),
    Lot("ZTFA", date(2026, 5, 5), _d("76118"), _d("0.9361")),
    Lot("ZTFA", date(2026, 8, 10), _d("14553"), _d("0.9662")),
    Lot("ZTFA", date(2026, 6, 4), _d("32294"), _d("1.0397")),
    # -- ZEC/EUR: crypto, fully closed at a large loss --------------------
    Lot(
        "ZEC/EUR",
        date(2025, 5, 26),
        _d("0.04821637"),
        _d("91204.70"),
        (Disposal(_d("0.04821637"), date(2026, 2, 5), _d("54118.30")),),
    ),
]


# ---------------------------------------------------------------------------
# Rendering -- reimplements the source UI's display conventions
# ---------------------------------------------------------------------------

CENT = Decimal("0.01")


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def plain(value: Decimal) -> str:
    """Decimal as a plain string: no exponent, no trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def fmt_money(value: Decimal, currency: str) -> str:
    """``-EUR 2,087.72`` | ``EUR 122`` | ``TRY86,201.21`` | ``TRY258.51K``.

    Three conventions, all observed in the real export and all easy to get
    wrong: the minus sign precedes the currency glyph rather than the digits;
    the euro glyph is followed by a space while the lira glyph never is; and a
    value that is exactly whole is rendered without any decimal part.
    """
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= K_THRESHOLD:
        body = plain(q2(magnitude / 1000)) + "K"
    elif magnitude == magnitude.to_integral_value():
        body = f"{int(magnitude):,}"
    else:
        body = f"{q2(magnitude):,.2f}"
    separator = " " if currency == "EUR" else ""
    return f"{sign}{CURRENCY_SYMBOL[currency]}{separator}{body}"


def fmt_quantity(value: Decimal) -> str:
    """``35.9144`` exactly, or ``412.3K`` with the precision destroyed."""
    if value >= K_THRESHOLD:
        return plain(q2(value / 1000)) + "K"
    return plain(value)


def fmt_price(value: Decimal) -> str:
    """Bare number: no currency glyph, thousands separator above 1000."""
    if abs(value) >= 1000:
        return f"{q2(value):,.2f}"
    return plain(value)


def fmt_pct(value: Decimal) -> str:
    return f"{q2(value):.2f}%"


def fmt_date(value: date) -> str:
    return value.strftime("%m/%d/%Y")


def to_eur(amount: Decimal, currency: str) -> Decimal:
    return amount if currency == "EUR" else amount / FX_TRY_PER_EUR


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def is_abbreviated(value: Decimal) -> bool:
    return value >= K_THRESHOLD


def k_interval(rendered: str) -> tuple[Decimal, Decimal]:
    """Bounds implied by a ``K`` rendering's displayed precision.

    ``412.3K`` shows one decimal, so the step is 100 and the true value lies
    within +/- 50. ``137.48K`` shows two, so +/- 5.
    """
    body = rendered.removesuffix("K")
    decimals = len(body.split(".")[1]) if "." in body else 0
    step = Decimal(1000) / (Decimal(10) ** decimals)
    centre = Decimal(body) * 1000
    return centre - step / 2, centre + step / 2


def build_open_rows() -> tuple[list[list[str]], list[dict[str, object]]]:
    rows: list[tuple[Decimal, list[str]]] = []
    truth: list[dict[str, object]] = []

    for index, lot in enumerate(LOTS):
        if lot.remaining <= 0:
            continue
        inst = INSTRUMENTS[lot.symbol]
        qty = lot.remaining
        market_value = qty * inst.current_price
        net_pl_local = qty * (inst.current_price - lot.open_price)
        net_pl_eur = to_eur(net_pl_local, inst.currency)

        # Deterministic, sign-varied, and small relative to the position.
        daily_eur = q2(to_eur(market_value, inst.currency) * Decimal("0.0017"))
        if index % 7 == 1:
            daily_eur = -daily_eur

        rendered_qty = fmt_quantity(qty)
        rendered_mv = fmt_money(market_value, inst.currency)

        rows.append(
            (
                net_pl_eur,
                [
                    inst.name + NBSP,
                    inst.symbol,
                    fmt_date(lot.open_date),
                    "BUY",
                    rendered_qty,
                    fmt_price(lot.open_price),
                    fmt_price(inst.current_price),
                    rendered_mv,
                    fmt_money(daily_eur, "EUR"),
                    fmt_money(net_pl_eur, "EUR"),
                ],
            )
        )

        entry: dict[str, object] = {
            "key": f"{lot.symbol}:{lot.open_date.isoformat()}:{plain(lot.open_price)}",
            "symbol": lot.symbol,
            "currency": inst.currency,
            "true_open_quantity": plain(lot.quantity),
            "true_remaining_quantity": plain(qty),
            "rendered_amount": rendered_qty,
            "abbreviated": is_abbreviated(qty),
        }
        if is_abbreviated(qty):
            lo, hi = k_interval(rendered_qty)
            # Recoverable only when the market value was NOT itself abbreviated,
            # because recovery divides market value by the current price.
            recoverable = not is_abbreviated(market_value)
            entry.update(
                {
                    "amount_interval": [plain(lo), plain(hi)],
                    "recoverable_from_market_value": recoverable,
                    "rendered_market_value": rendered_mv,
                }
            )
        truth.append(entry)

    # The source UI sorts open positions by profit, descending.
    rows.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in rows], truth


def build_closed_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    for lot in LOTS:
        inst = INSTRUMENTS[lot.symbol]
        for disposal in lot.disposals:
            gain_local = disposal.qty * (disposal.close_price - lot.open_price)
            gain_pct = (disposal.close_price - lot.open_price) / lot.open_price * 100
            rows.append(
                [
                    inst.name + NBSP,
                    inst.symbol,
                    fmt_date(lot.open_date),
                    "BUY",  # never SELL, even here: these rows are lots, not events
                    fmt_quantity(disposal.qty),
                    fmt_price(lot.open_price),
                    fmt_date(disposal.close_date),
                    fmt_price(disposal.close_price),
                    fmt_pct(gain_pct),
                    fmt_money(to_eur(gain_local, inst.currency), "EUR"),
                ]
            )
    return rows


# ---------------------------------------------------------------------------
# Byte-level output
# ---------------------------------------------------------------------------

BOM = b"\xef\xbb\xbf"


def render_csv(header: list[str], rows: list[list[str]]) -> bytes:
    """UTF-8 with a BOM, CRLF endings, quoted only where a comma forces it.

    Not ``csv.writer`` defaults: those three properties are the fixture's whole
    reason for existing, so they are asserted rather than assumed.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return BOM + buffer.getvalue().encode("utf-8")


LANDMINES: list[tuple[str, str]] = [
    ("UTF-8 BOM", "Both files start with EF BB BF. Read with `utf-8-sig` or the first header becomes `\\ufeffName`."),
    ("CRLF line endings", "Preserved via `.gitattributes -text`; normalising them would void the fixture."),
    ("Trailing U+00A0 on every Name", "A non-breaking space, not a regular one. Breaks exact-match joins; `strip()` handles it but `strip(' ')` does not."),
    ("Mixed currency in one column", "`Market Value` is EUR or TRY per row, while `Daily P/L` and `Net P/L` are always EUR."),
    ("Euro takes a space, lira does not", "`€ 507.08` versus `₺86,201.21`. A single regex `[€₺]\\s` drops every lira value."),
    ("Sign precedes the currency glyph", "`-€ 152.95`, never `€ -152.95`."),
    ("Quoted only when a comma appears", "`\"€ 10,697.10\"` versus `€ 507.08`."),
    ("Whole amounts lose their decimals", "`€ 125` means 125.00."),
    ("Prices carry no currency glyph", "And use a thousands separator above 1000: `91,204.70`."),
    ("US month-first dates", "`03/05/2026` is 5 March. Day <= 12 makes it ambiguous, so the parser must be pinned, not locale-driven."),
    ("Quantities abbreviated at 100,000", "`412.3K` -- one decimal shown means the true value is only known to +/- 50."),
    ("...but sometimes recoverable", "When `Market Value` is printed in full, `MV / Current Price` recovers the quantity exactly."),
    ("...and sometimes not", "When the market value is abbreviated too, the interval is all you get. Two lots of each kind are present."),
    ("Symbol containing a slash", "`ZEC/EUR` -- must not be split on."),
    ("Turkish diacritics", "U+0130, U+015F, U+0131, U+00FC, U+00F6. Do not case-fold the name column; key on the symbol."),
    ("One lot split across both files", "`ZOVN 06/13/2025`: 131.4756 open + 72.4118 closed = a real purchase of 203.8874."),
    ("One lot, two disposals, no open row", "`ZGLD 05/07/2025` appears twice in closed.csv and never in open.csv: 251.2193 + 250 = 501.2193."),
    ("Fully closed positions", "`GBEX 08/26/2025` and `ZEC/EUR` exist only in closed.csv."),
    ("Type is always BUY", "Including in closed.csv. These rows are lot states, not transactions -- there is no sell side to read."),
    ("Foreign-currency profit ignores FX", "Every lira row's EUR profit uses one current spot rate regardless of when it was bought, so it contains no currency effect."),
]  # fmt: skip


def render_readme(open_rows: int, closed_rows: int) -> bytes:
    lines = [
        "# Example data",
        "",
        "**Entirely fictional.** Generated by `scripts/make_example_data.py`; no real",
        "holding, quantity or price appears here. Regenerate with:",
        "",
        "```bash",
        "uv run python scripts/make_example_data.py",
        "uv run python scripts/make_example_data.py --check   # CI runs this",
        "```",
        "",
        f"`open.csv` has {open_rows} rows and `closed.csv` has {closed_rows}, matching the",
        "shape of the real export.",
        "",
        "## Why these files look wrong",
        "",
        "They are a faithful imitation of a real broker UI's export, quirks included.",
        "Each property below is deliberate and is exercised by the parser tests, so",
        "**do not hand-edit these files** -- an editor that strips trailing whitespace or",
        "normalises line endings will leave the tests passing while they test nothing.",
        "",
        "| Property | Detail |",
        "|---|---|",
    ]
    lines += [f"| {name} | {detail} |" for name, detail in LANDMINES]
    lines += [
        "",
        "## truth.json",
        "",
        "Records what the generator actually knew before rendering: the true quantity of",
        "every lot, whether the renderer abbreviated it, and the interval a reader can",
        "legitimately infer when it did. Tests assert against this rather than against",
        "hand-copied numbers, and `scripts/check_no_pii.py` uses its symbol list as the",
        "allowlist proving no real ticker was ever committed.",
        "",
        "## prices/ and fx/",
        "",
        "Daily series in the manual provider's own formats (`date,close` and `date,rate`),",
        "so the corpus can be **valued** and not merely imported. Point the fetchers at",
        "them and no network is involved at any step:",
        "",
        "```bash",
        "uv run pt fetch-fx --offline --manual-dir data/example/fx",
        "uv run pt fetch-prices --manual-dir data/example/prices",
        "```",
        "",
        "Every series passes exactly through the prices the CSVs already state -- each",
        "lot's open price on its open date, each disposal's close price, and the current",
        "price on the as-of date -- so the series can never contradict the export it",
        "accompanies. Between those anchors a seeded walk supplies the movement that",
        "makes volatility and drawdown mean anything.",
        "",
        f"`fx/TRY.csv` is the interesting one. It ends at exactly {plain(FX_TRY_PER_EUR)} on the as-of",
        "date, which is the single flat rate the export applies to *every* lira position",
        "regardless of when it was opened -- and it gets there from 35.10 in July 2024.",
        "So anything computed against the flat rate reproduces the export to the cent,",
        "and anything computed against the series shows the lira decline the export",
        "structurally cannot see. That difference is the reason this project exists.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Price and FX series
#
# Without these the corpus can be imported but never valued, so a clean clone
# reaches the ledger reconstruction and stops. They are built by interpolating
# between the prices the CSVs already state -- each lot's open price on its open
# date, each disposal's close price, and the current price on the as-of date --
# so the series cannot contradict the export it accompanies. Between anchors a
# seeded jitter supplies the wiggle that makes volatility and drawdown mean
# something; it is exactly zero at the anchors, so the reconciliation still
# lands on the cent.
#
# The FX series is the one that matters. The export translates every lira
# position at a single flat 55.25, which is the defect this project exists to
# expose -- so the generated rate *ends* at 55.25 on the as-of date, and gets
# there from 35 in mid-2024. Anything computed against the flat rate reproduces
# the export exactly; anything computed against the series shows the lira
# decline the export cannot see.
# ---------------------------------------------------------------------------

#: First day of price and FX history. Ten TARGET2 days before the earliest
#: trade, because a market can be shut on the day a position is opened and an
#: unvalued opening day breaks the whole series after it.
HISTORY_START = date(2024, 7, 1)

#: Anchors for EUR/TRY, in lira per euro. The lira roughly halved over this
#: window, which is the whole point of tracking it daily.
FX_ANCHORS: list[tuple[date, Decimal]] = [
    (date(2024, 7, 1), Decimal("35.10")),
    (date(2025, 1, 2), Decimal("40.55")),
    (date(2025, 7, 1), Decimal("45.80")),
    (date(2026, 1, 2), Decimal("51.00")),
    (AS_OF, FX_TRY_PER_EUR),
]

#: Jitter amplitude as a fraction of the level, per instrument class. Small
#: enough that a reader checking `qty x price` against the export still sees
#: the anchors line up, large enough that a flat line would look wrong.
JITTER = {"EUR": Decimal("0.010"), "TRY": Decimal("0.006")}

CALENDAR_FOR = {"ZTFA": "TEFAS", "ZTFB": "TEFAS", "ZEC/EUR": "ALL_DAYS"}


def _lcg(seed: int) -> Iterator[Decimal]:
    """A deterministic generator in [-1, 1], implemented rather than imported.

    ``random`` is seeded globally and a future import could reseed it; this
    corpus has to be byte-identical on every machine forever, so the sequence
    is spelled out. Numerical Recipes' constants.
    """
    state = seed & 0xFFFFFFFF
    while True:
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        yield Decimal(state) / Decimal(0x7FFFFFFF) - Decimal(1)


def _interpolate(anchors: list[tuple[date, Decimal]], days: list[date]) -> dict[date, Decimal]:
    """Piecewise-linear between anchors, flat outside them."""
    out: dict[date, Decimal] = {}
    for day in days:
        if day <= anchors[0][0]:
            out[day] = anchors[0][1]
            continue
        if day >= anchors[-1][0]:
            out[day] = anchors[-1][1]
            continue
        for (d0, p0), (d1, p1) in pairwise(anchors):
            if d0 <= day <= d1:
                span = Decimal((d1 - d0).days)
                out[day] = p0 if span == 0 else p0 + (p1 - p0) * Decimal((day - d0).days) / span
                break
    return out


def _series(
    anchors: list[tuple[date, Decimal]],
    days: list[date],
    *,
    seed: int,
    jitter: Decimal,
    places: Decimal,
) -> list[tuple[date, Decimal]]:
    """Interpolated levels plus a seeded wiggle that vanishes at the anchors."""
    base = _interpolate(anchors, days)
    anchor_days = {d for d, _ in anchors}
    noise = _lcg(seed)
    walk = Decimal(0)
    out: list[tuple[date, Decimal]] = []
    for day in days:
        # A random *walk*, not independent noise: successive daily returns that
        # are uncorrelated produce a sawtooth nobody would mistake for a price.
        walk = walk * Decimal("0.86") + next(noise)
        level = base[day]
        value = level if day in anchor_days else level * (Decimal(1) + jitter * walk)
        out.append((day, max(value, places).quantize(places, rounding=ROUND_HALF_UP)))
    return out


def price_anchors(symbol: str) -> list[tuple[date, Decimal]]:
    """Every price the CSVs already commit to, in date order."""
    instrument = INSTRUMENTS[symbol]
    anchors: dict[date, Decimal] = {}
    for lot in LOTS:
        if lot.symbol != symbol:
            continue
        anchors[lot.open_date] = lot.open_price
        for disposal in lot.disposals:
            anchors[disposal.close_date] = disposal.close_price
    # A fully closed instrument has no current price in the export, so pinning
    # one would invent a fact. Its series simply ends at the last disposal.
    if any(lot.symbol == symbol and lot.remaining > Decimal(0) for lot in LOTS):
        anchors[AS_OF] = instrument.current_price
    return sorted(anchors.items())


def build_price_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for index, symbol in enumerate(sorted(INSTRUMENTS)):
        anchors = price_anchors(symbol)
        if not anchors:
            continue
        calendar = CALENDAR_FOR.get(symbol, "TARGET2")
        start = min(HISTORY_START, anchors[0][0])
        # Always to the as-of date, even for a position that was fully sold.
        # The asset kept trading after the disposal; only the holding ended.
        # Truncating the series would report a price gap where there is none.
        days = grid_days(start, AS_OF, calendar)
        # Sub-cent instruments need four decimals or the lira funds round to
        # zero; the euro ones are quoted to the cent like the export.
        places = Decimal("0.0001") if anchors[-1][1] < Decimal(10) else Decimal("0.01")
        rows = _series(
            anchors,
            days,
            seed=1000 + index,
            jitter=JITTER[INSTRUMENTS[symbol].currency],
            places=places,
        )
        body = "date,close\n" + "".join(f"{d.isoformat()},{plain(p)}\n" for d, p in rows)
        files[f"prices/{safe_filename(symbol)}.csv"] = body.encode("utf-8")
    return files


def build_fx_file() -> bytes:
    days = grid_days(HISTORY_START, AS_OF, "TARGET2")
    rows = _series(FX_ANCHORS, days, seed=77, jitter=Decimal("0.004"), places=Decimal("0.0001"))
    return ("date,rate\n" + "".join(f"{d.isoformat()},{plain(r)}\n" for d, r in rows)).encode(
        "utf-8"
    )


def safe_filename(symbol: str) -> str:
    """``ZEC/EUR`` is a legal symbol and an illegal filename on every OS."""
    return symbol.replace("/", "_")


def build() -> dict[str, bytes]:
    open_rows, truth_lots = build_open_rows()
    closed_rows = build_closed_rows()

    truth = {
        "generated_by": "scripts/make_example_data.py",
        "as_of": AS_OF.isoformat(),
        "fx_try_per_eur": plain(FX_TRY_PER_EUR),
        "k_threshold": plain(K_THRESHOLD),
        "symbols": sorted(INSTRUMENTS),
        "open_rows": len(open_rows),
        "closed_rows": len(closed_rows),
        "history_start": HISTORY_START.isoformat(),
        "fx_anchors": {d.isoformat(): plain(r) for d, r in FX_ANCHORS},
        "lots": truth_lots,
    }

    artefacts = {
        "open.csv": render_csv(OPEN_HEADER, open_rows),
        "closed.csv": render_csv(CLOSED_HEADER, closed_rows),
        "truth.json": (json.dumps(truth, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        "README.md": render_readme(len(open_rows), len(closed_rows)),
        "fx/TRY.csv": build_fx_file(),
    }
    artefacts.update(build_price_files())
    return artefacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the synthetic example corpus.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed files match a fresh generation, byte for byte",
    )
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    artefacts = build()

    if args.check:
        stale: list[str] = []
        for name, expected in artefacts.items():
            path = args.out / name
            actual = path.read_bytes() if path.exists() else b""
            if actual != expected:
                stale.append(
                    f"  {path.relative_to(REPO_ROOT)}: {len(actual)} -> {len(expected)} bytes"
                )
        if stale:
            print("example data is stale or hand-edited:", file=sys.stderr)
            print("\n".join(stale), file=sys.stderr)
            print(
                "\nRegenerate with: python scripts/make_example_data.py",
                file=sys.stderr,
            )
            return 1
        print(f"example data current ({len(artefacts)} files)")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for name, payload in artefacts.items():
        (args.out / name).parent.mkdir(parents=True, exist_ok=True)
        (args.out / name).write_bytes(payload)
        print(f"wrote {(args.out / name).relative_to(REPO_ROOT)}  {len(payload)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
