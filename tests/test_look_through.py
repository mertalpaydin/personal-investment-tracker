"""ETF look-through: reading compositions, and what they change.

The claim this feature exists to make is one line long: a euro-quoted global
tracker is dollar exposure, and every v1 view said it was euro exposure. That
claim is tested first and directly, in
``test_a_euro_quoted_fund_of_dollar_assets_reports_as_dollar_exposure``.

Everything else in this file guards the two ways a look-through feature goes
wrong quietly. The first is the weight unit: the same column is ``7.24`` in one
issuer's file and ``0.0724`` in another's, and a wrong guess produces a
portfolio that is 99% cash and looks like a bug somewhere else. The second is
coverage: a breakdown computed over the two funds that happen to have holdings
files looks exactly like one computed over all seven, and the second is a much
stronger claim than the first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from portfolio_tracker.api.app import app as api_app
from portfolio_tracker.api.app import get_db, get_writable_db
from portfolio_tracker.api.render import render_dashboard
from portfolio_tracker.cli import app as cli_app
from portfolio_tracker.core import doctor, look_through
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.ingest import look_through as ingest

D = Decimal
runner = CliRunner()

AS_OF = "2026-06-30"
PUBLISHED = date(2026, 5, 31)

# A fund whose constituents are overwhelmingly not denominated in the currency
# it is quoted in. This is the whole feature in six rows.
GLOBAL_FUND_CSV = """Name,ISIN,Sector,Asset Class,Market Currency,Location,Weight (%)
Nordwind Technologies,XX0000000101,Information Technology,Equity,USD,United States,40.00
Halcyon Semiconductor,XX0000000102,Information Technology,Equity,USD,United States,25.00
Sakura Precision,XX0000000107,Industrials,Equity,JPY,Japan,15.00
Rheinturm Industrie,XX0000000109,Industrials,Equity,EUR,Germany,12.00
Acme Robotics C,XX0000000201,Industrials,Equity,EUR,Germany,6.00
Cash and/or Derivatives,,Cash,Cash,EUR,,2.00
"""

# The other dialect: different headers, fractions rather than per cent, no
# asset-class column at all.
BANK_FUND_CSV = """Holding,Identifier,Industry,Currency,Country,Weight
Alderman Bank,XX0000000112,Financials,EUR,Germany,0.6000
Banque Ravel,XX0000000113,Financials,EUR,France,0.4000
"""


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_a_per_cent_column_is_recognised_and_converted_exactly_once() -> None:
    """The trap: 41.5 -> 0.415 in the parser, 0.00415 in the writer.

    That bug has already shipped in this repository once, in the deposit-rate
    path, and it survived review because the wrong number was still plausible.
    """
    parsed = ingest.parse(GLOBAL_FUND_CSV)
    assert parsed.unit == "percent"
    assert parsed.weight_sum == D("1.0000")
    assert parsed.lines[0].weight == D("0.4000")


def test_a_fraction_column_is_recognised_and_left_alone() -> None:
    parsed = ingest.parse(BANK_FUND_CSV)
    assert parsed.unit == "fraction"
    assert parsed.lines[0].weight == D("0.6000")


def test_a_sum_that_is_neither_one_nor_a_hundred_is_refused_rather_than_guessed() -> None:
    """Halfway between the two readings is where a silent divide-by-100 lives."""
    ambiguous = "Name,Weight\nA,30\nB,25\n"
    with pytest.raises(ingest.LookThroughError, match="cannot tell what the weight column"):
        ingest.parse(ambiguous)


def test_an_explicit_unit_turns_ambiguity_into_a_specific_complaint() -> None:
    """Once the unit is stated, the failure is about the file, not the reader."""
    with pytest.raises(ingest.LookThroughError, match=r"0\.550000 of the fund"):
        ingest.parse("Name,Weight\nA,30\nB,25\n", unit="percent")


def test_both_issuer_dialects_map_onto_the_same_fields() -> None:
    a = ingest.parse(GLOBAL_FUND_CSV)
    b = ingest.parse(BANK_FUND_CSV)
    assert a.columns["weight"] == "Weight (%)"
    assert a.columns["country"] == "Location"
    assert b.columns["name"] == "Holding"
    assert b.columns["sector"] == "Industry"
    # Dialect B has no asset-class column. That is a gap, not an error.
    assert "asset_class" not in b.columns


def test_a_file_with_no_weight_column_names_the_headers_it_will_accept() -> None:
    with pytest.raises(ingest.LookThroughError, match="Accepted names for weight"):
        ingest.parse("Name,ISIN\nA,X\n")


def test_footer_and_spacer_rows_are_skipped_and_counted() -> None:
    """Issuer files are full of them, and a skipped row must not read as a small fund."""
    text = GLOBAL_FUND_CSV + ",,,,,,\nTotal,,,,,,100.00\n".replace("Total", "")
    parsed = ingest.parse(text)
    assert len(parsed.lines) == 6
    assert parsed.skipped == 2


def test_the_awkward_number_formats_real_files_use() -> None:
    """Every one of these raises on ``Decimal(raw)``, and all five occur.

    The tolerance is relaxed deliberately: this is a test of the number reader,
    and the sum rule is tested on its own above.
    """
    text = (
        "Name,Weight\n"
        'A,"1,234.00"\n'  # thousands separator
        "B,(234.00)\n"  # accounting negative
        "C,100.00%\n"  # trailing sign
        'D,"-1,000.00"\n'  # both at once
        "E,-100.00\n"
    )
    parsed = ingest.parse(text, unit="percent", tolerance=D(100))
    assert [line.weight for line in parsed.lines] == [
        D("12.34"),
        D("-2.34"),
        D("1"),
        D("-10"),
        D("-1"),
    ]
    assert parsed.negative == 3


def test_a_bom_and_crlf_file_reads(tmp_path: Path) -> None:
    """A spreadsheet export is the normal case, not the exotic one."""
    path = tmp_path / "h.csv"
    path.write_bytes(b"\xef\xbb\xbf" + BANK_FUND_CSV.replace("\n", "\r\n").encode("utf-8"))
    parsed, digest = ingest.load(path)
    assert len(parsed.lines) == 2
    assert len(digest) == 64


def test_weights_are_not_normalised_on_the_way_in() -> None:
    """The published sum is evidence and is kept; the scaling happens on read."""
    drifted = BANK_FUND_CSV.replace("0.4000", "0.3900")
    parsed = ingest.parse(drifted)
    assert parsed.weight_sum == D("0.9900")
    assert parsed.drift == D("-0.0100")


def test_a_partial_file_is_refused_before_it_can_be_scaled_up() -> None:
    """A filtered export and a whole fund look identical once normalised."""
    with pytest.raises(ingest.LookThroughError, match=r"not 1\.0"):
        ingest.parse(BANK_FUND_CSV.replace("0.4000", "0.1000"), unit="fraction")


# ---------------------------------------------------------------------------
# The fixture portfolio
# ---------------------------------------------------------------------------


def _position(conn: sqlite3.Connection, instrument_id: int, value: str) -> None:
    conn.execute(
        """
        INSERT INTO position_daily
            (date, account_id, instrument_id, quantity, currency, price_local,
             price_asof, price_stale_days, fx_rate, fx_asof, fx_stale_days,
             market_value_local, market_value_eur, cost_basis_local, cost_basis_eur,
             cost_basis_eur_at_spot, unrealized_eur, unrealized_local_eur,
             unrealized_fx_eur, valuation_status)
        VALUES (?, 1, ?, '1', 'EUR', ?, ?, 0, '1', ?, 0, ?, ?, ?, ?, ?, '0', '0', '0', 'OK')
        """,
        (AS_OF, instrument_id, value, AS_OF, AS_OF, value, value, value, value, value),
    )


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Five holdings, chosen so that every attribution basis occurs exactly once.

    ``ZGEQ``  a euro-quoted fund with a composition -- look_through
    ``ZBNK``  a euro fund with a composition in the other dialect -- look_through
    ``ZHDG``  a fund with no composition but a declared economic currency
    ``ZOVN``  a fund with neither -- quote_only, the honest gap
    ``ACRO``  a share, which is its own exposure -- direct
    """
    # `same_thread=False` for the same reason the API fixture needs it: anyio's
    # worker pool does not pin threads, so the TestClient below routinely
    # queries this connection from a different one than created it.
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    for iid, key, name, is_fund, economic, isin, sector in [
        (1, "ZGEQ", "Global Equity", 1, None, None, None),
        (2, "ZBNK", "Eurozone Banks", 1, None, None, None),
        (3, "ZHDG", "Hedged World", 1, "USD", None, None),
        (4, "ZOVN", "Overnight Return", 1, None, None, None),
        (5, "ACRO", "Acme Robotics C", 0, None, "XX0000000201", "Industrials"),
    ]:
        conn.execute(
            "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
            "economic_currency, isin, sector, domicile, teilfreistellung_pct) "
            "VALUES (?, ?, ?, 'ETF', 'EUR', ?, ?, ?, ?, 'Germany', '0.30')",
            (iid, key, name, is_fund, economic, isin, sector),
        )
    # 10,000 in total, in round numbers so every expectation below is exact.
    for iid, value in [(1, "5000"), (2, "1000"), (3, "1000"), (4, "2000"), (5, "1000")]:
        _position(conn, iid, value)
    conn.execute(
        "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
        "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
        "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
        "completeness, drawdown_pct) VALUES (?, 1, '10000', '10000', '10000', '100', "
        "'100', '0', '0', '0', '10000', '0', '0', 'PARTIAL', '0')",
        (AS_OF,),
    )

    for iid, text in [(1, GLOBAL_FUND_CSV), (2, BANK_FUND_CSV)]:
        parsed = ingest.parse(text)
        ingest.store(
            conn,
            instrument_id=iid,
            as_of=PUBLISHED,
            parsed=parsed,
            source="test fixture",
            file_sha256="0" * 64,
        )
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Exposure -- the claim the feature exists to make
# ---------------------------------------------------------------------------


def test_a_euro_quoted_fund_of_dollar_assets_reports_as_dollar_exposure(
    db: sqlite3.Connection,
) -> None:
    """The one sentence this feature is for.

    ZGEQ is quoted in euro and worth EUR 5,000. 65% of what it holds is
    denominated in dollars, so EUR 3,250 of the portfolio is dollar risk -- a
    figure that was structurally invisible in v1, where the same position
    contributed EUR 5,000 of "euro exposure". ZHDG's declared USD adds the
    other 1,000.

    The yen figure is the cleaner half of the claim: nothing in this portfolio
    is quoted in yen, so EUR 750 of yen exposure can only have come from
    opening a fund up.
    """
    result = look_through.exposure(db, dimension="currency")
    by_name = {b.name: b.value_eur for b in result.buckets}
    assert by_name["USD"] == D(4250)
    assert by_name["JPY"] == D(750)

    quoted = db.execute(
        "SELECT total(market_value_eur) FROM position_daily p "
        "JOIN instrument i ON i.id = p.instrument_id WHERE i.currency = 'EUR'"
    ).fetchone()[0]
    assert Decimal(str(quoted)) == D(10000), "every instrument is quoted in euro"


def test_the_buckets_sum_to_the_holdings_they_describe(db: sqlite3.Connection) -> None:
    """An identity, not an approximation: nothing may fall out of a breakdown."""
    for dimension in look_through.DIMENSIONS:
        result = look_through.exposure(db, dimension=dimension)
        assert sum((b.value_eur for b in result.buckets), D(0)) == D(10000)
        assert result.total_eur == D(10000)


def test_a_missing_attribute_becomes_an_explicit_unknown_bucket(db: sqlite3.Connection) -> None:
    """The cash line has no country and dialect B carries no asset class.

    Dropping those rows would leave the remaining weights summing to 1 over a
    smaller portfolio, which is the same lie told more quietly.
    """
    countries = {
        b.name: b.value_eur for b in look_through.exposure(db, dimension="country").buckets
    }
    assert countries[look_through.UNKNOWN] >= D(100)  # ZGEQ's 2% cash line
    classes = {
        b.name: b.value_eur for b in look_through.exposure(db, dimension="asset_class").buckets
    }
    assert classes[look_through.UNKNOWN] >= D(1000)  # all of ZBNK


def test_each_holding_is_attributed_on_exactly_one_named_basis(db: sqlite3.Connection) -> None:
    coverage = look_through.exposure(db, dimension="currency").coverage
    assert coverage.by_basis == {
        look_through.LOOK_THROUGH: D(6000),
        look_through.DECLARED: D(1000),
        look_through.QUOTE_ONLY: D(2000),
        look_through.DIRECT: D(1000),
    }
    assert sum(coverage.by_basis.values(), D(0)) == coverage.total_eur
    # Evidence means constituents or a holding that is its own exposure. A
    # declaration is neither, and does not count towards it.
    assert coverage.look_through_pct == D("0.7")


def test_a_declaration_is_used_for_currency_and_for_nothing_else(db: sqlite3.Connection) -> None:
    """``economic_currency`` is an assertion about currency. It says nothing
    about what sector a fund holds, and inventing one would be indistinguishable
    from evidence a week later."""
    currency = look_through.exposure(db, dimension="currency")
    assert {b.name: b.value_eur for b in currency.buckets}["USD"] >= D(1000)
    assert look_through.DECLARED in currency.coverage.by_basis

    sector = look_through.exposure(db, dimension="sector")
    assert look_through.DECLARED not in sector.coverage.by_basis
    assert sector.coverage.by_basis[look_through.QUOTE_ONLY] == D(3000)


def test_a_fund_with_no_composition_is_named_rather_than_absorbed(
    db: sqlite3.Connection,
) -> None:
    result = look_through.exposure(db, dimension="currency")
    assert [p.import_key for p in result.coverage.uncovered] == ["ZOVN"]
    assert any("ZOVN" in c and "listing fact" in c for c in result.caveats)


def test_a_composition_published_after_the_valuation_does_not_apply(
    db: sqlite3.Connection,
) -> None:
    """A file downloaded today does not describe what was held last year."""
    early = look_through.exposure(db, dimension="currency", as_of=date(2026, 1, 31))
    assert early.coverage.by_basis.get(look_through.LOOK_THROUGH) is None


def test_weights_that_do_not_sum_to_one_still_distribute_the_whole_position(
    db: sqlite3.Connection,
) -> None:
    """A file summing to 0.99 describes a whole fund badly, not 99% of a fund.

    Scaling is the only defensible reading, and it is stated rather than
    performed silently: the raw sum is stored and the caveat names it.
    """
    db.execute("DELETE FROM fund_holding WHERE instrument_id = 2 AND line_no = 2")
    db.execute(
        "UPDATE fund_holding_set SET weight_sum = '0.6000', holdings = 1 WHERE instrument_id = 2"
    )
    result = look_through.exposure(db, dimension="currency")
    assert result.total_eur == D(10000)
    assert sum((b.value_eur for b in result.buckets), D(0)) == D(10000)
    assert any("0.600000" in c for c in result.caveats)


def test_an_unknown_dimension_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown dimension"):
        look_through.exposure(db, dimension="account")


def test_exposure_without_a_valuation_says_what_to_run(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM position_daily")
    with pytest.raises(look_through.NoValuation, match="pt rebuild"):
        look_through.exposure(db)


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------


def test_a_company_held_directly_and_through_a_fund_is_one_exposure(
    db: sqlite3.Connection,
) -> None:
    """The view no instrument-level report can produce.

    Acme Robotics is EUR 1,000 held outright plus 6% of a EUR 5,000 fund. The
    positions table shows EUR 1,000 and nothing else; the real exposure is
    EUR 1,300, and it is only visible once the ISINs are matched.
    """
    result = look_through.concentration(db)
    acme = next(u for u in result.underlyings if "Acme" in u.name)
    assert acme.value_eur == D(1300)
    assert dict(acme.via) == {"ACRO": D(1000), "ZGEQ": D(300)}
    assert acme.identifier == "XX0000000201"


def test_weights_are_against_total_holdings_so_a_gap_dilutes_rather_than_inflates(
    db: sqlite3.Connection,
) -> None:
    """The direction of the error matters more than its size.

    Weighting against the resolved part alone would overstate every issuer by
    exactly the share that could not be resolved -- and a concentration measure
    that overstates is worse than useless, because it is read as a warning.
    """
    result = look_through.concentration(db)
    acme = next(u for u in result.underlyings if "Acme" in u.name)
    assert acme.weight == D("0.13")  # 1300 / 10000, not 1300 / 7000
    # ZGEQ + ZBNK + the direct share. ZHDG has a declared currency but no
    # constituents, so it resolves to no issuer at all.
    assert result.resolved_eur == D(7000)
    assert result.total_eur == D(10000)


def test_an_unlooked_through_fund_is_not_listed_as_though_it_were_an_issuer(
    db: sqlite3.Connection,
) -> None:
    result = look_through.concentration(db)
    assert not any("Overnight" in u.name for u in result.underlyings)
    assert any("ZOVN" in c for c in result.caveats)


def test_concentration_is_ranked_and_can_be_truncated(db: sqlite3.Connection) -> None:
    result = look_through.concentration(db, top=3)
    assert len(result.underlyings) == 3
    values = [u.value_eur for u in result.underlyings]
    assert values == sorted(values, reverse=True)


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------


def test_reimporting_the_same_date_replaces_rather_than_duplicates(
    db: sqlite3.Connection,
) -> None:
    """Merging would produce a composition nobody ever published."""
    parsed = ingest.parse(GLOBAL_FUND_CSV)
    result = ingest.store(
        db,
        instrument_id=1,
        as_of=PUBLISHED,
        parsed=parsed,
        source="second download",
        file_sha256="1" * 64,
    )
    assert result.replaced
    rows = db.execute("SELECT count(*) AS n FROM fund_holding WHERE instrument_id = 1").fetchone()[
        "n"
    ]
    assert rows == 6


def test_a_later_composition_supersedes_an_earlier_one(db: sqlite3.Connection) -> None:
    ingest.store(
        db,
        instrument_id=1,
        as_of=date(2026, 6, 30),
        parsed=ingest.parse(GLOBAL_FUND_CSV.replace("40.00", "20.00").replace("25.00", "45.00")),
        source="june",
        file_sha256="2" * 64,
    )
    result = look_through.exposure(db, dimension="currency")
    # Both files are 65% dollar in total; the June one moves the split between
    # the two dollar names, so the fund-level figure is unchanged and the
    # per-issuer one is not. Concentration is where a supersession shows.
    top = look_through.concentration(db)
    nordwind = next(u for u in top.underlyings if "Nordwind" in u.name)
    assert nordwind.value_eur == D(1000)  # 20% of 5,000, not 40%
    assert {b.name: b.value_eur for b in result.buckets}["USD"] == D(4250)


def test_deleting_a_composition_takes_its_lines_with_it(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM fund_holding_set WHERE instrument_id = 1")
    orphans = db.execute(
        "SELECT count(*) AS n FROM fund_holding WHERE instrument_id = 1"
    ).fetchone()["n"]
    assert orphans == 0


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def _look_through_findings(conn: sqlite3.Connection) -> doctor.DoctorReport:
    """Only the look-through checks, so an unrelated gap cannot mask them."""
    report = doctor.DoctorReport()
    doctor.check_look_through(conn, report)
    return report


def test_clean_compositions_raise_no_errors(db: sqlite3.Connection) -> None:
    assert not _look_through_findings(db).errors


def test_rows_lost_after_import_are_caught(db: sqlite3.Connection) -> None:
    """The one failure mode nothing else would notice.

    The remaining weights still sum to the stored divisor's *former* value, so
    the percentages still add to 100%. Only comparing the rows against what the
    file said catches it.
    """
    db.execute("DELETE FROM fund_holding WHERE instrument_id = 1 AND line_no = 1")
    codes = _look_through_findings(db).codes
    assert "LOOK_THROUGH_ROWS_MISSING" in codes
    assert "LOOK_THROUGH_SUM_MISMATCH" in codes


def test_a_stored_sum_that_is_not_a_whole_fund_is_an_error(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM fund_holding WHERE instrument_id = 2 AND line_no = 2")
    db.execute(
        "UPDATE fund_holding_set SET weight_sum = '0.6000', holdings = 1 WHERE instrument_id = 2"
    )
    assert "LOOK_THROUGH_WEIGHTS_UNBALANCED" in _look_through_findings(db).codes


def test_a_fund_with_no_composition_is_a_warning_with_its_share_of_the_portfolio(
    db: sqlite3.Connection,
) -> None:
    report = doctor.DoctorReport()
    doctor.check_look_through(db, report)
    finding = next(f for f in report.findings if f.code == "LOOK_THROUGH_MISSING")
    # Both funds without constituents, ZHDG included: a declared currency
    # answers one dimension and says nothing about the other three.
    assert "ZOVN" in finding.message and "ZHDG" in finding.message
    assert "30.0%" in finding.message
    # A gap in the source, not a bug in the code.
    assert finding not in report.errors


def test_an_old_composition_is_a_warning_not_a_silent_substitution(
    db: sqlite3.Connection,
) -> None:
    db.execute("DELETE FROM fund_holding_set WHERE instrument_id = 1")
    ingest.store(
        db,
        instrument_id=1,
        as_of=date(2024, 1, 1),
        parsed=ingest.parse(GLOBAL_FUND_CSV),
        source="two years old",
        file_sha256="3" * 64,
    )
    assert "LOOK_THROUGH_STALE" in _look_through_findings(db).codes


def test_the_whole_doctor_still_passes_with_look_through_data(db: sqlite3.Connection) -> None:
    """The new checks must not fire on clean data. Warnings are expected;
    errors would mean the feature broke an existing identity."""
    report = doctor.run(db)
    assert not report.errors, [str(f) for f in report.errors]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    api_app.dependency_overrides[get_db] = lambda: db
    api_app.dependency_overrides[get_writable_db] = lambda: db
    with TestClient(api_app) as test_client:
        yield test_client
    api_app.dependency_overrides.clear()


def test_the_default_basis_is_unchanged(client: TestClient) -> None:
    """v1 callers must see exactly what they saw before."""
    body = client.get("/api/allocation", params={"dim": "currency"}).json()
    assert body["basis"] == "quote_currency"
    assert body["buckets"] == [{"name": "EUR", "value_eur": 10000.0, "weight": 1.0}]


def test_the_look_through_basis_reports_the_currencies_underneath(client: TestClient) -> None:
    body = client.get("/api/allocation", params={"dim": "currency", "basis": "look_through"}).json()
    assert body["basis"] == "look_through"
    buckets = {b["name"]: b["value_eur"] for b in body["buckets"]}
    assert buckets["USD"] == 4250.0
    assert sum(b["weight"] for b in body["buckets"]) == pytest.approx(1.0)


def test_every_look_through_response_carries_its_coverage(client: TestClient) -> None:
    """A 60%-covered breakdown and a fully covered one are different claims."""
    body = client.get("/api/allocation", params={"dim": "currency", "basis": "look_through"}).json()
    coverage = body["coverage"]
    assert coverage["look_through_pct"] == pytest.approx(0.7)
    assert coverage["by_basis"]["quote_only"] == 2000.0
    assert [u["instrument"] for u in coverage["uncovered"]] == ["ZOVN"]
    assert body["caveats"]


def test_a_dimension_with_no_meaning_below_the_wrapper_is_refused(client: TestClient) -> None:
    response = client.get("/api/allocation", params={"dim": "account", "basis": "look_through"})
    assert response.status_code == 400
    assert "wrapper" in response.json()["detail"]


def test_quote_basis_gained_sector_and_country_without_dropping_null_rows(
    client: TestClient,
) -> None:
    body = client.get("/api/allocation", params={"dim": "sector"}).json()
    buckets = {b["name"]: b["value_eur"] for b in body["buckets"]}
    assert buckets[look_through.UNKNOWN] == 9000.0
    assert buckets["Industrials"] == 1000.0


def test_the_concentration_endpoint_merges_direct_and_fund_holdings(client: TestClient) -> None:
    body = client.get("/api/exposure/concentration", params={"top": 5}).json()
    acme = next(u for u in body["underlyings"] if "Acme" in u["name"])
    assert acme["value_eur"] == 1300.0
    assert {v["instrument"] for v in acme["via"]} == {"ACRO", "ZGEQ"}


def test_the_look_through_status_endpoint_lists_what_is_missing(client: TestClient) -> None:
    body = client.get("/api/look-through").json()
    assert body["as_of"] == AS_OF
    assert set(body["missing"]) == {"ZOVN", "ZHDG"}
    covered = {i["instrument"]: i for i in body["instruments"]}
    assert covered["ZGEQ"]["holdings"] == 6
    assert covered["ZGEQ"]["composition_as_of"] == PUBLISHED.isoformat()
    assert covered["ACRO"]["is_fund"] is False


def test_the_endpoints_answer_when_nothing_has_been_looked_through(
    db: sqlite3.Connection, client: TestClient
) -> None:
    """Partial data is the normal state and must not be a 500."""
    db.execute("DELETE FROM fund_holding")
    db.execute("DELETE FROM fund_holding_set")
    body = client.get("/api/allocation", params={"dim": "currency", "basis": "look_through"}).json()
    assert body["coverage"]["by_basis"].get("look_through") is None
    assert client.get("/api/exposure/concentration").status_code == 200
    assert client.get("/api/look-through").status_code == 200


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_page_shows_both_answers_side_by_side(db: sqlite3.Connection) -> None:
    """Showing only the corrected figure would hide that a correction happened."""
    html = render_dashboard(db)
    assert "Look-through" in html
    assert "Quoted" in html
    assert "Acme Robotics C" in html
    assert "attributed on evidence" in html


def test_the_page_points_at_the_command_when_there_is_nothing_to_show(
    db: sqlite3.Connection,
) -> None:
    db.execute("DELETE FROM fund_holding")
    db.execute("DELETE FROM fund_holding_set")
    html = render_dashboard(db)
    assert "pt look-through import" in html


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def db_file(tmp_path: Path, db: sqlite3.Connection) -> Path:
    """The fixture portfolio on disk, since the CLI opens its own connection."""
    target = tmp_path / "p.db"
    on_disk = connect(target)
    db.backup(on_disk)
    on_disk.commit()
    on_disk.close()
    return target


def test_import_prints_the_three_decisions_that_could_be_wrong(
    tmp_path: Path, db_file: Path
) -> None:
    holdings = tmp_path / "h.csv"
    holdings.write_text(GLOBAL_FUND_CSV, encoding="utf-8")
    result = runner.invoke(
        cli_app,
        ["look-through", "import", "-i", "ZOVN", "-f", str(holdings),
         "--as-of", "2026-05-31", "--db", str(db_file)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "read as percent" in result.output
    assert "'Weight (%)'" in result.output
    assert "weights sum to  1.000000" in result.output
    assert "stored 6 holding(s)" in result.output


def test_import_refuses_a_file_it_cannot_read_and_exits_non_zero(
    tmp_path: Path, db_file: Path
) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("Name,Weight\nA,30\nB,25\n", encoding="utf-8")
    result = runner.invoke(
        cli_app,
        ["look-through", "import", "-i", "ZOVN", "-f", str(bad),
         "--as-of", "2026-05-31", "--db", str(db_file)],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "cannot tell" in result.output


def test_import_dry_run_writes_nothing(tmp_path: Path, db_file: Path) -> None:
    holdings = tmp_path / "h.csv"
    holdings.write_text(GLOBAL_FUND_CSV, encoding="utf-8")
    result = runner.invoke(
        cli_app,
        ["look-through", "import", "-i", "ZOVN", "-f", str(holdings), "--as-of",
         "2026-05-31", "--db", str(db_file), "--dry-run"],
    )  # fmt: skip
    assert result.exit_code == 0
    conn = connect(db_file, read_only=True)
    try:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM fund_holding_set WHERE instrument_id = 4"
            ).fetchone()["n"]
            == 0
        )
    finally:
        conn.close()


def test_import_names_an_instrument_it_cannot_find(tmp_path: Path, db_file: Path) -> None:
    holdings = tmp_path / "h.csv"
    holdings.write_text(GLOBAL_FUND_CSV, encoding="utf-8")
    result = runner.invoke(
        cli_app,
        ["look-through", "import", "-i", "NOPE", "-f", str(holdings),
         "--as-of", "2026-05-31", "--db", str(db_file)],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "NOPE" in result.output


def test_exposure_puts_the_old_answer_beside_the_new_one(db_file: Path) -> None:
    result = runner.invoke(
        cli_app, ["look-through", "exposure", "--dim", "currency", "--db", str(db_file)]
    )
    assert result.exit_code == 0, result.output
    assert "look-through" in result.output
    assert "quoted" in result.output
    assert "USD" in result.output


def test_list_reports_the_share_with_no_composition(db_file: Path) -> None:
    result = runner.invoke(cli_app, ["look-through", "list", "--db", str(db_file)])
    assert result.exit_code == 0, result.output
    assert "no composition" in result.output
    assert "30.0% of holdings" in result.output


def test_show_prints_one_composition_largest_first(db_file: Path) -> None:
    result = runner.invoke(cli_app, ["look-through", "show", "-i", "ZGEQ", "--db", str(db_file)])
    assert result.exit_code == 0, result.output
    assert "Nordwind Technologies" in result.output
    assert "6 holding(s)" in result.output


def test_concentration_names_which_holdings_a_company_is_reached_through(
    db_file: Path,
) -> None:
    result = runner.invoke(
        cli_app, ["look-through", "concentration", "--top", "5", "--db", str(db_file)]
    )
    assert result.exit_code == 0, result.output
    assert "Acme Robotics C" in result.output
    assert "ACRO" in result.output and "ZGEQ" in result.output
