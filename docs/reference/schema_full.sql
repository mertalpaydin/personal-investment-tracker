-- =====================================================================
-- ASPIRATIONAL SCHEMA -- NOT EXECUTED.
--
-- This is the full ~45-table design from the original brief, kept for
-- reference. It is NOT the schema the application applies, and it cannot be
-- applied as-is: it has no IF NOT EXISTS, no version table, and its addenda
-- use ALTER TABLE, so it only works once against an empty file.
--
-- The executed schema is a versioned migration under
-- src/portfolio_tracker/db/migrations/, covering the 16 tables v1 actually
-- uses. See docs/adr/0003 for what was cut and why.
-- =====================================================================

-- ============================================================
-- Portfolio Tracker + Peer Advisor
-- SQLite schema. Authoritative. Apply with: sqlite3 data/portfolio.db < schema.sql
-- All monetary amounts in EUR unless the column name says otherwise.
-- All dates ISO-8601 TEXT ('YYYY-MM-DD'); timestamps 'YYYY-MM-DDTHH:MM:SSZ'.
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ------------------------------------------------------------
-- 1. People and accounts
-- ------------------------------------------------------------

CREATE TABLE person (
  id                 INTEGER PRIMARY KEY,
  name               TEXT NOT NULL,
  church_tax_rate    REAL NOT NULL DEFAULT 0.0,   -- 0.0 | 0.08 | 0.09
  joint_assessment   INTEGER NOT NULL DEFAULT 0   -- 1 = married + jointly assessed
);

CREATE TABLE account (
  id                        INTEGER PRIMARY KEY,
  person_id                 INTEGER NOT NULL REFERENCES person(id),
  name                      TEXT NOT NULL,
  broker                    TEXT,
  kind                      TEXT NOT NULL CHECK(kind IN ('DEPOT','CASH','TAGESGELD','FESTGELD')),
  currency                  TEXT NOT NULL DEFAULT 'EUR',
  freistellungsauftrag_eur  REAL NOT NULL DEFAULT 0,  -- amount filed at THIS bank
  opened_date               TEXT,
  active                    INTEGER NOT NULL DEFAULT 1
);

-- ------------------------------------------------------------
-- 2. Instrument universe (owned AND not-owned share this table)
-- ------------------------------------------------------------

CREATE TABLE instrument (
  id                    INTEGER PRIMARY KEY,
  isin                  TEXT UNIQUE NOT NULL,
  wkn                   TEXT,
  symbol                TEXT,             -- yfinance ticker, e.g. 'EUNL.DE', 'SAP.DE'
  name                  TEXT NOT NULL,
  asset_class           TEXT NOT NULL CHECK(asset_class IN ('ETF','STOCK','CASH','BOND','OTHER')),
  currency              TEXT NOT NULL DEFAULT 'EUR',   -- quote currency on XETRA
  economic_currency     TEXT,             -- underlying exposure, e.g. USD for a EUR-quoted US stock
  exchange              TEXT NOT NULL DEFAULT 'XETRA',
  xetra_tradeable       INTEGER NOT NULL DEFAULT 1,

  -- fund-only metadata; MANUAL entry from KID/factsheet, no reliable free API
  ter_bps               INTEGER,          -- 20 = 0.20% p.a.
  benchmark_index       TEXT,
  replication           TEXT CHECK(replication IN ('PHYSICAL','SAMPLED','SYNTHETIC') OR replication IS NULL),
  distribution_policy   TEXT CHECK(distribution_policy IN ('ACC','DIST') OR distribution_policy IS NULL),
  domicile              TEXT,             -- IE, LU, DE
  ucits                 INTEGER DEFAULT 1,

  -- tax
  teilfreistellung_pct  REAL NOT NULL DEFAULT 0.0,  -- 0.30 equity fund, 0.15 mixed, 0.0 stock
  is_fund               INTEGER NOT NULL DEFAULT 0, -- 1 => subject to Vorabpauschale

  -- classification / eligibility
  region                TEXT,
  sector                TEXT,
  index_membership      TEXT,             -- JSON array, e.g. '["SP500"]' or '["DAX40"]'

  status                TEXT NOT NULL DEFAULT 'UNIVERSE'
                        CHECK(status IN ('OWNED','WATCH','UNIVERSE','RETIRED')),
  metadata_verified_at  TEXT,
  notes                 TEXT
);
CREATE INDEX idx_instrument_status ON instrument(status);
CREATE INDEX idx_instrument_class ON instrument(asset_class);

-- weekly snapshot; screening thresholds read from here
CREATE TABLE instrument_fundamental (
  instrument_id     INTEGER NOT NULL REFERENCES instrument(id),
  as_of             TEXT NOT NULL,
  fund_size_eur     REAL,      -- ETF AUM
  market_cap_eur    REAL,      -- stock
  pe_ratio          REAL,
  dividend_yield    REAL,
  source            TEXT,
  PRIMARY KEY (instrument_id, as_of)
);

-- ------------------------------------------------------------
-- 3. Prices, FX, benchmarks
-- ------------------------------------------------------------

CREATE TABLE price (
  instrument_id        INTEGER NOT NULL REFERENCES instrument(id),
  date                 TEXT NOT NULL,
  close                REAL NOT NULL,   -- raw close. USE FOR TAX / NAV.
  total_return_close   REAL,            -- dividend-adjusted. USE FOR PERFORMANCE.
  volume               REAL,
  currency             TEXT NOT NULL,
  source               TEXT,
  PRIMARY KEY (instrument_id, date)
);
CREATE INDEX idx_price_date ON price(date);

CREATE TABLE fx_rate (
  date   TEXT NOT NULL,
  base   TEXT NOT NULL,
  quote  TEXT NOT NULL,
  rate   REAL NOT NULL,
  PRIMARY KEY (date, base, quote)
);

CREATE TABLE benchmark_series (
  code   TEXT NOT NULL,   -- 'WORLD_PROXY' | 'SP500_PROXY' | 'DAX_PROXY' | 'CASH_HURDLE' | 'MORTGAGE_HURDLE'
  date   TEXT NOT NULL,
  value  REAL NOT NULL,
  PRIMARY KEY (code, date)
);

-- ------------------------------------------------------------
-- 4. Transactions (source of truth)
-- ------------------------------------------------------------

CREATE TABLE transaction_ (
  id                INTEGER PRIMARY KEY,
  account_id        INTEGER NOT NULL REFERENCES account(id),
  instrument_id     INTEGER REFERENCES instrument(id),   -- NULL for pure cash movements
  type              TEXT NOT NULL CHECK(type IN (
                      'BUY','SELL','DIVIDEND','INTEREST','FEE','TAX_WITHHELD',
                      'DEPOSIT','WITHDRAWAL','VORABPAUSCHALE','SPLIT',
                      'TRANSFER_IN','TRANSFER_OUT')),
  trade_date        TEXT NOT NULL,
  settlement_date   TEXT,
  quantity          REAL,
  price_per_unit    REAL,
  gross_amount_eur  REAL,
  fees_eur          REAL NOT NULL DEFAULT 0,
  tax_withheld_eur  REAL NOT NULL DEFAULT 0,
  fx_rate           REAL NOT NULL DEFAULT 1.0,
  -- for TRANSFER_IN only: preserve original basis or FIFO breaks
  orig_acquire_date TEXT,
  orig_cost_eur     REAL,
  external_id       TEXT UNIQUE,       -- broker reference; makes re-import idempotent
  import_batch_id   INTEGER,
  note              TEXT
);
CREATE INDEX idx_txn_acct_date ON transaction_(account_id, trade_date);
CREATE INDEX idx_txn_instr ON transaction_(instrument_id, trade_date);

CREATE TABLE import_batch (
  id            INTEGER PRIMARY KEY,
  filename      TEXT,
  broker        TEXT,
  imported_at   TEXT,
  rows_total    INTEGER,
  rows_inserted INTEGER,
  rows_skipped  INTEGER
);

-- ------------------------------------------------------------
-- 5. Tax lots (derived, materialised)
-- ------------------------------------------------------------

CREATE TABLE lot (
  id                    INTEGER PRIMARY KEY,
  account_id            INTEGER NOT NULL REFERENCES account(id),
  instrument_id         INTEGER NOT NULL REFERENCES instrument(id),
  open_txn_id           INTEGER NOT NULL REFERENCES transaction_(id),
  open_date             TEXT NOT NULL,
  qty_opened            REAL NOT NULL,
  qty_remaining         REAL NOT NULL,
  cost_total_eur        REAL NOT NULL,   -- incl. purchase fees
  cost_per_unit_eur     REAL NOT NULL,
  vap_accumulated_eur   REAL NOT NULL DEFAULT 0  -- Vorabpauschalen already taxed on this lot
);
CREATE INDEX idx_lot_fifo ON lot(account_id, instrument_id, open_date);

CREATE TABLE lot_disposal (
  id                    INTEGER PRIMARY KEY,
  sell_txn_id           INTEGER NOT NULL REFERENCES transaction_(id),
  lot_id                INTEGER NOT NULL REFERENCES lot(id),
  qty                   REAL NOT NULL,
  proceeds_eur          REAL NOT NULL,   -- net of sale fees
  cost_basis_eur        REAL NOT NULL,
  gross_gain_eur        REAL NOT NULL,
  vap_offset_eur        REAL NOT NULL DEFAULT 0,
  teilfreistellung_eur  REAL NOT NULL DEFAULT 0,
  taxable_gain_eur      REAL NOT NULL,   -- signed; what hits allowance / loss pot
  holding_days          INTEGER
);

-- ------------------------------------------------------------
-- 6. German tax state
-- ------------------------------------------------------------

CREATE TABLE allowance_ledger (
  person_id          INTEGER NOT NULL REFERENCES person(id),
  tax_year           INTEGER NOT NULL,
  statutory_cap_eur  REAL NOT NULL,   -- 1000, or 2000 if joint_assessment
  PRIMARY KEY (person_id, tax_year)
);

CREATE TABLE allowance_entry (
  id           INTEGER PRIMARY KEY,
  person_id    INTEGER NOT NULL REFERENCES person(id),
  tax_year     INTEGER NOT NULL,
  source       TEXT NOT NULL CHECK(source IN ('DIVIDEND','INTEREST','VORABPAUSCHALE','REALIZED_GAIN')),
  txn_id       INTEGER REFERENCES transaction_(id),
  disposal_id  INTEGER REFERENCES lot_disposal(id),
  amount_eur   REAL NOT NULL,   -- POST-Teilfreistellung: allowance actually consumed
  status       TEXT NOT NULL CHECK(status IN ('ACTUAL','FORECAST','PLANNED')),
  entry_date   TEXT NOT NULL
);
CREATE INDEX idx_allow_person_year ON allowance_entry(person_id, tax_year, status);

CREATE TABLE loss_pot (
  person_id       INTEGER NOT NULL REFERENCES person(id),
  tax_year        INTEGER NOT NULL,
  pot             TEXT NOT NULL CHECK(pot IN ('AKTIEN','SONSTIGE')),
  carried_in_eur  REAL NOT NULL DEFAULT 0,
  additions_eur   REAL NOT NULL DEFAULT 0,
  offsets_eur     REAL NOT NULL DEFAULT 0,
  carried_out_eur REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (person_id, tax_year, pot)
);

CREATE TABLE vorabpauschale (
  id                    INTEGER PRIMARY KEY,
  account_id            INTEGER NOT NULL REFERENCES account(id),
  instrument_id         INTEGER NOT NULL REFERENCES instrument(id),
  tax_year              INTEGER NOT NULL,
  jan_first_price       REAL,
  year_end_price        REAL,
  qty_held              REAL,
  basiszins             REAL NOT NULL,      -- 0.0320 for 2026
  basisertrag_eur       REAL NOT NULL,
  distributions_eur     REAL NOT NULL DEFAULT 0,
  full_months_before    INTEGER NOT NULL DEFAULT 0,  -- pro-rating: 1/12 reduction each
  teilfreistellung_eur  REAL NOT NULL DEFAULT 0,
  taxable_eur           REAL NOT NULL,
  status                TEXT NOT NULL CHECK(status IN ('FORECAST','ACTUAL')),
  computed_at           TEXT
);

CREATE TABLE tax_constant (
  tax_year   INTEGER PRIMARY KEY,
  basiszins  REAL NOT NULL,
  kest_rate  REAL NOT NULL DEFAULT 0.25,
  soli_rate  REAL NOT NULL DEFAULT 0.055
);
INSERT INTO tax_constant (tax_year, basiszins) VALUES (2024, 0.0229), (2025, 0.0253), (2026, 0.0320);

-- ------------------------------------------------------------
-- 7. Peer comparison layer
-- ------------------------------------------------------------

CREATE TABLE instrument_performance (
  instrument_id      INTEGER NOT NULL REFERENCES instrument(id),
  as_of              TEXT NOT NULL,
  ret_1m REAL, ret_3m REAL, ret_6m REAL, ret_1y REAL,
  ret_3y_ann REAL, ret_5y_ann REAL,
  vol_1y REAL, vol_3y REAL,
  max_dd_3y REAL,
  sharpe_3y REAL,
  tracking_diff_1y   REAL,      -- ETF only, vs proxy benchmark where available
  PRIMARY KEY (instrument_id, as_of)
);

CREATE TABLE risk_bucket (
  instrument_id  INTEGER NOT NULL REFERENCES instrument(id),
  as_of          TEXT NOT NULL,
  bucket         TEXT NOT NULL,  -- 'EQ_US_LARGE','EQ_DE_LARGE','EQ_DM_BROAD','EQ_SECTOR','BOND_IG_EUR','CASH'
  vol_band       TEXT NOT NULL CHECK(vol_band IN ('LOW','MID','HIGH')),
  method         TEXT,
  PRIMARY KEY (instrument_id, as_of)
);

CREATE TABLE screen (
  id                  INTEGER PRIMARY KEY,
  name                TEXT NOT NULL,
  asset_class         TEXT,
  min_fund_size_eur   REAL,
  min_market_cap_eur  REAL,
  max_ter_bps         INTEGER,
  required_indices    TEXT,     -- JSON array; instrument must belong to >=1
  regions             TEXT,     -- JSON array
  active              INTEGER NOT NULL DEFAULT 1
);
INSERT INTO screen (name, asset_class, min_fund_size_eur, max_ter_bps, active)
  VALUES ('ETF default', 'ETF', 100000000, 50, 1);
INSERT INTO screen (name, asset_class, min_market_cap_eur, required_indices, active)
  VALUES ('Stock default', 'STOCK', 5000000000, '["SP500","DAX40"]', 1);

CREATE TABLE news_item (
  id               INTEGER PRIMARY KEY,
  instrument_id    INTEGER NOT NULL REFERENCES instrument(id),
  published_at     TEXT NOT NULL,
  headline         TEXT,
  source           TEXT,
  url              TEXT UNIQUE,
  sentiment        REAL,        -- -1..+1
  sentiment_method TEXT,
  summary          TEXT         -- OUR OWN one-line summary. Never store source article text.
);
CREATE INDEX idx_news_instr_date ON news_item(instrument_id, published_at);

-- ------------------------------------------------------------
-- 8. Goal, policy rules, LLM audit
-- ------------------------------------------------------------

CREATE TABLE goal (
  id                          INTEGER PRIMARY KEY,
  name                        TEXT NOT NULL DEFAULT 'Home purchase',
  target_date                 TEXT,
  target_amount_eur           REAL,
  mortgage_rate_assumption    REAL,   -- e.g. 0.037
  buffer_bps                  INTEGER,-- e.g. 150
  committed_bucket_target_eur REAL,
  monthly_contribution_eur    REAL
);

CREATE TABLE policy_rule (
  id         INTEGER PRIMARY KEY,
  rule_text  TEXT NOT NULL,
  rule_type  TEXT NOT NULL,   -- MAX_POSITION_PCT | MIN_CASH_EUR | MAX_SINGLE_STOCK_PCT | GLIDEPATH | MAX_TRADES_PER_MONTH
  params     TEXT NOT NULL,   -- JSON
  active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE llm_run (
  id            INTEGER PRIMARY KEY,
  run_type      TEXT NOT NULL,  -- daily_brief | peer_scan | harvest_plan | trade_check
  created_at    TEXT NOT NULL,
  provider      TEXT NOT NULL,  -- 'gemini'
  model         TEXT NOT NULL,  -- 'gemini-3.6-flash'
  input_json    TEXT,
  output_json   TEXT,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL DEFAULT 0,
  http_status   INTEGER,
  error         TEXT
);

-- ------------------------------------------------------------
-- 9. Materialised views (rebuilt nightly; safe to DROP and recompute)
-- ------------------------------------------------------------

CREATE TABLE position_daily (
  date              TEXT NOT NULL,
  account_id        INTEGER NOT NULL,
  instrument_id     INTEGER NOT NULL,
  quantity          REAL NOT NULL,
  market_value_eur  REAL NOT NULL,
  cost_basis_eur    REAL NOT NULL,
  unrealized_eur    REAL NOT NULL,
  PRIMARY KEY (date, account_id, instrument_id)
);

CREATE TABLE portfolio_daily (
  date                    TEXT NOT NULL,
  person_id               INTEGER NOT NULL,
  market_value_eur        REAL NOT NULL,
  net_contributions_eur   REAL NOT NULL,
  twr_index               REAL NOT NULL,   -- base 100 at inception
  xirr_ann                REAL,
  drawdown_pct            REAL,
  PRIMARY KEY (date, person_id)
);

CREATE TABLE what_if_run (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  scenario     TEXT NOT NULL,   -- JSON: transaction overrides / substitutions
  created_at   TEXT NOT NULL
);

CREATE TABLE what_if_result (
  run_id             INTEGER NOT NULL REFERENCES what_if_run(id),
  date               TEXT NOT NULL,
  market_value_eur   REAL NOT NULL,
  delta_vs_actual    REAL NOT NULL,
  PRIMARY KEY (run_id, date)
);

-- ============================================================
-- ADDENDUM A — manual entry, proposed entries, look-through
-- ============================================================

-- ---- A1. Provenance on transactions -------------------------

ALTER TABLE transaction_ ADD COLUMN source TEXT NOT NULL DEFAULT 'IMPORT';
    -- IMPORT | MANUAL | AUTO_CONFIRMED
ALTER TABLE transaction_ ADD COLUMN confidence TEXT NOT NULL DEFAULT 'ACTUAL';
    -- ACTUAL | ESTIMATE   (ESTIMATE = auto-proposed, amount not yet verified vs broker)
ALTER TABLE transaction_ ADD COLUMN pending_entry_id INTEGER;

-- ---- A2. Interest-bearing account terms ---------------------

ALTER TABLE account ADD COLUMN interest_rate REAL;             -- 0.0325
ALTER TABLE account ADD COLUMN interest_frequency TEXT;        -- MONTHLY|QUARTERLY|SEMIANNUAL|ANNUAL|AT_MATURITY
ALTER TABLE account ADD COLUMN interest_last_paid TEXT;
ALTER TABLE account ADD COLUMN interest_day_count TEXT DEFAULT 'ACT/365';
ALTER TABLE account ADD COLUMN maturity_date TEXT;             -- Festgeld only

-- ---- A3. Proposal inbox -------------------------------------
-- Everything auto-generated lands here and requires explicit user action.
-- Nothing auto-generated may write to transaction_ without a CONFIRMED row.

CREATE TABLE pending_entry (
  id               INTEGER PRIMARY KEY,
  kind             TEXT NOT NULL CHECK(kind IN ('DIVIDEND','INTEREST','VORABPAUSCHALE','SPLIT','MISSING_TRADE')),
  account_id       INTEGER NOT NULL REFERENCES account(id),
  instrument_id    INTEGER REFERENCES instrument(id),
  proposed_date    TEXT NOT NULL,
  proposed_qty     REAL,
  proposed_gross_eur REAL,
  proposed_tax_eur REAL DEFAULT 0,
  proposed_net_eur REAL,
  currency         TEXT DEFAULT 'EUR',
  fx_rate          REAL DEFAULT 1.0,
  basis            TEXT NOT NULL,   -- human-readable derivation, shown in the UI
  derivation       TEXT,            -- JSON: inputs used, so the number is auditable
  status           TEXT NOT NULL DEFAULT 'PROPOSED'
                   CHECK(status IN ('PROPOSED','CONFIRMED','EDITED','REJECTED','SUPERSEDED')),
  created_at       TEXT NOT NULL,
  resolved_at      TEXT,
  resulting_txn_id INTEGER REFERENCES transaction_(id),
  UNIQUE(kind, account_id, instrument_id, proposed_date)
);
CREATE INDEX idx_pending_status ON pending_entry(status, proposed_date);

-- ---- A4. Duplicate detection (manual entry vs later CSV) ----

CREATE TABLE duplicate_candidate (
  id             INTEGER PRIMARY KEY,
  txn_a_id       INTEGER NOT NULL REFERENCES transaction_(id),
  txn_b_id       INTEGER NOT NULL REFERENCES transaction_(id),
  match_score    REAL NOT NULL,
  reason         TEXT,
  status         TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','MERGED','DISMISSED')),
  detected_at    TEXT NOT NULL
);

-- ---- A5. Dividend calendar ----------------------------------

CREATE TABLE dividend_event (
  id                INTEGER PRIMARY KEY,
  instrument_id     INTEGER NOT NULL REFERENCES instrument(id),
  ex_date           TEXT NOT NULL,
  pay_date          TEXT,
  amount_per_share  REAL NOT NULL,
  currency          TEXT NOT NULL,
  source            TEXT,
  UNIQUE(instrument_id, ex_date)
);

-- ---- A6. Canonical companies (look-through targets) ---------
-- Separate from `instrument`: an ETF holding is a company, not necessarily
-- something you can trade on XETRA.

CREATE TABLE issuer_entity (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  isin           TEXT UNIQUE,
  ticker         TEXT,
  country        TEXT,
  sector         TEXT,
  instrument_id  INTEGER REFERENCES instrument(id)   -- set when directly tradeable/owned
);

CREATE TABLE security_alias (
  id                INTEGER PRIMARY KEY,
  issuer_entity_id  INTEGER NOT NULL REFERENCES issuer_entity(id),
  raw_name          TEXT NOT NULL,   -- exactly as it appears in an issuer holdings file
  raw_ticker        TEXT,
  raw_isin          TEXT,
  provider          TEXT,
  confirmed_by_user INTEGER NOT NULL DEFAULT 0,
  UNIQUE(raw_name, provider)
);

-- ---- A7. ETF constituents -----------------------------------

CREATE TABLE etf_holding_snapshot (
  id                INTEGER PRIMARY KEY,
  instrument_id     INTEGER NOT NULL REFERENCES instrument(id),
  as_of             TEXT NOT NULL,           -- issuer's stated holdings date, NOT download date
  downloaded_at     TEXT NOT NULL,
  provider          TEXT NOT NULL,           -- ishares | xtrackers | vanguard | amundi | spdr | manual
  source_url        TEXT,
  holdings_count    INTEGER,
  weight_sum        REAL,                    -- sanity check, should be ~1.0
  coverage_pct      REAL,                    -- share of weight successfully resolved to issuer_entity
  is_synthetic      INTEGER NOT NULL DEFAULT 0,  -- 1 => substitute basket, exclude from look-through
  status            TEXT NOT NULL DEFAULT 'OK' CHECK(status IN ('OK','STALE','PARSE_FAILED')),
  UNIQUE(instrument_id, as_of)
);

CREATE TABLE etf_holding (
  snapshot_id       INTEGER NOT NULL REFERENCES etf_holding_snapshot(id),
  issuer_entity_id  INTEGER REFERENCES issuer_entity(id),   -- NULL until resolved
  raw_name          TEXT NOT NULL,
  raw_ticker        TEXT,
  raw_isin          TEXT,
  weight            REAL NOT NULL,           -- 0.0721 = 7.21%
  asset_class       TEXT,
  PRIMARY KEY (snapshot_id, raw_name)
);
CREATE INDEX idx_etf_holding_entity ON etf_holding(issuer_entity_id);

-- ---- A8. Aggregated exposure (rebuilt nightly) --------------

CREATE TABLE lookthrough_exposure (
  as_of                TEXT NOT NULL,
  person_id            INTEGER NOT NULL,
  issuer_entity_id     INTEGER NOT NULL REFERENCES issuer_entity(id),
  direct_value_eur     REAL NOT NULL DEFAULT 0,   -- held as a stock
  indirect_value_eur   REAL NOT NULL DEFAULT 0,   -- via ETFs
  total_value_eur      REAL NOT NULL,
  pct_of_portfolio     REAL NOT NULL,
  contributing_etfs    TEXT,                      -- JSON [{instrument_id, weight, value_eur}]
  oldest_snapshot_date TEXT,                      -- staleness of the blend
  PRIMARY KEY (as_of, person_id, issuer_entity_id)
);

CREATE TABLE lookthrough_sector_exposure (
  as_of            TEXT NOT NULL,
  person_id        INTEGER NOT NULL,
  dimension        TEXT NOT NULL CHECK(dimension IN ('SECTOR','COUNTRY','CURRENCY')),
  bucket           TEXT NOT NULL,
  value_eur        REAL NOT NULL,
  pct_of_portfolio REAL NOT NULL,
  PRIMARY KEY (as_of, person_id, dimension, bucket)
);

-- ============================================================
-- ADDENDUM B — unit accounting, attribution, look-ahead
-- ============================================================

-- ---- B1. Unit accounting (the TWR engine) -------------------
-- Portfolio is modelled as a fund. External flows buy/redeem units at the
-- prevailing unit price, so unit_price is a pure time-weighted return index.

CREATE TABLE portfolio_unit (
  date              TEXT NOT NULL,
  person_id         INTEGER NOT NULL REFERENCES person(id),
  market_value_eur  REAL NOT NULL,
  net_flow_eur      REAL NOT NULL DEFAULT 0,   -- +deposit / -withdrawal, external only
  units_issued      REAL NOT NULL DEFAULT 0,   -- net_flow / unit_price_prior
  units_outstanding REAL NOT NULL,
  unit_price        REAL NOT NULL,             -- base 100 at inception
  daily_return      REAL,
  PRIMARY KEY (date, person_id)
);

-- ---- B2. Return attribution ---------------------------------

CREATE TABLE return_attribution (
  period_start      TEXT NOT NULL,
  period_end        TEXT NOT NULL,
  person_id         INTEGER NOT NULL,
  instrument_id     INTEGER NOT NULL REFERENCES instrument(id),
  avg_weight        REAL NOT NULL,     -- average share of portfolio over the period
  holding_return    REAL NOT NULL,     -- total return of the holding itself
  contribution      REAL NOT NULL,     -- Cariño-adjusted; SUM over instruments == portfolio TWR
  income_eur        REAL NOT NULL DEFAULT 0,
  realized_eur      REAL NOT NULL DEFAULT 0,
  unrealized_eur    REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (period_start, period_end, person_id, instrument_id)
);

CREATE TABLE pnl_bridge (
  period_start      TEXT NOT NULL,
  period_end        TEXT NOT NULL,
  person_id         INTEGER NOT NULL,
  opening_value_eur REAL NOT NULL,
  contributions_eur REAL NOT NULL DEFAULT 0,
  withdrawals_eur   REAL NOT NULL DEFAULT 0,
  income_eur        REAL NOT NULL DEFAULT 0,   -- dividends + interest
  realized_eur      REAL NOT NULL DEFAULT 0,
  unrealized_eur    REAL NOT NULL DEFAULT 0,
  fees_eur          REAL NOT NULL DEFAULT 0,
  taxes_eur         REAL NOT NULL DEFAULT 0,
  closing_value_eur REAL NOT NULL,
  twr               REAL,
  xirr              REAL,
  timing_gap        REAL,                       -- xirr - twr
  PRIMARY KEY (period_start, period_end, person_id)
);

-- ---- B3. Forward income calendar ----------------------------

CREATE TABLE cashflow_forecast (
  id                INTEGER PRIMARY KEY,
  run_id            INTEGER,
  expected_date     TEXT NOT NULL,
  account_id        INTEGER REFERENCES account(id),
  instrument_id     INTEGER REFERENCES instrument(id),
  person_id         INTEGER NOT NULL,
  kind              TEXT NOT NULL CHECK(kind IN ('DIVIDEND','INTEREST','VORABPAUSCHALE','MATURITY','CONTRIBUTION')),
  gross_eur         REAL NOT NULL,
  teilfreistellung_eur REAL NOT NULL DEFAULT 0,
  allowance_use_eur REAL NOT NULL DEFAULT 0,    -- post-Teilfreistellung
  confidence        TEXT NOT NULL CHECK(confidence IN ('DECLARED','HIGH','MEDIUM','LOW')),
  method            TEXT NOT NULL,              -- 'declared' | 'ttm_schedule' | 'contract_rate' | 'basiszins_assumed'
  assumptions       TEXT,                       -- JSON, auditable
  generated_at      TEXT NOT NULL
);
CREATE INDEX idx_cff_date ON cashflow_forecast(expected_date, person_id);

CREATE TABLE forecast_assumption (
  key         TEXT PRIMARY KEY,   -- 'dividend_growth_pct','tagesgeld_rate','basiszins_next','contribution_monthly'
  value       REAL NOT NULL,
  source      TEXT,               -- 'user' | 'derived' | 'statutory'
  as_of       TEXT NOT NULL,
  note        TEXT
);

-- ---- B4. Monte Carlo projection -----------------------------

CREATE TABLE projection_run (
  id            INTEGER PRIMARY KEY,
  created_at    TEXT NOT NULL,
  person_id     INTEGER,
  method        TEXT NOT NULL CHECK(method IN ('BLOCK_BOOTSTRAP','PARAMETRIC','DETERMINISTIC')),
  horizon_end   TEXT NOT NULL,
  n_paths       INTEGER NOT NULL,
  seed          INTEGER,
  history_start TEXT,             -- sample window used
  assumptions   TEXT NOT NULL     -- JSON: contributions, fees, tax drag, glidepath
);

CREATE TABLE projection_percentile (
  run_id  INTEGER NOT NULL REFERENCES projection_run(id),
  date    TEXT NOT NULL,
  p05 REAL, p25 REAL, p50 REAL, p75 REAL, p95 REAL,
  PRIMARY KEY (run_id, date)
);

CREATE TABLE projection_outcome (
  run_id  INTEGER NOT NULL REFERENCES projection_run(id),
  metric  TEXT NOT NULL,   -- 'prob_target_met' | 'shortfall_p10_eur' | 'months_delay_p10' | 'prob_below_committed'
  value   REAL NOT NULL,
  PRIMARY KEY (run_id, metric)
);

-- ============================================================
-- ADDENDUM C — savings tracking and projection scenarios
-- ============================================================

-- ---- C1. Flow classification --------------------------------

ALTER TABLE account ADD COLUMN in_perimeter INTEGER NOT NULL DEFAULT 1;
    -- 1 = part of the savings perimeter. A salary current account tracked only
    -- for balance should be 0; money arriving from it is then EXTERNAL_IN.

ALTER TABLE transaction_ ADD COLUMN flow_class TEXT;
    -- EXTERNAL_IN | EXTERNAL_OUT | INTERNAL_TRANSFER | INVESTMENT | INCOME | COST
ALTER TABLE transaction_ ADD COLUMN is_new_savings INTEGER NOT NULL DEFAULT 0;
    -- user-facing flag; set by classifier, overridable in the UI
ALTER TABLE transaction_ ADD COLUMN savings_label TEXT;
    -- optional user tag: 'salary','bonus','gift','tax refund'

CREATE TABLE internal_transfer_link (
  id            INTEGER PRIMARY KEY,
  out_txn_id    INTEGER NOT NULL REFERENCES transaction_(id),
  in_txn_id     INTEGER NOT NULL REFERENCES transaction_(id),
  amount_eur    REAL NOT NULL,
  matched_by    TEXT NOT NULL CHECK(matched_by IN ('AUTO','USER')),
  detected_at   TEXT NOT NULL,
  UNIQUE(out_txn_id, in_txn_id)
);

-- ---- C2. Savings aggregates ---------------------------------

CREATE TABLE savings_period (
  person_id             INTEGER NOT NULL REFERENCES person(id),
  period                TEXT NOT NULL,          -- 'YYYY-MM'
  gross_in_eur          REAL NOT NULL DEFAULT 0,
  gross_out_eur         REAL NOT NULL DEFAULT 0,
  net_new_money_eur     REAL NOT NULL DEFAULT 0,
  cumulative_net_eur    REAL NOT NULL DEFAULT 0,
  income_received_eur   REAL NOT NULL DEFAULT 0,  -- dividends+interest; NOT savings
  market_growth_eur     REAL NOT NULL DEFAULT 0,
  closing_value_eur     REAL NOT NULL,
  net_income_eur        REAL,                     -- optional user input
  savings_rate          REAL,                     -- net_new_money / net_income if available
  PRIMARY KEY (person_id, period)
);

-- ---- C3. Projection scenarios -------------------------------

CREATE TABLE scenario (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  person_id    INTEGER,
  is_baseline  INTEGER NOT NULL DEFAULT 0,
  levers       TEXT NOT NULL,      -- JSON, schema in IMPLEMENTATION.md §C4
  projection_run_id INTEGER REFERENCES projection_run(id),
  created_at   TEXT NOT NULL,
  pinned       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE scenario_result (
  scenario_id        INTEGER NOT NULL REFERENCES scenario(id),
  metric             TEXT NOT NULL,   -- prob_target_met_50/75/90 | months_to_target_75 |
                                      -- months_earlier_75 | shortfall_p10_eur | efficiency_months_per_100
  value              REAL NOT NULL,
  delta_vs_baseline  REAL,
  PRIMARY KEY (scenario_id, metric)
);
