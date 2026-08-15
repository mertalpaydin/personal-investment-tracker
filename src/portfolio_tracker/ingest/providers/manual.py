"""User-supplied price files.

Not a fallback. This is the only provider with no external dependency, and it is
therefore the only one guaranteed to work in five years' time. Every other
provider's failure path points here.

Format is a plain CSV with ``date`` and ``close``, plus an optional
``total_return_close``::

    date,close
    2026-08-13,0.627
    2026-08-14,0.629
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from portfolio_tracker.ingest.providers.base import (
    InstrumentRef,
    PriceObservation,
    ProviderUnavailable,
)

MANUAL_DIR = Path("data/prices_manual")


def parse_csv(text: str, *, label: str = "<memory>") -> list[PriceObservation]:
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.strip().lower() for f in (reader.fieldnames or [])}
    if not {"date", "close"} <= fields:
        raise ProviderUnavailable(
            f"{label}: a manual price file needs at least 'date' and 'close' columns, "
            f"got {sorted(fields) or 'nothing'}"
        )

    observations: list[PriceObservation] = []
    for number, row in enumerate(reader, start=2):
        lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        raw_close = lowered.get("close", "")
        if not raw_close:
            continue
        try:
            close = Decimal(raw_close)
            total_return = (
                Decimal(lowered["total_return_close"])
                if lowered.get("total_return_close")
                else None
            )
        except InvalidOperation as exc:
            raise ProviderUnavailable(f"{label}:{number}: unparseable price {raw_close!r}") from exc
        if close <= 0:
            raise ProviderUnavailable(f"{label}:{number}: non-positive price {raw_close!r}")
        observations.append(
            PriceObservation(date.fromisoformat(lowered["date"]), close, total_return)
        )
    return observations


class ManualProvider:
    name = "manual"

    def __init__(self, directory: Path = MANUAL_DIR) -> None:
        self.directory = directory

    def path_for(self, instrument: InstrumentRef) -> Path:
        return self.directory / f"{instrument.import_key}.csv"

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(self, instrument: InstrumentRef, start: date, end: date) -> list[PriceObservation]:
        path = self.path_for(instrument)
        if not path.exists():
            raise ProviderUnavailable(
                f"{instrument.import_key}: no manual price file. "
                f"Create {path} with columns: date,close"
            )
        observations = parse_csv(path.read_text(encoding="utf-8-sig"), label=str(path))
        return [o for o in observations if start <= o.on <= end]
