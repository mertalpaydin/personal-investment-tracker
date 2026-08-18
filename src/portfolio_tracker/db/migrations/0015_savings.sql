-- =====================================================================
-- 0015 -- telling new money apart from money moved around.
--
-- `flow_class` already says what kind of movement a row is. What it cannot
-- say is whether an EXTERNAL_IN is genuinely new savings or the other half
-- of an EXTERNAL_OUT from an account in the same household -- the same
-- money, counted twice, once as a withdrawal and once as a contribution.
--
-- Getting this wrong is not cosmetic. Unit accounting issues units on
-- external flows, so a transfer counted as new money issues units it should
-- not, and the return earned on that capital is attributed to a contribution
-- instead of to performance. The figure moves in the flattering direction on
-- both counts: it understates the return and overstates the saving.
--
-- The rule that matters most is not in this schema because it is already
-- true: reinvested income is INCOME, never EXTERNAL_IN. A dividend that
-- buys more shares is a return being compounded, not a contribution.
-- =====================================================================

-- ---- Provenance on the classification -----------------------------------

ALTER TABLE transaction_ ADD COLUMN is_new_savings INTEGER NOT NULL DEFAULT 0
    CHECK (is_new_savings IN (0, 1));

-- Free text the owner chooses: 'salary', 'bonus', 'gift', 'tax refund'.
ALTER TABLE transaction_ ADD COLUMN savings_label TEXT;

-- Set when a person overrode the classifier. A re-import must not undo a
-- decision somebody made, so the classifier skips any row carrying this.
ALTER TABLE transaction_ ADD COLUMN flow_class_source TEXT NOT NULL DEFAULT 'AUTO'
    CHECK (flow_class_source IN ('AUTO', 'USER'));

CREATE INDEX idx_txn_savings ON transaction_ (trade_date, is_new_savings);

-- ---- Two halves of one movement -----------------------------------------

CREATE TABLE internal_transfer_link (
    id          INTEGER PRIMARY KEY,
    out_txn_id  INTEGER NOT NULL REFERENCES transaction_ (id) ON DELETE CASCADE,
    in_txn_id   INTEGER NOT NULL REFERENCES transaction_ (id) ON DELETE CASCADE,
    amount_eur  DECIMAL_TEXT NOT NULL,
    -- AUTO: matched on amount and date. USER: somebody said so. A USER link
    -- is never undone by re-running the matcher, for the same reason a USER
    -- flow_class is not.
    matched_by  TEXT NOT NULL CHECK (matched_by IN ('AUTO', 'USER')),
    detected_at TEXT NOT NULL,
    UNIQUE (out_txn_id, in_txn_id)
);

CREATE INDEX idx_transfer_out ON internal_transfer_link (out_txn_id);
CREATE INDEX idx_transfer_in ON internal_transfer_link (in_txn_id);

-- ---- Monthly savings, rebuilt nightly -----------------------------------
--
-- Derived, and listed in DERIVED_TABLES: every figure here is a pure
-- function of the ledger and is thrown away and recomputed by a rebuild.

CREATE TABLE savings_period (
    person_id           INTEGER NOT NULL REFERENCES person (id),
    period              TEXT NOT NULL,              -- 'YYYY-MM'
    gross_in_eur        DECIMAL_TEXT NOT NULL DEFAULT '0',
    gross_out_eur       DECIMAL_TEXT NOT NULL DEFAULT '0',
    net_new_money_eur   DECIMAL_TEXT NOT NULL DEFAULT '0',
    cumulative_net_eur  DECIMAL_TEXT NOT NULL DEFAULT '0',
    -- Dividends and interest received. Emphatically NOT savings: counting
    -- them would take a return and call it discipline.
    income_received_eur DECIMAL_TEXT NOT NULL DEFAULT '0',
    -- What the market did, as the residual: closing minus opening minus
    -- everything that was put in or taken out.
    market_growth_eur   DECIMAL_TEXT NOT NULL DEFAULT '0',
    closing_value_eur   DECIMAL_TEXT NOT NULL DEFAULT '0',
    -- Optional, and only ever supplied by the owner. With no denominator
    -- there is no savings rate, and inventing one is worse than omitting it.
    net_income_eur      DECIMAL_TEXT,
    savings_rate        DECIMAL_TEXT,
    PRIMARY KEY (person_id, period)
);
