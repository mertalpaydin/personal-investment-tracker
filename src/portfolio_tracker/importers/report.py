"""The import report: what was read, what was inferred, and what is missing.

Every finding carries a stable ``code`` so tests assert on codes rather than on
prose, and so the wording can improve without breaking anything.

The report is not decoration. This importer reconstructs a ledger from a file
that is not one, infers quantities the source destroyed, and leaves fees and
dividends unknown because they were never present. Any of that could quietly
produce a plausible, wrong number. The report is where those inferences are
stated out loud.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.severity:<7} {self.code:<28} {self.message}"


@dataclass
class ImportReport:
    source_files: dict[str, str] = field(default_factory=dict)
    rows_read: int = 0
    rows_parsed: int = 0
    instruments_seen: int = 0
    instruments_created: int = 0
    lots_reconstructed: int = 0
    buys_inserted: int = 0
    buys_updated: int = 0
    sells_inserted: int = 0
    unchanged: int = 0
    dry_run: bool = False
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: Severity, message: str, **detail: Any) -> None:
        self.findings.append(Finding(code, severity, message, detail))

    def of(self, code: str) -> list[Finding]:
        return [f for f in self.findings if f.code == code]

    @property
    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str, ensure_ascii=False)

    def to_text(self) -> str:
        lines = [
            "Investing.com positions import" + ("  [DRY RUN]" if self.dry_run else ""),
            "",
        ]
        for name, digest in self.source_files.items():
            lines.append(f"  {name}  sha256 {digest[:12]}")
        lines += [
            "",
            f"  rows read           {self.rows_read}",
            f"  rows parsed         {self.rows_parsed}",
            f"  instruments         {self.instruments_seen} seen, {self.instruments_created} new",
            f"  lots reconstructed  {self.lots_reconstructed}",
            f"  BUY                 {self.buys_inserted} inserted, {self.buys_updated} updated",
            f"  SELL                {self.sells_inserted} inserted",
            f"  unchanged           {self.unchanged}",
            "",
        ]
        if self.findings:
            for finding in self.findings:
                lines.append(f"  {finding}")
            lines.append("")
        lines.append(f"  RESULT  {len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        return "\n".join(lines)
