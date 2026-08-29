-- =====================================================================
-- 0020 -- numerical risk level representation (1 to 7 SRI standard).
--
-- Expands the stated taxonomy with a standardized integer scale (1-7),
-- aligning with the European UCITS / PRIIPs Summary Risk Indicator (SRI):
--
--   1: Ultra Low  (Cash, Overnight Money Market)
--   2: Very Low   (Short-duration Sovereign & IG Bonds)
--   3: Low-Mid    (Broad Aggregate & Corporate Bonds)
--   4: Moderate   (Core Global & Large-Cap Developed Equities)
--   5: Mid-High   (Emerging Markets, Commodities/Gold, Small Cap)
--   6: High Risk  (Thematic & Sector Equities, Single Company Stocks)
--   7: Speculative (Cryptocurrency, Leveraged & High-Beta Assets)
-- =====================================================================

ALTER TABLE instrument ADD COLUMN risk_level INTEGER CHECK (risk_level IS NULL OR (risk_level >= 1 AND risk_level <= 7));

CREATE INDEX idx_instrument_risk_level ON instrument (risk_level);

-- Backfill existing instruments based on their category and asset class
UPDATE instrument SET risk_bucket = 'CRYPTO', risk_level = 7 WHERE asset_class = 'CRYPTO' OR import_key = 'BTC/EUR';
UPDATE instrument SET risk_bucket = 'CASH', risk_level = 1 WHERE (asset_class = 'CASH' OR is_cash_proxy = 1) AND currency = 'EUR';
UPDATE instrument SET risk_bucket = 'CASH', risk_level = 2 WHERE (asset_class = 'CASH' OR is_cash_proxy = 1) AND currency != 'EUR';
UPDATE instrument SET risk_level = 1 WHERE risk_bucket = 'MONEY_MARKET';
UPDATE instrument SET risk_level = 3 WHERE risk_bucket = 'BOND' AND risk_level IS NULL;
UPDATE instrument SET risk_level = 4 WHERE risk_bucket IN ('EQ_GLOBAL_BROAD', 'EQ_US_LARGE', 'EQ_EUROPE') AND risk_level IS NULL;
UPDATE instrument SET risk_level = 5 WHERE risk_bucket IN ('EQ_EM', 'COMMODITY') AND risk_level IS NULL;
UPDATE instrument SET risk_level = 6 WHERE risk_bucket IN ('EQ_SECTOR', 'EQ_SINGLE_STOCK') AND risk_level IS NULL;
UPDATE instrument SET risk_level = 7 WHERE risk_bucket = 'CRYPTO' AND risk_level IS NULL;
UPDATE instrument SET risk_level = 4 WHERE risk_level IS NULL;
