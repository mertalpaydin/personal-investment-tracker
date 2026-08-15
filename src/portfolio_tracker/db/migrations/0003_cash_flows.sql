-- =====================================================================
-- 0003 -- cash flows across the portfolio boundary.
--
-- The source export records purchases and sales but never records the
-- account being funded, so roughly EUR 114,000 of contributions are
-- invisible. Left unmodelled, unit accounting reads every purchase as the
-- portfolio spontaneously growing, and reports the owner's own savings
-- back as investment return.
--
-- The model is a running cash balance: a sale credits it, a purchase
-- debits it, and a purchase that exceeds the balance implies a deposit of
-- the shortfall on that day. Proceeds waiting to be reinvested stay as
-- cash, so they count as held during the gap rather than vanishing and
-- reappearing.
--
-- Nothing here writes to transaction_ automatically. Inferences land in
-- pending_entry and become transactions only when confirmed.
-- =====================================================================

CREATE TABLE pending_entry (
    id   INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN (
        'EXTERNAL_IN', 'EXTERNAL_OUT', 'DIVIDEND', 'INTEREST', 'VORABPAUSCHALE')),

    account_id    TEXT,
    proposed_date TEXT NOT NULL,
    currency      TEXT NOT NULL,
    amount_local  DECIMAL_TEXT NOT NULL,

    -- Human-readable derivation, shown at review time. A proposal the user
    -- cannot check is a proposal they will rubber-stamp.
    basis      TEXT NOT NULL,
    derivation TEXT,

    -- The transaction that prompted this, where there is one.
    source_txn_id INTEGER REFERENCES transaction_ (id),

    status TEXT NOT NULL DEFAULT 'PROPOSED'
           CHECK (status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')),
    resulting_txn_id INTEGER REFERENCES transaction_ (id),

    created_at TEXT NOT NULL,
    decided_at TEXT,
    note       TEXT,

    -- One live proposal per source transaction and kind; re-running the
    -- generator must not queue the same inference twice.
    UNIQUE (kind, source_txn_id, proposed_date, amount_local)
);

CREATE INDEX idx_pending_status ON pending_entry (status, kind);

-- Whether a sale's proceeds stayed in the portfolio or left it. NULL means
-- nobody has said yet; the cash engine treats that as reinvestment (the
-- overwhelmingly common case) but the review command lists it as unresolved,
-- so an unanswered question stays visible rather than becoming an answer.
ALTER TABLE transaction_ ADD COLUMN proceeds_disposition TEXT
    CHECK (proceeds_disposition IN ('REINVEST', 'WITHDRAW'));

-- Materialised daily cash, so idle proceeds are a held position rather than a
-- gap in the portfolio's value.
CREATE TABLE cash_daily (
    date       TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES account (id),
    currency   TEXT NOT NULL,

    balance_local DECIMAL_TEXT NOT NULL,
    balance_eur   DECIMAL_TEXT,
    fx_rate       DECIMAL_TEXT,

    --: Consecutive days this balance has gone unspent. Surfaced past a
    --: threshold in case the money was actually withdrawn and never recorded.
    idle_days INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (date, account_id, currency)
);
