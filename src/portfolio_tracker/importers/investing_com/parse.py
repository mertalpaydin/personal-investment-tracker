"""Scalar parsers for the Investing.com positions export.

Pure functions, no I/O, no database. Everything here is driven by conventions
observed in the real file and pinned by `data/example/`.

The important design choice is that :func:`parse_quantity` and
:func:`parse_money` return values that **carry their own uncertainty**. The
source UI abbreviates magnitudes at or above 100,000 (``412.3K``), and a parser
that returned a bare ``Decimal("412300")`` would be asserting a precision the
file does not contain. Returning an interval instead lets the caller decide
whether it can be narrowed -- and forces it to notice that it should.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

Currency = Literal["EUR", "TRY"]

# Written as chr() rather than literals: three of these five are invisible in
# an editor, and a literal NBSP is indistinguishable from a normal space --
# which is exactly the confusion this parser exists to resolve.
EURO: Final = chr(0x20AC)
LIRA: Final = chr(0x20BA)
NBSP: Final = chr(0x00A0)
ZWSP: Final = chr(0x200B)
BOM: Final = chr(0xFEFF)

GLYPH_TO_CURRENCY: Final[dict[str, Currency]] = {EURO: "EUR", LIRA: "TRY"}

#: At or above this magnitude the source UI abbreviates, losing precision.
K_THRESHOLD: Final = Decimal(100_000)

_TRIM = f" \t\r\n{NBSP}{ZWSP}{BOM}"

_MONEY = re.compile(
    rf"^(?P<sign>-?)\s*(?P<glyph>[{EURO}{LIRA}])\s*(?P<body>[\d,]+(?:\.\d+)?)(?P<k>K?)$"
)
_NUMBER = re.compile(r"^(?P<sign>-?)(?P<body>[\d,]+(?:\.\d+)?)(?P<k>K?)$")
_PERCENT = re.compile(r"^(?P<sign>-?)(?P<body>[\d,]+(?:\.\d+)?)%$")


class ParseError(ValueError):
    """A field did not match any convention observed in the source format.

    Raised rather than guessed at: an unrecognised shape means the export
    layout changed, and silently coercing it would corrupt the ledger.
    """


@dataclass(frozen=True)
class Interval:
    """Bounds implied by a rendered value's displayed precision."""

    lo: Decimal
    hi: Decimal

    @property
    def width(self) -> Decimal:
        return self.hi - self.lo

    def contains(self, value: Decimal) -> bool:
        return self.lo <= value <= self.hi

    def intersect(self, other: Interval) -> Interval | None:
        lo, hi = max(self.lo, other.lo), min(self.hi, other.hi)
        return Interval(lo, hi) if lo <= hi else None


@dataclass(frozen=True)
class ParsedQuantity:
    value: Decimal
    abbreviated: bool
    interval: Interval

    @property
    def is_exact(self) -> bool:
        return not self.abbreviated


@dataclass(frozen=True)
class ParsedMoney:
    amount: Decimal
    currency: Currency
    abbreviated: bool
    interval: Interval


def clean_text(raw: str) -> str:
    """Strip the invisible characters the export carries.

    Every ``Name`` cell ends with U+00A0. Python's ``str.strip()`` removes it,
    but ``strip(' ')`` and SQL ``TRIM()`` do not, so exact-match joins fail in a
    way that is invisible in a terminal.
    """
    return raw.strip(_TRIM)


def _interval_for(body: str, value: Decimal, abbreviated: bool) -> Interval:
    """Bounds implied by the number of decimals actually displayed.

    ``412.3K`` shows one decimal, so the step is 1000/10 = 100 and the true
    value lies within +/- 50. ``137.48K`` shows two, so +/- 5. An unabbreviated
    value is exact and its interval is a point.
    """
    if not abbreviated:
        return Interval(value, value)
    decimals = len(body.split(".")[1]) if "." in body else 0
    step = Decimal(1000) / (Decimal(10) ** decimals)
    half = step / 2
    return Interval(value - half, value + half)


def _to_decimal(body: str) -> Decimal:
    try:
        return Decimal(body.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex already constrains this
        raise ParseError(f"not a number: {body!r}") from exc


def parse_money(raw: str) -> ParsedMoney:
    """``-€ 2,087.72`` | ``€ 125`` | ``₺86,201.21`` | ``₺258.51K``.

    Three conventions that are each easy to get wrong: the minus sign precedes
    the currency glyph rather than the digits; the euro glyph is followed by a
    space while the lira glyph never is; and a whole value is printed with no
    decimal part at all.
    """
    text = clean_text(raw)
    match = _MONEY.match(text)
    if not match:
        raise ParseError(f"unrecognised money value: {raw!r}")

    body = match["body"]
    abbreviated = bool(match["k"])
    magnitude = _to_decimal(body) * (1000 if abbreviated else 1)
    interval = _interval_for(body, magnitude, abbreviated)

    if match["sign"]:
        magnitude = -magnitude
        interval = Interval(-interval.hi, -interval.lo)

    return ParsedMoney(magnitude, GLYPH_TO_CURRENCY[match["glyph"]], abbreviated, interval)


def parse_quantity(raw: str) -> ParsedQuantity:
    """``35.9144`` exactly, or ``412.3K`` with the precision already destroyed.

    Never returns an exact value for an abbreviated token. Recovering the true
    figure needs evidence from elsewhere -- see ``reconcile.refine_quantity``.
    """
    text = clean_text(raw)
    match = _NUMBER.match(text)
    if not match:
        raise ParseError(f"unrecognised quantity: {raw!r}")
    if match["sign"]:
        raise ParseError(f"negative quantity in a positions export: {raw!r}")

    body = match["body"]
    abbreviated = bool(match["k"])
    value = _to_decimal(body) * (1000 if abbreviated else 1)
    return ParsedQuantity(value, abbreviated, _interval_for(body, value, abbreviated))


def parse_number(raw: str) -> Decimal:
    """A bare price: ``91,204.70``. No currency glyph is expected.

    A glyph here would mean the column layout changed, so it raises rather than
    tolerating it.
    """
    text = clean_text(raw)
    if any(glyph in text for glyph in GLYPH_TO_CURRENCY):
        raise ParseError(f"currency glyph in a price field, layout may have changed: {raw!r}")
    match = _NUMBER.match(text)
    if not match:
        raise ParseError(f"unrecognised number: {raw!r}")
    value = _to_decimal(match["body"]) * (1000 if match["k"] else 1)
    return -value if match["sign"] else value


def parse_us_date(raw: str) -> date:
    """Strictly ``%m/%d/%Y``.

    Pinned rather than locale-driven on purpose. The corpus contains
    ``03/05/2026``, where both fields are <= 12, so a day-first parser would
    silently read it as 3 May and shift the lot by two months without erroring.
    A genuinely day-first value such as ``13/01/2025`` must raise, not fall back.
    """
    text = clean_text(raw)
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError as exc:
        raise ParseError(f"not an MM/DD/YYYY date: {raw!r}") from exc


def parse_percent(raw: str) -> Decimal:
    """``-40.66%`` -> ``Decimal('-0.4066')``."""
    text = clean_text(raw)
    match = _PERCENT.match(text)
    if not match:
        raise ParseError(f"unrecognised percentage: {raw!r}")
    value = _to_decimal(match["body"]) / 100
    return -value if match["sign"] else value


def is_abbreviated_magnitude(value: Decimal) -> bool:
    """Would the source UI have abbreviated this value?"""
    return abs(value) >= K_THRESHOLD
