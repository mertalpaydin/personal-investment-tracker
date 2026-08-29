-- =====================================================================
-- 0019 -- answers that survive closing the app.
--
-- The projection is deterministic: fixed seed, fixed blocks, so the same
-- question against the same ledger has the same answer every time. It was
-- cached in memory, which meant the first visit to the plan page in every
-- session paid for it again -- and since the app is started fresh each
-- time, that was very nearly every visit.
--
-- Keyed on a fingerprint of the ledger rather than on a clock. A
-- time-to-live would either serve a stale plan after a trade was entered
-- or expire a good one for nothing; a fingerprint invalidates the moment
-- the data moves and never otherwise.
--
-- `body` is the response verbatim. It is a cache and nothing derives from
-- it, so it is safe to truncate at any point -- which is what
-- `db/migrate.py` does on a rebuild, since a rebuild changes exactly the
-- figures these answers were computed from.
-- =====================================================================

CREATE TABLE cached_answer (
    -- What was asked: endpoint plus its parameters, already normalised by
    -- the caller. Opaque here on purpose; this table stores, it does not
    -- interpret.
    cache_key   TEXT NOT NULL,
    -- What the ledger looked like when it was answered. A row whose
    -- fingerprint no longer matches is simply never read again.
    fingerprint TEXT NOT NULL,
    body        TEXT NOT NULL,
    stored_at   TEXT NOT NULL,
    PRIMARY KEY (cache_key, fingerprint)
);

-- Sweeping superseded rows is by key, so this is the index that matters.
CREATE INDEX idx_cached_answer_key ON cached_answer (cache_key);
