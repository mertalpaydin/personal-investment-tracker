-- =====================================================================
-- 0008 -- make the pending inbox actually idempotent.
--
-- 0003 declared UNIQUE (kind, source_txn_id, proposed_date, amount_local)
-- and three modules documented themselves as idempotent on the strength
-- of it. They were not. SQLite treats NULLs as distinct in a UNIQUE
-- constraint, so any proposal without a source transaction -- every
-- distribution, every modelled interest credit -- collided with nothing
-- and `INSERT OR IGNORE` inserted a duplicate every single run.
--
-- The failure mode is quiet and cumulative: re-running discovery after
-- adding a holding re-queues everything already waiting, the inbox fills
-- with identical entries, and confirming the list twice books the same
-- income twice. Nothing errors.
--
-- An expression index fixes it without touching the table. COALESCE gives
-- the NULLs a value they can collide on, and the sentinels are chosen so
-- they cannot be confused with real data: no row id is negative and no
-- account id is the empty string.
-- =====================================================================

CREATE UNIQUE INDEX ux_pending_dedup ON pending_entry (
    kind,
    ifnull(source_txn_id, -1),
    ifnull(account_id, ''),
    proposed_date,
    amount_local
);
