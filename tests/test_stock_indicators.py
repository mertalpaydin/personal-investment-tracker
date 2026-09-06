from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_tracker.api.app import app, get_db, get_writable_db
from portfolio_tracker.core import stock_indicators as si
from portfolio_tracker.core.stock_indicators import (
    StockFundamentals,
    calculate_value_score,
    get_stock_fundamentals,
    parse_raw_stock_info,
    save_stock_fundamentals,
    score_universe,
)
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect


def _stock(conn: sqlite3.Connection, id_: int, key: str, name: str, currency: str = "EUR") -> None:
    conn.execute(
        """
        INSERT INTO instrument
            (id, import_key, symbol, name, asset_class, currency, trading_calendar, price_source)
        VALUES (?, ?, ?, ?, 'STOCK', ?, 'XETRA', 'yfinance')
        """,
        (id_, key, key, name, currency),
    )


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_writable_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# A complete, healthy input set. Tests override single fields.
GOOD = dict(
    forward_pe=Decimal("11.5"),
    trailing_pe=Decimal("13.0"),
    fcf_yield=Decimal("0.085"),
    pct_off_52w_high=Decimal("-0.40"),
    range_52w_pct=Decimal("15.0"),
    ev_ebitda=Decimal("7.5"),
    price_to_book=Decimal("1.4"),
    target_upside_pct=Decimal("0.25"),
    dividend_yield=Decimal("0.038"),
    debt_to_equity=Decimal("60.0"),
    operating_margin=Decimal("0.20"),
    analyst_count=12,
    buy_pct=Decimal("0.75"),
    recommendation_mean=Decimal("1.8"),
)


# ---------------------------------------------------------------- scoring


def test_calculate_value_score_bargain():
    score, badge, narrative = calculate_value_score(**GOOD)
    assert score is not None and score >= 70
    assert badge == "BARGAIN"
    assert "Attractively valued" in narrative
    assert "12 analysts, 75% rate it a buy" in narrative


def test_full_marks_is_exactly_100():
    score, _, _ = calculate_value_score(**GOOD)
    assert score == 100


def test_calculate_value_score_dip():
    score, badge, narrative = calculate_value_score(
        **{
            **GOOD,
            "forward_pe": Decimal("16.0"),
            "trailing_pe": Decimal("17.5"),
            "fcf_yield": Decimal("0.04"),
            "pct_off_52w_high": Decimal("-0.28"),
            "range_52w_pct": Decimal("20.0"),
            "ev_ebitda": Decimal("12.0"),
            "price_to_book": Decimal("2.5"),
            "target_upside_pct": Decimal("0.18"),
            "dividend_yield": Decimal("0.02"),
            "debt_to_equity": Decimal("80.0"),
            "operating_margin": Decimal("0.18"),
            "analyst_count": 8,
            "buy_pct": Decimal("0.4"),
        }
    )
    assert score is not None and 55 <= score < 70
    assert badge == "DIP"
    assert "Pullback opportunity" in narrative


def test_dip_threshold_is_twenty_percent():
    """The badge, the narrative and the UI all say 20%; the code must agree."""
    base = {
        **GOOD,
        "forward_pe": Decimal("16.0"),
        "fcf_yield": Decimal("0.04"),
        "ev_ebitda": Decimal("12.0"),
        "target_upside_pct": Decimal("0.05"),
        "dividend_yield": Decimal("0.02"),
        "buy_pct": Decimal("0.4"),
        "range_52w_pct": Decimal("40.0"),
    }
    _, badge_20, _ = calculate_value_score(**{**base, "pct_off_52w_high": Decimal("-0.20")})
    _, badge_19, _ = calculate_value_score(**{**base, "pct_off_52w_high": Decimal("-0.19")})
    assert badge_20 == "DIP"
    assert badge_19 == "FAIR"


def test_calculate_value_score_premium():
    score, badge, narrative = calculate_value_score(
        forward_pe=Decimal("45.0"),
        trailing_pe=Decimal("50.0"),
        fcf_yield=Decimal("0.015"),
        pct_off_52w_high=Decimal("-0.02"),
        range_52w_pct=Decimal("95.0"),
        ev_ebitda=Decimal("32.0"),
        price_to_book=Decimal("12.0"),
        target_upside_pct=Decimal("0.05"),
        dividend_yield=Decimal("0.005"),
        debt_to_equity=Decimal("120.0"),
        operating_margin=Decimal("0.25"),
    )
    assert score is not None and score < 40
    assert badge == "PREMIUM"
    assert "premium" in narrative.lower()


def test_premium_with_growth_gets_peg_credit():
    kwargs = dict(
        forward_pe=Decimal("30.0"),
        trailing_pe=Decimal("34.0"),
        fcf_yield=Decimal("0.02"),
        pct_off_52w_high=Decimal("-0.05"),
        range_52w_pct=Decimal("80.0"),
        ev_ebitda=Decimal("22.0"),
        price_to_book=Decimal("8.0"),
        target_upside_pct=Decimal("0.05"),
        dividend_yield=Decimal("0.0"),
        debt_to_equity=Decimal("50.0"),
        operating_margin=Decimal("0.30"),
    )
    no_growth, _, _ = calculate_value_score(**kwargs)
    growth, _, narrative = calculate_value_score(**kwargs, peg_ratio=Decimal("0.9"))
    assert growth is not None and no_growth is not None
    assert growth == no_growth + 8
    assert "backed by growth" in narrative


def test_calculate_value_score_value_trap_leverage():
    score, badge, narrative = calculate_value_score(
        forward_pe=Decimal("7.5"),
        trailing_pe=Decimal("8.0"),
        fcf_yield=Decimal("-0.02"),
        pct_off_52w_high=Decimal("-0.45"),
        range_52w_pct=Decimal("5.0"),
        ev_ebitda=Decimal("18.0"),
        price_to_book=Decimal("0.7"),
        target_upside_pct=Decimal("0.02"),
        dividend_yield=Decimal("0.0"),
        debt_to_equity=Decimal("350.0"),
        operating_margin=Decimal("-0.04"),
    )
    assert badge == "TRAP_RISK"
    assert score is not None and score <= 38
    assert "Low P/E" in narrative and "leverage" in narrative.lower()
    assert "negative operating margin" in narrative


def test_value_trap_when_earnings_expected_to_fall():
    """Forward P/E far above trailing means the E is shrinking: cheap for a reason."""
    _, badge, narrative = calculate_value_score(
        **{**GOOD, "trailing_pe": Decimal("8.0"), "forward_pe": Decimal("12.0")}
    )
    assert badge == "TRAP_RISK"
    assert "expected to fall" in narrative


def test_value_trap_when_earnings_collapsed():
    _, badge, narrative = calculate_value_score(**GOOD, earnings_growth=Decimal("-0.35"))
    assert badge == "TRAP_RISK"
    assert "earnings down 35%" in narrative


def test_financials_are_not_trapped_by_leverage():
    """A bank's deposits are 'debt'. D/E of 900% is normal, not a warning."""
    score, badge, _ = calculate_value_score(
        **{**GOOD, "debt_to_equity": Decimal("900.0"), "ev_ebitda": None},
        sector="Financial Services",
    )
    assert badge == "BARGAIN"
    assert score is not None and score >= 70


def test_financials_use_price_to_book_not_ev_ebitda():
    with_ev, _, _ = calculate_value_score(
        **{**GOOD, "ev_ebitda": Decimal("60.0")}, sector="Financial Services"
    )
    without_ev, _, _ = calculate_value_score(
        **{**GOOD, "ev_ebitda": None}, sector="Financial Services"
    )
    assert with_ev == without_ev


def test_no_data_when_too_few_inputs():
    score, badge, narrative = calculate_value_score(
        forward_pe=None,
        trailing_pe=None,
        fcf_yield=None,
        pct_off_52w_high=Decimal("-0.05"),
        range_52w_pct=None,
        ev_ebitda=None,
        price_to_book=None,
        target_upside_pct=None,
        dividend_yield=None,
        debt_to_equity=None,
        operating_margin=None,
    )
    assert score is None
    assert badge == "NO_DATA"
    assert "1 of 8" in narrative


def test_negative_pe_counts_as_missing_not_cheap():
    _, badge_neg, _ = calculate_value_score(
        **{**GOOD, "forward_pe": Decimal("-5.0"), "trailing_pe": Decimal("-3.0")}
    )
    assert badge_neg != "TRAP_RISK"  # trap needs a positive low multiple


def test_analyst_upside_needs_three_analysts():
    crowd, _, _ = calculate_value_score(**{**GOOD, "buy_pct": None})
    lone, _, _ = calculate_value_score(**{**GOOD, "buy_pct": None, "analyst_count": 1})
    unknown, _, _ = calculate_value_score(**{**GOOD, "buy_pct": None, "analyst_count": None})
    assert crowd is not None and lone is not None and unknown is not None
    assert crowd - lone == 10  # upside 5 + consensus 5, both need a crowd
    assert unknown == lone


def test_buy_share_adds_points():
    high, _, _ = calculate_value_score(**{**GOOD, "buy_pct": Decimal("0.75")})
    mid, _, _ = calculate_value_score(**{**GOOD, "buy_pct": Decimal("0.55")})
    low, _, _ = calculate_value_score(**{**GOOD, "buy_pct": Decimal("0.20")})
    assert (high, mid, low) == (100, 98, 95)


def test_sector_relative_rank_helps_a_cheap_member_of_a_dear_sector():
    """P/E 30 is 5 points in absolute terms; cheapest of its sector lifts it."""
    kwargs = {**GOOD, "forward_pe": Decimal("30.0"), "trailing_pe": None}
    absolute, _, _ = calculate_value_score(**kwargs)
    relative, _, _ = calculate_value_score(**kwargs, relative={"pe": Decimal("1")})
    assert absolute is not None and relative is not None
    assert relative == absolute + 10  # (5 + 25) / 2 = 15, up from 5


KHC = dict(
    forward_pe=Decimal("11.87"),
    trailing_pe=None,
    fcf_yield=Decimal("0.11"),
    pct_off_52w_high=Decimal("-0.115"),
    range_52w_pct=Decimal("54.0"),
    ev_ebitda=Decimal("8.36"),
    price_to_book=Decimal("0.82"),
    target_upside_pct=Decimal("0.012"),
    dividend_yield=Decimal("0.064"),
    debt_to_equity=Decimal("52.6"),
    operating_margin=Decimal("0.155"),
    sector="Consumer Defensive",
    peg_ratio=Decimal("0.99"),
    revenue_growth=Decimal("-0.014"),
    profit_margin=Decimal("-0.136"),
    return_on_equity=Decimal("-0.088"),
    payout_ratio=Decimal("0.73"),
    analyst_count=17,
    buy_pct=Decimal("0.059"),
    recommendation_mean=Decimal("3.1"),
)


def test_khc_is_no_longer_a_bargain():
    """The case that prompted the change: cheap on every multiple, loss-making,
    a 6% dividend paid out of losses, and 17 analysts who say hold-to-sell."""
    score, badge, narrative = calculate_value_score(**KHC)
    assert score is not None and score <= 55
    assert badge == "FAIR"
    assert "capped at 55" in narrative
    assert "net loss" in narrative
    assert "dividend may not be sustainable" in narrative


def test_quality_gate_caps_but_does_not_trap():
    healthy, _, _ = calculate_value_score(**GOOD)
    capped, badge, narrative = calculate_value_score(**GOOD, profit_margin=Decimal("-0.05"))
    assert healthy == 100
    assert capped == 55
    assert badge == "FAIR"
    assert "net loss (-5% profit margin)" in narrative


def test_shrinking_revenue_caps_score():
    capped, _, narrative = calculate_value_score(**GOOD, revenue_growth=Decimal("-0.12"))
    assert capped == 55
    assert "revenue down 12%" in narrative


def test_analyst_consensus_can_subtract():
    liked, _, _ = calculate_value_score(**{**GOOD, "recommendation_mean": Decimal("1.5")})
    neutral, _, _ = calculate_value_score(**{**GOOD, "recommendation_mean": Decimal("2.8")})
    disliked, _, _ = calculate_value_score(**{**GOOD, "recommendation_mean": Decimal("3.3")})
    hated, _, _ = calculate_value_score(**{**GOOD, "recommendation_mean": Decimal("4.0")})
    assert (liked, neutral, disliked, hated) == (100, 95, 92, 90)


def test_fully_priced_target_with_large_crowd_costs_points():
    upside, _, _ = calculate_value_score(**{**GOOD, "target_upside_pct": Decimal("0.07")})
    flat, _, _ = calculate_value_score(**{**GOOD, "target_upside_pct": Decimal("0.01")})
    small_crowd, _, _ = calculate_value_score(
        **{**GOOD, "target_upside_pct": Decimal("0.01"), "analyst_count": 5}
    )
    assert upside - flat == 3
    assert small_crowd == upside  # five analysts are not a verdict


def test_dividend_only_counts_when_affordable():
    safe, _, _ = calculate_value_score(**GOOD, payout_ratio=Decimal("0.5"))
    stretched, _, narrative = calculate_value_score(**GOOD, payout_ratio=Decimal("0.95"))
    assert safe - stretched == 5
    assert "may not be sustainable" in narrative


def test_contrarian_badge_when_cheap_but_disliked():
    score, badge, narrative = calculate_value_score(
        **{**GOOD, "recommendation_mean": Decimal("3.2"), "buy_pct": Decimal("0.1")}
    )
    assert score is not None and score >= 70
    assert badge == "CONTRARIAN"
    assert "analysts disagree" in narrative
    assert "consensus 3.2" in narrative


def test_consensus_mean_from_counts_when_yahoo_omits_it():
    item = StockFundamentals(1, "X", "X", strong_buy=2, buy=6, hold=2, sell=0, strong_sell=0)
    assert si.consensus_mean(item) == Decimal("2")
    assert si.consensus_mean(StockFundamentals(1, "X", "X")) is None


# ---------------------------------------------------------------- quality


STEADY = dict(
    revenue_growth=Decimal("0.08"),
    earnings_growth=Decimal("0.12"),
    revenue_cagr_3y=Decimal("0.11"),
    revenue_up_years=3,
    income_up_years=3,
    years_reported=4,
    operating_margin=Decimal("0.24"),
    profit_margin=Decimal("0.18"),
    return_on_equity=Decimal("0.22"),
    debt_to_equity=Decimal("40"),
    vol_1y=Decimal("0.18"),
    max_dd_3y=Decimal("-0.15"),
)


def test_quality_score_steady_grower_is_100():
    score, narrative = si.calculate_quality_score(**STEADY)
    assert score == 100
    assert narrative.startswith("Steady grower")
    assert "revenue up every year for 3 years" in narrative
    assert "calm price" in narrative


def test_quality_score_penalises_volatility_and_losses():
    score, narrative = si.calculate_quality_score(
        **{
            **STEADY,
            "profit_margin": Decimal("-0.05"),
            "vol_1y": Decimal("0.55"),
            "max_dd_3y": Decimal("-0.62"),
            "revenue_up_years": 1,
            "income_up_years": 0,
        }
    )
    assert score is not None and score < 60
    assert "loss-making" in narrative
    assert "volatile (55% a year)" in narrative
    assert "fell 62% at worst" in narrative


def test_quality_score_needs_three_input_groups():
    score, narrative = si.calculate_quality_score(
        revenue_growth=None,
        earnings_growth=None,
        revenue_cagr_3y=None,
        revenue_up_years=None,
        income_up_years=None,
        years_reported=None,
        operating_margin=Decimal("0.1"),
        profit_margin=None,
        return_on_equity=None,
        debt_to_equity=None,
        vol_1y=Decimal("0.2"),
        max_dd_3y=None,
    )
    assert score is None
    assert "2 of 5" in narrative


def test_quality_score_financials_skip_leverage():
    bank, _ = si.calculate_quality_score(
        **{**STEADY, "debt_to_equity": Decimal("900")}, sector="Financial Services"
    )
    industrial, _ = si.calculate_quality_score(**{**STEADY, "debt_to_equity": Decimal("900")})
    assert bank is not None and industrial is not None
    assert bank > industrial


def test_parse_income_statement():
    pd = pytest.importorskip("pandas")
    cols = [
        pd.Timestamp("2025-12-31"),
        pd.Timestamp("2024-12-31"),
        pd.Timestamp("2023-12-31"),
        pd.Timestamp("2022-12-31"),
    ]
    df = pd.DataFrame(
        {
            cols[0]: [133.1, 12.0],
            cols[1]: [121.0, 9.0],
            cols[2]: [110.0, 10.0],
            cols[3]: [100.0, 8.0],
        },
        index=["Total Revenue", "Net Income"],
    )
    out = si.parse_income_statement(df)
    assert out["years_reported"] == 4
    assert out["revenue_up_years"] == 3
    assert out["income_up_years"] == 2
    assert out["revenue_cagr_3y"] == Decimal("0.1")


def test_parse_income_statement_tolerates_garbage():
    assert si.parse_income_statement(None) == {}
    assert si.parse_income_statement("not a frame") == {}


def test_apply_score_fills_quality():
    item = StockFundamentals(1, "X", "X", current_price=Decimal("10"), **STEADY)
    si.apply_score(item)
    assert item.quality_score == 100
    assert item.quality_narrative.startswith("Steady grower")


def test_quality_round_trips_through_db(db: sqlite3.Connection):
    _stock(db, 1, "Q", "Quality Co")
    item = StockFundamentals(
        1,
        "Q",
        "Quality Co",
        quality_score=77,
        quality_narrative="Steady grower",
        revenue_up_years=3,
        vol_1y=Decimal("0.19"),
        fetched_at="t",
    )
    save_stock_fundamentals(db, [item])
    (got,) = get_stock_fundamentals(db, instrument_id=1)
    assert got.quality_score == 77
    assert got.revenue_up_years == 3
    assert got.vol_1y == Decimal("0.19")


def test_watchlist_api_carries_stock_scores(client: TestClient, db: sqlite3.Connection):
    _stock(db, 7, "SAP", "SAP SE")
    db.execute("UPDATE instrument SET status = 'WATCH' WHERE id = 7")
    db.commit()
    save_stock_fundamentals(
        db,
        [
            StockFundamentals(
                7,
                "SAP",
                "SAP SE",
                sector="Technology",
                value_score=61,
                value_badge="FAIR",
                quality_score=82,
                quality_narrative="Steady grower",
                fetched_at="t",
            )
        ],
    )
    body = client.get("/api/watchlist").json()
    (entry,) = [e for e in body["watchlist"] if e["instrument"] == "SAP"]
    assert entry["fundamentals"]["value_score"] == 61
    assert entry["fundamentals"]["quality_score"] == 82


# ---------------------------------------------------------------- universe ranks


def _item(id_: int, sector: str, pe: str, **extra) -> StockFundamentals:
    return StockFundamentals(
        instrument_id=id_,
        symbol=f"S{id_}",
        name=f"Stock {id_}",
        sector=sector,
        current_price=Decimal("10"),
        forward_pe=Decimal(pe),
        fcf_yield=Decimal("0.04"),
        pct_off_52w_high=Decimal("-0.05"),
        operating_margin=Decimal("0.1"),
        debt_to_equity=Decimal("50"),
        **extra,
    )


def test_score_universe_ranks_within_sector():
    tech = [_item(i, "Technology", pe) for i, pe in enumerate(["20", "25", "30", "35", "40"], 1)]
    banks = [
        _item(i, "Financial Services", pe) for i, pe in enumerate(["6", "7", "8", "9", "10"], 11)
    ]
    items = tech + banks
    score_universe(items)
    by_id = {s.instrument_id: s for s in items}
    # The cheapest tech stock beats the dearest tech stock.
    assert by_id[1].value_score > by_id[5].value_score  # type: ignore[operator]
    # The cheapest tech (P/E 20, absolute 12 pts, relative 25) scores better than the
    # absolute-only reading would give it.
    ranks = si._cheapness_ranks(items)
    assert ranks[1]["pe"] == Decimal("1")
    assert ranks[5]["pe"] == Decimal("0")
    assert ranks[11]["pe"] == Decimal("1")


def test_small_sector_ranks_against_whole_universe():
    tech = [_item(i, "Technology", pe) for i, pe in enumerate(["20", "25", "30", "35", "40"], 1)]
    lone = [_item(99, "Real Estate", "10")]
    ranks = si._cheapness_ranks(tech + lone)
    assert ranks[99]["pe"] == Decimal("1")  # cheapest of everyone, not "cheapest of one"


# ---------------------------------------------------------------- parsing


BASE_INFO = {
    "currency": "EUR",
    "financialCurrency": "EUR",
    "sector": "Technology",
    "industry": "Semiconductors",
    "currentPrice": 85.5,
    "marketCap": 150_000_000_000,
    "forwardPE": 14.2,
    "trailingPE": 15.8,
    "pegRatio": 1.1,
    "priceToBook": 2.8,
    "enterpriseToEbitda": 9.2,
    "freeCashflow": 12_000_000_000,
    "trailingAnnualDividendYield": 0.025,
    "dividendYield": 2.5,
    "payoutRatio": 0.35,
    "debtToEquity": 45.0,
    "operatingMargins": 0.28,
    "profitMargins": 0.22,
    "returnOnEquity": 0.20,
    "earningsGrowth": 0.12,
    "revenueGrowth": 0.08,
    "fiftyTwoWeekHigh": 120.0,
    "fiftyTwoWeekLow": 75.0,
    "twoHundredDayAverage": 95.0,
    "targetMeanPrice": 110.0,
    "recommendationKey": "buy",
    "recommendationMean": 1.8,
    "numberOfAnalystOpinions": 20,
}


def test_parse_raw_stock_info():
    fundamentals = parse_raw_stock_info(
        instrument_id=99,
        symbol="SEMI.DE",
        name="Semiconductor AG",
        info=BASE_INFO,
        fast_info=None,
        now_iso="2026-09-06T12:00:00Z",
        recommendations={"strongBuy": 5, "buy": 10, "hold": 4, "sell": 1, "strongSell": 0},
        price_targets={"low": 90.0, "high": 140.0, "mean": 112.0, "median": 110.0},
    )
    assert fundamentals is not None
    assert fundamentals.instrument_id == 99
    assert fundamentals.sector == "Technology"
    assert fundamentals.forward_pe == Decimal("14.2")
    assert fundamentals.fcf_yield == Decimal("0.08")
    assert fundamentals.dividend_yield == Decimal("0.025")
    assert fundamentals.pct_off_52w_high is not None and fundamentals.pct_off_52w_high < 0
    assert fundamentals.analyst_count == 20
    assert fundamentals.buy_pct == Decimal("0.75")
    assert fundamentals.target_mean_price == Decimal("112")  # analyst table wins over info
    assert fundamentals.target_low_price == Decimal("90")
    assert fundamentals.earnings_growth == Decimal("0.12")
    assert fundamentals.data_points == 8
    assert fundamentals.value_badge in ("BARGAIN", "DIP", "FAIR")


def test_parse_returns_none_when_yahoo_gave_no_price():
    """A blank answer is a failed fetch, and must not overwrite the last good row."""
    assert parse_raw_stock_info(1, "X", "X", info={}, now_iso="t") is None
    assert parse_raw_stock_info(1, "X", "X", info={"sector": "Tech"}, now_iso="t") is None


def test_dividend_yield_small_percent_is_not_read_as_eighty_percent():
    info = {**BASE_INFO}
    del info["trailingAnnualDividendYield"]
    del info["dividendYield"]
    info["dividendYield"] = 0.8  # newer yfinance: this means 0.8%
    item = parse_raw_stock_info(1, "X", "X", info=info, now_iso="t")
    assert item is not None
    assert item.dividend_yield == Decimal("0.008")


def test_dividend_yield_prefers_fraction_field_then_rate_over_price():
    info = {k: v for k, v in BASE_INFO.items() if not k.startswith(("dividend", "trailingAnnual"))}
    info["dividendRate"] = 1.71
    item = parse_raw_stock_info(1, "X", "X", info=info, now_iso="t")
    assert item is not None
    assert item.dividend_yield == Decimal("0.02")  # 1.71 / 85.5


def test_fcf_yield_converts_reporting_currency():
    """Free cash flow in USD, market cap in EUR: convert, never mix."""
    info = {**BASE_INFO, "financialCurrency": "USD"}
    rates = {"EUR": Decimal("1"), "USD": Decimal("1.25")}  # 1 EUR = 1.25 USD
    item = parse_raw_stock_info(1, "X", "X", info=info, now_iso="t", fx_rates=rates)
    assert item is not None
    # 12bn USD = 9.6bn EUR; / 150bn EUR = 6.4%
    assert item.fcf_yield == Decimal("0.064")


def test_fcf_yield_skipped_when_currencies_differ_and_no_rate():
    info = {**BASE_INFO, "financialCurrency": "USD"}
    item = parse_raw_stock_info(1, "X", "X", info=info, now_iso="t", fx_rates=None)
    assert item is not None
    assert item.fcf_yield is None


def test_pence_quotes_count_as_pounds_for_fcf():
    info = {**BASE_INFO, "currency": "GBp", "financialCurrency": "GBP"}
    item = parse_raw_stock_info(1, "X", "X", info=info, now_iso="t")
    assert item is not None
    assert item.currency == "GBP"
    assert item.fcf_yield == Decimal("0.08")


def test_analyst_count_falls_back_to_rating_total():
    info = {k: v for k, v in BASE_INFO.items() if k != "numberOfAnalystOpinions"}
    item = parse_raw_stock_info(
        1, "X", "X", info=info, now_iso="t", recommendations={"strongBuy": 2, "buy": 3, "hold": 5}
    )
    assert item is not None
    assert item.analyst_count == 10
    assert item.buy_pct == Decimal("0.5")


# ---------------------------------------------------------------- persistence


def test_db_persistence_round_trip(db: sqlite3.Connection):
    _stock(db, 999, "TEST_STOCK", "Test Stock Corp")
    fund = StockFundamentals(
        instrument_id=999,
        symbol="TEST_STOCK",
        name="Test Stock Corp",
        sector="Technology",
        current_price=Decimal("50.0"),
        forward_pe=Decimal("12.0"),
        fcf_yield=Decimal("0.07"),
        analyst_count=7,
        strong_buy=2,
        buy=3,
        hold=2,
        buy_pct=Decimal("0.714286"),
        earnings_growth=Decimal("0.05"),
        financial_currency="USD",
        data_points=6,
        value_score=75,
        value_badge="BARGAIN",
        value_narrative="Bargain test stock",
        fetched_at="2026-09-06T12:00:00Z",
    )
    save_stock_fundamentals(db, [fund])
    (got,) = get_stock_fundamentals(db, instrument_id=999)
    assert got.symbol == "TEST_STOCK"
    assert got.sector == "Technology"
    assert got.forward_pe == Decimal("12.0")
    assert got.analyst_count == 7
    assert got.strong_buy == 2
    assert got.buy_pct == Decimal("0.714286")
    assert got.financial_currency == "USD"
    assert got.data_points == 6
    assert got.value_badge == "BARGAIN"
    # sector copied onto the instrument when it had none
    assert db.execute("SELECT sector FROM instrument WHERE id = 999").fetchone()[0] == "Technology"


def test_no_data_rows_sort_last(db: sqlite3.Connection):
    _stock(db, 1, "A", "A")
    _stock(db, 2, "B", "B")
    save_stock_fundamentals(
        db,
        [
            StockFundamentals(1, "A", "A", value_score=None, value_badge="NO_DATA", fetched_at="t"),
            StockFundamentals(2, "B", "B", value_score=40, value_badge="FAIR", fetched_at="t"),
        ],
    )
    got = get_stock_fundamentals(db)
    assert [g.symbol for g in got] == ["B", "A"]


def test_rescore_uses_stored_rows(db: sqlite3.Connection):
    for i in range(1, 7):
        _stock(db, i, f"T{i}", f"Tech {i}")
    items = [_item(i, "Technology", str(15 + 5 * i), fetched_at="t") for i in range(1, 7)]
    save_stock_fundamentals(db, items)  # stored with provisional absolute scores
    n = si.rescore_stock_fundamentals(db)
    assert n == 6
    got = {g.instrument_id: g for g in get_stock_fundamentals(db)}
    assert got[1].value_score > got[6].value_score  # type: ignore[operator]


# ---------------------------------------------------------------- API


def test_api_stocks_fundamentals_lists_new_fields(client: TestClient, db: sqlite3.Connection):
    _stock(db, 5, "SAP", "SAP SE")
    save_stock_fundamentals(
        db,
        [
            StockFundamentals(
                5,
                "SAP",
                "SAP SE",
                sector="Technology",
                current_price=Decimal("180"),
                analyst_count=26,
                buy_pct=Decimal("0.857143"),
                strong_buy=3,
                buy=21,
                hold=4,
                value_score=55,
                value_badge="FAIR",
                fetched_at="2026-09-06T12:00:00Z",
            )
        ],
    )
    res = client.get("/api/stocks/fundamentals")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    assert data["sectors"] == ["Technology"]
    (stock,) = data["stocks"]
    assert stock["analyst_count"] == 26
    assert stock["buy"] == 21
    assert stock["buy_pct"] == pytest.approx(0.857143)
    assert stock["value_badge"] == "FAIR"


def test_api_stocks_fundamentals_empty(client: TestClient):
    data = client.get("/api/stocks/fundamentals").json()
    assert data == {"count": 0, "sectors": [], "last_synced": None, "stocks": []}


def test_api_stocks_fundamentals_status(client: TestClient):
    res = client.get("/api/stocks/fundamentals/status")
    assert res.status_code == 200
    data = res.json()
    assert data["running"] is False
    assert data["last_synced"] is None


def test_api_peers_rejects_unknown_mode(client: TestClient):
    assert client.get("/api/peers?mode=sideways").status_code == 422


def test_sync_all_stock_fundamentals_empty(db: sqlite3.Connection):
    success, total = si.sync_all_stock_fundamentals(db)
    assert (success, total) == (0, 0)


def test_sync_keeps_old_row_when_yahoo_answers_blank(db: sqlite3.Connection, monkeypatch):
    """The failure mode the review found: a blank answer used to wipe the row."""
    _stock(db, 1, "OLD", "Old Co")
    good = StockFundamentals(
        1,
        "OLD",
        "Old Co",
        current_price=Decimal("10"),
        forward_pe=Decimal("9"),
        value_score=70,
        value_badge="BARGAIN",
        fetched_at="2026-01-01T00:00:00Z",
    )
    save_stock_fundamentals(db, [good])

    class BlankTicker:
        def __init__(self, sym: str) -> None:
            self.fast_info = None
            self.info: dict = {}
            self.recommendations = None
            self.analyst_price_targets = {}

    import types

    fake_yf = types.SimpleNamespace(Ticker=BlankTicker)
    monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)
    monkeypatch.setattr(
        si.time if hasattr(si, "time") else __import__("time"), "sleep", lambda *_: None
    )

    success, total = si.sync_all_stock_fundamentals(db, max_workers=1, retries=1)
    assert (success, total) == (0, 1)
    (kept,) = get_stock_fundamentals(db, instrument_id=1)
    assert kept.forward_pe == Decimal("9")
    assert kept.fetched_at == "2026-01-01T00:00:00Z"
