-- =====================================================================
-- 0002 -- allow 'fintables' as a price source.
--
-- TEFAS's own endpoint sits behind a bot-protection challenge, so Turkish
-- fund NAVs need a different route. Fintables republishes the same figures
-- and matches the broker export to within 0.02%.
--
-- SQLite cannot alter a CHECK constraint in place, so the table is rebuilt.
-- The twelve-step procedure from the SQLite docs is followed deliberately:
-- foreign keys OFF for the swap (they are re-enabled by connection.py and
-- verified below), create-copy-drop-rename, then restore indexes and
-- triggers, which are dropped along with the old table.
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

    -- The only change: 'fintables' added.
    price_source TEXT NOT NULL DEFAULT 'yfinance'
                 CHECK (price_source IN
                        ('yfinance', 'stooq', 'tefas', 'fintables', 'manual', 'synthetic')),
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
    notes  TEXT
);

INSERT INTO instrument_new SELECT * FROM instrument;

DROP TABLE instrument;
ALTER TABLE instrument_new RENAME TO instrument;

CREATE INDEX idx_instrument_pricing ON instrument (price_source, status);
CREATE INDEX idx_instrument_tax ON instrument (tax_regime);

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
