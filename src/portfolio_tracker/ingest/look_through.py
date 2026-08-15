"""Fund holdings files: parse, check, store.

A fund's constituents are the only evidence that turns quote-currency
exposure into economic exposure. There is no feed for them and there is not
going to be one: every issuer publishes its own CSV, on its own schedule, with
its own column names, and none of them is under any obligation to keep the
format stable. So the manual file is the contract here, exactly as it is for
prices -- ``ingest/providers/manual.py`` exists for the same reason.

What this module refuses to do is the interesting part.

**It does not guess the weight unit unless the arithmetic is unambiguous.**
The same column is ``7.24`` in one issuer's file and ``0.0724`` in another's.
Dividing by 100 in the wrong place produces a portfolio that is 99% cash and
looks like a data problem somewhere else entirely -- this repository has
already shipped one bug of exactly that shape (a 41.5% deposit rate divided
twice). ``auto`` resolves the unit only when the sum lands unmistakably near 1
or near 100, and otherwise says what it saw and asks.

**It does not normalise on the way in.** Published files sum to 0.9997, or to
1.0043, or to 0.62 because somebody exported a filtered view. The raw sum is
stored, the aggregation divides by it, and ``pt doctor`` reports it. A file
scaled silently to 1.0 at import destroys the evidence that it was ever
anything else.

**It does not fill in blanks.** A constituent with no country stays NULL and
lands in an explicit ``UNKNOWN`` bucket downstream. An inferred country would
be indistinguishable from a published one a week later.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from portfolio_tracker.money import ZERO

WeightUnit = Literal["percent", "fraction"]

#: How far a file's published weights may sum from 1.0 and still be accepted.
#: Two percent is generous for rounding and tight enough to catch a filtered
#: export, a truncated download, or a column read as the wrong thing.
DEFAULT_TOLERANCE = Decimal("0.02")

#: Bands within which ``auto`` will commit to a unit. Deliberately wider than
#: DEFAULT_TOLERANCE: the point is to identify the unit confidently and *then*
#: let the tolerance check produce the specific complaint, so a file summing to
#: 0.62 is rejected for summing to 62% rather than for being unreadable.
_FRACTION_BAND = (Decimal("0.90"), Decimal("1.10"))
_PERCENT_BAND = (Decimal(90), Decimal(110))

#: Column aliases, lower-cased and stripped of punctuation. Issuers name the
#: same field five ways; a wrong guess here is silent, so the mapping that was
#: actually used is reported back and printed by the CLI.
ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "name",
        "holding",
        "holding name",
        "security name",
        "security",
        "issuer name",
        "instrument",
        "description",
        "constituent",
    ),
    "identifier": ("isin", "identifier", "ticker", "symbol", "sedol", "cusip"),
    "weight": (
        "weight",
        "weight pct",
        "weight percent",
        "weighting",
        "of net assets",
        "pct of net assets",
        "percent of net assets",
        "net assets pct",
        "portfolio weight",
        "allocation",
        "share",
    ),
    "currency": ("currency", "market currency", "trading currency", "ccy", "local currency"),
    "country": ("country", "location", "domicile", "country of risk", "issuer country"),
    "sector": ("sector", "industry", "gics sector", "sector name", "industry group"),
    "asset_class": ("asset class", "type", "instrument type", "security type", "class"),
}

#: Fields a file must supply. Everything else is optional and its absence
#: becomes an UNKNOWN bucket rather than an error -- a currency-only breakdown
#: is still worth having.
REQUIRED = ("name", "weight")

_PUNCTUATION = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


class LookThroughError(ValueError):
    """A holdings file cannot be read, or reads as something implausible."""


@dataclass(frozen=True)
class HoldingLine:
    """One constituent, as published. Weight is a fraction of fund NAV."""

    line_no: int
    name: str
    weight: Decimal
    identifier: str | None = None
    currency: str | None = None
    country: str | None = None
    sector: str | None = None
    asset_class: str | None = None


@dataclass
class HoldingsFile:
    """A parsed holdings file, plus what had to be decided to read it."""

    lines: list[HoldingLine]
    unit: WeightUnit
    weight_sum: Decimal
    columns: dict[str, str] = field(default_factory=dict)
    skipped: int = 0
    negative: int = 0

    @property
    def drift(self) -> Decimal:
        """How far the published weights are from summing to the whole fund."""
        return self.weight_sum - Decimal(1)


def _normalise_header(value: str) -> str:
    """``"Weight (%)"`` and ``"weight_pct"`` are the same column."""
    lowered = value.replace("﻿", "").strip().lower().replace("_", " ").replace("%", " pct ")
    return _SPACES.sub(" ", _PUNCTUATION.sub(" ", lowered)).strip()


def _map_columns(fieldnames: list[str]) -> dict[str, str]:
    """Resolve the file's headers onto our field names.

    First match wins per field, and a header is consumed once, so a file with
    both ``ISIN`` and ``Ticker`` maps the ISIN and leaves the ticker alone
    rather than overwriting it with the weaker identifier.
    """
    available = {_normalise_header(f): f for f in fieldnames if f is not None}
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for field_name, aliases in ALIASES.items():
        for alias in aliases:
            header = available.get(alias)
            if header is not None and header not in taken:
                mapping[field_name] = header
                taken.add(header)
                break
    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise LookThroughError(
            f"holdings file has no {' or '.join(missing)} column. "
            f"Headers seen: {', '.join(sorted(available.values()))}. "
            f"Accepted names for {missing[0]}: {', '.join(ALIASES[missing[0]])}"
        )
    return mapping


def _number(raw: str) -> Decimal | None:
    """Parse a published weight. Returns None for a blank or a dash.

    Handles thousands separators, a trailing per-cent sign, and the accounting
    convention of writing a negative in parentheses -- all three appear in
    issuer files, and ``Decimal(raw)`` raises on every one of them.
    """
    text = raw.strip().replace("\xa0", "").replace(" ", "")
    if not text or text in {"-", "--", "n/a", "N/A", "NA"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace("%", "").replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise LookThroughError(f"not a number: {raw!r}") from exc
    return -value if negative else value


def _clean(raw: str | None, *, upper: bool = False) -> str | None:
    if raw is None:
        return None
    text = raw.replace("\xa0", " ").strip()
    if not text or text in {"-", "--"}:
        return None
    return text.upper() if upper else text


def _resolve_unit(raw_sum: Decimal, requested: WeightUnit | Literal["auto"]) -> WeightUnit:
    if requested != "auto":
        return requested
    if _FRACTION_BAND[0] <= raw_sum <= _FRACTION_BAND[1]:
        return "fraction"
    if _PERCENT_BAND[0] <= raw_sum <= _PERCENT_BAND[1]:
        return "percent"
    raise LookThroughError(
        f"cannot tell what the weight column is in: it sums to {raw_sum}, which is "
        f"{raw_sum:.4f} read as fractions and {raw_sum / 100:.4f} read as per cent -- "
        "neither is a whole fund. Pass the unit explicitly (--weights percent|fraction) "
        "if you know which it is; the tolerance check will then say what is missing."
    )


def parse(
    text: str,
    *,
    unit: WeightUnit | Literal["auto"] = "auto",
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> HoldingsFile:
    """Read a holdings CSV into fractional weights, or refuse and say why."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise LookThroughError("holdings file is empty")
    columns = _map_columns(list(reader.fieldnames))

    raw_rows: list[tuple[int, dict[str, str]]] = []
    raw_sum = ZERO
    skipped = 0
    for line_no, row in enumerate(reader, start=1):
        raw_weight = row.get(columns["weight"]) or ""
        try:
            weight = _number(raw_weight)
        except LookThroughError as exc:
            raise LookThroughError(f"line {line_no}: {exc}") from exc
        name = _clean(row.get(columns["name"]))
        # A blank weight or a blank name is a footer, a section break or a
        # spacer row. Issuer files are full of them. They are counted, so a
        # file that is mostly footer does not read as a small fund.
        if weight is None or name is None:
            skipped += 1
            continue
        raw_rows.append((line_no, row))
        raw_sum += weight

    if not raw_rows:
        raise LookThroughError(f"holdings file has no usable rows ({skipped} skipped)")

    resolved = _resolve_unit(raw_sum, unit)
    divisor = Decimal(100) if resolved == "percent" else Decimal(1)

    lines: list[HoldingLine] = []
    total = ZERO
    negative = 0
    for line_no, row in raw_rows:
        weight = _number(row.get(columns["weight"]) or "")
        assert weight is not None  # filtered above
        weight = weight / divisor
        if weight < ZERO:
            negative += 1
        name = _clean(row.get(columns["name"]))
        assert name is not None  # filtered above
        lines.append(
            HoldingLine(
                line_no=line_no,
                name=name,
                weight=weight,
                identifier=_clean(row.get(columns.get("identifier", "")), upper=True),
                currency=_clean(row.get(columns.get("currency", "")), upper=True),
                country=_clean(row.get(columns.get("country", ""))),
                sector=_clean(row.get(columns.get("sector", ""))),
                asset_class=_clean(row.get(columns.get("asset_class", ""))),
            )
        )
        total += weight

    if abs(total - Decimal(1)) > tolerance:
        raise LookThroughError(
            f"{len(lines)} holding(s) read as {resolved} sum to {total:.6f} of the fund, "
            f"not 1.0 (tolerance {tolerance}). A filtered export, a truncated download or "
            "the wrong column would each look like this. Raise --tolerance only once you "
            "know which part of the fund is missing."
        )

    return HoldingsFile(
        lines=lines,
        unit=resolved,
        weight_sum=total,
        columns=columns,
        skipped=skipped,
        negative=negative,
    )


def load(
    path: Path,
    *,
    unit: WeightUnit | Literal["auto"] = "auto",
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> tuple[HoldingsFile, str]:
    """Parse a file from disk. Returns the parse and a SHA-256 of its bytes.

    ``utf-8-sig`` for the same reason the positions importer uses it: issuer
    exports come out of a spreadsheet with a BOM, and without this the first
    header is ``\\ufeffName`` and matches nothing.
    """
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    return parse(raw.decode("utf-8-sig"), unit=unit, tolerance=tolerance), digest


@dataclass(frozen=True)
class StoreResult:
    instrument_id: int
    as_of: date
    holdings: int
    replaced: bool


def store(
    conn: sqlite3.Connection,
    *,
    instrument_id: int,
    as_of: date,
    parsed: HoldingsFile,
    source: str,
    file_sha256: str,
) -> StoreResult:
    """Write one composition, replacing any previous one for the same date.

    Replacing rather than merging is deliberate. A holdings file is a single
    observation of a whole fund; keeping half of an old one alongside half of a
    new one would produce a composition that was never published by anybody.
    The cascade on the set row is what makes that atomic.
    """
    key = (instrument_id, as_of.isoformat())
    replaced = (
        conn.execute(
            "SELECT count(*) AS n FROM fund_holding_set WHERE instrument_id = ? AND as_of = ?",
            key,
        ).fetchone()["n"]
        > 0
    )
    if replaced:
        # The cascade needs foreign keys on, which `connect` guarantees; the
        # explicit delete of children keeps this correct even if it is not.
        conn.execute("DELETE FROM fund_holding WHERE instrument_id = ? AND as_of = ?", key)
        conn.execute("DELETE FROM fund_holding_set WHERE instrument_id = ? AND as_of = ?", key)

    conn.execute(
        "INSERT INTO fund_holding_set (instrument_id, as_of, source, weight_sum, unit, "
        "holdings, file_sha256, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            *key,
            source,
            parsed.weight_sum,
            parsed.unit,
            len(parsed.lines),
            file_sha256,
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    conn.executemany(
        "INSERT INTO fund_holding (instrument_id, as_of, line_no, name, identifier, weight, "
        "currency, country, sector, asset_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                *key,
                line.line_no,
                line.name,
                line.identifier,
                line.weight,
                line.currency,
                line.country,
                line.sector,
                line.asset_class,
            )
            for line in parsed.lines
        ],
    )
    return StoreResult(instrument_id, as_of, len(parsed.lines), replaced)
