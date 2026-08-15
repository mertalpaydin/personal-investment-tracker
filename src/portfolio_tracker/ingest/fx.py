"""Fetching exchange rates.

The ECB is the primary source: free, no key, no rate limit, published back to
1999, quoted natively as units-per-euro (so no inversion is ever needed), and it
is the rate German tax practice uses for currency conversion.

Two secondary paths exist. A cross-check against a second source catches the one
failure the ECB will not announce -- a convention change. And a manual CSV drop
is a permanent escape hatch: an undocumented endpoint that stops working must
degrade to "put the file here yourself", never to a crash.

Parsing is separated from fetching throughout, so the tests never touch the
network.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from portfolio_tracker.core.fx import BASE, SOURCE_PRIORITY

ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.{quote}.EUR.SP00.A"
    "?format=csvdata&startPeriod={start}"
)

MANUAL_DIR = Path("data/fx_manual")

Fetcher = Callable[[str], str]


class FxIngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    on: date
    quote: str
    rate: Decimal
    source: str


def _http_get(url: str, *, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "portfolio-tracker/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload: bytes = response.read()
            return payload.decode("utf-8")
    except urllib.error.URLError as exc:
        raise FxIngestError(f"could not reach {url}: {exc}") from exc


def parse_ecb_csv(text: str, quote: str) -> list[Observation]:
    """Read the ECB's csvdata response.

    Only ``TIME_PERIOD`` and ``OBS_VALUE`` are used, by name rather than
    position -- the response carries about thirty columns and their order is not
    a documented guarantee. Rows with an empty value are holidays and are
    skipped rather than being carried as zero.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "TIME_PERIOD" not in reader.fieldnames:
        raise FxIngestError(
            f"unexpected ECB response: no TIME_PERIOD column (got {reader.fieldnames})"
        )

    observations: list[Observation] = []
    for row in reader:
        raw_value = (row.get("OBS_VALUE") or "").strip()
        if not raw_value:
            continue
        try:
            rate = Decimal(raw_value)
        except InvalidOperation as exc:
            raise FxIngestError(f"unparseable ECB rate {raw_value!r}") from exc
        if rate <= 0:
            raise FxIngestError(f"non-positive ECB rate {raw_value!r}")
        observations.append(Observation(date.fromisoformat(row["TIME_PERIOD"]), quote, rate, "ecb"))
    return observations


def fetch_ecb(quote: str, start: date, *, fetcher: Fetcher = _http_get) -> list[Observation]:
    """Daily reference rates for ``quote`` from ``start`` onwards."""
    url = ECB_URL.format(quote=quote, start=start.isoformat())
    return parse_ecb_csv(fetcher(url), quote)


def parse_manual_csv(text: str, quote: str) -> list[Observation]:
    """A two-column ``date,rate`` file: the permanent fallback.

    Rates must be quoted the same way as everywhere else -- units of `quote` per
    one euro -- so a file cannot introduce an inverted rate through the back
    door.
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.lower() for f in (reader.fieldnames or [])}
    if not {"date", "rate"} <= fields:
        raise FxIngestError(f"manual FX file needs 'date' and 'rate' columns, got {fields}")

    observations = []
    for row in reader:
        lowered = {k.lower(): v for k, v in row.items()}
        raw = (lowered.get("rate") or "").strip()
        if not raw:
            continue
        observations.append(
            Observation(date.fromisoformat(lowered["date"].strip()), quote, Decimal(raw), "manual")
        )
    return observations


def load_manual(quote: str, directory: Path = MANUAL_DIR) -> list[Observation]:
    path = directory / f"{quote}.csv"
    if not path.exists():
        return []
    return parse_manual_csv(path.read_text(encoding="utf-8-sig"), quote)


def store(conn: sqlite3.Connection, observations: list[Observation]) -> int:
    """Insert rates, never downgrading a better source with a worse one.

    An ECB rate is not overwritten by a cross-check source that happened to
    arrive later; a manually supplied rate outranks both, because someone chose
    to put it there.
    """
    if not observations:
        return 0

    written = 0
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for obs in observations:
        existing = conn.execute(
            "SELECT source FROM fx_rate WHERE date = ? AND base = ? AND quote = ?",
            (obs.on.isoformat(), BASE, obs.quote),
        ).fetchone()
        if existing is not None:
            incumbent = SOURCE_PRIORITY.get(existing["source"], 99)
            challenger = SOURCE_PRIORITY.get(obs.source, 99)
            if challenger >= incumbent:
                continue
            conn.execute(
                "UPDATE fx_rate SET rate = ?, source = ?, fetched_at = ? "
                "WHERE date = ? AND base = ? AND quote = ?",
                (obs.rate, obs.source, now, obs.on.isoformat(), BASE, obs.quote),
            )
        else:
            conn.execute(
                "INSERT INTO fx_rate (date, base, quote, rate, source, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (obs.on.isoformat(), BASE, obs.quote, obs.rate, obs.source, now),
            )
        written += 1
    return written


@dataclass(frozen=True)
class Divergence:
    quote: str
    on: date
    primary: Decimal
    secondary: Decimal

    @property
    def bps(self) -> Decimal:
        return abs(self.primary - self.secondary) / self.primary * 10000


def compare(
    primary: list[Observation], secondary: list[Observation], *, tolerance_bps: int = 20
) -> list[Divergence]:
    """Days where two sources disagree beyond tolerance.

    A persistent divergence means one of them changed convention, which is the
    one failure the ECB will not announce and which no amount of validating a
    single source can detect.
    """
    by_date = {obs.on: obs.rate for obs in secondary}
    out = []
    for obs in primary:
        other = by_date.get(obs.on)
        if other is None:
            continue
        divergence = Divergence(obs.quote, obs.on, obs.rate, other)
        if divergence.bps > tolerance_bps:
            out.append(divergence)
    return out
