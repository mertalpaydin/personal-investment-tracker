-- =====================================================================
-- 0004 -- carry cash in the portfolio series.
--
-- Idle sale proceeds are a held position (see 0003), so the portfolio's
-- total value has to include them. Without this the value dips on a sale
-- and recovers when the money is spent, and unit accounting reads that
-- round trip as a loss followed by an identical gain.
--
-- ALTER TABLE ADD COLUMN only, so no table rebuild is needed here.
-- =====================================================================

ALTER TABLE portfolio_daily ADD COLUMN cash_eur DECIMAL_TEXT NOT NULL DEFAULT '0';

-- Realised gains to date, so the value series can be reconciled against
-- contributions plus growth without recomputing FIFO.
ALTER TABLE portfolio_daily ADD COLUMN realized_gain_eur DECIMAL_TEXT NOT NULL DEFAULT '0';
