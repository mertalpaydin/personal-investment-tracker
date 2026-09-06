"""Stock fundamental valuation indicators, ratios, and cheapness scoring.

For single stocks, past price return and Sharpe ratio can be deceiving -- pushing
bubble momentum stocks to the top and penalizing quality turnaround bargains.
This module evaluates stocks on fundamental multiples (P/E, EV/EBITDA, P/B, FCF yield),
pullback discounts from 52-week highs, financial solvency, growth, and analyst consensus.

Scoring is in two layers:

* absolute thresholds (a P/E of 10 is cheap for anything), and
* sector-relative percentiles (a P/E of 25 is cheap for software, dear for a bank).

The two are blended 50/50 for the multiple blocks so that a whole sector cannot be
labelled "premium" just because its business model carries high multiples, while an
expensive sector's cheapest member still does not get full marks.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

#: Sectors where EV/EBITDA and debt-to-equity say nothing useful. Banks and
#: insurers carry deposits and float as "debt"; their EBITDA is not defined.
FINANCIAL_SECTORS = frozenset({"Financial Services"})

#: Fewer available inputs than this and the stock gets NO_DATA, not a score.
MIN_DATA_POINTS = 3

#: Analyst targets from fewer analysts than this are ignored: one house's
#: number is an opinion, not a consensus.
MIN_ANALYSTS = 3

#: A sector with fewer members than this is ranked against the whole universe.
MIN_SECTOR_SIZE = 5

#: Percentage below the 52-week high that counts as a "dip". Used by the badge,
#: the narrative and the UI filter text -- keep them in step.
DIP_THRESHOLD = Decimal("-0.20")

#: Fields read from the database row. Everything in `StockFundamentals` except
#: the identity columns, which come from the instrument table.
_DB_COLUMNS = (
    "sector",
    "industry",
    "current_price",
    "market_cap",
    "currency",
    "financial_currency",
    "trailing_pe",
    "forward_pe",
    "peg_ratio",
    "price_to_book",
    "price_to_sales",
    "ev_ebitda",
    "fcf_yield",
    "dividend_yield",
    "payout_ratio",
    "debt_to_equity",
    "operating_margin",
    "profit_margin",
    "return_on_equity",
    "earnings_growth",
    "revenue_growth",
    "fifty_two_week_high",
    "fifty_two_week_low",
    "pct_off_52w_high",
    "range_52w_pct",
    "two_hundred_dma",
    "dist_200_dma_pct",
    "target_mean_price",
    "target_median_price",
    "target_low_price",
    "target_high_price",
    "target_upside_pct",
    "recommendation_key",
    "recommendation_mean",
    "analyst_count",
    "strong_buy",
    "buy",
    "hold",
    "sell",
    "strong_sell",
    "buy_pct",
    "data_points",
    "value_score",
    "value_badge",
    "value_narrative",
    "fetched_at",
)

_INT_COLUMNS = frozenset(
    {
        "analyst_count",
        "strong_buy",
        "buy",
        "hold",
        "sell",
        "strong_sell",
        "data_points",
        "value_score",
    }
)
_TEXT_COLUMNS = frozenset(
    {
        "sector",
        "industry",
        "currency",
        "financial_currency",
        "recommendation_key",
        "value_badge",
        "value_narrative",
        "fetched_at",
    }
)


@dataclass
class StockFundamentals:
    instrument_id: int
    symbol: str
    name: str
    sector: str | None = None
    industry: str | None = None
    current_price: Decimal | None = None
    market_cap: Decimal | None = None
    currency: str = "EUR"
    financial_currency: str | None = None
    trailing_pe: Decimal | None = None
    forward_pe: Decimal | None = None
    peg_ratio: Decimal | None = None
    price_to_book: Decimal | None = None
    price_to_sales: Decimal | None = None
    ev_ebitda: Decimal | None = None
    fcf_yield: Decimal | None = None
    dividend_yield: Decimal | None = None
    payout_ratio: Decimal | None = None
    debt_to_equity: Decimal | None = None
    operating_margin: Decimal | None = None
    profit_margin: Decimal | None = None
    return_on_equity: Decimal | None = None
    earnings_growth: Decimal | None = None
    revenue_growth: Decimal | None = None
    fifty_two_week_high: Decimal | None = None
    fifty_two_week_low: Decimal | None = None
    pct_off_52w_high: Decimal | None = None
    #: Position inside the 52-week range, 0..100 (percent, unlike the other ratios).
    range_52w_pct: Decimal | None = None
    two_hundred_dma: Decimal | None = None
    dist_200_dma_pct: Decimal | None = None
    target_mean_price: Decimal | None = None
    target_median_price: Decimal | None = None
    target_low_price: Decimal | None = None
    target_high_price: Decimal | None = None
    target_upside_pct: Decimal | None = None
    recommendation_key: str | None = None
    #: 1.0 = strong buy .. 5.0 = sell (Yahoo's scale).
    recommendation_mean: Decimal | None = None
    analyst_count: int | None = None
    strong_buy: int | None = None
    buy: int | None = None
    hold: int | None = None
    sell: int | None = None
    strong_sell: int | None = None
    #: (strong_buy + buy) / all ratings, 0..1.
    buy_pct: Decimal | None = None
    #: How many of the scoring inputs Yahoo actually provided (0..8).
    data_points: int = 0
    value_score: int | None = None
    value_badge: str = "NO_DATA"
    value_narrative: str = ""
    fetched_at: str = ""


def _to_decimal(val: Any) -> Decimal | None:
    if val is None or val == "" or val != val:  # NaN check
        return None
    try:
        return Decimal(str(round(float(val), 6)))
    except (ValueError, TypeError, OverflowError):
        return None


def _to_int(val: Any) -> int | None:
    if val is None or val == "" or val != val:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError, OverflowError):
        return None


def _is_financial(sector: str | None) -> bool:
    return sector in FINANCIAL_SECTORS


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _blend(absolute: int, relative: Decimal | None, block_max: int) -> int:
    """Average the absolute points with the sector-relative points, if known.

    ``relative`` is a cheapness rank 0..1 where 1 means "cheapest in its sector".
    """
    if relative is None:
        return absolute
    rel_pts = round(float(relative) * block_max)
    return round((absolute + rel_pts) / 2)


def count_data_points(
    *,
    pe: Decimal | None,
    fcf_yield: Decimal | None,
    pct_off_52w_high: Decimal | None,
    ev_ebitda: Decimal | None,
    price_to_book: Decimal | None,
    target_upside_pct: Decimal | None,
    dividend_yield: Decimal | None,
    debt_to_equity: Decimal | None,
    operating_margin: Decimal | None,
) -> int:
    """How many of the eight scoring inputs are present."""
    return sum(
        1
        for present in (
            pe is not None,
            fcf_yield is not None,
            pct_off_52w_high is not None,
            ev_ebitda is not None or price_to_book is not None,
            target_upside_pct is not None,
            dividend_yield is not None,
            debt_to_equity is not None,
            operating_margin is not None,
        )
        if present
    )


def calculate_value_score(
    *,
    forward_pe: Decimal | None,
    trailing_pe: Decimal | None,
    fcf_yield: Decimal | None,
    pct_off_52w_high: Decimal | None,
    range_52w_pct: Decimal | None,
    ev_ebitda: Decimal | None,
    price_to_book: Decimal | None,
    target_upside_pct: Decimal | None,
    dividend_yield: Decimal | None,
    debt_to_equity: Decimal | None,
    operating_margin: Decimal | None,
    sector: str | None = None,
    peg_ratio: Decimal | None = None,
    earnings_growth: Decimal | None = None,
    analyst_count: int | None = None,
    buy_pct: Decimal | None = None,
    relative: Mapping[str, Decimal] | None = None,
) -> tuple[int | None, str, str]:
    """Calculate a 0-100 composite Value Score and assign a badge & narrative.

    ``relative`` holds sector-relative cheapness ranks (0..1, 1 = cheapest) under
    the keys ``pe``, ``fcf_yield``, ``ev_ebitda`` and ``price_to_book``. Without it
    the score is purely absolute.

    Blocks (max points):
      P/E 25 (+ growth credit via PEG), FCF yield 20, pullback 15, EV/EBITDA or P/B 15,
      analyst consensus 10, dividend 5, quality & solvency 10. Total 100.

    Badges:
      - NO_DATA: Fewer than three inputs available; the score is None.
      - BARGAIN: High value score, reasonable leverage, high cash generation.
      - DIP: Quality stock trading at a significant discount (20%+ off 52W high).
      - FAIR: In line with normal valuation ranges.
      - PREMIUM: Expensive valuation (high P/E, trading near 52W high).
      - TRAP_RISK: Looks cheap but leverage, losses or falling earnings say why.
    """
    relative = relative or {}
    financial = _is_financial(sector)
    pe = forward_pe if forward_pe is not None else trailing_pe
    if pe is not None and pe <= 0:
        pe = None  # negative earnings: no multiple, not a cheap one

    data_points = count_data_points(
        pe=pe,
        fcf_yield=fcf_yield,
        pct_off_52w_high=pct_off_52w_high,
        ev_ebitda=ev_ebitda,
        price_to_book=price_to_book,
        target_upside_pct=target_upside_pct,
        dividend_yield=dividend_yield,
        debt_to_equity=debt_to_equity,
        operating_margin=operating_margin,
    )
    if data_points < MIN_DATA_POINTS:
        return (
            None,
            "NO_DATA",
            f"Not enough data from Yahoo Finance ({data_points} of 8 inputs available).",
        )

    score = 0

    # 1. P/E multiple (0 to 25 pts), sector-blended, with growth credit.
    if pe is not None:
        if pe <= Decimal("12.0"):
            pts = 25
        elif pe <= Decimal("18.0"):
            pts = 20
        elif pe <= Decimal("25.0"):
            pts = 12
        elif pe <= Decimal("35.0"):
            pts = 5
        else:
            pts = 0
        pts = _blend(pts, relative.get("pe"), 25)
        # A high multiple backed by growth is not the same as a high multiple.
        if pe > Decimal("18.0") and peg_ratio is not None and peg_ratio > 0:
            if peg_ratio <= Decimal("1.0"):
                pts += 8
            elif peg_ratio <= Decimal("1.5"):
                pts += 4
        score += min(25, pts)

    # 2. Free cash flow yield (0 to 20 pts), sector-blended.
    if fcf_yield is not None:
        if fcf_yield >= Decimal("0.08"):
            pts = 20
        elif fcf_yield >= Decimal("0.05"):
            pts = 15
        elif fcf_yield >= Decimal("0.03"):
            pts = 10
        elif fcf_yield > Decimal("0.0"):
            pts = 5
        else:
            pts = 0
        score += _blend(pts, relative.get("fcf_yield"), 20)

    # 3. Pullback discount & 52-week range position (0 to 15 pts).
    if pct_off_52w_high is not None:
        if pct_off_52w_high <= Decimal("-0.35"):
            score += 10
        elif pct_off_52w_high <= DIP_THRESHOLD:
            score += 7
        elif pct_off_52w_high <= Decimal("-0.10"):
            score += 3
    if range_52w_pct is not None:
        if range_52w_pct <= Decimal("25.0"):
            score += 5
        elif range_52w_pct <= Decimal("50.0"):
            score += 3

    # 4. EV/EBITDA, or P/B for financials and where EV/EBITDA is missing (0 to 15 pts).
    use_ev = ev_ebitda is not None and ev_ebitda > 0 and not financial
    if use_ev:
        if ev_ebitda <= Decimal("9.0"):  # type: ignore[operator]
            pts = 15
        elif ev_ebitda <= Decimal("14.0"):  # type: ignore[operator]
            pts = 9
        elif ev_ebitda <= Decimal("20.0"):  # type: ignore[operator]
            pts = 4
        else:
            pts = 0
        score += _blend(pts, relative.get("ev_ebitda"), 15)
    elif price_to_book is not None and price_to_book > 0:
        if price_to_book <= Decimal("1.5"):
            pts = 15
        elif price_to_book <= Decimal("3.0"):
            pts = 9
        elif price_to_book <= Decimal("5.0"):
            pts = 4
        else:
            pts = 0
        score += _blend(pts, relative.get("price_to_book"), 15)

    # 5. Analyst consensus (0 to 10 pts): upside needs a real crowd behind it.
    enough_analysts = analyst_count is not None and analyst_count >= MIN_ANALYSTS
    if enough_analysts and target_upside_pct is not None:
        if target_upside_pct >= Decimal("0.20"):
            score += 5
        elif target_upside_pct >= Decimal("0.10"):
            score += 3
    if enough_analysts and buy_pct is not None:
        if buy_pct >= Decimal("0.70"):
            score += 5
        elif buy_pct >= Decimal("0.50"):
            score += 3

    # 6. Dividend (0 to 5 pts).
    if dividend_yield is not None:
        if dividend_yield >= Decimal("0.035"):
            score += 5
        elif dividend_yield >= Decimal("0.015"):
            score += 3

    # 7. Quality & solvency guards (0 to 10 pts).
    if operating_margin is not None:
        if operating_margin >= Decimal("0.15"):
            score += 5
        elif operating_margin >= Decimal("0.05"):
            score += 3
    if financial:
        score += 3  # leverage is the business model; neither reward nor punish
    elif debt_to_equity is not None:
        if debt_to_equity <= Decimal("100.0"):
            score += 5
        elif debt_to_equity <= Decimal("180.0"):
            score += 3

    score = max(0, min(100, score))

    # Value trap: looks cheap, and there is a visible reason why.
    trap_reasons: list[str] = []
    if pe is not None and pe <= Decimal("14.0"):
        if not financial and debt_to_equity is not None and debt_to_equity >= Decimal("280.0"):
            trap_reasons.append(f"high leverage ({float(debt_to_equity):.0f}% D/E)")
        if operating_margin is not None and operating_margin < 0:
            trap_reasons.append("negative operating margin")
        if earnings_growth is not None and earnings_growth <= Decimal("-0.20"):
            trap_reasons.append(f"earnings down {abs(float(earnings_growth)) * 100:.0f}%")
        if (
            forward_pe is not None
            and trailing_pe is not None
            and trailing_pe > 0
            and forward_pe > trailing_pe * Decimal("1.25")
        ):
            trap_reasons.append(
                f"earnings expected to fall (forward P/E {forward_pe:.1f}x "
                f"vs trailing {trailing_pe:.1f}x)"
            )

    analyst_note = None
    if enough_analysts and buy_pct is not None:
        analyst_note = f"{analyst_count} analysts, {float(buy_pct) * 100:.0f}% rate it a buy"

    narrative_parts: list[str] = []
    if trap_reasons:
        badge = "TRAP_RISK"
        score = min(score, 38)
        narrative_parts.append(
            f"Low P/E ({pe:.1f}x) appears cheap, but: {', '.join(trap_reasons)}."
        )
    elif score >= 70:
        badge = "BARGAIN"
        if pe is not None:
            narrative_parts.append(f"Attractively valued at {pe:.1f}x P/E")
        if fcf_yield is not None and fcf_yield >= Decimal("0.05"):
            narrative_parts.append(f"strong {float(fcf_yield) * 100:.1f}% FCF yield")
        if pct_off_52w_high is not None and pct_off_52w_high <= DIP_THRESHOLD:
            narrative_parts.append(f"trading {abs(float(pct_off_52w_high)) * 100:.0f}% off highs")
        if analyst_note:
            narrative_parts.append(analyst_note)
    elif pct_off_52w_high is not None and pct_off_52w_high <= DIP_THRESHOLD and score >= 55:
        badge = "DIP"
        narrative_parts.append(
            f"Pullback opportunity: down {abs(float(pct_off_52w_high)) * 100:.0f}% from 52W high"
        )
        if enough_analysts and target_upside_pct is not None and target_upside_pct > 0:
            narrative_parts.append(f"{float(target_upside_pct) * 100:.0f}% analyst upside target")
        if analyst_note:
            narrative_parts.append(analyst_note)
    elif score >= 40:
        badge = "FAIR"
        if pe is not None:
            narrative_parts.append(f"Fairly valued at {pe:.1f}x P/E")
        else:
            narrative_parts.append("Trading within balanced valuation parameters")
        if analyst_note:
            narrative_parts.append(analyst_note)
    else:
        badge = "PREMIUM"
        if pe is None:
            narrative_parts.append("No positive earnings to value against; priced on hope")
        elif pe >= Decimal("30.0"):
            if peg_ratio is not None and 0 < peg_ratio <= Decimal("1.5"):
                narrative_parts.append(
                    f"High multiple at {pe:.1f}x P/E, partly backed by growth (PEG {peg_ratio:.1f})"
                )
            else:
                narrative_parts.append(f"Premium multiple at {pe:.1f}x P/E")
        else:
            narrative_parts.append("Trading at a premium with limited margin of safety")
        if analyst_note:
            narrative_parts.append(analyst_note)

    return score, badge, "; ".join(narrative_parts)


def _cheapness_ranks(items: list[StockFundamentals]) -> dict[int, dict[str, Decimal]]:
    """Sector-relative cheapness rank (0..1, 1 = cheapest) per stock and metric.

    A sector with fewer than ``MIN_SECTOR_SIZE`` members is ranked against the
    whole universe instead, so a lone real-estate stock is not "the cheapest
    of one".
    """

    def metric_pe(s: StockFundamentals) -> Decimal | None:
        pe = s.forward_pe if s.forward_pe is not None else s.trailing_pe
        return pe if pe is not None and pe > 0 else None

    def metric_ev(s: StockFundamentals) -> Decimal | None:
        return s.ev_ebitda if s.ev_ebitda is not None and s.ev_ebitda > 0 else None

    def metric_pb(s: StockFundamentals) -> Decimal | None:
        return s.price_to_book if s.price_to_book is not None and s.price_to_book > 0 else None

    def metric_fcf(s: StockFundamentals) -> Decimal | None:
        return s.fcf_yield

    #: (key, getter, lower_is_cheaper)
    metrics = (
        ("pe", metric_pe, True),
        ("ev_ebitda", metric_ev, True),
        ("price_to_book", metric_pb, True),
        ("fcf_yield", metric_fcf, False),
    )

    by_sector: dict[str | None, list[StockFundamentals]] = {}
    for s in items:
        by_sector.setdefault(s.sector, []).append(s)

    out: dict[int, dict[str, Decimal]] = {s.instrument_id: {} for s in items}
    for sector_items in by_sector.values():
        group = sector_items if len(sector_items) >= MIN_SECTOR_SIZE else items
        for key, getter, lower_is_cheaper in metrics:
            values = [(s.instrument_id, getter(s)) for s in group]
            known = [(sid, v) for sid, v in values if v is not None]
            if len(known) < MIN_SECTOR_SIZE:
                continue
            n = len(known)
            for s in sector_items:
                mine = getter(s)
                if mine is None:
                    continue
                if lower_is_cheaper:
                    dearer = sum(1 for _, v in known if v > mine)
                else:
                    dearer = sum(1 for _, v in known if v < mine)
                out[s.instrument_id][key] = Decimal(dearer) / Decimal(n - 1)
    return out


def apply_score(item: StockFundamentals, relative: Mapping[str, Decimal] | None = None) -> None:
    """Fill ``value_score``, ``value_badge``, ``value_narrative`` and ``data_points`` in place."""
    pe = item.forward_pe if item.forward_pe is not None else item.trailing_pe
    item.data_points = count_data_points(
        pe=pe if pe is not None and pe > 0 else None,
        fcf_yield=item.fcf_yield,
        pct_off_52w_high=item.pct_off_52w_high,
        ev_ebitda=item.ev_ebitda,
        price_to_book=item.price_to_book,
        target_upside_pct=item.target_upside_pct,
        dividend_yield=item.dividend_yield,
        debt_to_equity=item.debt_to_equity,
        operating_margin=item.operating_margin,
    )
    score, badge, narrative = calculate_value_score(
        forward_pe=item.forward_pe,
        trailing_pe=item.trailing_pe,
        fcf_yield=item.fcf_yield,
        pct_off_52w_high=item.pct_off_52w_high,
        range_52w_pct=item.range_52w_pct,
        ev_ebitda=item.ev_ebitda,
        price_to_book=item.price_to_book,
        target_upside_pct=item.target_upside_pct,
        dividend_yield=item.dividend_yield,
        debt_to_equity=item.debt_to_equity,
        operating_margin=item.operating_margin,
        sector=item.sector,
        peg_ratio=item.peg_ratio,
        earnings_growth=item.earnings_growth,
        analyst_count=item.analyst_count,
        buy_pct=item.buy_pct,
        relative=relative,
    )
    item.value_score = score
    item.value_badge = badge
    item.value_narrative = narrative


def score_universe(items: list[StockFundamentals]) -> None:
    """Score every stock against its sector peers, in place."""
    ranks = _cheapness_ranks(items)
    for item in items:
        apply_score(item, ranks.get(item.instrument_id))


# ---------------------------------------------------------------------------
# Parsing Yahoo's answer
# ---------------------------------------------------------------------------


def _dividend_yield(info: Mapping[str, Any]) -> Decimal | None:
    """Dividend yield as a fraction, whatever unit Yahoo used this month.

    ``trailingAnnualDividendYield`` has always been a fraction. ``dividendYield``
    switched from a fraction to a percentage in yfinance 0.2.5x, and a threshold
    guess ("bigger than 1 must be percent") misreads a 0.8% yield as 80%. So:
    fraction field first, then dividend rate over price, and the ambiguous field
    only as a last resort with the percent reading.
    """
    frac = _to_decimal(info.get("trailingAnnualDividendYield"))
    if frac is not None and frac >= 0:
        return frac
    rate = _to_decimal(info.get("dividendRate"))
    price = _to_decimal(info.get("currentPrice") or info.get("regularMarketPrice"))
    if rate is not None and price is not None and price > 0:
        return _to_decimal(rate / price)
    raw = _to_decimal(info.get("dividendYield"))
    if raw is None:
        return None
    return _to_decimal(raw / 100)  # current yfinance: percent


def _normalise_currency(code: str | None) -> str | None:
    if not code:
        return None
    code = str(code)
    # London quotes in pence, but market cap is in pounds.
    if code == "GBp":
        return "GBP"
    return code.upper()


def parse_raw_stock_info(
    instrument_id: int,
    symbol: str,
    name: str,
    info: Mapping[str, Any],
    fast_info: Any | None = None,
    now_iso: str = "",
    recommendations: Mapping[str, Any] | None = None,
    price_targets: Mapping[str, Any] | None = None,
    fx_rates: Mapping[str, Decimal] | None = None,
) -> StockFundamentals | None:
    """Parse yfinance ``info`` (plus optional analyst tables) into ``StockFundamentals``.

    Returns ``None`` when Yahoo gave no price at all -- a blank answer is a failed
    fetch, and must not overwrite the last good row.

    ``fx_rates`` maps a currency code to its quote-per-euro rate, used to bring
    free cash flow (reported in ``financialCurrency``) into the trading currency
    before dividing by market cap.
    """
    currency = _normalise_currency(info.get("currency")) or "EUR"
    financial_currency = _normalise_currency(info.get("financialCurrency"))
    sector = info.get("sector") or None
    industry = info.get("industry") or None

    current_price = _to_decimal(info.get("currentPrice") or info.get("regularMarketPrice"))
    if current_price is None and fast_info is not None:
        with contextlib.suppress(Exception):
            current_price = _to_decimal(getattr(fast_info, "last_price", None))
    if current_price is None or current_price <= 0:
        return None

    market_cap = _to_decimal(info.get("marketCap"))
    if market_cap is None and fast_info is not None:
        with contextlib.suppress(Exception):
            market_cap = _to_decimal(getattr(fast_info, "market_cap", None))

    trailing_pe = _to_decimal(info.get("trailingPE"))
    forward_pe = _to_decimal(info.get("forwardPE"))
    peg_ratio = _to_decimal(info.get("pegRatio") or info.get("trailingPegRatio"))
    price_to_book = _to_decimal(info.get("priceToBook"))
    price_to_sales = _to_decimal(info.get("priceToSalesTrailing12Months"))
    ev_ebitda = _to_decimal(info.get("enterpriseToEbitda"))

    # Free cash flow yield. FCF is in the reporting currency, market cap in the
    # trading currency; convert or refuse, never mix.
    fcf = _to_decimal(info.get("freeCashflow"))
    fcf_yield = None
    if fcf is not None and market_cap is not None and market_cap > 0:
        report_ccy = financial_currency or currency
        if report_ccy == currency:
            fcf_yield = _to_decimal(fcf / market_cap)
        elif fx_rates and report_ccy in fx_rates and currency in fx_rates:
            fcf_in_trading = fcf / fx_rates[report_ccy] * fx_rates[currency]
            fcf_yield = _to_decimal(fcf_in_trading / market_cap)
        else:
            log.debug(
                "%s: FCF in %s but market cap in %s and no FX rate; FCF yield skipped",
                symbol,
                report_ccy,
                currency,
            )

    dividend_yield = _dividend_yield(info)
    payout_ratio = _to_decimal(info.get("payoutRatio"))
    debt_to_equity = _to_decimal(info.get("debtToEquity"))
    operating_margin = _to_decimal(info.get("operatingMargins"))
    profit_margin = _to_decimal(info.get("profitMargins"))
    return_on_equity = _to_decimal(info.get("returnOnEquity"))
    earnings_growth = _to_decimal(info.get("earningsGrowth"))
    revenue_growth = _to_decimal(info.get("revenueGrowth"))

    high_52 = _to_decimal(info.get("fiftyTwoWeekHigh"))
    low_52 = _to_decimal(info.get("fiftyTwoWeekLow"))
    if high_52 is None and fast_info is not None:
        with contextlib.suppress(Exception):
            high_52 = _to_decimal(getattr(fast_info, "year_high", None))
            low_52 = _to_decimal(getattr(fast_info, "year_low", None))

    pct_off_52w_high = None
    if high_52 is not None and high_52 > 0:
        pct_off_52w_high = _to_decimal((current_price - high_52) / high_52)

    range_52w_pct = None
    if high_52 is not None and low_52 is not None:
        diff = high_52 - low_52
        if diff > 0:
            range_52w_pct = _to_decimal(((current_price - low_52) / diff) * 100)

    two_hundred_dma = _to_decimal(info.get("twoHundredDayAverage"))
    if two_hundred_dma is None and fast_info is not None:
        with contextlib.suppress(Exception):
            two_hundred_dma = _to_decimal(getattr(fast_info, "two_hundred_day_average", None))

    dist_200_dma_pct = None
    if two_hundred_dma is not None and two_hundred_dma > 0:
        dist_200_dma_pct = _to_decimal((current_price - two_hundred_dma) / two_hundred_dma)

    # Analyst consensus.
    targets = price_targets or {}
    target_mean = _to_decimal(targets.get("mean")) or _to_decimal(info.get("targetMeanPrice"))
    target_median = _to_decimal(targets.get("median")) or _to_decimal(info.get("targetMedianPrice"))
    target_low = _to_decimal(targets.get("low")) or _to_decimal(info.get("targetLowPrice"))
    target_high = _to_decimal(targets.get("high")) or _to_decimal(info.get("targetHighPrice"))
    target_upside = None
    if target_mean is not None:
        target_upside = _to_decimal((target_mean - current_price) / current_price)

    recommendation = info.get("recommendationKey") or None
    recommendation_mean = _to_decimal(info.get("recommendationMean"))
    analyst_count = _to_int(info.get("numberOfAnalystOpinions"))

    recs = recommendations or {}
    strong_buy = _to_int(recs.get("strongBuy"))
    buy = _to_int(recs.get("buy"))
    hold = _to_int(recs.get("hold"))
    sell = _to_int(recs.get("sell"))
    strong_sell = _to_int(recs.get("strongSell"))
    buy_pct = None
    counts = [strong_buy, buy, hold, sell, strong_sell]
    if any(c is not None for c in counts):
        total = sum(c or 0 for c in counts)
        if total > 0:
            buy_pct = _to_decimal(Decimal((strong_buy or 0) + (buy or 0)) / Decimal(total))
            if analyst_count is None:
                analyst_count = total

    item = StockFundamentals(
        instrument_id=instrument_id,
        symbol=symbol,
        name=name,
        sector=sector,
        industry=industry,
        current_price=current_price,
        market_cap=market_cap,
        currency=currency,
        financial_currency=financial_currency,
        trailing_pe=trailing_pe,
        forward_pe=forward_pe,
        peg_ratio=peg_ratio,
        price_to_book=price_to_book,
        price_to_sales=price_to_sales,
        ev_ebitda=ev_ebitda,
        fcf_yield=fcf_yield,
        dividend_yield=dividend_yield,
        payout_ratio=payout_ratio,
        debt_to_equity=debt_to_equity,
        operating_margin=operating_margin,
        profit_margin=profit_margin,
        return_on_equity=return_on_equity,
        earnings_growth=earnings_growth,
        revenue_growth=revenue_growth,
        fifty_two_week_high=high_52,
        fifty_two_week_low=low_52,
        pct_off_52w_high=pct_off_52w_high,
        range_52w_pct=range_52w_pct,
        two_hundred_dma=two_hundred_dma,
        dist_200_dma_pct=dist_200_dma_pct,
        target_mean_price=target_mean,
        target_median_price=target_median,
        target_low_price=target_low,
        target_high_price=target_high,
        target_upside_pct=target_upside,
        recommendation_key=recommendation,
        recommendation_mean=recommendation_mean,
        analyst_count=analyst_count,
        strong_buy=strong_buy,
        buy=buy,
        hold=hold,
        sell=sell,
        strong_sell=strong_sell,
        buy_pct=buy_pct,
        fetched_at=now_iso,
    )
    # Provisional, absolute-only score; `score_universe` refines it once the
    # whole universe is known.
    apply_score(item)
    return item


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _db_value(item: StockFundamentals, column: str) -> Any:
    val = getattr(item, column)
    if val is None:
        return None
    if column in _INT_COLUMNS:
        return int(val)
    if column in _TEXT_COLUMNS:
        return str(val)
    return str(val)


def save_stock_fundamentals(conn: sqlite3.Connection, items: list[StockFundamentals]) -> None:
    """Upsert stock fundamentals into stock_fundamental table."""
    if not items:
        return
    columns = ("instrument_id", *_DB_COLUMNS)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _DB_COLUMNS)
    conn.executemany(
        f"""
        INSERT INTO stock_fundamental ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(instrument_id) DO UPDATE SET {updates}
        """,
        [(item.instrument_id, *(_db_value(item, c) for c in _DB_COLUMNS)) for item in items],
    )
    # Also keep instrument.sector synchronized if sector was discovered
    for item in items:
        if item.sector:
            conn.execute(
                "UPDATE instrument SET sector = ? WHERE id = ? AND (sector IS NULL OR sector = '')",
                (item.sector, item.instrument_id),
            )
    conn.commit()


def _row_to_fundamentals(r: sqlite3.Row) -> StockFundamentals:
    keys = set(r.keys())
    kwargs: dict[str, Any] = {
        "instrument_id": int(r["instrument_id"]),
        "symbol": str(r["symbol"]),
        "name": str(r["instrument_name"]),
    }
    for column in _DB_COLUMNS:
        if column not in keys:
            continue
        raw = r[column]
        if column in _INT_COLUMNS:
            kwargs[column] = _to_int(raw)
        elif column in _TEXT_COLUMNS:
            kwargs[column] = str(raw) if raw is not None else None
        else:
            kwargs[column] = _to_decimal(raw)
    if kwargs.get("currency") is None:
        kwargs["currency"] = "EUR"
    if kwargs.get("value_badge") is None:
        kwargs["value_badge"] = "NO_DATA"
    if kwargs.get("value_narrative") is None:
        kwargs["value_narrative"] = ""
    if kwargs.get("fetched_at") is None:
        kwargs["fetched_at"] = ""
    if kwargs.get("data_points") is None:
        kwargs["data_points"] = 0
    return StockFundamentals(**kwargs)


def load_all_stock_fundamentals(conn: sqlite3.Connection) -> list[StockFundamentals]:
    """Every stored row, unfiltered and in instrument order."""
    rows = conn.execute(
        """
        SELECT sf.*, i.import_key AS symbol, i.name AS instrument_name
        FROM stock_fundamental sf
        JOIN instrument i ON i.id = sf.instrument_id
        ORDER BY sf.instrument_id
        """
    ).fetchall()
    return [_row_to_fundamentals(r) for r in rows]


def get_stock_fundamentals(
    conn: sqlite3.Connection,
    *,
    instrument_id: int | None = None,
    sector: str | None = None,
    min_score: int | None = None,
    max_forward_pe: Decimal | None = None,
    min_fcf_yield: Decimal | None = None,
) -> list[StockFundamentals]:
    """Retrieve fundamentals for one stock or filtered across the universe."""
    where_clauses = ["1=1"]
    params: list[Any] = []

    if instrument_id is not None:
        where_clauses.append("sf.instrument_id = ?")
        params.append(instrument_id)
    if sector:
        where_clauses.append("sf.sector = ?")
        params.append(sector)
    if min_score is not None:
        where_clauses.append("sf.value_score >= ?")
        params.append(min_score)

    sql = f"""
        SELECT
            sf.*,
            i.import_key AS symbol,
            i.name AS instrument_name
        FROM stock_fundamental sf
        JOIN instrument i ON i.id = sf.instrument_id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY sf.value_score IS NULL, sf.value_score DESC, sf.forward_pe ASC
    """
    rows = conn.execute(sql, params).fetchall()
    out: list[StockFundamentals] = []
    for r in rows:
        item = _row_to_fundamentals(r)
        if max_forward_pe is not None and (
            item.forward_pe is None or item.forward_pe > max_forward_pe
        ):
            continue
        if min_fcf_yield is not None and (item.fcf_yield is None or item.fcf_yield < min_fcf_yield):
            continue
        out.append(item)
    return out


def rescore_stock_fundamentals(conn: sqlite3.Connection) -> int:
    """Re-run sector-relative scoring over the stored rows without refetching."""
    items = load_all_stock_fundamentals(conn)
    score_universe(items)
    save_stock_fundamentals(conn, items)
    return len(items)


# ---------------------------------------------------------------------------
# Fetching from Yahoo
# ---------------------------------------------------------------------------


def _fx_rates_for(conn: sqlite3.Connection, currencies: set[str]) -> dict[str, Decimal]:
    """Quote-per-euro rates for the given currencies, as of today, best effort."""
    from datetime import date

    from portfolio_tracker.core import fx

    out: dict[str, Decimal] = {"EUR": Decimal(1)}
    today = date.today()
    for ccy in currencies:
        if ccy in out:
            continue
        try:
            out[ccy] = fx.get_rate(conn, ccy, today, strict=False).rate
        except Exception as exc:  # FxUnavailable or a bare table
            log.debug("no FX rate for %s: %s", ccy, exc)
    return out


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "rate limit" in text or "429" in text or "too many" in text


def _current_recommendations(ticker: Any) -> dict[str, Any] | None:
    """The ``0m`` row of ``ticker.recommendations`` as a plain dict, or None."""
    with contextlib.suppress(Exception):
        df = ticker.recommendations
        if df is None or len(df) == 0:
            return None
        row = None
        if "period" in df.columns:
            current = df[df["period"] == "0m"]
            if len(current):
                row = current.iloc[0]
        if row is None:
            row = df.iloc[0]
        return {k: row[k] for k in ("strongBuy", "buy", "hold", "sell", "strongSell") if k in row}
    return None


def _price_targets(ticker: Any) -> dict[str, Any] | None:
    with contextlib.suppress(Exception):
        targets = ticker.analyst_price_targets
        if isinstance(targets, Mapping) and targets:
            return dict(targets)
    return None


def sync_all_stock_fundamentals(
    conn: sqlite3.Connection,
    *,
    max_workers: int = 4,
    progress_cb: Any | None = None,
    retries: int = 3,
) -> tuple[int, int]:
    """Fetch and update fundamental valuation indicators for all single stocks.

    Returns ``(fetched, total)``. Stocks Yahoo answered blank for keep their old
    row; every stored row is then re-scored against its sector so that the
    relative ranks reflect the whole universe, not just today's successes.
    """
    import concurrent.futures
    import time
    from datetime import UTC, datetime

    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for stock fundamental syncing") from exc

    rows = conn.execute("""
        SELECT id, import_key, price_symbol, name, currency
        FROM instrument
        WHERE asset_class = 'STOCK'
        ORDER BY id
    """).fetchall()

    if not rows:
        return (0, 0)

    total = len(rows)
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    known_currencies = {str(r["currency"]).upper() for r in rows if r["currency"]}
    known_currencies.update({"USD", "GBP", "CHF", "SEK", "DKK", "NOK", "JPY"})
    fx_rates = _fx_rates_for(conn, known_currencies)

    def fetch_one(r: sqlite3.Row) -> StockFundamentals | None:
        inst_id = r["id"]
        key = r["import_key"]
        sym = r["price_symbol"] or key
        name = r["name"]
        for attempt in range(retries):
            try:
                time.sleep(0.15 * (attempt + 1))  # be polite before, not after
                ticker = yf.Ticker(sym)
                fast_info = getattr(ticker, "fast_info", None)
                info = getattr(ticker, "info", None) or {}
                item = parse_raw_stock_info(
                    instrument_id=inst_id,
                    symbol=key,
                    name=name,
                    info=info,
                    fast_info=fast_info,
                    now_iso=now_iso,
                    recommendations=_current_recommendations(ticker),
                    price_targets=_price_targets(ticker),
                    fx_rates=fx_rates,
                )
                if item is None:
                    log.warning("%s (%s): Yahoo returned no price; keeping previous row", key, sym)
                return item
            except Exception as exc:
                if _is_rate_limit(exc) and attempt < retries - 1:
                    wait = 2.0 * (2**attempt)
                    log.warning("%s: rate limited, retrying in %.0fs", key, wait)
                    time.sleep(wait)
                    continue
                log.warning("%s (%s): fetch failed: %s", key, sym, exc)
                return None
        return None

    fetched: dict[int, StockFundamentals] = {}
    completed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_key = {executor.submit(fetch_one, r): r["import_key"] for r in rows}
        for future in concurrent.futures.as_completed(future_to_key):
            completed += 1
            key = future_to_key[future]
            if progress_cb:
                progress_cb(completed, total, key)
            try:
                fund = future.result()
            except Exception as exc:
                log.warning("%s: worker crashed: %s", key, exc)
                continue
            if fund:
                fetched[fund.instrument_id] = fund

    if fetched:
        # Merge with what is already stored so the sector ranks see everyone.
        existing = {s.instrument_id: s for s in load_all_stock_fundamentals(conn)}
        existing.update(fetched)
        universe = list(existing.values())
        score_universe(universe)
        save_stock_fundamentals(conn, universe)

    return (len(fetched), total)
