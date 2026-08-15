"""Authoritative values for fields the source export renders lossily.

Keyed by lot key -- the same identity used to build ``external_id`` -- so there
is one notion of "which lot" in the system rather than two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path("seeds/import_overrides.yaml")


class OverrideError(ValueError):
    """The override file is malformed. Raised rather than skipped: a typo here
    silently reverts a correction the user made deliberately."""


@dataclass(frozen=True)
class Overrides:
    quantities: dict[str, Decimal] = field(default_factory=dict)
    fees_eur: dict[str, Decimal] = field(default_factory=dict)
    instruments: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.quantities or self.fees_eur or self.instruments)


def _decimal(value: Any, context: str) -> Decimal:
    if isinstance(value, float):
        raise OverrideError(
            f"{context}: write {value!r} as a quoted string, not a YAML float -- "
            "the precision is already lost by the time it is parsed"
        )
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise OverrideError(f"{context}: not a number: {value!r}") from exc


def load(path: Path | str = DEFAULT_PATH) -> Overrides:
    """Load overrides, or return an empty set if the file does not exist."""
    target = Path(path)
    if not target.exists():
        return Overrides()

    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise OverrideError(f"{target}: expected a mapping at the top level")

    version = raw.get("version")
    if version != 1:
        raise OverrideError(f"{target}: unsupported version {version!r}, expected 1")

    quantities: dict[str, Decimal] = {}
    fees: dict[str, Decimal] = {}

    for entry in raw.get("lots") or []:
        if not isinstance(entry, dict) or "key" not in entry:
            raise OverrideError(f"{target}: every lots[] entry needs a 'key'")
        key = str(entry["key"])
        if entry.get("quantity") is not None:
            quantities[key] = _decimal(entry["quantity"], f"{target}: lot {key} quantity")
        if entry.get("fees_eur") is not None:
            fees[key] = _decimal(entry["fees_eur"], f"{target}: lot {key} fees_eur")

    instruments = raw.get("instruments") or {}
    if not isinstance(instruments, dict):
        raise OverrideError(f"{target}: 'instruments' must be a mapping")

    return Overrides(quantities=quantities, fees_eur=fees, instruments=instruments)
