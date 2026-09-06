-- =====================================================================
-- 0024 -- dated score snapshots, so the score formulas can be judged later.
--
-- One row per stock per calculation day: both composite scores, every
-- block's points (the decomposition), the raw inputs as JSON, the close
-- price that was on file that day, and the formula version. These are
-- facts (what the formula said on that day), never rebuilt and never
-- truncated by `reset_derived`. Forward returns are computed later from
-- the `price` table, so nothing here looks ahead.
-- =====================================================================

CREATE TABLE IF NOT EXISTS stock_score_snapshot (
    instrument_id           INTEGER NOT NULL REFERENCES instrument(id),
    calc_date               TEXT    NOT NULL,
    formula_version         TEXT    NOT NULL,
    source                  TEXT    NOT NULL CHECK (source IN ('SYNC', 'RESCORE', 'DAILY')),
    fundamentals_fetched_at TEXT,
    sector                  TEXT,
    data_points             INTEGER,
    -- last stored close on or before calc_date, never Yahoo's live quote
    price_date              TEXT,
    price_close             DECIMAL_TEXT,
    price_tr_close          DECIMAL_TEXT,
    price_currency          TEXT,
    -- value score and its blocks
    value_score             INTEGER,
    value_badge             TEXT    NOT NULL,
    value_raw_total         INTEGER,
    v_pe                    INTEGER,
    v_fcf_yield             INTEGER,
    v_pullback              INTEGER,
    v_ev_or_pb              INTEGER,
    v_analyst               INTEGER,
    v_dividend              INTEGER,
    v_solvency              INTEGER,
    v_quality_capped        INTEGER NOT NULL DEFAULT 0,
    v_trap_capped           INTEGER NOT NULL DEFAULT 0,
    -- quality score and its blocks
    quality_score           INTEGER,
    q_growth                INTEGER,
    q_profitability         INTEGER,
    q_balance               INTEGER,
    q_steadiness            INTEGER,
    inputs_json             TEXT    NOT NULL,
    created_at              TEXT    NOT NULL,
    PRIMARY KEY (instrument_id, calc_date)
);

CREATE INDEX IF NOT EXISTS idx_score_snapshot_date
    ON stock_score_snapshot (calc_date);
CREATE INDEX IF NOT EXISTS idx_score_snapshot_version
    ON stock_score_snapshot (formula_version, calc_date);
