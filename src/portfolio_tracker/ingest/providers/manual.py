"""User-supplied price files.

Not a fallback. This is the only provider with no external dependency, and it is
therefore the only one guaranteed to work in five years' time. Every other
provider's failure path points here.

Two dialects are accepted, because the useful one is whatever the source
actually produces.

**Native** -- a plain CSV with ``date`` and ``close``, plus an optional
``total_return_close``::

    date,close
    2026-08-13,0.627
    2026-08-14,0.629

**TEFAS export** -- what the Turkish fund platform's own download button
produces, taken verbatim with no editing::

    Dışa Aktarım Tarihi:,15.08.2026 08:15:41
    Toplam Kayıt Sayısı:,753

    Tarih,Fiyat
    14.08.2023,"0,178578"

That dialect writes dates day-first and uses a comma as the decimal
separator. The two are never guessed between: the header row decides, so a
``0.627`` in one dialect can never be silently read as ``0,627`` in the other.
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


#: Header of the TEFAS platform's own export: "Date, Price".
_TEFAS_HEADER = ("tarih", "fiyat")


def _turkish_decimal(raw: str) -> Decimal:
    """``"1,005931"`` and ``"1.234,56"`` -- comma decimal, dot thousands."""
    return Decimal(raw.replace(".", "").replace(",", "."))


def _turkish_date(raw: str) -> date:
    """``14.08.2023`` is day-first, unlike the broker export's month-first form.

    Both appear in this project, which is precisely why each parser is pinned
    to one format rather than sniffing.
    """
    day, month, year = raw.split(".")
    return date(int(year), int(month), int(day))


def _find_header(text: str) -> tuple[list[str], int]:
    """Locate the header row, skipping any preamble.

    The TEFAS export opens with an export timestamp, a record count and a blank
    line before the real header.
    """
    lines = text.splitlines()
    for offset, line in enumerate(lines):
        cells = [c.strip().strip('"').lower() for c in line.split(",")]
        if {"date", "close"} <= set(cells) or tuple(cells[:2]) == _TEFAS_HEADER:
            return lines[offset:], offset + 1
    raise ProviderUnavailable(
        "a manual price file needs either 'date,close' or the TEFAS export's "
        "'Tarih,Fiyat' header row; found neither"
    )


def parse_csv(text: str, *, label: str = "<memory>") -> list[PriceObservation]:
    try:
        body, header_line = _find_header(text)
    except ProviderUnavailable as exc:
        raise ProviderUnavailable(f"{label}: {exc}") from exc

    reader = csv.DictReader(io.StringIO("\n".join(body)))
    fields = [f.strip().lower() for f in (reader.fieldnames or [])]
    tefas = tuple(fields[:2]) == _TEFAS_HEADER

    observations: list[PriceObservation] = []
    for offset, row in enumerate(reader, start=header_line + 1):
        lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        raw_close = lowered.get("fiyat" if tefas else "close", "")
        raw_date = lowered.get("tarih" if tefas else "date", "")
        if not raw_close or not raw_date:
            continue
        try:
            close = _turkish_decimal(raw_close) if tefas else Decimal(raw_close)
            on = _turkish_date(raw_date) if tefas else date.fromisoformat(raw_date)
            total_return = (
                Decimal(lowered["total_return_close"])
                if lowered.get("total_return_close")
                else None
            )
        except (InvalidOperation, ValueError) as exc:
            raise ProviderUnavailable(
                f"{label}:{offset}: unparseable row (date={raw_date!r}, price={raw_close!r})"
            ) from exc
        if close <= 0:
            raise ProviderUnavailable(f"{label}:{offset}: non-positive price {raw_close!r}")
        observations.append(PriceObservation(on, close, total_return))
    return observations


class ManualProvider:
    name = "manual"

    def __init__(self, directory: Path = MANUAL_DIR) -> None:
        self.directory = directory

    def path_for(self, instrument: InstrumentRef) -> Path:
        # ``ZEC/EUR`` is a legal symbol and an illegal filename on every
        # operating system. Without this the crypto pair silently resolves to
        # a `ZEC/` subdirectory on POSIX and raises on Windows.
        return self.directory / f"{instrument.import_key.replace('/', '_')}.csv"

    def supports(self, instrument: InstrumentRef) -> bool:
        return True

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        # `weekly` is accepted and ignored: a file holds whatever the person
        # put in it, and thinning that out would discard data they typed.
        path = self.path_for(instrument)
        if not path.exists():
            raise ProviderUnavailable(
                f"{instrument.import_key}: no manual price file. "
                f"Create {path} with columns: date,close"
            )
        observations = parse_csv(path.read_text(encoding="utf-8-sig"), label=str(path))
        return [o for o in observations if start <= o.on <= end]
