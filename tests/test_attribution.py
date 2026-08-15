"""Where the return came from, and why the parts add up.

The fixture this file is built on is one line of arithmetic: a lira fund rises
10% while the lira falls 10%, so the euro value does not move at all. The
attribution must report **+9.09% from prices and -9.09% from currency**, not
zero and zero. A decomposition that cannot separate those two is the same
decomposition the source export ships, and correcting it is the reason this
project exists.

The second thing under test is the linking, and it is the one that fails
silently. Daily contributions that are individually correct do not sum to the
compounded period return -- on the example corpus the gap is **eleven
percentage points** -- and nothing about the wrong answer looks wrong.
``test_naive_summation_is_wrong_by_a_lot`` states that number, so the Carino
tests cannot quietly become vacuous.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_tracker.api.app import app as api_app
from portfolio_tracker.api.app import get_db, get_writable_db
from portfolio_tracker.api.render import render_dashboard
from portfolio_tracker.core import attribution, doctor
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect

D = Decimal

#: Decimal carries 28 significant digits; terms that cancel exactly in real
#: arithmetic leave a few units in the last place.
EXACT = D("0.0000000000000000001")

DAY0, DAY1, DAY2 = date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)


# ---------------------------------------------------------------------------
# Carino linking, as pure arithmetic
# ---------------------------------------------------------------------------


def test_a_flat_period_does_not_divide_by_zero() -> None:
    """R = 0 is a removable singularity, not a degenerate case worth refusing."""
    assert attribution.carino_factor(D(0)) == D(1)


def test_naive_summation_is_wrong_by_a_lot() -> None:
    """The gap the linking closes, stated as a number so it cannot be assumed away.

    Twenty days of +3% compounds to +80.6%, while the daily contributions add
    to +60%. Twenty percentage points, from arithmetic alone, on figures that
    are each individually correct.
    """
    daily = [D("0.03")] * 20
    naive = sum(daily, D(0))

    compounded = D(1)
    for r in daily:
        compounded *= D(1) + r
    compounded -= D(1)

    assert naive == D("0.60")
    assert compounded > D("0.80")
    assert compounded - naive > D("0.20")


def test_carino_smoothing_reproduces_the_compounded_return_exactly() -> None:
    """The identity the whole method rests on::

    Sum_t R_t * k_t/K  =  Sum_t ln(1+R_t)/K  =  ln(prod(1+R_t))/K  =  R
    """
    daily = [D("0.03"), D("-0.015"), D("0.008"), D("0.021"), D("-0.004")]
    terms = [
        attribution.DayTerms(DAY0, local=r, currency=D(0), income=D(0), unexplained=D(0), actual=r)
        for r in daily
    ]
    total, parts = attribution.link(terms)

    compounded = D(1)
    for r in daily:
        compounded *= D(1) + r
    assert abs(total - (compounded - D(1))) < EXACT
    assert abs(parts["local"] - total) < EXACT


def test_linking_preserves_the_split_between_factors() -> None:
    """Smoothing must scale the parts, not redistribute between them.

    A factor contributing exactly half of every day's return must contribute
    half of the period's -- otherwise the linking is inventing attribution.
    """
    daily = [D("0.04"), D("-0.02"), D("0.03")]
    terms = [
        attribution.DayTerms(
            DAY0, local=r / 2, currency=r / 2, income=D(0), unexplained=D(0), actual=r
        )
        for r in daily
    ]
    total, parts = attribution.link(terms)
    assert abs(parts["local"] - parts["currency"]) < EXACT
    assert abs(parts["local"] + parts["currency"] - total) < EXACT


def test_a_negative_period_links_as_exactly_as_a_positive_one() -> None:
    daily = [D("-0.05"), D("-0.03"), D("0.01")]
    terms = [
        attribution.DayTerms(DAY0, local=r, currency=D(0), income=D(0), unexplained=D(0), actual=r)
        for r in daily
    ]
    total, parts = attribution.link(terms)
    assert total < D(0)
    assert abs(parts["local"] - total) < EXACT


def test_a_flat_period_attributes_nothing_to_anything() -> None:
    """Two days that cancel. The total is zero and so is each part."""
    terms = [
        attribution.DayTerms(
            DAY0, local=D("0.25"), currency=D(0), income=D(0), unexplained=D(0), actual=D("0.25")
        ),
        attribution.DayTerms(
            DAY1, local=D("-0.2"), currency=D(0), income=D(0), unexplained=D(0), actual=D("-0.2")
        ),
    ]
    total, parts = attribution.link(terms)
    assert total == D(0)
    assert abs(parts["local"]) < EXACT


# ---------------------------------------------------------------------------
# Daily terms, against hand-computed arithmetic
# ---------------------------------------------------------------------------


def _instrument(conn: sqlite3.Connection, iid: int, key: str, currency: str) -> None:
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "teilfreistellung_pct) VALUES (?, ?, ?, 'FUND', ?, 1, '0.00')",
        (iid, key, f"{key} fund", currency),
    )


def _position(
    conn: sqlite3.Connection,
    when: date,
    iid: int,
    *,
    quantity: str,
    price: str,
    rate: str,
    currency: str,
) -> None:
    value_local = D(quantity) * D(price)
    value_eur = value_local / D(rate)
    conn.execute(
        """
        INSERT INTO position_daily
            (date, account_id, instrument_id, quantity, currency, price_local,
             price_asof, price_stale_days, fx_rate, fx_asof, fx_stale_days,
             market_value_local, market_value_eur, cost_basis_local, cost_basis_eur,
             cost_basis_eur_at_spot, unrealized_eur, unrealized_local_eur,
             unrealized_fx_eur, valuation_status)
        VALUES (?, 1, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, ?, '0', '0', '0', 'OK')
        """,
        (
            when.isoformat(), iid, D(quantity), currency, D(price), when.isoformat(),
            D(rate), when.isoformat(), value_local, value_eur, value_local, value_eur,
            value_eur,
        ),
    )  # fmt: skip


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """One lira fund, up 10% locally while the lira falls 10%.

    100 units at 10.00 TRY, EUR/TRY 40 -> 1,000 TRY -> EUR 25.00
    100 units at 11.00 TRY, EUR/TRY 44 -> 1,100 TRY -> EUR 25.00

    The euro value is identical on both days. The export this project is built
    on would report that as nothing happening. Two things happened.
    """
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name, joint_assessment) VALUES (1, 'A', 0)")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    _instrument(conn, 1, "ZTRY", "TRY")
    _position(conn, DAY0, 1, quantity="100", price="10", rate="40", currency="TRY")
    _position(conn, DAY1, 1, quantity="100", price="11", rate="44", currency="TRY")
    conn.commit()
    yield conn
    conn.close()


def _terms(conn: sqlite3.Connection, basis: dict[date, Decimal], actual: dict[date, Decimal]):  # type: ignore[no-untyped-def]
    return attribution.daily_terms(
        conn,
        person_id=1,
        days=sorted(basis),
        basis=basis,
        actual=actual,
    )


def test_a_ten_percent_local_gain_and_a_ten_percent_currency_fall(
    db: sqlite3.Connection,
) -> None:
    """The claim, at its smallest.

    local = 100 * (11 - 10) / 44  = 2.2727... EUR, on a EUR 25 opening = +1/11
    fx    = 100 * 10 * (1/44 - 1/40) = -2.2727... EUR                 = -1/11

    A tracker that reports "0%" is not wrong about the euro value. It is
    silent about both of the things that produced it.
    """
    terms = _terms(db, {DAY0: D(25), DAY1: D(25)}, {DAY1: D(0)})
    day = terms[DAY1]

    assert abs(day.local - D(1) / D(11)) < EXACT
    assert abs(day.currency + D(1) / D(11)) < EXACT
    assert day.local > D("0.0909")
    assert day.currency < D("-0.0909")
    # And they cancel, which is the part the export gets right.
    assert abs(day.local + day.currency) < EXACT


def test_the_parts_sum_to_the_day_by_construction(db: sqlite3.Connection) -> None:
    terms = _terms(db, {DAY0: D(25), DAY1: D(25)}, {DAY1: D(0)})
    day = terms[DAY1]
    assert day.local + day.currency + day.income + day.unexplained == day.actual


def test_a_return_the_model_cannot_explain_is_named_not_hidden(
    db: sqlite3.Connection,
) -> None:
    """If prices and rates explain nothing, `unexplained` is the whole return.

    The alternative -- silently scaling the modelled terms until they match --
    is how an attribution starts reporting trading skill as stock selection.
    """
    terms = _terms(db, {DAY0: D(25), DAY1: D(25)}, {DAY1: D("0.05")})
    day = terms[DAY1]
    assert abs(day.unexplained - D("0.05")) < EXACT
    assert day.local + day.currency + day.income + day.unexplained == day.actual


def test_a_euro_only_portfolio_has_no_currency_term(db: sqlite3.Connection) -> None:
    _instrument(db, 2, "ZEUR", "EUR")
    _position(db, DAY0, 2, quantity="10", price="100", rate="1", currency="EUR")
    _position(db, DAY1, 2, quantity="10", price="110", rate="1", currency="EUR")
    db.execute("DELETE FROM position_daily WHERE instrument_id = 1")
    db.commit()

    terms = _terms(db, {DAY0: D(1000), DAY1: D(1100)}, {DAY1: D("0.10")})
    day = terms[DAY1]
    assert day.currency == D(0)
    assert abs(day.local - D("0.10")) < EXACT


def test_a_position_opened_that_day_is_trading_not_a_price_move(
    db: sqlite3.Connection,
) -> None:
    """A holding that did not exist yesterday cannot have moved in price.

    Attributing its whole value to `local` would report a purchase as
    performance, which is the same error inferring contributions exists to
    prevent, one layer up.
    """
    _instrument(db, 2, "NEW", "EUR")
    _position(db, DAY1, 2, quantity="10", price="100", rate="1", currency="EUR")
    db.commit()

    terms = _terms(db, {DAY0: D(25), DAY1: D(1025)}, {DAY1: D("0.4")})
    day = terms[DAY1]
    # The lira fund's own two terms still cancel; everything else is trading.
    assert abs(day.local + day.currency) < EXACT
    assert abs(day.unexplained - D("0.4")) < EXACT


def test_a_foreign_cash_balance_carries_a_currency_term(db: sqlite3.Connection) -> None:
    """A lira balance loses euro value when the lira falls. Cash is not inert."""
    db.execute("DELETE FROM position_daily")
    for when, rate in ((DAY0, "40"), (DAY1, "44")):
        db.execute(
            "INSERT INTO cash_daily (date, account_id, currency, balance_local, balance_eur, "
            "fx_rate, idle_days, income_local, income_eur) VALUES (?, 1, 'TRY', '4400', ?, ?, "
            "0, '0', '0')",
            (when.isoformat(), D(4400) / D(rate), D(rate)),
        )
    db.commit()

    opening = D(4400) / D(40)
    terms = _terms(db, {DAY0: opening, DAY1: D(100)}, {DAY1: D(-1) / D(11)})
    day = terms[DAY1]
    assert day.local == D(0)
    assert day.currency < D(0)
    assert abs(day.currency - (D(-1) / D(11))) < EXACT


def test_a_distribution_is_income_rather_than_an_unexplained_windfall(
    db: sqlite3.Connection,
) -> None:
    """Without an income term, an ex-date is a loss with nothing offsetting it.

    That is the exact failure the distribution work fixed in the ledger, and it
    would reappear here as "prices fell, and something we cannot account for
    went up by the same amount".
    """
    db.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, currency, "
        "gross_amount_local, gross_amount_eur, source, confidence, flow_class, external_id) "
        "VALUES (1, 1, 'DIVIDEND', ?, 'EUR', '2.50', '2.50', 'SYNTHESIZED', 'ESTIMATE', 'INCOME', 'd1')",
        (DAY1.isoformat(),),
    )
    db.commit()

    terms = _terms(db, {DAY0: D(25), DAY1: D(25)}, {DAY1: D("0.10")})
    day = terms[DAY1]
    assert day.income == D("2.50") / D(25)
    assert abs(day.unexplained) < EXACT


def test_a_day_with_no_opening_value_is_skipped_rather_than_divided_by(
    db: sqlite3.Connection,
) -> None:
    assert _terms(db, {DAY0: D(0), DAY1: D(25)}, {DAY1: D(0)}) == {}


# ---------------------------------------------------------------------------
# Stored, summarised and checked
# ---------------------------------------------------------------------------


def _store(conn: sqlite3.Connection, rows: list[tuple[date, str, str, str, str, str]]) -> None:
    """Write portfolio_daily rows with attribution already decomposed.

    The unit price is derived from the daily returns rather than fixed, because
    the doctor check compares the two and a fixture where they disagree would
    be testing the fixture. Day one is inception: no prior day, so no return to
    attribute, which is exactly what `build_portfolio_daily` stores.
    """
    price = D(100)
    for index, (when, ret, local, currency, income, unexplained) in enumerate(rows):
        inception = index == 0
        if not inception:
            price *= D(1) + D(ret)
        conn.execute(
            """
            INSERT INTO portfolio_daily
                (date, person_id, market_value_eur, total_value_eur, cost_basis_eur,
                 units_outstanding, unit_price, cash_eur, deposits_eur, realized_gain_eur,
                 valuation_basis_eur, income_cash_eur, income_received_eur, completeness,
                 drawdown_pct, daily_return, ret_local_contrib, ret_fx_contrib,
                 ret_income_contrib, ret_residual)
            VALUES (?, 1, '100', '100', '100', '1', ?, '0', '0', '0', '100', '0', '0',
                    'FULL', '0', ?, ?, ?, ?, ?)
            """,
            (
                when.isoformat(),
                price,
                None if inception else D(ret),
                None if inception else D(local),
                None if inception else D(currency),
                None if inception else D(income),
                None if inception else D(unexplained),
            ),
        )
    conn.commit()


@pytest.fixture
def summarised(db: sqlite3.Connection) -> sqlite3.Connection:
    """Three days: prices up, currency down, a little income, a little trading."""
    _store(
        db,
        [
            (DAY0, "0", "0", "0", "0", "0"),  # inception; nothing to attribute
            (DAY1, "-0.01", "0.004", "-0.015", "0.0", "0.001"),
            (DAY2, "0.03", "0.041", "-0.012", "0.0", "0.001"),
        ],
    )
    return db


def test_the_summary_parts_sum_to_the_period_return(summarised: sqlite3.Connection) -> None:
    state = attribution.summarise(summarised, person_id=1)
    assert state is not None
    assert abs(state.residual) < attribution.LINKING_TOLERANCE
    assert state.local > D(0)
    assert state.currency < D(0)


def test_the_period_total_is_compounded_not_added(summarised: sqlite3.Connection) -> None:
    state = attribution.summarise(summarised, person_id=1)
    assert state is not None
    naive = D("-0.01") + D("0.03")
    compounded = (D("0.99") * D("1.03")) - D(1)
    assert abs(state.total - compounded) < EXACT
    assert state.total != naive


def test_no_history_reports_nothing_rather_than_zeroes(db: sqlite3.Connection) -> None:
    """A flat year and no data are different answers and must not look alike."""
    assert attribution.summarise(db, person_id=1) is None


def test_the_daily_identity_is_checked_and_a_break_is_caught(
    summarised: sqlite3.Connection,
) -> None:
    summarised.execute("UPDATE portfolio_daily SET ret_local_contrib = '0.9' WHERE date = ?",
                       (DAY1.isoformat(),))  # fmt: skip
    report = doctor.DoctorReport()
    doctor.check_attribution(summarised, report)
    assert "ATTRIBUTION_DOES_NOT_SUM" in report.codes


def test_clean_attribution_raises_no_errors(summarised: sqlite3.Connection) -> None:
    report = doctor.DoctorReport()
    doctor.check_attribution(summarised, report)
    assert not report.errors


def test_an_attribution_that_disagrees_with_the_unit_series_is_caught(
    summarised: sqlite3.Connection,
) -> None:
    """Two independent code paths compute the period return. They must agree.

    The unit price and the compounded daily returns never consult each other,
    so this catches a decomposition that is internally consistent and describes
    a different portfolio.
    """
    summarised.execute("UPDATE portfolio_daily SET unit_price = '250' WHERE date = ?",
                       (DAY2.isoformat(),))  # fmt: skip
    report = doctor.DoctorReport()
    doctor.check_attribution(summarised, report)
    assert "ATTRIBUTION_TOTAL_DISAGREES_WITH_TWR" in report.codes


# ---------------------------------------------------------------------------
# HTTP and the page
# ---------------------------------------------------------------------------


@pytest.fixture
def client(summarised: sqlite3.Connection) -> Iterator[TestClient]:
    api_app.dependency_overrides[get_db] = lambda: summarised
    api_app.dependency_overrides[get_writable_db] = lambda: summarised
    with TestClient(api_app) as test_client:
        yield test_client
    api_app.dependency_overrides.clear()


def test_the_api_reports_the_split_and_proves_the_residual(client: TestClient) -> None:
    body = client.get("/api/returns").json()["attribution"]
    assert body["linking_method"] == "carino"
    assert body["residual"] == pytest.approx(0.0, abs=1e-12)
    parts = body["local"] + body["currency"] + body["income"] + body["unexplained"]
    assert parts == pytest.approx(body["total"], abs=1e-12)


def test_the_api_names_what_each_term_means(client: TestClient) -> None:
    """A signed percentage with no definition is not a decomposition."""
    meanings = client.get("/api/returns").json()["attribution"]["meanings"]
    assert set(meanings) == {"local", "currency", "income", "unexplained"}
    assert "not distributed" in meanings["unexplained"]


def test_the_page_shows_where_the_return_came_from(summarised: sqlite3.Connection) -> None:
    page = render_dashboard(summarised)
    assert "Where the return came from" in page
    assert "Asset prices" in page
    assert "Currency" in page
