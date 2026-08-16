-- =====================================================================
-- 0012 -- an audit trail for trades entered and edited by hand.
--
-- Until now the only way a trade could enter this database was a broker CSV,
-- and the only way to correct one was to edit the export and re-import. The
-- page can now record a trade directly, which raises a question the importer
-- never had to answer: what happens to a row that was entered wrongly?
--
-- The answer is that it is deleted, and this table remembers it.
--
-- The alternative -- a `voided_at` column and a soft delete -- was rejected.
-- It would have to be honoured by lots.py, cash.py, returns.py, tax.py,
-- attribution.py, vorabpauschale.py, valuation.py and every ad-hoc query in
-- the API and the debug page. One missed filter is not a visible bug; it is a
-- tax figure that is quietly wrong. The ledger stays "what is true", and the
-- history of what a person did to it lives here, where nothing derives from
-- it and a missed filter is therefore impossible.
--
-- `txn_id` deliberately carries NO foreign key. The row it names may be gone,
-- and that is precisely the case this table exists to record.
-- =====================================================================

CREATE TABLE manual_entry_log (
    id       INTEGER PRIMARY KEY,
    at       TEXT NOT NULL,
    action   TEXT NOT NULL CHECK (action IN ('CREATE', 'UPDATE', 'DELETE')),
    txn_id   INTEGER,
    -- The row as written, JSON. On DELETE this is what an undo would re-post,
    -- which is why the whole row is kept rather than a summary.
    payload  TEXT NOT NULL,
    -- The row before an UPDATE or DELETE, JSON. NULL on CREATE.
    previous TEXT,
    note     TEXT
);

CREATE INDEX idx_manual_entry_at ON manual_entry_log (at DESC);
