-- =====================================================================
-- 0022 -- analyst coverage, growth and data-quality columns for stocks.
--
-- Adds the analyst rating breakdown (how many say buy), the price target
-- range, earnings and revenue growth, the reporting currency of the
-- financial statements, and how many scoring inputs were actually present.
-- =====================================================================

ALTER TABLE stock_fundamental ADD COLUMN financial_currency TEXT;
ALTER TABLE stock_fundamental ADD COLUMN earnings_growth DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN revenue_growth DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN target_median_price DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN target_low_price DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN target_high_price DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN recommendation_mean DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN analyst_count INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN strong_buy INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN buy INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN hold INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN sell INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN strong_sell INTEGER;
ALTER TABLE stock_fundamental ADD COLUMN buy_pct DECIMAL_TEXT;
ALTER TABLE stock_fundamental ADD COLUMN data_points INTEGER;
