-- =====================================================================
-- 0005 -- separate the headline total from the valuation basis.
--
-- The headline total is what the owner considers he has: holdings plus
-- deposit balances. Uninvested cash sitting in the brokerage account --
-- sale proceeds and distributions -- is deliberately excluded, because
-- the tracker cannot know whether that money is still there or was spent.
--
-- Returns still need a COMPLETE value series. If proceeds vanish on the
-- day of a sale and reappear when they are spent, the round trip reads
-- as a loss followed by an identical gain, and a distribution's ex-date
-- price fall reads as a loss with nothing offsetting it. So unit
-- accounting runs on valuation_basis_eur, which includes that cash, while
-- total_value_eur is the figure shown.
--
-- Two numbers for two questions, each labelled.
-- =====================================================================

-- Deposit-account balances: money in an account whose balance is known.
ALTER TABLE portfolio_daily ADD COLUMN deposits_eur DECIMAL_TEXT NOT NULL DEFAULT '0';

-- total_value_eur + uninvested brokerage cash. Used for return maths only.
ALTER TABLE portfolio_daily ADD COLUMN valuation_basis_eur DECIMAL_TEXT NOT NULL DEFAULT '0';
