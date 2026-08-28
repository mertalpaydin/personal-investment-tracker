-- =====================================================================
-- 0018 -- stop the savings split calling invested cash "market growth".
--
-- `savings_period` derived market growth as a residual: closing value
-- minus opening value minus what moved in or out. That is only sound if
-- the value series is complete, and `total_value_eur` is not -- migration
-- 0005 removed uninvested brokerage cash from it on purpose, because the
-- tracker cannot vouch for money it has not seen spent.
--
-- So a purchase raised `total_value_eur` and lowered cash the series does
-- not carry, and the residual booked the difference as performance. On
-- this ledger EUR 110,921 of opening balance was spent into holdings over
-- two years and every euro of it was reported as market growth: EUR
-- 137,022 claimed against EUR 26,562 actually earned, on a portfolio
-- whose own time-weighted return said +20%. The two figures sat on the
-- same page contradicting each other.
--
-- Unit accounting never had this problem. `returns.py` runs on
-- `valuation_basis_eur`, which includes that cash precisely so a sale
-- does not read as a loss and a purchase does not read as a gain. The
-- savings split simply used the other column.
--
-- `closing_basis_eur` is that complete series, and the identity now
-- closes on it. `closing_value_eur` keeps its meaning -- the headline
-- figure the owner is shown -- so nothing that reads it changes.
--
-- `opening_value_eur` is what the accounts were already worth on the
-- first day there is any record at all. Neither saving nor return: it was
-- not paid in during the tracked period, and nothing here earned it.
-- Without it the very first day's value has nowhere to come from and
-- becomes performance too.
-- =====================================================================

ALTER TABLE savings_period
    ADD COLUMN closing_basis_eur DECIMAL_TEXT NOT NULL DEFAULT '0';

ALTER TABLE savings_period
    ADD COLUMN opening_value_eur DECIMAL_TEXT NOT NULL DEFAULT '0';
