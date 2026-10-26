-- =====================================================================
-- 0027 -- record the ticker symbol Onvista resolved an instrument to.
--
-- Catches silent mismatches where an instrument's ISIN maps to a different
-- fund or share class on Onvista than its price_symbol in the database.
-- =====================================================================

ALTER TABLE onvista_notation ADD COLUMN resolved_symbol TEXT;
