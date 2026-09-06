-- =====================================================================
-- 0023 -- quality (steady grower) score for stocks.
--
-- Adds the composite Quality score with its narrative, the multi-year
-- revenue and income record read from the annual income statement, and
-- the price-history steadiness figures (volatility, worst fall) that the
-- score uses, copied here so the table is self-contained.
-- =====================================================================

ALTER TABLE stock_fundamental ADD COLUMN quality_score INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN quality_narrative TEXT;
ALTER TABLE stock_fundamental ADD COLUMN revenue_cagr_3y DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN revenue_up_years INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN income_up_years INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN years_reported INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN vol_1y DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN max_dd_3y DECIMAL_TEXT;
