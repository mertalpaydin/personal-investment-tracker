-- =====================================================================
-- 0007 -- split the idle cash balance by where the money came from.
--
-- "Uninvested cash: EUR 2,548.77" answers the wrong question. The owner
-- does not want to know the balance; he wants to know whether that is
-- capital waiting to be reinvested or income he may already have spent.
-- Those two have different meanings and different fates, and one number
-- covering both is the same mistake as folding cash into the total.
--
-- Two pots, tracked as the ledger is replayed:
--
--   CAPITAL  contributions and sale proceeds -- money that entered to be
--            invested and has not been yet.
--   INCOME   distributions and credited interest -- return that has been
--            realised in cash and not reinvested.
--
-- Drawdown order is CAPITAL FIRST, income last. That is a stated
-- convention, not a discovered fact: a bank account is fungible and no
-- record says which euro funded which purchase. It is the convention that
-- matches intent -- money deposited in order to buy something is spent on
-- it -- and it is deliberately the conservative one, because it leaves
-- income showing as unspent rather than quietly consuming it and letting
-- the owner conclude the dividends went back into the market.
--
-- Neither pot enters total_value_eur. This changes what is *reported*
-- about the cash, not what is counted.
-- =====================================================================

-- Per account and currency. balance_local stays the total, so nothing that
-- reads it needs to change; income is the part of it that is return.
ALTER TABLE cash_daily ADD COLUMN income_local DECIMAL_TEXT NOT NULL DEFAULT '0';
ALTER TABLE cash_daily ADD COLUMN income_eur   DECIMAL_TEXT;

-- The same split at portfolio level. capital = cash_eur - income_cash_eur.
ALTER TABLE portfolio_daily ADD COLUMN income_cash_eur DECIMAL_TEXT NOT NULL DEFAULT '0';

-- Cumulative income *received*, whether or not it is still sitting in cash.
-- Distinct from income_cash_eur: reinvested dividends leave the cash balance
-- but remain part of what the portfolio has earned, and a tool that reports
-- only the balance makes an income stream disappear the moment it is used.
ALTER TABLE portfolio_daily ADD COLUMN income_received_eur DECIMAL_TEXT NOT NULL DEFAULT '0';
