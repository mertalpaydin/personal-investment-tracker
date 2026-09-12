-- =====================================================================
-- 0025 -- allow 'onvista' as a price source, and a cache for its lookups.
--
-- Yahoo Finance's history for roughly half of the .DE ETFs in this database
-- is wrong: USD values stored against EUR instruments (a constant ~14.4% gap
-- for some, a series 100x off or from a different fund entirely for others).
-- It also dropped 110 Xetra symbols in September 2026 and publishes ETF daily
-- bars a day late. onvista's public JSON API has correct Xetra EUR closes,
-- same-day.
--
-- SQLite cannot alter a CHECK constraint in place, so the table is rebuilt,
-- following the same twelve-step procedure as 0002 -- this time rebuilding
-- from the CURRENT instrument shape (risk_bucket and risk_level, added by
-- 0013 and 0020 via ALTER TABLE, are carried over explicitly since a
-- `CREATE TABLE ... AS` copy would not otherwise be told about them).
-- =====================================================================

-- Foreign keys are toggled by the runner, outside the transaction:
-- PRAGMA foreign_keys is a no-op inside one.

-- Dropped FIRST. SQLite validates every existing trigger when a table is
-- dropped, and this one references `instrument`, so leaving it in place makes
-- the swap fail with "no such table" partway through.
DROP TRIGGER IF EXISTS trg_price_currency_matches_instrument;

-- Defensive: a previous interrupted attempt may have left this behind.
DROP TABLE IF EXISTS instrument_new;

CREATE TABLE instrument_new (
    id   INTEGER PRIMARY KEY,

    isin TEXT UNIQUE,
    wkn  TEXT,

    import_key TEXT UNIQUE,
    symbol     TEXT,
    name       TEXT NOT NULL,

    asset_class TEXT NOT NULL
                CHECK (asset_class IN ('ETF', 'STOCK', 'FUND', 'CASH', 'BOND', 'CRYPTO', 'OTHER')),

    currency          TEXT NOT NULL,
    economic_currency TEXT,
    exchange          TEXT,

    trading_calendar TEXT NOT NULL DEFAULT 'XETRA'
                     CHECK (trading_calendar IN ('XETRA', 'TEFAS', 'TARGET2', 'ALL_DAYS')),

    -- The only change: 'onvista' added.
    price_source TEXT NOT NULL DEFAULT 'yfinance'
                 CHECK (price_source IN
                        ('yfinance', 'stooq', 'tefas', 'fintables', 'onvista', 'manual', 'synthetic')),
    price_symbol TEXT,

    ter_bps             INTEGER,
    distribution_policy TEXT CHECK (distribution_policy IN ('ACC', 'DIST')),
    domicile            TEXT,

    teilfreistellung_pct DECIMAL_TEXT,
    is_fund              INTEGER NOT NULL DEFAULT 0 CHECK (is_fund IN (0, 1)),

    tax_regime TEXT NOT NULL DEFAULT 'DE_INVESTMENT'
               CHECK (tax_regime IN ('DE_INVESTMENT', 'DE_PRIVATE_SALE', 'OUT_OF_SCOPE')),
    tax_scope_note TEXT,

    is_cash_proxy     INTEGER NOT NULL DEFAULT 0 CHECK (is_cash_proxy IN (0, 1)),
    linked_account_id INTEGER REFERENCES account (id),

    region TEXT,
    sector TEXT,
    status TEXT NOT NULL DEFAULT 'UNIVERSE'
           CHECK (status IN ('OWNED', 'WATCH', 'UNIVERSE', 'RETIRED')),
    notes  TEXT,

    risk_bucket TEXT,
    risk_level  INTEGER CHECK (risk_level IS NULL OR (risk_level >= 1 AND risk_level <= 7))
);

INSERT INTO instrument_new SELECT * FROM instrument;

DROP TABLE instrument;
ALTER TABLE instrument_new RENAME TO instrument;

CREATE INDEX idx_instrument_pricing ON instrument (price_source, status);
CREATE INDEX idx_instrument_tax ON instrument (tax_regime);
CREATE INDEX idx_instrument_bucket ON instrument (risk_bucket);
CREATE INDEX idx_instrument_risk_level ON instrument (risk_level);

-- Dropped with the old table, so it has to be recreated here. Without it a
-- price could be stored in a currency its instrument does not use, which
-- silently misvalues the whole position.
CREATE TRIGGER trg_price_currency_matches_instrument
BEFORE INSERT ON price
FOR EACH ROW
WHEN NEW.currency <> (SELECT currency FROM instrument WHERE id = NEW.instrument_id)
BEGIN
    SELECT RAISE(ABORT, 'price.currency must equal instrument.currency');
END;

-- Resolved instrument/notation pairs, so a repeat fetch skips the search and
-- snapshot calls and goes straight to EOD history. Kept outside the provider
-- (which stays DB-free like every other provider) and populated by the
-- caller through ingest/onvista_cache.py.
CREATE TABLE onvista_notation (
    instrument_id INTEGER PRIMARY KEY REFERENCES instrument (id),
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    id_notation   TEXT NOT NULL,
    market        TEXT NOT NULL,
    currency      TEXT NOT NULL,
    resolved_at   TEXT NOT NULL
);

-- Configuration, not a figure: repoint the .DE ETFs and stocks currently
-- priced by yfinance at onvista. Anything already on 'manual', 'tefas',
-- 'fintables' or 'synthetic' is untouched, and so is anything not on Xetra.
UPDATE instrument
SET price_source = 'onvista'
WHERE price_source = 'yfinance'
  AND price_symbol LIKE '%.DE'
  AND asset_class IN ('ETF', 'STOCK');
