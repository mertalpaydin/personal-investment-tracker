-- =====================================================================
-- 0016 -- what was already there.
--
-- Every figure in this tool is built from a ledger that starts on the day
-- the first export happens to cover. The accounts did not. Money sitting
-- in a depot before that date funded purchases the ledger cannot explain,
-- and with no way to say so the tool reports them as unaccounted for ever
-- -- there is no earlier data to go and fetch.
--
-- An opening balance is neither savings nor return. It is not a
-- contribution, because it was not paid in during the tracked period; it
-- is not performance, because nothing earned it here. It is the starting
-- point, and it has to be stated rather than inferred: only the owner
-- knows what was in the account, and guessing is what produced 85% of a
-- savings figure that had to be thrown away.
--
-- Per (account, currency) rather than per account, because one depot
-- holds euro and lira side by side and `core/cash.py` tracks a balance
-- for each.
-- =====================================================================

CREATE TABLE opening_balance (
    account_id   INTEGER NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    currency     TEXT NOT NULL,
    -- The day the balance applies from. Anything earlier is outside what
    -- this tool claims to know about.
    on_date      TEXT NOT NULL,
    amount_local DECIMAL_TEXT NOT NULL,
    -- Free text: "from the 2024 year-end statement", "estimated". The
    -- figure is a statement by a person and deserves to carry its source.
    source       TEXT,
    set_at       TEXT NOT NULL,
    PRIMARY KEY (account_id, currency)
);
