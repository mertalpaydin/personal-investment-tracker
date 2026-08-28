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
from portfolio_tracker.core import attribution, cash, doctor, fx, look_through, rebuild, returns
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.importers.investing_com.importer import import_positions
from portfolio_tracker.ingest import fx as ingest_fx
from portfolio_tracker.ingest import look_through as look_through_ingest
from portfolio_tracker.ingest import prices as ingest_prices
from portfolio_tracker.ingest.providers.manual import ManualProvider

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "data" / "example"
SEED_FILE = REPO_ROOT / "seeds" / "instruments.example.yaml"

#: The corpus's as-of date. Fixed, so the test does not drift with the calendar.
AS_OF = date(2026, 8, 14)

#: The compositions the corpus ships, with the dates their issuers published
#: them. ZDIV's is deliberately half a year old and ZOVN/ZTFA/ZTFB have none.
COMPOSITIONS: dict[str, date] = {
    "ZGEQ": date(2026, 7, 31),
    "ZSEM": date(2026, 7, 31),
    "ZBNK": date(2026, 6, 30),
    "ZGLD": date(2026, 7, 31),
    "ZDIV": date(2026, 1, 30),
}

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
    """The full documented pipeline, contributions recorded.

    The contributions are entered here rather than inferred from purchases.
    That inference used to run automatically and is gone: a purchase larger
    than any recorded cash might have been funded by a deposit, or by an
    account this tool cannot see, and assuming the first invented 85% of the
    owner's savings figure. What a person records is what counts, so the
    corpus records some.
    """
    conn = _imported(tmp_path)
    _priced(conn)

    # Each unfunded purchase, entered as the deposit that funded it -- the
    # decision a person makes on the Trades page, made here in one line.
    for event in cash.unfunded_purchases(conn):
        rate = D(1)
        if event.currency != fx.BASE:
            rate = fx.get_rate(conn, event.currency, event.on, strict=False).rate
        conn.execute(
            "INSERT INTO transaction_ (account_id, type, trade_date, currency, "
            "gross_amount_local, gross_amount_eur, fx_rate, source, confidence, "
            "flow_class, note) VALUES (?, 'DEPOSIT', ?, ?, ?, ?, ?, 'MANUAL', 'ACTUAL', "
            "'EXTERNAL_IN', 'recorded contribution')",
            (
                event.account_id,
                event.on.isoformat(),
                event.currency,
                str(event.amount),
                str(fx.to_eur(event.amount, rate)),
                str(rate),
            ),
        )
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


def test_recorded_contributions_make_the_return_believable(
    built: sqlite3.Connection,
) -> None:
    """Unit accounting needs the contributions to be there.

    Without them the money crossing in is read as investment return, and the
    owner's saving is played back as skill. The difference between recording
    them and inferring them is only *who decided* -- the arithmetic below is
    the same either way, and it is the arithmetic that makes the figure sane.
    """
    summary = returns.summarise(built, person_id=1)
    assert summary.external_flows_eur > D(130_000)
    assert summary.twr_annualised is not None
    assert D("0.05") < summary.twr_annualised < D("0.50")


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


# ---------------------------------------------------------------------------
# Where the return came from
# ---------------------------------------------------------------------------


def test_the_corpus_return_is_mostly_prices_and_partly_the_lira(
    built: sqlite3.Connection,
) -> None:
    """The headline claim, at portfolio level rather than per position.

    The holdings rose far more than the euro return shows, and the lira took
    the difference. Both halves are real; a tracker that reports only the euro
    figure is not wrong, it is silent about the larger of the two movements.
    """
    state = attribution.summarise(built, person_id=1)
    assert state is not None

    assert state.local > state.total, "prices did more than the euro return shows"
    assert state.currency < D("-0.15"), "the lira cost more than fifteen points"
    # And the parts are the whole. Exactly, not nearly.
    assert abs(state.residual) < attribution.LINKING_TOLERANCE


def test_the_model_explains_almost_all_of_the_return(built: sqlite3.Connection) -> None:
    """`unexplained` is trading, and on this corpus trading is small.

    Worth pinning: if a refactor started dumping price moves into the
    unexplained term the totals would still reconcile perfectly and every
    identity would still hold.
    """
    state = attribution.summarise(built, person_id=1)
    assert state is not None
    assert abs(state.unexplained) < D("0.01"), f"unexplained is {state.unexplained:.2%}"


def test_chaining_the_daily_contributions_naively_would_be_wrong(
    built: sqlite3.Connection,
) -> None:
    """The number the linking exists for, on real data.

    Adding up 527 correct daily contributions gives about 45% against a true
    56%. Nothing about the wrong figure looks wrong, which is why this is a
    test rather than a comment.
    """
    rows = built.execute(
        "SELECT ret_local_contrib l, ret_fx_contrib f, ret_income_contrib i, ret_residual r "
        "FROM portfolio_daily WHERE person_id = 1 AND ret_local_contrib IS NOT NULL"
    ).fetchall()
    assert len(rows) > 500

    naive = sum((r["l"] + r["f"] + (r["i"] or D(0)) + (r["r"] or D(0)) for r in rows), D(0))
    state = attribution.summarise(built, person_id=1)
    assert state is not None
    assert abs(state.total - naive) > D("0.10"), "the linking gap should be ten points or more"


def test_the_attribution_agrees_with_the_unit_series(built: sqlite3.Connection) -> None:
    """Two code paths, one number, neither consulting the other."""
    state = attribution.summarise(built, person_id=1)
    summary = returns.summarise(built, person_id=1)
    assert state is not None and summary.twr is not None
    assert abs(state.total - summary.twr) < D("0.0001")


# ---------------------------------------------------------------------------
# Look-through, on the committed compositions
# ---------------------------------------------------------------------------


@pytest.fixture
def with_compositions(built: sqlite3.Connection) -> sqlite3.Connection:
    """`pt look-through import`, for every composition the corpus ships."""
    for key, as_of in COMPOSITIONS.items():
        parsed, digest = look_through_ingest.load(EXAMPLE / "holdings" / f"{key}.csv")
        instrument_id = built.execute(
            "SELECT id FROM instrument WHERE import_key = ?", (key,)
        ).fetchone()["id"]
        look_through_ingest.store(
            built,
            instrument_id=int(instrument_id),
            as_of=as_of,
            parsed=parsed,
            source=f"example corpus {key}",
            file_sha256=digest,
        )
    built.commit()
    return built


def test_every_committed_composition_loads_without_a_unit_hint(
    with_compositions: sqlite3.Connection,
) -> None:
    """Two issuer dialects, one per-cent and one fractional, both read by ``auto``.

    If this ever needs a hint the corpus has stopped covering the case it was
    built for.
    """
    stored = with_compositions.execute(
        "SELECT count(*) AS sets, sum(holdings) AS lines FROM fund_holding_set"
    ).fetchone()
    assert stored["sets"] == len(COMPOSITIONS)
    assert stored["lines"] == 48
    units = {
        r["unit"] for r in with_compositions.execute("SELECT DISTINCT unit FROM fund_holding_set")
    }
    assert units == {"percent", "fraction"}


def test_the_corpus_is_all_euro_quoted_and_not_remotely_all_euro_exposure(
    with_compositions: sqlite3.Connection,
) -> None:
    """The v1 gap and the v2 answer, on the same portfolio, in one assertion.

    Every euro-quoted instrument in the corpus contributes 100% euro exposure on
    a quote basis. Opening five of them up moves EUR 37,000 of that into seven
    other currencies -- risk that was not merely unreported before, it was
    unrepresentable.
    """
    quoted = {
        r["currency"]: r["v"]
        for r in with_compositions.execute(
            "SELECT i.currency, total(p.market_value_eur) AS v FROM position_daily p "
            "JOIN instrument i ON i.id = p.instrument_id WHERE p.date = ? GROUP BY i.currency",
            (AS_OF.isoformat(),),
        )
    }
    result = look_through.exposure(with_compositions, dimension="currency", as_of=AS_OF)
    economic = {b.name: b.value_eur for b in result.buckets}

    assert set(quoted) == {"EUR", "TRY"}
    assert economic["EUR"] < D(str(quoted["EUR"])) - D(30000)
    assert {"USD", "JPY", "GBP", "CHF", "CAD", "AUD", "SEK"} <= set(economic)
    # The lira funds have no composition, so their exposure is unchanged and
    # correct: a lira-quoted Turkish fund really is lira exposure.
    assert economic["TRY"] == D(str(quoted["TRY"]))


def test_the_corpus_coverage_is_partial_and_says_so(
    with_compositions: sqlite3.Connection,
) -> None:
    """A corpus where every fund is covered would teach nothing about coverage."""
    result = look_through.exposure(with_compositions, dimension="currency", as_of=AS_OF)
    assert {p.import_key for p in result.coverage.uncovered} == {"ZOVN", "ZTFA", "ZTFB"}
    assert result.coverage.look_through_pct < D("0.6")
    assert any("no published composition" in c for c in result.caveats)
    # ZDIV is dated six months before the valuation, deliberately.
    assert any("may no longer describe it" in c for c in result.caveats)


def test_a_share_held_outright_and_inside_two_funds_is_one_exposure(
    with_compositions: sqlite3.Connection,
) -> None:
    """The number no instrument-level view in this repository can produce."""
    result = look_through.concentration(with_compositions, as_of=AS_OF)
    acme = next(u for u in result.underlyings if u.name == "Acme Robotics C")
    assert {key for key, _ in acme.via} == {"ACRO", "ZGEQ", "ZSEM"}
    direct = with_compositions.execute(
        "SELECT market_value_eur FROM position_daily p JOIN instrument i ON i.id = p.instrument_id "
        "WHERE p.date = ? AND i.import_key = 'ACRO'",
        (AS_OF.isoformat(),),
    ).fetchone()["market_value_eur"]
    assert acme.value_eur > direct


def test_look_through_leaves_the_doctor_clean(with_compositions: sqlite3.Connection) -> None:
    """Warnings for the gaps, errors for nothing."""
    report = doctor.run(with_compositions)
    assert report.errors == [], [str(f) for f in report.errors]
    assert {"LOOK_THROUGH_MISSING", "LOOK_THROUGH_STALE"} <= report.codes
