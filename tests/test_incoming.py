"""The drop folder, and the page controls that replaced the command line.

The owner of this tool does not use a command line, so every routine act has to
be reachable from the page. Two properties carry that:

``test_a_request_cannot_name_a_path_outside_the_folder`` is the security one. A
web surface that accepts a path is a web surface that reads ``/etc/passwd``,
localhost or not.

``test_a_holdings_file_is_never_imported_without_being_told_which_fund`` is the
correctness one. A holdings file does not say which fund it belongs to, and
attaching one issuer's constituents to another fund corrupts every exposure
figure without erroring anywhere.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_tracker.api import app as api_module
from portfolio_tracker.api import deps as api_deps
from portfolio_tracker.api.app import app as api_app
from portfolio_tracker.api.app import get_db, get_writable_db
from portfolio_tracker.core import incoming
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

AS_OF = "2026-06-30"
PUBLISHED = date(2026, 5, 31)

HOLDINGS_CSV = """Name,ISIN,Sector,Asset Class,Market Currency,Location,Weight (%)
Nordwind Technologies,XX0000000101,Information Technology,Equity,USD,United States,60.00
Halcyon Semiconductor,XX0000000102,Information Technology,Equity,USD,United States,40.00
"""

POSITIONS_CSV = "Name,Symbol,Open Date,Type,Amount,Open Price,Current Price\nA,B,1,BUY,1,1,1\n"
PRICES_CSV = "date,close\n2026-01-01,100\n"
FX_CSV = "date,rate\n2026-01-01,35\n"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    drop = tmp_path / "incoming"
    drop.mkdir()
    return drop


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "teilfreistellung_pct) VALUES (1, 'ZGEQ', 'Global Equity', 'ETF', 'EUR', 1, '0.30')"
    )
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# What is in the folder
# ---------------------------------------------------------------------------


def test_an_empty_or_missing_folder_is_not_an_error(tmp_path: Path, folder: Path) -> None:
    assert incoming.scan(folder) == []
    assert incoming.scan(tmp_path / "never-created") == []


def test_a_holdings_file_is_recognised_and_summarised(folder: Path) -> None:
    (folder / "whatever.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    found = incoming.scan(folder)[0]
    assert found.kind == incoming.HOLDINGS
    assert found.importable
    assert found.lines == 2
    assert found.unit == "percent"
    assert "2 holdings" in found.summary


def test_the_other_file_shapes_are_named_rather_than_offered(folder: Path) -> None:
    """A folder that silently ignores things is how a quarter's data goes missing."""
    (folder / "open.csv").write_text(POSITIONS_CSV, encoding="utf-8")
    (folder / "prices.csv").write_text(PRICES_CSV, encoding="utf-8")
    (folder / "TRY.csv").write_text(FX_CSV, encoding="utf-8")
    kinds = {f.name: f.kind for f in incoming.scan(folder)}
    assert kinds["open.csv"] == incoming.POSITIONS_OPEN
    assert kinds["prices.csv"] == incoming.PRICES
    assert kinds["TRY.csv"] == incoming.FX
    assert all(not f.importable for f in incoming.scan(folder))


def test_a_file_that_cannot_be_read_says_why(folder: Path) -> None:
    (folder / "nonsense.csv").write_text("garbage,not,a,file\n1,2,3,4\n", encoding="utf-8")
    found = incoming.scan(folder)[0]
    assert not found.importable
    assert found.problem is not None
    assert "weight" in found.problem


def test_a_binary_file_is_reported_not_crashed_on(folder: Path) -> None:
    (folder / "statement.pdf").write_bytes(b"\x00\x01\x02\xff\xfe")
    found = incoming.scan(folder)[0]
    assert found.problem == "not a UTF-8 text file"


def test_a_date_in_the_filename_is_offered_as_a_suggestion(folder: Path) -> None:
    """Issuers put the publication date in the filename often enough to help,
    and never reliably enough to trust."""
    (folder / "ishares-global-2026-07-31.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    assert incoming.scan(folder)[0].suggested_as_of == date(2026, 7, 31)


def test_a_filename_with_no_date_suggests_none(folder: Path) -> None:
    (folder / "holdings.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    assert incoming.scan(folder)[0].suggested_as_of is None


def test_hidden_files_and_subfolders_are_skipped(folder: Path) -> None:
    (folder / ".DS_Store").write_text("x", encoding="utf-8")
    (folder / "imported").mkdir()
    (folder / "real.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    assert [f.name for f in incoming.scan(folder)] == ["real.csv"]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../secrets.txt", "../../etc/passwd", "..\\..\\windows", "sub/nested.csv", "/etc/passwd"],
)
def test_a_request_cannot_name_a_path_outside_the_folder(folder: Path, name: str) -> None:
    """A web surface that accepts a path reads whatever it is pointed at.

    Rejected rather than sanitised: a sanitised path behaves however clever the
    sanitiser was, and that is not a property anyone can check.
    """
    with pytest.raises((ValueError, FileNotFoundError)):
        incoming.resolve(name, folder)


def test_a_name_that_is_simply_absent_is_refused(folder: Path) -> None:
    with pytest.raises(FileNotFoundError):
        incoming.resolve("missing.csv", folder)


def test_a_real_file_resolves(folder: Path) -> None:
    (folder / "real.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    assert incoming.resolve("real.csv", folder).name == "real.csv"


# ---------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------


def test_importing_stores_the_composition_and_files_the_csv_away(
    db: sqlite3.Connection, folder: Path
) -> None:
    (folder / "h.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    result = incoming.import_holdings(
        db, name="h.csv", instrument="ZGEQ", as_of=PUBLISHED, directory=folder
    )
    assert result.holdings == 2
    assert result.instrument == "ZGEQ"

    stored = db.execute("SELECT count(*) AS n FROM fund_holding").fetchone()["n"]
    assert stored == 2

    # Moved, not deleted. Nothing the owner supplied is destroyed, and the
    # folder shows what is still waiting.
    assert not (folder / "h.csv").exists()
    assert result.archived_to is not None
    assert (folder / result.archived_to).exists()


def test_an_unknown_fund_is_refused_before_anything_is_written(
    db: sqlite3.Connection, folder: Path
) -> None:
    (folder / "h.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    with pytest.raises(LookupError, match="no instrument"):
        incoming.import_holdings(
            db, name="h.csv", instrument="NOPE", as_of=PUBLISHED, directory=folder
        )
    assert db.execute("SELECT count(*) AS n FROM fund_holding").fetchone()["n"] == 0
    assert (folder / "h.csv").exists(), "the file stays put when nothing was imported"


def test_two_imports_in_the_same_second_do_not_clobber_each_other(
    db: sqlite3.Connection, folder: Path
) -> None:
    for name in ("a.csv", "b.csv"):
        (folder / name).write_text(HOLDINGS_CSV, encoding="utf-8")
        incoming.import_holdings(
            db, name=name, instrument="ZGEQ", as_of=PUBLISHED, directory=folder
        )
    archived = list((folder / incoming.ARCHIVE_NAME).iterdir())
    assert len(archived) == 2


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db: sqlite3.Connection, folder: Path) -> Iterator[TestClient]:
    api_app.dependency_overrides[get_db] = lambda: db
    api_app.dependency_overrides[get_writable_db] = lambda: db
    previous = api_deps.INCOMING_DIR
    api_deps.INCOMING_DIR = folder
    with TestClient(api_app) as test_client:
        yield test_client
    api_deps.INCOMING_DIR = previous
    api_app.dependency_overrides.clear()


def test_the_endpoint_lists_files_and_the_funds_to_choose_from(
    client: TestClient, folder: Path
) -> None:
    (folder / "h.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    body = client.get("/api/incoming").json()
    assert [f["name"] for f in body["files"]] == ["h.csv"]
    assert body["files"][0]["importable"] is True
    assert [f["instrument"] for f in body["funds"]] == ["ZGEQ"]


def test_a_holdings_file_is_never_imported_without_being_told_which_fund(
    client: TestClient, folder: Path
) -> None:
    """The file names the issuer's product code at best. Guessing which fund it
    belongs to would corrupt every exposure figure and error nowhere."""
    (folder / "h.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    listing = client.get("/api/incoming").json()
    # The scan reports what it read and volunteers no fund.
    assert "instrument" not in listing["files"][0]
    assert "choose which fund" in listing["note"]

    for missing in ({"name": "h.csv"}, {"name": "h.csv", "instrument": "ZGEQ"}):
        assert client.post("/api/incoming/import", json=missing).status_code == 422


def test_importing_over_http_works_end_to_end(client: TestClient, folder: Path) -> None:
    (folder / "h.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    response = client.post(
        "/api/incoming/import",
        json={"name": "h.csv", "instrument": "ZGEQ", "as_of": "2026-05-31"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["holdings"] == 2
    assert body["rebuild_required"] is False
    assert client.get("/api/incoming").json()["files"] == []


@pytest.mark.parametrize("name", ["../x.csv", "..\\x.csv", "sub/x.csv"])
def test_the_endpoint_refuses_a_path(client: TestClient, name: str) -> None:
    response = client.post(
        "/api/incoming/import",
        json={"name": name, "instrument": "ZGEQ", "as_of": "2026-05-31"},
    )
    assert response.status_code == 400


def test_importing_takes_the_writable_connection_and_listing_does_not(
    client: TestClient,
) -> None:
    """Write access is something a route asks for, not something it inherits."""
    import inspect

    assert (
        inspect.signature(api_module.incoming_files).parameters["conn"].default.dependency
        is api_module.get_db
    )
    assert (
        inspect.signature(api_module.import_incoming).parameters["conn"].default.dependency
        is api_module.get_writable_db
    )


def test_notes_left_in_the_folder_are_not_reported_as_data(folder: Path) -> None:
    """Reporting a README as 'unrecognised' trains the reader to ignore the
    column where real problems appear."""
    (folder / "README.txt").write_text("how to use this folder\n", encoding="utf-8")
    (folder / "notes.md").write_text("reminders\n", encoding="utf-8")
    (folder / "real.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    assert [f.name for f in incoming.scan(folder)] == ["real.csv"]


# ---------------------------------------------------------------------------
# The page's own wiring
# ---------------------------------------------------------------------------


def _page(db: sqlite3.Connection, folder: Path) -> str:
    from portfolio_tracker.api import deps as module
    from portfolio_tracker.api.render import render_dashboard

    db.execute(
        "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
        "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
        "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
        "completeness, drawdown_pct) VALUES ('2026-06-30', 1, '100', '100', '100', '1', "
        "'100', '0', '0', '0', '100', '0', '0', 'FULL', '0')"
    )
    db.commit()
    previous = module.INCOMING_DIR
    module.INCOMING_DIR = folder
    try:
        return render_dashboard(db)
    finally:
        module.INCOMING_DIR = previous


def test_every_button_the_page_offers_is_actually_wired_up(
    db: sqlite3.Connection, folder: Path
) -> None:
    """The bug this exists to prevent, which shipped and was hit.

    ``ptPost`` lived in the review section's script, and that section renders
    nothing when the inbox is empty. So with nothing awaiting review the
    function did not exist: Import and the Vorabpauschale button each disabled
    themselves, said "working...", threw a ReferenceError before any promise
    existed for a ``.catch`` to attach to, and hung.

    A control that hangs is worse than one that errors, because there is
    nothing to read and nothing to retry.
    """
    import re

    (folder / "h.csv").write_text(HOLDINGS_CSV, encoding="utf-8")
    page = _page(db, folder)

    defined = set(re.findall(r"function\s+(pt\w+)\s*\(", page))
    called = set(re.findall(r"(pt[A-Z]\w*)\s*\(", page))
    assert called - defined == set(), f"called but never defined: {sorted(called - defined)}"


def test_the_shared_helpers_survive_an_empty_review_inbox(
    db: sqlite3.Connection, folder: Path
) -> None:
    """Specifically the state the owner was in when it broke."""
    from portfolio_tracker.core import inbox

    assert inbox.listing(db) == []
    page = _page(db, folder)
    assert "function ptPost(" in page
    assert "function ptRun(" in page
    assert "function ptRebuild(" in page
