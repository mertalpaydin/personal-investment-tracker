#!/usr/bin/env python
"""Refuse to commit real financial data or third-party personal information.

This repository holds a real investment portfolio locally while being published
publicly. `.gitignore` is the first line of defence; this script is the second,
because an ignore rule only covers file shapes somebody thought of in advance.

Three call sites, one implementation:

    check_no_pii.py FILE...     pre-commit hook, staged files
    check_no_pii.py --all       CI, every tracked file in the worktree
    check_no_pii.py --history   CI, every path ever added in any commit

The history scan matters more than it looks. A file that was committed and then
deleted is still reachable through the object store and still served by the
GitHub API. Deleting it in a later commit does not undo the disclosure, so a
leak has to fail the build even after it has been "removed".

Exit 0 when clean, 1 when anything is found. Every finding prints as
``path:line  RULE_CODE  message`` so failures are actionable rather than a wall
of regex output.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The single personal datum permitted to be public, per the repository owner.
ALLOWED_EMAILS = frozenset({"mertalpaydin.91@gmail.com"})

# Text files we bother reading. Anything else is checked on its path alone.
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".txt",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".sql",
        ".ini",
        ".cfg",
        ".html",
        ".js",
        ".ts",
        ".ipynb",
        ".env",
    }
)

MAX_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.code}  {self.message}"


# ---------------------------------------------------------------------------
# Rule 1 — path deny. Structural, cheap, and catches the overwhelming majority.
# ---------------------------------------------------------------------------

PATH_DENY: tuple[tuple[str, str, str], ...] = (
    (r"^data/(?!example/)", "PATH_REAL_DATA", "real portfolio data must never be committed"),
    (r"\.(db|sqlite3?)$", "PATH_DATABASE", "database files contain real holdings"),
    (r"^\.env$", "PATH_DOTENV", "secrets file"),
    (r"^config\.yaml$", "PATH_USER_CONFIG", "user config; commit config.example.yaml instead"),
    (
        r"^seeds/(?!.*\.example\.yaml$).*\.ya?ml$",
        "PATH_SEED_PII",
        "seed files carry real quantities; commit the .example.yaml instead",
    ),
    (r"^reports/", "PATH_IMPORT_REPORT", "import reports quote real symbols and amounts"),
)


def check_path(path: str) -> Iterator[Finding]:
    posix = path.replace("\\", "/")
    for pattern, code, message in PATH_DENY:
        if re.search(pattern, posix):
            yield Finding(posix, 0, code, message)


# ---------------------------------------------------------------------------
# Rule 2 — synthetic-symbol allowlist.
#
# The strong rule. Any file shaped like the broker export must contain only
# symbols from the fictional universe. Deliberately an allowlist: a committed
# denylist of real tickers would itself disclose what the owner holds.
# ---------------------------------------------------------------------------

INVESTING_HEADER_PREFIX = "Name,Symbol,Open Date,Type,Amount,"
TRUTH_JSON = REPO_ROOT / "data" / "example" / "truth.json"


def load_synthetic_symbols() -> frozenset[str] | None:
    """Symbols the generator declared. None if the corpus does not exist yet."""
    try:
        data = json.loads(TRUTH_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    symbols = data.get("symbols")
    if not isinstance(symbols, list):
        return None
    return frozenset(str(s) for s in symbols)


def check_positions_csv(path: str, text: str, allowed: frozenset[str] | None) -> Iterator[Finding]:
    stripped = text.lstrip("﻿")
    if not stripped.startswith(INVESTING_HEADER_PREFIX):
        return
    if allowed is None:
        yield Finding(
            path,
            1,
            "CSV_NO_ALLOWLIST",
            "file looks like a positions export but data/example/truth.json is missing, "
            "so its symbols cannot be verified as synthetic",
        )
        return
    for i, row in enumerate(csv.DictReader(io.StringIO(stripped)), start=2):
        symbol = (row.get("Symbol") or "").strip()
        if symbol and symbol not in allowed:
            yield Finding(
                path,
                i,
                "CSV_NON_SYNTHETIC_SYMBOL",
                f"symbol {symbol!r} is not in the fictional universe",
            )


# ---------------------------------------------------------------------------
# Rule 3 — content patterns.
# ---------------------------------------------------------------------------

CONTENT_DENY: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){3,7}[A-Z0-9]{1,4}\b"),
        "IBAN",
        "looks like an IBAN",
    ),
    (
        re.compile(r"(?:Depot|Konto|Kontonummer|Depotnummer|Kundennummer)\s*[:#]?\s*\d{4,}", re.I),
        "ACCOUNT_NUMBER",
        "looks like an account or customer number",
    ),
    # No leading \b: there is no word boundary between a space and '+', so an
    # anchored version of this pattern silently never matches.
    (
        re.compile(r"(?<!\d)(?:\+49|0049)[\d /-]{6,}"),
        "PHONE_DE",
        "looks like a German phone number",
    ),
    (
        re.compile(r"\bSteuer-?(?:ID|Nummer)\s*[:#]?\s*\d{9,}", re.I),
        "STEUER_ID",
        "looks like a tax ID",
    ),
)

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")

# Substrings that only appear in genuine German broker paperwork.
FINGERPRINTS = (
    "Steuerbescheinigung",
    "Wertpapierabrechnung",
    "Freistellungsauftrag",
    "Verrechnungskonto",
)

# These two files necessarily contain the patterns they search for: the guard
# defines them, and its test plants deliberately fake ones to prove each rule
# fires. The exemption is an explicit two-entry list rather than a blanket
# `tests/` or `scripts/` rule, because a genuine leak pasted into some other
# test or script must still be caught.
SELF_REFERENTIAL = frozenset(
    {
        "scripts/check_no_pii.py",
        "tests/test_check_no_pii.py",
    }
)


def check_content(path: str, text: str) -> Iterator[Finding]:
    if path in SELF_REFERENTIAL:
        return
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, code, message in CONTENT_DENY:
            if pattern.search(line):
                yield Finding(path, lineno, code, message)
        for match in EMAIL_RE.finditer(line):
            address = match.group(0)
            if address.lower() not in ALLOWED_EMAILS and not address.endswith("@example.com"):
                yield Finding(
                    path,
                    lineno,
                    "EMAIL",
                    f"third-party email address {address!r}",
                )
        for term in FINGERPRINTS:
            # The word appears legitimately in prose about German tax; only flag
            # it inside data-shaped files, where it indicates a pasted statement.
            if term in line and path.endswith((".csv", ".json", ".txt")):
                yield Finding(
                    path,
                    lineno,
                    "BROKER_FINGERPRINT",
                    f"{term!r} in a data file suggests a real broker document",
                )


# ---------------------------------------------------------------------------
# Rule 4 — notebook outputs. A committed .ipynb with rendered output is a
# screenshot of real holdings in JSON form.
# ---------------------------------------------------------------------------


def check_notebook(path: str, text: str) -> Iterator[Finding]:
    if not path.endswith(".ipynb"):
        return
    try:
        nb = json.loads(text)
    except json.JSONDecodeError:
        return
    for idx, cell in enumerate(nb.get("cells", [])):
        if cell.get("outputs") or cell.get("execution_count") is not None:
            yield Finding(
                path,
                idx + 1,
                "NOTEBOOK_HAS_OUTPUT",
                "notebook cell has stored output; clear outputs before committing",
            )
            return


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def check_file(rel: str, allowed: frozenset[str] | None) -> list[Finding]:
    findings = list(check_path(rel))
    text = read_text(REPO_ROOT / rel)
    if text is not None:
        findings += list(check_positions_csv(rel, text, allowed))
        findings += list(check_content(rel, text))
        findings += list(check_notebook(rel, text))
    return findings


def git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def scan_history() -> list[Finding]:
    """Every path ever added, in any commit on any ref."""
    try:
        paths = set(git("log", "--all", "--diff-filter=A", "--name-only", "--pretty=format:"))
    except subprocess.CalledProcessError:
        return []
    findings: list[Finding] = []
    for path in sorted(paths):
        for f in check_path(path):
            findings.append(
                Finding(f.path, 0, f.code + "_IN_HISTORY", f.message + " (present in git history)")
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="paths to check (pre-commit passes these)")
    parser.add_argument("--all", action="store_true", help="check every tracked file")
    parser.add_argument("--history", action="store_true", help="check every path ever added")
    args = parser.parse_args(argv)

    findings: list[Finding] = []

    if args.history:
        findings += scan_history()

    targets = git("ls-files") if args.all else [f.replace("\\", "/") for f in args.files]

    if targets:
        allowed = load_synthetic_symbols()
        for rel in targets:
            findings += check_file(rel, allowed)

    if findings:
        print("PII guard: refusing to proceed\n", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print(
            f"\n{len(findings)} finding(s). If a match is a false positive, adjust the rule "
            "in scripts/check_no_pii.py -- do not bypass with --no-verify.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
