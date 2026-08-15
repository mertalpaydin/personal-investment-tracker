"""Exact decimal arithmetic, and how it survives a round trip through SQLite.

Money is ``Decimal`` everywhere. A project whose acceptance criterion is stated
in cents cannot represent money in binary floating point: ``0.1 + 0.2`` is not
``0.3``, and the error compounds across a FIFO chain until a tax figure is wrong
by a few cents with no single line to blame.

There is one non-obvious trap in storing ``Decimal`` in SQLite, documented in
:data:`DECIMAL_SQL_TYPE` below.
"""

from __future__ import annotations

import sqlite3
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

#: Declared column type for every monetary and quantity column.
#:
#: **This must contain the substring "TEXT".** SQLite assigns column affinity by
#: substring match on the declared type, and a type named ``DECIMAL`` gets
#: NUMERIC affinity -- which silently converts the string ``'0.1'`` back into
#: the float ``0.1`` on the way in, defeating the entire point of using Decimal.
#: A name containing "TEXT" gets TEXT affinity, so the digits are stored
#: verbatim. It must also not contain "INT", which would win as INTEGER affinity.
DECIMAL_SQL_TYPE: Final = "DECIMAL_TEXT"

CENT: Final = Decimal("0.01")
ZERO: Final = Decimal(0)


def register_decimal_support() -> None:
    """Teach sqlite3 to store Decimal as text and read it back as Decimal.

    Idempotent; safe to call on every connection.
    """
    sqlite3.register_adapter(Decimal, str)
    sqlite3.register_converter(DECIMAL_SQL_TYPE, _convert_decimal)


def _convert_decimal(raw: bytes) -> Decimal | None:
    if raw in (b"", b"NULL"):
        return None
    return Decimal(raw.decode("utf-8"))


def to_decimal(value: str | int | Decimal) -> Decimal:
    """Parse to Decimal, rejecting float input outright.

    ``float`` is deliberately not accepted: ``Decimal(0.1)`` yields
    ``0.1000000000000000055511151231257827``, which is a silent precision leak
    at the boundary rather than an error. Callers holding a float have already
    lost the information and must say so explicitly.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):  # pragma: no cover - guarded by type checker too
        raise TypeError(
            "refusing to build a Decimal from a float; the precision is already lost. "
            "Pass a str or int, or use Decimal(str(x)) at the point the float was created."
        )
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a valid decimal: {value!r}") from exc


def quantize_money(value: Decimal) -> Decimal:
    """Round to cents, half-up.

    Banker's rounding (Python's default) is wrong for money here: tax figures
    are reconciled against statements that round half away from zero.
    """
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def plain(value: Decimal) -> str:
    """Render without exponent or trailing zeros, for stable keys and reports."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
