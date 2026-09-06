"""Stock fundamental valuation indicators, ratios, and cheapness scoring.

For single stocks, past price return and Sharpe ratio can be deceiving -- pushing
bubble momentum stocks to the top and penalizing quality turnaround bargains.
This module evaluates stocks on fundamental multiples (P/E, EV/EBITDA, P/B, FCF yield),
pullback discounts from 52-week highs, financial solvency, and analyst consensus upside.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


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
    fifty_two_week_high: Decimal | None = None
    fifty_two_week_low: Decimal | None = None
    pct_off_52w_high: Decimal | None = None
    range_52w_pct: Decimal | None = None
    two_hundred_dma: Decimal | None = None
    dist_200_dma_pct: Decimal | None = None
    target_mean_price: Decimal | None = None
    target_upside_pct: Decimal | None = None
    recommendation_key: str | None = None
    value_score: int = 50
    value_badge: str = "FAIR"
    value_narrative: str = ""
    fetched_at: str = ""


def _to_decimal(val: Any) -> Decimal | None:
    if val is None or val == "" or val != val:  # NaN check
        return None
    try:
        return Decimal(str(round(float(val), 6)))
    except (ValueError, TypeError):
        return None


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
) -> tuple[int, str, str]:
    """Calculate a 0-100 composite Value Score and assign a badge & narrative.

    Badges:
      - BARGAIN: High value score, reasonable leverage, high cash generation.
      - DIP: Quality stock trading at significant discount (>20% off 52W high).
      - FAIR: In line with normal valuation ranges.
      - PREMIUM: Expensive valuation (high P/E, trading near 52W high).
      - TRAP_RISK: Low P/E but high debt or negative margins (potential value trap).
    """
    score = 0
    pe = forward_pe if forward_pe is not None else trailing_pe

    # 1. P/E Multiple (0 to 25 pts)
    if pe is not None and pe > 0:
        if pe <= Decimal("12.0"):
            score += 25
        elif pe <= Decimal("18.0"):
            score += 20
        elif pe <= Decimal("25.0"):
            score += 12
        elif pe <= Decimal("35.0"):
            score += 5
        # Above 35 is 0 pts

    # 2. Free Cash Flow Yield (0 to 20 pts)
    if fcf_yield is not None:
        if fcf_yield >= Decimal("0.08"):  # >= 8% FCF yield
            score += 20
        elif fcf_yield >= Decimal("0.05"):  # 5-8%
            score += 15
        elif fcf_yield >= Decimal("0.03"):  # 3-5%
            score += 10
        elif fcf_yield > Decimal("0.0"):
            score += 5

    # 3. Pullback Discount & 52-Week Range Position (0 to 20 pts)
    if pct_off_52w_high is not None:
        if pct_off_52w_high <= Decimal("-0.35"):  # Down 35%+ from high
            score += 15
        elif pct_off_52w_high <= Decimal("-0.20"):  # Down 20-35%
            score += 10
        elif pct_off_52w_high <= Decimal("-0.10"):  # Down 10-20%
            score += 5

    if range_52w_pct is not None:
        if range_52w_pct <= Decimal("25.0"):  # In bottom quartile of 52W range
            score += 5
        elif range_52w_pct <= Decimal("50.0"):
            score += 3

    # 4. EV/EBITDA or Price-to-Book (0 to 15 pts)
    if ev_ebitda is not None and ev_ebitda > 0:
        if ev_ebitda <= Decimal("9.0"):
            score += 10
        elif ev_ebitda <= Decimal("14.0"):
            score += 6
        elif ev_ebitda <= Decimal("20.0"):
            score += 3
    elif price_to_book is not None and price_to_book > 0:
        if price_to_book <= Decimal("1.5"):
            score += 10
        elif price_to_book <= Decimal("3.0"):
            score += 6
        elif price_to_book <= Decimal("5.0"):
            score += 3

    # 5. Analyst Upside & Dividend (0 to 10 pts)
    if target_upside_pct is not None and target_upside_pct >= Decimal("0.20"):
        score += 5
    elif target_upside_pct is not None and target_upside_pct >= Decimal("0.10"):
        score += 3

    if dividend_yield is not None and dividend_yield >= Decimal("0.035"):
        score += 5
    elif dividend_yield is not None and dividend_yield >= Decimal("0.015"):
        score += 3

    # 6. Quality & Solvency Guards (0 to 10 pts)
    quality_pts = 0
    if operating_margin is not None and operating_margin >= Decimal("0.15"):
        quality_pts += 5
    elif operating_margin is not None and operating_margin >= Decimal("0.05"):
        quality_pts += 3

    if debt_to_equity is not None and debt_to_equity <= Decimal("100.0"):
        quality_pts += 5
    elif debt_to_equity is not None and debt_to_equity <= Decimal("180.0"):
        quality_pts += 3
    score += quality_pts

    # Clamp score to 0..100
    score = max(0, min(100, score))

    # Check for Value Trap Risk:
    # Low P/E but excessive leverage or operating losses
    is_trap = False
    if (
        pe is not None
        and pe <= Decimal("14.0")
        and (
            (debt_to_equity is not None and debt_to_equity >= Decimal("280.0"))
            or (operating_margin is not None and operating_margin < Decimal("0.0"))
        )
    ):
        is_trap = True

    # Narrative creation
    narrative_parts: list[str] = []
    if is_trap:
        badge = "TRAP_RISK"
        score = min(score, 38)
        narrative_parts.append(
            f"Low P/E ({pe:.1f}x) appears cheap, but faces high leverage "
            f"({float(debt_to_equity or 0):.0f}% D/E) or negative operating margins."
        )
    elif score >= 70:
        badge = "BARGAIN"
        if pe is not None:
            narrative_parts.append(f"Attractively valued at {pe:.1f}x P/E")
        if fcf_yield is not None and fcf_yield >= Decimal("0.05"):
            narrative_parts.append(f"strong {float(fcf_yield) * 100:.1f}% FCF yield")
        if pct_off_52w_high is not None and pct_off_52w_high <= Decimal("-0.20"):
            narrative_parts.append(f"trading {abs(float(pct_off_52w_high)) * 100:.0f}% off highs")
    elif pct_off_52w_high is not None and pct_off_52w_high <= Decimal("-0.22") and score >= 55:
        badge = "DIP"
        narrative_parts.append(
            f"Pullback opportunity: down {abs(float(pct_off_52w_high)) * 100:.0f}% from 52W high"
        )
        if target_upside_pct is not None and target_upside_pct > 0:
            narrative_parts.append(f"{float(target_upside_pct) * 100:.0f}% analyst upside target")
    elif score >= 40:
        badge = "FAIR"
        if pe is not None:
            narrative_parts.append(f"Fairly valued at {pe:.1f}x P/E")
        else:
            narrative_parts.append("Trading within balanced valuation parameters")
    else:
        badge = "PREMIUM"
        if pe is not None and pe >= Decimal("30.0"):
            narrative_parts.append(f"Growth premium multiple at {pe:.1f}x P/E")
        else:
            narrative_parts.append("Trading at a premium with limited margin of safety")

    narrative = "; ".join(narrative_parts)
    return score, badge, narrative


def parse_raw_stock_info(
    instrument_id: int,
    symbol: str,
    name: str,
    info: dict[str, Any],
    fast_info: Any | None = None,
    now_iso: str = "",
) -> StockFundamentals:
    """Parse yfinance info dict and fast_info object into a clean StockFundamentals."""
    currency = str(info.get("currency") or "EUR")
    sector = info.get("sector") or None
    industry = info.get("industry") or None

    current_price = _to_decimal(info.get("currentPrice") or info.get("regularMarketPrice"))
    if current_price is None and fast_info is not None:
        with contextlib.suppress(Exception):
            current_price = _to_decimal(getattr(fast_info, "last_price", None))

    market_cap = _to_decimal(info.get("marketCap"))
    if market_cap is None and fast_info is not None:
        with contextlib.suppress(Exception):
            market_cap = _to_decimal(getattr(fast_info, "market_cap", None))

    trailing_pe = _to_decimal(info.get("trailingPE"))
    forward_pe = _to_decimal(info.get("forwardPE"))
    peg_ratio = _to_decimal(info.get("pegRatio"))
    price_to_book = _to_decimal(info.get("priceToBook"))
    price_to_sales = _to_decimal(info.get("priceToSalesTrailing12Months"))
    ev_ebitda = _to_decimal(info.get("enterpriseToEbitda"))

    # Free cash flow yield
    fcf = _to_decimal(info.get("freeCashflow"))
    fcf_yield = None
    if fcf is not None and market_cap is not None and market_cap > 0:
        fcf_yield = _to_decimal(fcf / market_cap)

    # Dividend yield (yfinance returns e.g. 0.024 or 2.4 depending on version)
    raw_div = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
    dividend_yield = None
    if raw_div is not None:
        d_val = float(raw_div)
        if d_val > 1.0:  # e.g. 3.5% returned as 3.5
            d_val = d_val / 100.0
        dividend_yield = _to_decimal(d_val)

    payout_ratio = _to_decimal(info.get("payoutRatio"))
    debt_to_equity = _to_decimal(info.get("debtToEquity"))
    operating_margin = _to_decimal(info.get("operatingMargins"))
    profit_margin = _to_decimal(info.get("profitMargins"))
    return_on_equity = _to_decimal(info.get("returnOnEquity"))

    high_52 = _to_decimal(info.get("fiftyTwoWeekHigh"))
    low_52 = _to_decimal(info.get("fiftyTwoWeekLow"))
    if high_52 is None and fast_info is not None:
        with contextlib.suppress(Exception):
            high_52 = _to_decimal(getattr(fast_info, "year_high", None))
            low_52 = _to_decimal(getattr(fast_info, "year_low", None))

    pct_off_52w_high = None
    if current_price is not None and high_52 is not None and high_52 > 0:
        pct_off_52w_high = _to_decimal((current_price - high_52) / high_52)

    range_52w_pct = None
    if current_price is not None and high_52 is not None and low_52 is not None:
        diff = high_52 - low_52
        if diff > 0:
            range_52w_pct = _to_decimal(((current_price - low_52) / diff) * 100)

    two_hundred_dma = _to_decimal(info.get("twoHundredDayAverage"))
    if two_hundred_dma is None and fast_info is not None:
        with contextlib.suppress(Exception):
            two_hundred_dma = _to_decimal(getattr(fast_info, "two_hundred_day_average", None))

    dist_200_dma_pct = None
    if current_price is not None and two_hundred_dma is not None and two_hundred_dma > 0:
        dist_200_dma_pct = _to_decimal((current_price - two_hundred_dma) / two_hundred_dma)

    target_mean = _to_decimal(info.get("targetMeanPrice"))
    target_upside = None
    if target_mean is not None and current_price is not None and current_price > 0:
        target_upside = _to_decimal((target_mean - current_price) / current_price)

    recommendation = info.get("recommendationKey") or None

    score, badge, narrative = calculate_value_score(
        forward_pe=forward_pe,
        trailing_pe=trailing_pe,
        fcf_yield=fcf_yield,
        pct_off_52w_high=pct_off_52w_high,
        range_52w_pct=range_52w_pct,
        ev_ebitda=ev_ebitda,
        price_to_book=price_to_book,
        target_upside_pct=target_upside,
        dividend_yield=dividend_yield,
        debt_to_equity=debt_to_equity,
        operating_margin=operating_margin,
    )

    return StockFundamentals(
        instrument_id=instrument_id,
        symbol=symbol,
        name=name,
        sector=sector,
        industry=industry,
        current_price=current_price,
        market_cap=market_cap,
        currency=currency,
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
        fifty_two_week_high=high_52,
        fifty_two_week_low=low_52,
        pct_off_52w_high=pct_off_52w_high,
        range_52w_pct=range_52w_pct,
        two_hundred_dma=two_hundred_dma,
        dist_200_dma_pct=dist_200_dma_pct,
        target_mean_price=target_mean,
        target_upside_pct=target_upside,
        recommendation_key=recommendation,
        value_score=score,
        value_badge=badge,
        value_narrative=narrative,
        fetched_at=now_iso,
    )


def save_stock_fundamentals(conn: sqlite3.Connection, items: list[StockFundamentals]) -> None:
    """Upsert stock fundamentals into stock_fundamental table."""
    conn.executemany(
        """
        INSERT INTO stock_fundamental (
            instrument_id, sector, industry, current_price, market_cap,
            currency, trailing_pe, forward_pe, peg_ratio, price_to_book,
            price_to_sales, ev_ebitda, fcf_yield, dividend_yield, payout_ratio,
            debt_to_equity, operating_margin, profit_margin, return_on_equity,
            fifty_two_week_high, fifty_two_week_low, pct_off_52w_high,
            range_52w_pct, two_hundred_dma, dist_200_dma_pct,
            target_mean_price, target_upside_pct, recommendation_key,
            value_score, value_badge, value_narrative, fetched_at
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?
        )
        ON CONFLICT(instrument_id) DO UPDATE SET
            sector = excluded.sector,
            industry = excluded.industry,
            current_price = excluded.current_price,
            market_cap = excluded.market_cap,
            currency = excluded.currency,
            trailing_pe = excluded.trailing_pe,
            forward_pe = excluded.forward_pe,
            peg_ratio = excluded.peg_ratio,
            price_to_book = excluded.price_to_book,
            price_to_sales = excluded.price_to_sales,
            ev_ebitda = excluded.ev_ebitda,
            fcf_yield = excluded.fcf_yield,
            dividend_yield = excluded.dividend_yield,
            payout_ratio = excluded.payout_ratio,
            debt_to_equity = excluded.debt_to_equity,
            operating_margin = excluded.operating_margin,
            profit_margin = excluded.profit_margin,
            return_on_equity = excluded.return_on_equity,
            fifty_two_week_high = excluded.fifty_two_week_high,
            fifty_two_week_low = excluded.fifty_two_week_low,
            pct_off_52w_high = excluded.pct_off_52w_high,
            range_52w_pct = excluded.range_52w_pct,
            two_hundred_dma = excluded.two_hundred_dma,
            dist_200_dma_pct = excluded.dist_200_dma_pct,
            target_mean_price = excluded.target_mean_price,
            target_upside_pct = excluded.target_upside_pct,
            recommendation_key = excluded.recommendation_key,
            value_score = excluded.value_score,
            value_badge = excluded.value_badge,
            value_narrative = excluded.value_narrative,
            fetched_at = excluded.fetched_at
        """,
        [
            (
                item.instrument_id,
                item.sector,
                item.industry,
                str(item.current_price) if item.current_price is not None else None,
                str(item.market_cap) if item.market_cap is not None else None,
                item.currency,
                str(item.trailing_pe) if item.trailing_pe is not None else None,
                str(item.forward_pe) if item.forward_pe is not None else None,
                str(item.peg_ratio) if item.peg_ratio is not None else None,
                str(item.price_to_book) if item.price_to_book is not None else None,
                str(item.price_to_sales) if item.price_to_sales is not None else None,
                str(item.ev_ebitda) if item.ev_ebitda is not None else None,
                str(item.fcf_yield) if item.fcf_yield is not None else None,
                str(item.dividend_yield) if item.dividend_yield is not None else None,
                str(item.payout_ratio) if item.payout_ratio is not None else None,
                str(item.debt_to_equity) if item.debt_to_equity is not None else None,
                str(item.operating_margin) if item.operating_margin is not None else None,
                str(item.profit_margin) if item.profit_margin is not None else None,
                str(item.return_on_equity) if item.return_on_equity is not None else None,
                str(item.fifty_two_week_high) if item.fifty_two_week_high is not None else None,
                str(item.fifty_two_week_low) if item.fifty_two_week_low is not None else None,
                str(item.pct_off_52w_high) if item.pct_off_52w_high is not None else None,
                str(item.range_52w_pct) if item.range_52w_pct is not None else None,
                str(item.two_hundred_dma) if item.two_hundred_dma is not None else None,
                str(item.dist_200_dma_pct) if item.dist_200_dma_pct is not None else None,
                str(item.target_mean_price) if item.target_mean_price is not None else None,
                str(item.target_upside_pct) if item.target_upside_pct is not None else None,
                item.recommendation_key,
                item.value_score,
                item.value_badge,
                item.value_narrative,
                item.fetched_at,
            )
            for item in items
        ],
    )
    # Also keep instrument.sector synchronized if sector was discovered
    for item in items:
        if item.sector:
            conn.execute(
                "UPDATE instrument SET sector = ? WHERE id = ? AND (sector IS NULL OR sector = '')",
                (item.sector, item.instrument_id),
            )
    conn.commit()


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
        ORDER BY sf.value_score DESC, sf.forward_pe ASC
    """
    rows = conn.execute(sql, params).fetchall()
    out: list[StockFundamentals] = []
    for r in rows:
        fwd_pe = _to_decimal(r["forward_pe"])
        if max_forward_pe is not None and (fwd_pe is None or fwd_pe > max_forward_pe):
            continue
        fcf = _to_decimal(r["fcf_yield"])
        if min_fcf_yield is not None and (fcf is None or fcf < min_fcf_yield):
            continue

        out.append(
            StockFundamentals(
                instrument_id=int(r["instrument_id"]),
                symbol=str(r["symbol"]),
                name=str(r["instrument_name"]),
                sector=r["sector"],
                industry=r["industry"],
                current_price=_to_decimal(r["current_price"]),
                market_cap=_to_decimal(r["market_cap"]),
                currency=str(r["currency"] or "EUR"),
                trailing_pe=_to_decimal(r["trailing_pe"]),
                forward_pe=fwd_pe,
                peg_ratio=_to_decimal(r["peg_ratio"]),
                price_to_book=_to_decimal(r["price_to_book"]),
                price_to_sales=_to_decimal(r["price_to_sales"]),
                ev_ebitda=_to_decimal(r["ev_ebitda"]),
                fcf_yield=fcf,
                dividend_yield=_to_decimal(r["dividend_yield"]),
                payout_ratio=_to_decimal(r["payout_ratio"]),
                debt_to_equity=_to_decimal(r["debt_to_equity"]),
                operating_margin=_to_decimal(r["operating_margin"]),
                profit_margin=_to_decimal(r["profit_margin"]),
                return_on_equity=_to_decimal(r["return_on_equity"]),
                fifty_two_week_high=_to_decimal(r["fifty_two_week_high"]),
                fifty_two_week_low=_to_decimal(r["fifty_two_week_low"]),
                pct_off_52w_high=_to_decimal(r["pct_off_52w_high"]),
                range_52w_pct=_to_decimal(r["range_52w_pct"]),
                two_hundred_dma=_to_decimal(r["two_hundred_dma"]),
                dist_200_dma_pct=_to_decimal(r["dist_200_dma_pct"]),
                target_mean_price=_to_decimal(r["target_mean_price"]),
                target_upside_pct=_to_decimal(r["target_upside_pct"]),
                recommendation_key=r["recommendation_key"],
                value_score=int(r["value_score"]) if r["value_score"] is not None else 50,
                value_badge=str(r["value_badge"] or "FAIR"),
                value_narrative=str(r["value_narrative"] or ""),
                fetched_at=str(r["fetched_at"] or ""),
            )
        )
    return out
