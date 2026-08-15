"""The clean-clone walkthrough, run for real.

The README promises that a fresh clone reaches a working dashboard with no
network access. A promise in a README is a comment; this is the test. It runs
the documented commands end to end against the committed corpus and asserts on
what a visitor would actually see.

The number the whole file exists for is in
``test_without_inferred_contributions_the_return_is_nonsense``: the source
export records no deposits, so every purchase looks like internal growth and the
time-weighted return comes out above 15,000%. That is not a rounding problem, it
is the central difficulty of reconstructing a ledger from a positions view, and
it deserves a test that states the wrong number as well as the right one.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_tracker import seeds as instrument_seeds
from portfolio_tracker.core import cash, doctor, rebuild, returns
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.importers.investing_com.importer import import_positions
from portfolio_tracker.ingest import fx as ingest_fx
from portfolio_tracker.ingest import prices as ingest_prices
from portfolio_tracker.ingest.providers.manual import ManualProvider

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "data" / "example"
SEED_FILE = REPO_ROOT / "seeds" / "instruments.example.yaml"

#: The corpus's as-of date. Fixed, so the test does not drift with the calendar.
AS_OF = date(2026, 8, 14)

D = Decimal


def _imported(tmp_path: Path) -> sqlite3.Connection:
    """`pt init-db` + `pt import` + `pt seed`, as the README runs them."""
    conn = connect(tmp_path / "p.db")
    M.migrate(conn)
    assert M.seed_default_owner(conn), "a fresh database must get a person and a depot"
    conn.commit()

    report = import_positions(conn, EXAMPLE / "open.csv", EXAMPLE / "closed.csv", account_id=1)
    assert report.ok, [str(f) for f in report.errors]

    instrument_seeds.apply(conn, instrument_seeds.load(SEED_FILE))
    conn.commit()
    return conn


def _priced(conn: sqlite3.Connection) -> None:
    """`pt fetch-fx --offline` + `pt fetch-prices --manual-dir`. No network."""
    ingest_fx.store(conn, ingest_fx.load_manual("TRY", EXAMPLE / "fx"))
    conn.commit()

    providers = ingest_prices.default_providers()
    manual = ManualProvider(EXAMPLE / "prices")
    # Every provider is the manual one: a networked provider reached from a
    # test is a flaky test, and the corpus is fictional so none could answer.
    for name in list(providers):
        providers[name] = manual
    coverage = ingest_prices.fetch_all(conn, end=AS_OF, providers=providers)
    assert coverage, "the corpus has instruments; something should have been priced"
    for result in coverage:
        assert result.pct == D(100), f"{result.import_key} at {result.pct}% coverage"


@pytest.fixture
def built(tmp_path: Path) -> sqlite3.Connection:
    """The full documented pipeline, contributions included."""
    conn = _imported(tmp_path)
    _priced(conn)
    cash.propose_deposits(conn)
    conn.commit()
    for entry in conn.execute("SELECT id FROM pending_entry WHERE status = 'PROPOSED'").fetchall():
        cash.confirm(conn, int(entry["id"]))
    conn.commit()
    result = rebuild.run(conn, end=AS_OF)
    assert result.breaks == [], result.breaks
    return conn


# ---------------------------------------------------------------------------
# The corpus can be valued at all
# ---------------------------------------------------------------------------


def test_every_instrument_is_priced_from_committed_files(tmp_path: Path) -> None:
    """No network, no missing days. Asserted in `_priced`."""
    conn = _imported(tmp_path)
    _priced(conn)
    assert conn.execute("SELECT count(*) FROM price").fetchone()[0] > 3000


def test_the_seed_covers_every_instrument_in_the_corpus(tmp_path: Path) -> None:
    """An uncovered instrument means `pt doctor` fails on the documented run."""
    conn = _imported(tmp_path)
    unresolved = conn.execute(
        "SELECT group_concat(import_key) FROM instrument "
        "WHERE tax_regime = 'DE_INVESTMENT' AND teilfreistellung_pct IS NULL"
    ).fetchone()[0]
    assert unresolved is None, f"no Teilfreistellung for: {unresolved}"


# ---------------------------------------------------------------------------
# The point of the exercise
# ---------------------------------------------------------------------------


def test_without_inferred_contributions_the_return_is_nonsense(tmp_path: Path) -> None:
    """Skip `flows propose` and the tool reports five-figure percentage growth.

    Not a subtle error. Every purchase is money arriving that the export never
    recorded, so a portfolio that starts near zero and reaches EUR 155,000 is
    read as pure investment skill.
    """
    conn = _imported(tmp_path)
    _priced(conn)
    rebuild.run(conn, end=AS_OF)
    summary = returns.summarise(conn, person_id=1)
    assert summary.external_flows_eur == D(0)
    assert summary.twr is not None and summary.twr > D(100)  # i.e. over 10,000%


def test_inferring_contributions_makes_the_return_believable(
    built: sqlite3.Connection,
) -> None:
    summary = returns.summarise(built, person_id=1)
    assert summary.external_flows_eur > D(130_000)
    assert summary.twr_annualised is not None
    assert D("0.05") < summary.twr_annualised < D("0.50")
    assert summary.xirr_reliability == "low"  # the flows were inferred, and it says so


def test_the_headline_total_is_holdings_plus_deposits(built: sqlite3.Connection) -> None:
    row = built.execute(
        "SELECT * FROM portfolio_daily WHERE person_id = 1 ORDER BY date DESC LIMIT 1"
    ).fetchone()
    assert row["date"] == AS_OF.isoformat()
    assert row["total_value_eur"] == row["market_value_eur"] + row["deposits_eur"]
    assert row["total_value_eur"] > D(150_000)


# ---------------------------------------------------------------------------
# The claim the project is built on, demonstrated on public data
# ---------------------------------------------------------------------------


def _export_net_pl(symbol: str) -> Decimal:
    """Sum the export's own EUR profit column for one symbol."""
    total = D(0)
    with (EXAMPLE / "open.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["Symbol"] != symbol:
                continue
            raw = row["Net P/L"]
            for junk in ("€", " ", ",", " "):
                raw = raw.replace(junk, "")
            total += D(raw)
    return total


@pytest.mark.parametrize("symbol", ["ZTFA", "ZTFB"])
def test_the_tool_reproduces_the_export_and_then_corrects_it(
    built: sqlite3.Connection, symbol: str
) -> None:
    """Both halves matter, and the second is the product.

    Reproducing the export's number proves the parsing is right. Differing from
    it *by a stated amount* proves the export is wrong -- it translates a lira
    gain at today's rate, as if the capital had been invested this morning.
    """
    row = built.execute(
        "SELECT p.unrealized_eur, p.unrealized_local_eur, p.unrealized_fx_eur "
        "FROM position_daily p JOIN instrument i ON i.id = p.instrument_id "
        "WHERE i.import_key = ? AND p.date = ?",
        (symbol, AS_OF.isoformat()),
    ).fetchone()

    expected = _export_net_pl(symbol)
    # We reproduce their figure...
    assert abs(row["unrealized_local_eur"] - expected) <= D("0.05")
    # ...and ours is lower, by exactly the currency effect, which is the lira
    # decline on the original capital that their arithmetic cannot represent.
    assert row["unrealized_fx_eur"] < D(0)
    assert row["unrealized_eur"] == row["unrealized_local_eur"] + row["unrealized_fx_eur"]


# ---------------------------------------------------------------------------
# And it all holds together
# ---------------------------------------------------------------------------


def test_the_documented_run_ends_with_a_clean_doctor(built: sqlite3.Connection) -> None:
    report = doctor.run(built)
    assert report.errors == [], [str(f) for f in report.errors]
    assert report.rows_examined > 4000


def test_the_dashboard_renders_on_the_example_corpus(built: sqlite3.Connection) -> None:
    """The README's last line. A visitor gets a page, not a stack trace."""
    from portfolio_tracker.api.render import render_dashboard

    page = render_dashboard(built, person_id=1)
    assert "Portfolio" in page
    assert AS_OF.isoformat() in page
    assert "Outside the total" in page
