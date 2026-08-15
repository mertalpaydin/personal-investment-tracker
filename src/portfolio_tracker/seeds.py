"""Instrument metadata the source export does not carry.

Price symbols, ISINs and tax classification all have to come from somewhere,
and none of them is in a positions export. This applies a YAML file to the
``instrument`` table.

Only keys explicitly present in the file are written, so a partially filled
seed never blanks out a column that was already correct.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path("seeds/instruments.yaml")

#: Columns a seed file is allowed to set. Anything else is a typo, and is
#: reported rather than ignored -- a misspelled key that silently does nothing
#: is worse than a hard failure, because the user believes it took effect.
SETTABLE: frozenset[str] = frozenset(
    {
        "isin",
        "wkn",
        "symbol",
        "name",
        "asset_class",
        "economic_currency",
        "exchange",
        "trading_calendar",
        "price_source",
        "price_symbol",
        "ter_bps",
        "distribution_policy",
        "domicile",
        "teilfreistellung_pct",
        "is_fund",
        "tax_regime",
        "tax_scope_note",
        "region",
        "sector",
        "notes",
    }
)

#: Never settable by seed. Currency is decided at import from the export's own
#: currency glyph, which is authoritative; letting a seed override it would
#: reintroduce exactly the "valued as though euro" failure the schema prevents.
PROTECTED: frozenset[str] = frozenset({"id", "import_key", "currency", "status"})


class SeedError(ValueError):
    """A seed file is malformed, or offered a value the schema refuses."""


@dataclass
class SeedResult:
    updated: list[str] = field(default_factory=list)
    unknown_instruments: list[str] = field(default_factory=list)
    fields_written: int = 0


def _coerce(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in {"teilfreistellung_pct"}:
        if isinstance(value, float):
            raise SeedError(
                f"{column}: write {value!r} as a quoted string, not a YAML float -- "
                "0.30 as a float is not exactly 0.30"
            )
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise SeedError(f"{column}: not a number: {value!r}") from exc
    if column in {"is_fund"}:
        return 1 if value else 0
    return value


def load(path: Path | str = DEFAULT_PATH) -> dict[str, dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return {}
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SeedError(f"{target}: expected a mapping at the top level")
    if raw.get("version") != 1:
        raise SeedError(f"{target}: unsupported version {raw.get('version')!r}, expected 1")

    instruments = raw.get("instruments") or {}
    if not isinstance(instruments, dict):
        raise SeedError(f"{target}: 'instruments' must be a mapping of import_key -> fields")

    validated: dict[str, dict[str, Any]] = {}
    for key, fields in instruments.items():
        if not isinstance(fields, dict):
            raise SeedError(f"{target}: instrument {key!r} must map to a set of fields")
        coerced: dict[str, Any] = {}
        for column, value in fields.items():
            if column in PROTECTED:
                raise SeedError(
                    f"{target}: {key}.{column} cannot be set from a seed file "
                    "(it is established at import time and is authoritative)"
                )
            if column not in SETTABLE:
                raise SeedError(
                    f"{target}: {key}.{column} is not a known instrument field. "
                    f"Valid fields: {', '.join(sorted(SETTABLE))}"
                )
            # Coerced here, at the boundary, rather than at write time: a bad
            # value should fail before anything touches the database, not
            # halfway through applying a file.
            try:
                coerced[column] = _coerce(column, value)
            except SeedError as exc:
                raise SeedError(f"{target}: {key}.{exc}") from exc
        validated[key] = coerced
    return validated


def apply(conn: sqlite3.Connection, instruments: dict[str, dict[str, Any]]) -> SeedResult:
    result = SeedResult()
    for import_key, fields in instruments.items():
        row = conn.execute(
            "SELECT id FROM instrument WHERE import_key = ?", (import_key,)
        ).fetchone()
        if row is None:
            result.unknown_instruments.append(import_key)
            continue
        if not fields:
            continue
        assignments = ", ".join(f"{column} = ?" for column in fields)
        # Values arrive already coerced by `load`; a caller passing raw values
        # directly is coerced here for symmetry.
        values = [_coerce(column, value) for column, value in fields.items()]
        try:
            conn.execute(f"UPDATE instrument SET {assignments} WHERE id = ?", (*values, row["id"]))
        except sqlite3.IntegrityError as exc:
            # A CHECK rejection here means the seed file offered a value the
            # schema does not allow. Which instrument and which column is the
            # only useful thing to say; a traceback says neither.
            raise SeedError(f"{import_key}: {exc} (fields: {', '.join(fields)})") from exc
        result.updated.append(import_key)
        result.fields_written += len(fields)
    return result
