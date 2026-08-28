-- =====================================================================
-- 0017 -- money you already had, arriving for the first time.
--
-- A savings account joins this tool part-way through its life. Its
-- statement opens with the whole balance -- EUR 20,186.08 on the first
-- line of a Tagesgeld export, TRY 176,177 as a term deposit's principal
-- -- and the importer books that as a deposit, because that is what the
-- statement calls it.
--
-- It is a deposit. It is not saving. That money was set aside over
-- years; only its arrival in *this ledger* happened in one month. Left
-- alone it made a single April worth EUR 22,686 against a normal month
-- of about EUR 1,500, dragged the twelve-month average to more than
-- twice the typical month, and the projection then assumed that pace
-- continued for fifteen years.
--
-- Why a flag and not a reclassification. The money genuinely did enter
-- the tracked portfolio on that day, so unit accounting must still see
-- an external flow: without one the value appears from nowhere and is
-- read as investment return -- the same disease that once turned a 49%
-- return into a 13,000% one. So the row stays EXTERNAL_IN, the balance
-- and the interest accrual stay correct, and only the savings figures
-- set it aside. Two different questions, two different answers, one row.
--
-- `savings_period` carries it as its own column rather than folding it
-- into market growth, because growth there is a residual: anything not
-- subtracted becomes performance the portfolio never earned.
-- =====================================================================

ALTER TABLE transaction_
    ADD COLUMN is_opening_transfer INTEGER NOT NULL DEFAULT 0
        CHECK (is_opening_transfer IN (0, 1));

ALTER TABLE savings_period
    ADD COLUMN opening_transfer_eur DECIMAL_TEXT NOT NULL DEFAULT '0';

-- Only ever a handful of rows, but every savings query filters on it.
CREATE INDEX idx_transaction_opening_transfer
    ON transaction_ (is_opening_transfer)
    WHERE is_opening_transfer = 1;
