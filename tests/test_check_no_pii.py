"""The PII guard is the mechanism the whole publish-safely story rests on.

An untested guard is decoration, so each rule gets a planted violation that it
must catch, plus a negative case that it must not flag. The false-positive
tests matter as much as the true-positive ones: a guard that cries wolf gets
bypassed with --no-verify, and then it protects nothing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = REPO_ROOT / "scripts" / "check_no_pii.py"


def _load_guard():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("check_no_pii", GUARD_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_no_pii"] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def codes(findings) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.code for f in findings}


# ---------------------------------------------------------------------------
# Rule 1 — path deny
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("data/open.csv", "PATH_REAL_DATA"),
        ("data/closed.csv", "PATH_REAL_DATA"),
        ("data/holdings_manual/ishares.csv", "PATH_REAL_DATA"),
        ("data/portfolio.db", "PATH_DATABASE"),
        ("some/nested/thing.sqlite3", "PATH_DATABASE"),
        (".env", "PATH_DOTENV"),
        ("config.yaml", "PATH_USER_CONFIG"),
        ("seeds/instruments.yaml", "PATH_SEED_PII"),
        ("seeds/import_overrides.yaml", "PATH_SEED_PII"),
        ("reports/import.md", "PATH_IMPORT_REPORT"),
    ],
)
def test_path_deny_catches_real_data(path: str, expected: str) -> None:
    assert expected in codes(guard.check_path(path))


@pytest.mark.parametrize(
    "path",
    [
        "data/example/open.csv",
        "data/example/closed.csv",
        "data/example/truth.json",
        "data/example/README.md",
        "seeds/instruments.example.yaml",
        "config.example.yaml",
        ".env.example",
        "src/portfolio_tracker/core/fx.py",
        "tests/data/tiny.csv",
    ],
)
def test_path_deny_allows_legitimate_files(path: str) -> None:
    assert list(guard.check_path(path)) == []


def test_windows_separators_are_normalised() -> None:
    """pre-commit on Windows can hand over backslash paths."""
    assert "PATH_REAL_DATA" in codes(guard.check_path(r"data\open.csv"))


# ---------------------------------------------------------------------------
# Rule 2 — synthetic-symbol allowlist
# ---------------------------------------------------------------------------

HEADER = "Name,Symbol,Open Date,Type,Amount,Open Price,Current Price,Market Value,Daily P/L,Net P/L"


def _positions_csv(symbol: str) -> str:
    return f'{HEADER}\nSome Fund ,{symbol},05/09/2025,BUY,35.9144,139.22,297.85,"€ 10,697.10",€ 17.95,"€ 5,697.10"\n'


def test_real_ticker_in_positions_csv_is_rejected() -> None:
    allowed = frozenset({"ZGEQ", "ZTFA"})
    findings = guard.check_positions_csv("data/example/open.csv", _positions_csv("ABECG"), allowed)
    assert "CSV_NON_SYNTHETIC_SYMBOL" in codes(findings)


def test_synthetic_ticker_in_positions_csv_is_accepted() -> None:
    allowed = frozenset({"ZGEQ", "ZTFA"})
    assert (
        list(guard.check_positions_csv("data/example/open.csv", _positions_csv("ZGEQ"), allowed))
        == []
    )


def test_positions_csv_survives_a_utf8_bom() -> None:
    """The corpus is written with a BOM on purpose; the guard must see past it."""
    allowed = frozenset({"ZGEQ"})
    text = "﻿" + _positions_csv("ABECG")
    assert "CSV_NON_SYNTHETIC_SYMBOL" in codes(
        guard.check_positions_csv("data/example/open.csv", text, allowed)
    )


def test_missing_allowlist_fails_closed() -> None:
    """No truth.json means symbols cannot be verified -- refuse, don't wave through."""
    findings = guard.check_positions_csv("data/example/open.csv", _positions_csv("ABECG"), None)
    assert "CSV_NO_ALLOWLIST" in codes(findings)


def test_non_positions_csv_is_ignored_by_rule_2() -> None:
    text = "date,rate\n2026-08-14,55.25\n"
    assert list(guard.check_positions_csv("data/fx.csv", text, frozenset())) == []


# ---------------------------------------------------------------------------
# Rule 3 — content patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("IBAN: DE89 3704 0044 0532 0130 00", "IBAN"),
        ("Kontonummer: 1234567890", "ACCOUNT_NUMBER"),
        ("Depotnummer 998877665", "ACCOUNT_NUMBER"),
        ("call +49 170 1234567", "PHONE_DE"),
        ("Steuer-ID: 12345678901", "STEUER_ID"),
        ("contact someone.else@gmail.com for details", "EMAIL"),
    ],
)
def test_content_patterns_are_caught(text: str, expected: str) -> None:
    assert expected in codes(guard.check_content("docs/notes.md", text))


@pytest.mark.parametrize(
    "text",
    [
        "the owner's address is mertalpaydin.91@gmail.com",  # allowlisted
        "write to someone@example.com for the docs",  # documentation placeholder
        "Teilfreistellung is 30% for equity funds",
        "the Sparerpauschbetrag is EUR 1,000 per person",
        "EGV2 holds 196.3038 units",
    ],
)
def test_content_patterns_do_not_fire_on_legitimate_prose(text: str) -> None:
    assert list(guard.check_content("docs/notes.md", text)) == []


def test_broker_fingerprint_flags_data_files_only() -> None:
    line = "Steuerbescheinigung 2025"
    assert "BROKER_FINGERPRINT" in codes(guard.check_content("data/example/x.csv", line))
    # The same word is ordinary prose in a design document.
    assert "BROKER_FINGERPRINT" not in codes(guard.check_content("docs/adr/0001.md", line))


def test_guard_does_not_flag_itself() -> None:
    """The guard necessarily contains every pattern it searches for."""
    text = GUARD_PATH.read_text(encoding="utf-8")
    assert list(guard.check_content("scripts/check_no_pii.py", text)) == []


def test_guard_does_not_flag_its_own_test() -> None:
    """This file plants fake PII on purpose, so it is exempt too."""
    text = Path(__file__).read_text(encoding="utf-8")
    assert list(guard.check_content("tests/test_check_no_pii.py", text)) == []


def test_self_exemption_is_narrow() -> None:
    """Exactly two files are exempt -- not all of tests/ or scripts/.

    A leak pasted into some other test must still be caught, so this pins the
    carve-out rather than letting it quietly widen.
    """
    assert set(guard.SELF_REFERENTIAL) == {
        "scripts/check_no_pii.py",
        "tests/test_check_no_pii.py",
    }
    leak = "Kontonummer: 1234567890"
    assert "ACCOUNT_NUMBER" in codes(guard.check_content("tests/test_something_else.py", leak))
    assert "ACCOUNT_NUMBER" in codes(guard.check_content("scripts/other_tool.py", leak))


# ---------------------------------------------------------------------------
# Rule 4 — notebook outputs
# ---------------------------------------------------------------------------


def test_notebook_with_stored_output_is_rejected() -> None:
    nb = json.dumps(
        {"cells": [{"cell_type": "code", "execution_count": 3, "outputs": [{"text": "€133,217"}]}]}
    )
    assert "NOTEBOOK_HAS_OUTPUT" in codes(guard.check_notebook("notebooks/scratch.ipynb", nb))


def test_cleared_notebook_is_accepted() -> None:
    nb = json.dumps({"cells": [{"cell_type": "code", "execution_count": None, "outputs": []}]})
    assert list(guard.check_notebook("notebooks/clean.ipynb", nb)) == []


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_main_passes_on_the_current_worktree() -> None:
    """The repository as committed must be clean, or the guard is already failing."""
    assert guard.main(["--all"]) == 0


def test_main_rejects_a_planted_violation() -> None:
    assert guard.main(["data/open.csv"]) == 1


def test_history_scan_is_clean() -> None:
    """No commit on any ref may ever have added a real data file."""
    assert guard.scan_history() == []
