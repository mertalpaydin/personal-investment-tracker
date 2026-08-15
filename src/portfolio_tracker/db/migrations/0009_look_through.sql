-- =====================================================================
-- 0009 -- ETF look-through: what a fund actually holds.
--
-- v1 reports currency exposure on a QUOTE basis. A euro-quoted S&P 500
-- tracker therefore shows zero currency contribution while carrying full
-- dollar risk. That is mechanically correct and economically misleading,
-- and it is the largest honest gap the tool has. Only look-through fixes
-- it, and look-through needs the constituents.
--
-- Two tables rather than one, because a holdings file has facts of its
-- own that are not properties of any single line:
--
--   fund_holding_set   one row per (fund, publication date). Where the
--                      file came from, what its weights summed to before
--                      anything was normalised, and a hash of the bytes.
--   fund_holding       the lines. Weights are FRACTIONS of fund NAV.
--
-- Storing the raw sum is the point of the header table. Published files
-- rarely sum to exactly 1: rounding, a cash line, a futures offset. The
-- aggregation normalises by this figure, so the scaling is a stated
-- operation on a recorded number rather than an invisible fudge. If a
-- file is loaded that sums to 0.62, that fact survives in the database
-- and `pt doctor` says so.
--
-- Weights are stored as fractions, converted exactly ONCE at parse time,
-- with the unit the file used recorded here. This repository has already
-- shipped a bug where a rate was divided by 100 in the parser and again
-- in the writer (41.5% -> 0.00415), and the result was plausible enough
-- to survive review. `unit` is what makes the conversion auditable.
--
-- These are FACTS, not derivations: they come from a published file and
-- cannot be recomputed from anything else in the database. They are
-- deliberately NOT in `DERIVED_TABLES`, so `--reset-derived` leaves them
-- alone.
-- =====================================================================

CREATE TABLE fund_holding_set (
    instrument_id INTEGER NOT NULL REFERENCES instrument (id),

    -- The date the FUND published this composition, not the date it was
    -- imported. Exposure on a valuation date uses the newest set at or
    -- before that date, so a file dated in the future never back-dates
    -- itself onto history that predates it.
    as_of TEXT NOT NULL,

    -- Free text, and required. "iShares holdings CSV, downloaded
    -- 2026-08-14" is the difference between a number you can defend and
    -- a number you found.
    source TEXT NOT NULL,

    -- The published weights, summed, as fractions. NOT normalised.
    weight_sum DECIMAL_TEXT NOT NULL,

    -- What the file's own weight column was in. Recorded so that a
    -- percent-vs-fraction misread is visible after the fact.
    unit TEXT NOT NULL CHECK (unit IN ('percent', 'fraction')),

    holdings      INTEGER NOT NULL CHECK (holdings > 0),
    file_sha256   TEXT NOT NULL,
    imported_at   TEXT NOT NULL,

    PRIMARY KEY (instrument_id, as_of)
);

CREATE TABLE fund_holding (
    id INTEGER PRIMARY KEY,

    instrument_id INTEGER NOT NULL,
    as_of         TEXT NOT NULL,

    -- Position in the source file. Two lines of a holdings file can be
    -- genuinely identical (two tranches of one bond, an unnamed cash
    -- line), so the file's own ordering is the only honest identity.
    line_no INTEGER NOT NULL,

    name       TEXT NOT NULL,

    -- ISIN, ticker, whatever the file supplied. NULL is normal and not a
    -- defect: cash and derivative lines rarely carry one. Concentration
    -- falls back to the name when it is absent, and says which it used.
    identifier TEXT,

    -- Fraction of fund NAV. Not constrained non-negative: a futures
    -- offset or a short line is legitimately negative in a published
    -- file, and refusing to store it would mean refusing real data. It
    -- is counted and reported instead.
    weight DECIMAL_TEXT NOT NULL,

    -- The constituent's own attributes, exactly as the file gave them,
    -- trimmed and upper-cased for currency only. NULL means the file did
    -- not say; it aggregates into an explicit UNKNOWN bucket and is never
    -- filled in by inference.
    currency    TEXT,
    country     TEXT,
    sector      TEXT,
    asset_class TEXT,

    FOREIGN KEY (instrument_id, as_of)
        REFERENCES fund_holding_set (instrument_id, as_of) ON DELETE CASCADE
);

-- Re-importing the same file must not double the fund. The set row is
-- replaced wholesale and the cascade takes the lines with it.
CREATE UNIQUE INDEX ux_fund_holding ON fund_holding (instrument_id, as_of, line_no);
CREATE INDEX idx_fund_holding_lookup ON fund_holding (instrument_id, as_of);

-- Concentration groups across every fund, so this is the access path
-- that matters once more than one holdings file exists.
CREATE INDEX idx_fund_holding_identifier ON fund_holding (identifier);
