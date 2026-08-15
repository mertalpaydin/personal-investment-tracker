"""CLI behaviour, including that failures exit non-zero.

A command that prints an error and exits 0 is worse than one that crashes: it
passes in a script, in CI, and in a cron job, and the failure is only noticed
when the data it was supposed to produce turns out to be missing.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from portfolio_tracker import __version__
from portfolio_tracker.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_bare_invocation_shows_help_rather_than_failing() -> None:
    result = runner.invoke(app, [])
    assert "init-db" in result.stdout


def test_init_db_creates_the_database(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.db"
    result = runner.invoke(app, ["init-db", "--db", str(target)])
    assert result.exit_code == 0, result.stdout
    assert "0001" in result.stdout
    assert target.exists()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.db"
    runner.invoke(app, ["init-db", "--db", str(target)])
    result = runner.invoke(app, ["init-db", "--db", str(target)])
    assert result.exit_code == 0
    assert "already up to date" in result.stdout


def test_init_db_reset_derived_reports_what_it_cleared(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.db"
    runner.invoke(app, ["init-db", "--db", str(target)])
    result = runner.invoke(app, ["init-db", "--db", str(target), "--reset-derived"])
    assert result.exit_code == 0
    assert "derived table" in result.stdout


def test_doctor_refuses_a_database_that_does_not_exist(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--db", str(tmp_path / "absent.db")])
    assert result.exit_code == 1
    assert "init-db" in result.output


def test_doctor_passes_on_an_empty_but_valid_database(tmp_path: Path) -> None:
    """Nothing to check is not the same as something wrong."""
    target = tmp_path / "portfolio.db"
    runner.invoke(app, ["init-db", "--db", str(target)])
    result = runner.invoke(app, ["doctor", "--db", str(target)])
    assert result.exit_code == 0, result.output
    assert "every identity holds" in result.output


def test_doctor_json_is_machine_readable(tmp_path: Path) -> None:
    target = tmp_path / "portfolio.db"
    runner.invoke(app, ["init-db", "--db", str(target)])
    result = runner.invoke(app, ["doctor", "--db", str(target), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["checks_run"] > 0
    assert isinstance(payload["findings"], list)


def test_serve_refuses_a_database_that_does_not_exist(tmp_path: Path) -> None:
    """A 503 on the first request is worse: nobody is watching the log by then."""
    result = runner.invoke(app, ["serve", "--db", str(tmp_path / "absent.db")])
    assert result.exit_code == 1
    assert "init-db" in result.output
