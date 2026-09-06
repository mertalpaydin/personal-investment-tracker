-- =====================================================================
-- 0021 -- stock fundamental valuation indicators and metrics.
--
-- Stores point-in-time valuation multiples, financial health ratios,
-- pullback discounts, and composite value scores for single stocks.
-- =====================================================================

CREATE TABLE IF NOT EXISTS stock_fundamental (
    instrument_id INTEGER PRIMARY KEY REFERENCES instrument(id) ON DELETE CASCADE,
    sector TEXT,
    industry TEXT,
    current_price DECIMAL_TEXT,
    market_cap DECIMAL_TEXT,
    currency TEXT,
    trailing_pe DECIMAL_TEXT,
    forward_pe DECIMAL_TEXT,
    peg_ratio DECIMAL_TEXT,
    price_to_book DECIMAL_TEXT,
    price_to_sales DECIMAL_TEXT,
    ev_ebitda DECIMAL_TEXT,
    fcf_yield DECIMAL_TEXT,
    dividend_yield DECIMAL_TEXT,
    payout_ratio DECIMAL_TEXT,
    debt_to_equity DECIMAL_TEXT,
    operating_margin DECIMAL_TEXT,
    profit_margin DECIMAL_TEXT,
    return_on_equity DECIMAL_TEXT,
    fifty_two_week_high DECIMAL_TEXT,
    fifty_two_week_low DECIMAL_TEXT,
    pct_off_52w_high DECIMAL_TEXT,
    range_52w_pct DECIMAL_TEXT,
    two_hundred_dma DECIMAL_TEXT,
    dist_200_dma_pct DECIMAL_TEXT,
    target_mean_price DECIMAL_TEXT,
    target_upside_pct DECIMAL_TEXT,
    recommendation_key TEXT,
    value_score INTEGER,
    value_badge TEXT,
    value_narrative TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stock_fundamental_sector ON stock_fundamental (sector);
CREATE INDEX IF NOT EXISTS idx_stock_fundamental_score ON stock_fundamental (value_score);
