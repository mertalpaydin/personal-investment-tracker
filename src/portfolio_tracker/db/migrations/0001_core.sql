-- =====================================================================
-- 0001_core -- the v1 schema.
--
-- Seventeen tables, cut down from the ~45 in the inherited design. See
-- docs/adr/0003 for what was dropped and, more importantly, for the two
-- things pulled FORWARD (unit accounting and flow_class), because
-- retrofitting either would mean recomputing all history.
--
-- Conventions:
--   * Dates are ISO-8601 TEXT, 'YYYY-MM-DD'. Timestamps carry a 'Z'.
--   * Money and quantities are DECIMAL_TEXT -- see money.py for why the
--     type name must contain "TEXT" and must not contain "INT".
--   * Every amount exists twice: *_local in its own currency, *_eur as a
--     derivation stored alongside the rate that produced it.
--   * Foreign-key enforcement is per-connection; connection.py sets it.
-- =====================================================================

-- ---------------------------------------------------------------------
-- People and accounts
-- ---------------------------------------------------------------------

CREATE TABLE person (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL,
    church_tax_rate  DECIMAL_TEXT NOT NULL DEFAULT '0.0',   -- 0.0 | 0.08 | 0.09
    joint_assessment INTEGER NOT NULL DEFAULT 0 CHECK (joint_assessment IN (0, 1))
);

CREATE TABLE account (
    id        INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person (id),
    name      TEXT    NOT NULL,
    broker    TEXT,
    kind      TEXT    NOT NULL CHECK (kind IN ('DEPOT', 'CASH', 'TAGESGELD', 'FESTGELD')),
    currency  TEXT    NOT NULL,

    -- Amount of the German tax allowance filed at THIS bank. Summed per person
    -- and compared against the statutory cap to warn about misallocation.
    freistellungsauftrag_eur DECIMAL_TEXT NOT NULL DEFAULT '0',

    opened_date  TEXT,
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    in_perimeter INTEGER NOT NULL DEFAULT 1 CHECK (in_perimeter IN (0, 1)),

    -- Interest-bearing account terms.
    interest_rate          DECIMAL_TEXT,           -- nominal annual, GROSS, e.g. '0.4250'
    interest_day_count     TEXT NOT NULL DEFAULT 'ACT/365'
                           CHECK (interest_day_count IN ('ACT/365', 'ACT/360', '30E/360')),
    interest_frequency     TEXT CHECK (interest_frequency IN (
                               'DAILY_CAPITALIZE', 'MONTHLY', 'QUARTERLY',
                               'SEMIANNUAL', 'ANNUAL', 'AT_MATURITY')),
    interest_last_paid     TEXT,
    interest_first_accrual TEXT,

    -- Withheld at source (Turkish stopaj, for instance). Without modelling it
    -- the projected balance drifts above the real one at every payment date.
    withholding_rate DECIMAL_TEXT NOT NULL DEFAULT '0',

    rollover_policy TEXT NOT NULL DEFAULT 'CAPITALIZE'
                    CHECK (rollover_policy IN ('CAPITALIZE', 'PAYOUT', 'NONE')),
    maturity_date   TEXT,

    -- The synthetic instrument carrying this account's balance, so that cash
    -- is revalued by the same daily FX path as everything else.
    cash_instrument_id INTEGER REFERENCES instrument (id) DEFERRABLE INITIALLY DEFERRED,

    CHECK (interest_rate IS NULL OR interest_first_accrual IS NOT NULL)
);

CREATE INDEX idx_account_person ON account (person_id, active);

-- ---------------------------------------------------------------------
-- Instruments
-- ---------------------------------------------------------------------

CREATE TABLE instrument (
    id   INTEGER PRIMARY KEY,

    -- NULLABLE, unlike the inherited design. The source export contains no
    -- ISINs whatsoever, so NOT NULL would make import impossible. SQLite
    -- permits multiple NULLs in a UNIQUE column, so uniqueness still holds
    -- for the ones that do get backfilled.
    isin TEXT UNIQUE,
    wkn  TEXT,

    -- The source export's symbol. This, not the ISIN, is the importer's
    -- stable identity.
    import_key TEXT UNIQUE,
    symbol     TEXT,
    name       TEXT NOT NULL,

    asset_class TEXT NOT NULL
                CHECK (asset_class IN ('ETF', 'STOCK', 'FUND', 'CASH', 'BOND', 'CRYPTO', 'OTHER')),

    -- No DEFAULT, deliberately. A silent 'EUR' default is exactly how a
    -- lira-denominated fund ends up valued as though it were euro.
    currency          TEXT NOT NULL,
    economic_currency TEXT,      -- underlying exposure; metadata only in v1
    exchange          TEXT,

    trading_calendar TEXT NOT NULL DEFAULT 'XETRA'
                     CHECK (trading_calendar IN ('XETRA', 'TEFAS', 'TARGET2', 'ALL_DAYS')),

    price_source TEXT NOT NULL DEFAULT 'yfinance'
                 CHECK (price_source IN ('yfinance', 'stooq', 'tefas', 'manual', 'synthetic')),
    price_symbol TEXT,

    ter_bps             INTEGER,
    distribution_policy TEXT CHECK (distribution_policy IN ('ACC', 'DIST')),
    domicile            TEXT,

    -- German tax classification.
    teilfreistellung_pct DECIMAL_TEXT,   -- NULL means unresolved; never default to 0.30
    is_fund              INTEGER NOT NULL DEFAULT 0 CHECK (is_fund IN (0, 1)),

    -- The ONLY scope test the tax engine performs. No currency comparison
    -- appears anywhere in tax.py, so bringing an instrument into scope later
    -- is a data change rather than a code change.
    tax_regime TEXT NOT NULL DEFAULT 'DE_INVESTMENT'
               CHECK (tax_regime IN ('DE_INVESTMENT', 'DE_PRIVATE_SALE', 'OUT_OF_SCOPE')),
    tax_scope_note TEXT,

    -- Cash proxies carry a deposit balance as a position priced at 1.0.
    is_cash_proxy     INTEGER NOT NULL DEFAULT 0 CHECK (is_cash_proxy IN (0, 1)),
    linked_account_id INTEGER REFERENCES account (id),

    region TEXT,
    sector TEXT,
    status TEXT NOT NULL DEFAULT 'UNIVERSE'
           CHECK (status IN ('OWNED', 'WATCH', 'UNIVERSE', 'RETIRED')),
    notes  TEXT
);

CREATE INDEX idx_instrument_pricing ON instrument (price_source, status);
CREATE INDEX idx_instrument_tax ON instrument (tax_regime);

-- ---------------------------------------------------------------------
-- Prices and FX
-- ---------------------------------------------------------------------

CREATE TABLE price (
    instrument_id      INTEGER NOT NULL REFERENCES instrument (id),
    date               TEXT    NOT NULL,
    close              DECIMAL_TEXT NOT NULL,   -- in instrument.currency, NEVER converted
    total_return_close DECIMAL_TEXT,            -- dividend-adjusted; performance only
    volume             DECIMAL_TEXT,
    currency           TEXT    NOT NULL,
    source             TEXT    NOT NULL,
    fetched_at         TEXT    NOT NULL,
    PRIMARY KEY (instrument_id, date)
);

CREATE INDEX idx_price_date ON price (date);

-- SQLite cannot express a cross-table CHECK, so the invariant "a price is
-- always quoted in its instrument's own currency" is enforced by trigger.
-- Without it, one mislabelled row silently values a whole position wrong.
CREATE TRIGGER trg_price_currency_matches_instrument
BEFORE INSERT ON price
FOR EACH ROW
WHEN NEW.currency <> (SELECT currency FROM instrument WHERE id = NEW.instrument_id)
BEGIN
    SELECT RAISE(ABORT, 'price.currency must equal instrument.currency');
END;

CREATE TABLE fx_rate (
    date  TEXT NOT NULL,

    -- One canonical direction, enforced rather than documented: rate is always
    -- units of `quote` per 1 EUR, so conversion to EUR is ALWAYS division.
    -- There is no multiply path, which removes inverted-rate bugs entirely.
    base  TEXT NOT NULL DEFAULT 'EUR' CHECK (base = 'EUR'),
    quote TEXT NOT NULL CHECK (quote <> 'EUR'),
    rate  DECIMAL_TEXT NOT NULL,

    source     TEXT NOT NULL CHECK (source IN ('ecb', 'yfinance', 'manual')),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, base, quote)
);

CREATE INDEX idx_fx_quote_date ON fx_rate (quote, date);

-- ---------------------------------------------------------------------
-- Transactions -- the only source of truth
-- ---------------------------------------------------------------------

CREATE TABLE import_batch (
    id            INTEGER PRIMARY KEY,
    filename      TEXT,
    source        TEXT,
    source_sha256 TEXT,
    imported_at   TEXT NOT NULL,
    rows_total    INTEGER,
    rows_inserted INTEGER,
    rows_skipped  INTEGER,
    report_json   TEXT
);

CREATE TABLE transaction_ (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES account (id),
    instrument_id INTEGER REFERENCES instrument (id),

    type TEXT NOT NULL CHECK (type IN (
        'BUY', 'SELL', 'DIVIDEND', 'INTEREST', 'FEE', 'TAX_WITHHELD',
        'DEPOSIT', 'WITHDRAWAL', 'VORABPAUSCHALE', 'SPLIT',
        'TRANSFER_IN', 'TRANSFER_OUT')),

    trade_date      TEXT NOT NULL,
    settlement_date TEXT,
    quantity        DECIMAL_TEXT,

    -- Native leg: what the source actually said.
    currency             TEXT NOT NULL,
    price_per_unit_local DECIMAL_TEXT,
    gross_amount_local   DECIMAL_TEXT,
    fees_local           DECIMAL_TEXT NOT NULL DEFAULT '0',
    tax_withheld_local   DECIMAL_TEXT NOT NULL DEFAULT '0',

    -- Euro leg: frozen at trade-date FX, stored with the rate that produced it.
    -- NULL means "not yet known", which is the truthful value for a lira row
    -- whose historical rate has not been backfilled. Writing a current spot
    -- rate onto a two-year-old acquisition would fabricate a cost basis.
    fx_rate          DECIMAL_TEXT,
    fx_rate_source   TEXT,
    fx_rate_asof     TEXT,
    gross_amount_eur DECIMAL_TEXT,
    fees_eur         DECIMAL_TEXT,
    tax_withheld_eur DECIMAL_TEXT,

    -- TRANSFER_IN only: preserving these is what stops FIFO from breaking.
    orig_acquire_date TEXT,
    orig_cost_local   DECIMAL_TEXT,
    orig_cost_eur     DECIMAL_TEXT,

    -- Provenance. The tax engine will one day print a gain computed from a
    -- basis that was reverse-engineered from a rounded column, and the API
    -- must be able to say so.
    source     TEXT NOT NULL DEFAULT 'IMPORT'
               CHECK (source IN ('IMPORT', 'MANUAL', 'AUTO_CONFIRMED', 'SYNTHESIZED')),
    confidence TEXT NOT NULL DEFAULT 'ACTUAL'
               CHECK (confidence IN ('ACTUAL', 'ESTIMATE', 'RECONSTRUCTED')),

    -- Graded independently of each other, because they fail independently: a
    -- lot can have an exact quantity and an estimated cost basis.
    quantity_confidence TEXT NOT NULL DEFAULT 'EXACT'
                        CHECK (quantity_confidence IN ('OVERRIDE', 'EXACT', 'DERIVED', 'ESTIMATED')),
    quantity_lo   DECIMAL_TEXT,
    quantity_hi   DECIMAL_TEXT,
    basis_quality TEXT NOT NULL DEFAULT 'EXACT'
                  CHECK (basis_quality IN ('EXACT', 'DERIVED', 'ESTIMATED')),

    -- The source export has no fees at all, so this is 0 for every imported
    -- row and every taxable gain computed from it is biased high.
    fees_known INTEGER NOT NULL DEFAULT 0 CHECK (fees_known IN (0, 1)),

    -- Required by unit accounting: without it there is no way to tell a
    -- deposit from a dividend, and every time-weighted return is wrong.
    flow_class TEXT NOT NULL CHECK (flow_class IN (
        'EXTERNAL_IN', 'EXTERNAL_OUT', 'INTERNAL_TRANSFER',
        'INVESTMENT', 'INCOME', 'COST')),

    -- What makes re-importing the same export idempotent.
    external_id     TEXT UNIQUE,
    import_batch_id INTEGER REFERENCES import_batch (id),
    note            TEXT,

    CHECK (currency <> 'EUR' OR fx_rate IS NULL OR fx_rate = '1'),
    CHECK (quantity_lo IS NULL OR quantity_hi IS NOT NULL)
);

CREATE INDEX idx_txn_fifo ON transaction_ (account_id, instrument_id, trade_date, id);
CREATE INDEX idx_txn_flow ON transaction_ (trade_date, flow_class);

-- ---------------------------------------------------------------------
-- Derived: tax lots
-- ---------------------------------------------------------------------

CREATE TABLE lot (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES account (id),
    instrument_id INTEGER NOT NULL REFERENCES instrument (id),
    open_txn_id   INTEGER NOT NULL REFERENCES transaction_ (id),
    open_date     TEXT    NOT NULL,

    qty_opened    DECIMAL_TEXT NOT NULL,
    qty_remaining DECIMAL_TEXT NOT NULL,

    currency            TEXT NOT NULL,
    cost_total_local    DECIMAL_TEXT NOT NULL,   -- includes purchase fees
    cost_per_unit_local DECIMAL_TEXT NOT NULL,

    -- FROZEN at acquisition. German law computes the taxable gain on a foreign
    -- asset as proceeds at sale-date FX minus cost at acquisition-date FX, so
    -- this column has to exist before it is needed or the history is unusable.
    fx_rate_at_open   DECIMAL_TEXT,
    cost_total_eur    DECIMAL_TEXT,
    cost_per_unit_eur DECIMAL_TEXT,

    -- Vorabpauschale already taxed on this lot, deducted from the gain on
    -- eventual sale. The field most home-built trackers omit, causing the
    -- owner to be taxed twice.
    vap_accumulated_eur DECIMAL_TEXT NOT NULL DEFAULT '0',

    basis_quality TEXT NOT NULL DEFAULT 'EXACT'
                  CHECK (basis_quality IN ('EXACT', 'DERIVED', 'ESTIMATED'))
);

CREATE INDEX idx_lot_fifo ON lot (account_id, instrument_id, open_date, id);
CREATE INDEX idx_lot_open ON lot (instrument_id, qty_remaining);

CREATE TABLE lot_disposal (
    id          INTEGER PRIMARY KEY,
    sell_txn_id INTEGER NOT NULL REFERENCES transaction_ (id),
    lot_id      INTEGER NOT NULL REFERENCES lot (id),
    qty         DECIMAL_TEXT NOT NULL,

    currency         TEXT NOT NULL,
    proceeds_local   DECIMAL_TEXT NOT NULL,
    cost_basis_local DECIMAL_TEXT NOT NULL,
    proceeds_eur     DECIMAL_TEXT,             -- net of sale fees
    cost_basis_eur   DECIMAL_TEXT,

    gross_gain_eur DECIMAL_TEXT,
    -- Realised currency effect. Part of the German taxable base, but a
    -- separate line in the owner's mental model.
    fx_gain_eur    DECIMAL_TEXT,

    vap_offset_eur       DECIMAL_TEXT NOT NULL DEFAULT '0',
    teilfreistellung_eur DECIMAL_TEXT NOT NULL DEFAULT '0',
    taxable_gain_eur     DECIMAL_TEXT,          -- signed; feeds allowance or loss pot
    holding_days         INTEGER,

    basis_quality TEXT NOT NULL DEFAULT 'EXACT'
                  CHECK (basis_quality IN ('EXACT', 'DERIVED', 'ESTIMATED'))
);

CREATE INDEX idx_disposal_lot ON lot_disposal (lot_id);
CREATE INDEX idx_disposal_txn ON lot_disposal (sell_txn_id);

-- ---------------------------------------------------------------------
-- Derived: interest accrual
-- ---------------------------------------------------------------------

CREATE TABLE interest_accrual (
    account_id INTEGER NOT NULL REFERENCES account (id),
    date       TEXT    NOT NULL,

    opening_balance_local DECIMAL_TEXT NOT NULL,
    rate                  DECIMAL_TEXT NOT NULL,
    day_count_frac        DECIMAL_TEXT NOT NULL,
    accrued_local         DECIMAL_TEXT NOT NULL,   -- gross, this day
    accrued_cum_local     DECIMAL_TEXT NOT NULL,   -- resets on payment
    fx_rate               DECIMAL_TEXT,
    accrued_cum_eur       DECIMAL_TEXT,

    PRIMARY KEY (account_id, date)
);

-- ---------------------------------------------------------------------
-- Derived: daily valuation
-- ---------------------------------------------------------------------

CREATE TABLE position_daily (
    date          TEXT    NOT NULL,
    account_id    INTEGER NOT NULL REFERENCES account (id),
    instrument_id INTEGER NOT NULL REFERENCES instrument (id),
    quantity      DECIMAL_TEXT NOT NULL,
    currency      TEXT    NOT NULL,

    price_local      DECIMAL_TEXT NOT NULL,
    price_asof       TEXT    NOT NULL,          -- earlier than `date` if forward-filled
    price_stale_days INTEGER NOT NULL DEFAULT 0,
    tr_price_local   DECIMAL_TEXT,

    fx_rate       DECIMAL_TEXT NOT NULL,        -- 1 for EUR, never NULL
    fx_asof       TEXT    NOT NULL,
    fx_stale_days INTEGER NOT NULL DEFAULT 0,

    market_value_local   DECIMAL_TEXT NOT NULL,
    market_value_eur     DECIMAL_TEXT NOT NULL,
    accrued_income_local DECIMAL_TEXT NOT NULL DEFAULT '0',
    accrued_income_eur   DECIMAL_TEXT NOT NULL DEFAULT '0',

    cost_basis_local DECIMAL_TEXT NOT NULL,
    cost_basis_eur   DECIMAL_TEXT NOT NULL,     -- at each lot's FROZEN acquisition rate
    -- The same cost re-translated at today's rate. The difference between this
    -- and cost_basis_eur is precisely the currency effect, which is what makes
    -- the level-view decomposition exact with no cross term.
    cost_basis_eur_at_spot DECIMAL_TEXT NOT NULL,

    unrealized_eur       DECIMAL_TEXT NOT NULL,  -- the true euro gain
    unrealized_local_eur DECIMAL_TEXT NOT NULL,  -- what the source export reports
    unrealized_fx_eur    DECIMAL_TEXT NOT NULL,  -- the gap between them

    ret_local_d DECIMAL_TEXT,
    ret_fx_d    DECIMAL_TEXT,
    ret_eur_d   DECIMAL_TEXT,

    valuation_status TEXT NOT NULL DEFAULT 'OK'
                     CHECK (valuation_status IN ('OK', 'STALE', 'MISSING')),

    PRIMARY KEY (date, account_id, instrument_id)
);

CREATE INDEX idx_position_daily_instrument ON position_daily (instrument_id, date);

CREATE TABLE portfolio_daily (
    date      TEXT    NOT NULL,
    person_id INTEGER NOT NULL REFERENCES person (id),

    market_value_eur   DECIMAL_TEXT NOT NULL,
    accrued_income_eur DECIMAL_TEXT NOT NULL DEFAULT '0',
    total_value_eur    DECIMAL_TEXT NOT NULL,
    cost_basis_eur     DECIMAL_TEXT NOT NULL,

    -- Unit accounting: the portfolio modelled as a fund. unit_price IS the
    -- time-weighted return index, and unlike a naive TWR it is correct in the
    -- presence of contributions.
    net_external_flow_eur DECIMAL_TEXT NOT NULL DEFAULT '0',
    units_issued          DECIMAL_TEXT NOT NULL DEFAULT '0',
    units_outstanding     DECIMAL_TEXT NOT NULL,
    unit_price            DECIMAL_TEXT NOT NULL,   -- base 100 at inception
    daily_return          DECIMAL_TEXT,

    -- Daily contributions. The residual holds cross terms and intraday
    -- trading; it is stored rather than absorbed, because a user who adds the
    -- parts and does not get the whole would rightly distrust the tool.
    ret_local_contrib DECIMAL_TEXT,
    ret_fx_contrib    DECIMAL_TEXT,
    ret_residual      DECIMAL_TEXT,

    drawdown_pct    DECIMAL_TEXT,
    fx_exposure_pct DECIMAL_TEXT,

    completeness      TEXT NOT NULL DEFAULT 'FULL' CHECK (completeness IN ('FULL', 'PARTIAL')),
    stale_instruments INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (date, person_id)
);

-- ---------------------------------------------------------------------
-- Derived: German tax state
-- ---------------------------------------------------------------------

CREATE TABLE allowance_entry (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES person (id),
    tax_year    INTEGER NOT NULL,
    source      TEXT    NOT NULL
                CHECK (source IN ('DIVIDEND', 'INTEREST', 'VORABPAUSCHALE', 'REALIZED_GAIN')),
    txn_id      INTEGER REFERENCES transaction_ (id),
    disposal_id INTEGER REFERENCES lot_disposal (id),

    -- POST-Teilfreistellung: the allowance actually consumed.
    amount_eur DECIMAL_TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('ACTUAL', 'FORECAST', 'PLANNED')),
    entry_date TEXT NOT NULL
);

CREATE INDEX idx_allowance_person_year ON allowance_entry (person_id, tax_year, status);

CREATE TABLE loss_pot (
    person_id INTEGER NOT NULL REFERENCES person (id),
    tax_year  INTEGER NOT NULL,
    -- AKTIEN losses offset only individual-share gains; SONSTIGE offsets any
    -- other investment income. Mixing them understates tax.
    pot       TEXT    NOT NULL CHECK (pot IN ('AKTIEN', 'SONSTIGE')),

    carried_in_eur  DECIMAL_TEXT NOT NULL DEFAULT '0',
    additions_eur   DECIMAL_TEXT NOT NULL DEFAULT '0',
    offsets_eur     DECIMAL_TEXT NOT NULL DEFAULT '0',
    carried_out_eur DECIMAL_TEXT NOT NULL DEFAULT '0',

    PRIMARY KEY (person_id, tax_year, pot)
);

CREATE TABLE vorabpauschale (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES account (id),
    instrument_id INTEGER NOT NULL REFERENCES instrument (id),
    tax_year      INTEGER NOT NULL,

    jan_first_price DECIMAL_TEXT,
    year_end_price  DECIMAL_TEXT,
    qty_held        DECIMAL_TEXT,
    basiszins       DECIMAL_TEXT NOT NULL,
    basisertrag_eur DECIMAL_TEXT NOT NULL,

    distributions_eur    DECIMAL_TEXT NOT NULL DEFAULT '0',
    full_months_before   INTEGER NOT NULL DEFAULT 0,
    teilfreistellung_eur DECIMAL_TEXT NOT NULL DEFAULT '0',
    taxable_eur          DECIMAL_TEXT NOT NULL,

    status      TEXT NOT NULL CHECK (status IN ('FORECAST', 'ACTUAL')),
    computed_at TEXT,

    UNIQUE (account_id, instrument_id, tax_year)
);

-- Statutory constants. Not derived -- seeded, and updated once a year.
CREATE TABLE tax_constant (
    tax_year  INTEGER PRIMARY KEY,
    basiszins DECIMAL_TEXT NOT NULL,
    kest_rate DECIMAL_TEXT NOT NULL DEFAULT '0.25',
    soli_rate DECIMAL_TEXT NOT NULL DEFAULT '0.055'
);

INSERT INTO tax_constant (tax_year, basiszins) VALUES
    (2024, '0.0229'),
    (2025, '0.0253'),
    (2026, '0.0320');
