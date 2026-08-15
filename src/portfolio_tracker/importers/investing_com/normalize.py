"""Raw CSV rows into typed position records.

Still pure: no database, no inference across rows. Everything that needs to look
at more than one row lives in :mod:`reconcile`.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from portfolio_tracker.importers.investing_com.parse import (
    Currency,
    ParsedMoney,
    ParsedQuantity,
    ParseError,
    clean_text,
    parse_money,
    parse_number,
    parse_percent,
    parse_quantity,
    parse_us_date,
)

OPEN_COLUMNS = (
    "Name", "Symbol", "Open Date", "Type", "Amount",
    "Open Price", "Current Price", "Market Value", "Daily P/L", "Net P/L",
)  # fmt: skip

CLOSED_COLUMNS = (
    "Name", "Symbol", "Open Date", "Type", "Amount",
    "Open Price", "Close Date", "Close Price", "Gain%", "Net P/L",
)  # fmt: skip


@dataclass(frozen=True)
class OpenPosition:
    line: int
    name: str
    symbol: str
    open_date: date
    quantity: ParsedQuantity
    open_price: Decimal
    current_price: Decimal
    market_value: ParsedMoney
    net_pl_eur: Decimal

    @property
    def currency(self) -> Currency:
        """Authoritative: the glyph on the market value, not a guess."""
        return self.market_value.currency


@dataclass(frozen=True)
class ClosedPosition:
    line: int
    name: str
    symbol: str
    open_date: date
    quantity: ParsedQuantity
    open_price: Decimal
    close_date: date
    close_price: Decimal
    gain_pct: Decimal
    net_pl_eur: Decimal


def _read_rows(path: Path, expected: tuple[str, ...]) -> list[dict[str, str]]:
    """Decode with ``utf-8-sig``.

    Plain ``utf-8`` leaves the byte-order mark glued to the first header, so the
    column is named ``\\ufeffName`` and every lookup misses.
    """
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = tuple(reader.fieldnames or ())
    if header != expected:
        raise ParseError(
            f"{path.name}: unexpected columns.\n  expected {expected}\n  got      {header}"
        )
    return list(reader)


def load_open(path: Path) -> list[OpenPosition]:
    rows = []
    for offset, raw in enumerate(_read_rows(path, OPEN_COLUMNS), start=2):
        if clean_text(raw["Type"]) != "BUY":
            raise ParseError(f"{path.name}:{offset}: expected Type=BUY, got {raw['Type']!r}")
        rows.append(
            OpenPosition(
                line=offset,
                name=clean_text(raw["Name"]),
                symbol=clean_text(raw["Symbol"]),
                open_date=parse_us_date(raw["Open Date"]),
                quantity=parse_quantity(raw["Amount"]),
                open_price=parse_number(raw["Open Price"]),
                current_price=parse_number(raw["Current Price"]),
                market_value=parse_money(raw["Market Value"]),
                net_pl_eur=parse_money(raw["Net P/L"]).amount,
            )
        )
    return rows


def load_closed(path: Path) -> list[ClosedPosition]:
    """Note that closed rows also say ``BUY``.

    They describe the lot that was bought, with disposal columns appended --
    they are not sell events, and there is no sell side in the file to read.
    """
    rows = []
    for offset, raw in enumerate(_read_rows(path, CLOSED_COLUMNS), start=2):
        if clean_text(raw["Type"]) != "BUY":
            raise ParseError(f"{path.name}:{offset}: expected Type=BUY, got {raw['Type']!r}")
        rows.append(
            ClosedPosition(
                line=offset,
                name=clean_text(raw["Name"]),
                symbol=clean_text(raw["Symbol"]),
                open_date=parse_us_date(raw["Open Date"]),
                quantity=parse_quantity(raw["Amount"]),
                open_price=parse_number(raw["Open Price"]),
                close_date=parse_us_date(raw["Close Date"]),
                close_price=parse_number(raw["Close Price"]),
                gain_pct=parse_percent(raw["Gain%"]),
                net_pl_eur=parse_money(raw["Net P/L"]).amount,
            )
        )
    return rows
