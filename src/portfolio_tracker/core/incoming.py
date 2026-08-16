"""A folder you drop files into, and a page that says what it found.

The tool's owner does not use a command line. Every routine act therefore has to
be reachable from the page, and importing a fund's constituent list is the most
routine of them -- issuers publish quarterly, and a `pt look-through import`
incantation with a `--instrument` and an `--as-of` is not a thing anyone should
have to remember four times a year.

So: drop the file in ``data/incoming/``. This module reads what is there and
reports what it thinks each file is. It never imports anything on its own.

## Detect, then ask

The detection is a proposal, exactly like every other inference here. A holdings
file does not say which fund it belongs to -- issuers name the file after their
own product code, or after nothing -- and guessing wrong would attach one fund's
constituents to another and silently corrupt every exposure figure downstream.

So the scan reports *what it can read*: which columns it matched, how many lines,
what the weights sum to. The fund and the publication date are chosen by a
person, on the page, with that evidence in front of them. That is the same
bargain the review inbox makes.

## Reading is safe; importing is not

``scan`` only reads. It is safe to call on every page load, and does.

``resolve`` is the guard on the other side: a request names a *file*, never a
path, and the name is resolved inside the incoming directory and rejected if it
escapes. A web surface that accepts a path is a web surface that reads
``/etc/passwd``, even when it is bound to localhost and used by one person.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from portfolio_tracker.ingest import look_through as holdings_ingest

#: Where files are dropped. Under `data/`, which `.gitignore` denies wholesale,
#: so a real holdings file cannot reach GitHub by being left here.
DEFAULT_INCOMING = Path("data/incoming")

#: Imported files move here rather than being deleted, so the folder shows what
#: is still waiting and nothing the owner supplied is ever destroyed.
ARCHIVE_NAME = "imported"

#: Anything larger is not a holdings file, and reading it on a page load would
#: make the page slow for no reason.
MAX_BYTES = 8 * 1024 * 1024

HOLDINGS = "holdings"
POSITIONS_OPEN = "positions_open"
POSITIONS_CLOSED = "positions_closed"
PRICES = "prices"
FX = "fx"
UNKNOWN = "unknown"

#: The Investing.com export's header, which the positions importer keys on.
_POSITIONS_PREFIX = "Name,Symbol,Open Date,Type,Amount,"


@dataclass
class Detected:
    """One file in the folder, and what it appears to be."""

    name: str
    size_bytes: int
    sha256: str
    kind: str
    #: Human-readable summary of what was read. Never a guess about *which*
    #: fund -- only about the shape of the file.
    summary: str = ""
    #: Why it could not be read, when it could not be. A file the tool cannot
    #: parse is reported, not hidden: a folder that silently ignores things is
    #: how a quarter's data goes missing.
    problem: str | None = None
    columns: dict[str, str] = field(default_factory=dict)
    lines: int = 0
    weight_sum: Decimal | None = None
    unit: str | None = None
    #: A date parsed out of the filename, if one is there. A suggestion for the
    #: form, never applied on its own.
    suggested_as_of: date | None = None

    @property
    def importable(self) -> bool:
        return self.kind == HOLDINGS and self.problem is None


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _suggest_date(name: str) -> date | None:
    """An ISO date in the filename, if the issuer put one there."""
    stem = name.replace("_", "-").replace(".", "-")
    parts = stem.split("-")
    for index in range(len(parts) - 2):
        chunk = "-".join(parts[index : index + 3])
        try:
            return date.fromisoformat(chunk)
        except ValueError:
            continue
    return None


def _classify(name: str, raw: bytes) -> Detected:
    found = Detected(name=name, size_bytes=len(raw), sha256=_sha256(raw), kind=UNKNOWN)
    found.suggested_as_of = _suggest_date(name)

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        found.problem = "not a UTF-8 text file"
        return found

    first = text.splitlines()[0] if text.splitlines() else ""
    if first.startswith(_POSITIONS_PREFIX):
        found.kind = POSITIONS_CLOSED if "Close Date" in first else POSITIONS_OPEN
        found.summary = (
            "an Investing.com positions export. Import these from the command line: "
            "they establish the whole ledger and the report needs reading before "
            "anything is written."
        )
        return found

    header = {h.strip().lower() for h in first.split(",")}
    if header == {"date", "close"}:
        found.kind = PRICES
        found.summary = "a manual price series. Point `pt fetch-prices --manual-dir` at it."
        return found
    if header == {"date", "rate"}:
        found.kind = FX
        found.summary = "a manual FX series. Point `pt fetch-fx --manual-dir` at it."
        return found

    # Everything else gets offered to the holdings parser, which is the one
    # that reports precisely why it refused.
    try:
        parsed = holdings_ingest.parse(text)
    except holdings_ingest.LookThroughError as exc:
        found.problem = str(exc)
        # Still worth saying whether it even looked like a table.
        try:
            rows = list(csv.reader(io.StringIO(text)))
            found.summary = f"{len(rows)} row(s), headers: {', '.join(rows[0][:8])}" if rows else ""
        except csv.Error:  # pragma: no cover - malformed beyond csv's tolerance
            found.summary = ""
        return found

    found.kind = HOLDINGS
    found.columns = parsed.columns
    found.lines = len(parsed.lines)
    found.weight_sum = parsed.weight_sum
    found.unit = parsed.unit
    found.summary = (
        f"{len(parsed.lines)} holdings, weights read as {parsed.unit} and summing to "
        f"{parsed.weight_sum:.4f}"
    )
    return found


def scan(directory: Path = DEFAULT_INCOMING) -> list[Detected]:
    """Read the folder and report what each file appears to be. Writes nothing."""
    if not directory.exists():
        return []
    out: list[Detected] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        # Notes left in the folder are not data. Reporting a README as
        # "unrecognised" is noise that trains the reader to ignore the column
        # where real problems appear.
        if path.stem.lower() in {"readme", "notes", "read me"}:
            continue
        if path.stat().st_size > MAX_BYTES:
            out.append(
                Detected(
                    name=path.name,
                    size_bytes=path.stat().st_size,
                    sha256="",
                    kind=UNKNOWN,
                    problem=f"larger than {MAX_BYTES // (1024 * 1024)} MB; not read",
                )
            )
            continue
        out.append(_classify(path.name, path.read_bytes()))
    return out


def resolve(name: str, directory: Path = DEFAULT_INCOMING) -> Path:
    """Turn a filename from a request into a path inside the folder, or refuse.

    The whole guard against a request naming ``../../etc/passwd``. Callers pass
    a *name*; anything that resolves outside the incoming directory is rejected
    rather than sanitised, because a sanitised path is one whose behaviour
    depends on how clever the sanitiser was.
    """
    directory = directory.resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory:
        raise ValueError(f"{name!r} is not a file in the incoming folder")
    if not candidate.is_file():
        raise FileNotFoundError(f"no file named {name!r} in the incoming folder")
    return candidate


@dataclass(frozen=True)
class ImportResult:
    instrument: str
    as_of: date
    holdings: int
    replaced: bool
    archived_to: str | None


def import_holdings(
    conn: sqlite3.Connection,
    *,
    name: str,
    instrument: str,
    as_of: date,
    directory: Path = DEFAULT_INCOMING,
    archive: bool = True,
) -> ImportResult:
    """Import one dropped holdings file against a fund the owner chose.

    The fund and the date are arguments because nothing in the file reliably
    states either, and attaching one issuer's constituents to another fund
    would corrupt every exposure figure without erroring anywhere.
    """
    path = resolve(name, directory)
    row = conn.execute(
        "SELECT id, import_key FROM instrument WHERE import_key = ? OR isin = ? OR symbol = ?",
        (instrument, instrument.upper(), instrument),
    ).fetchone()
    if row is None:
        raise LookupError(f"no instrument matches {instrument!r}")

    parsed, digest = holdings_ingest.load(path)
    result = holdings_ingest.store(
        conn,
        instrument_id=int(row["id"]),
        as_of=as_of,
        parsed=parsed,
        source=f"{path.name} (dropped in {directory.name})",
        file_sha256=digest,
    )

    archived: str | None = None
    if archive:
        archived = _archive(path, directory)
    return ImportResult(
        instrument=str(row["import_key"]),
        as_of=as_of,
        holdings=result.holdings,
        replaced=result.replaced,
        archived_to=archived,
    )


def _archive(path: Path, directory: Path) -> str:
    """Move an imported file aside. Never deletes anything the owner supplied."""
    target_dir = directory / ARCHIVE_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"{stamp}-{path.name}"
    # A second import in the same second would otherwise clobber the first.
    counter = 1
    while target.exists():
        target = target_dir / f"{stamp}-{counter}-{path.name}"
        counter += 1
    path.rename(target)
    return f"{ARCHIVE_NAME}/{target.name}"
