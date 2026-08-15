-- =====================================================================
-- 0006 -- user settings, so the goal target is not a constant in the code.
--
-- The €120,000 / 2030 figures in the original brief were invented by the
-- document that proposed them. A number nobody chose has no business
-- driving a progress bar, so the target lives here, is set by its owner,
-- and carries the date it was set.
--
-- Deliberately a key/value table rather than a column per setting: these
-- are display preferences, not facts about the portfolio, and giving them
-- a schema would invite the derived layer to depend on them.
--
-- This is also the ONLY table the HTTP surface may write. Everything that
-- touches the ledger goes through a CLI command where an inference can be
-- reviewed first; a goal target is not an inference.
-- =====================================================================

CREATE TABLE setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The default the owner chose. Stored rather than defaulted in code, so
-- that "never set" and "set back to the default" stay distinguishable.
INSERT INTO setting (key, value, updated_at)
VALUES ('goal_target_eur', '200000', '2026-08-15T00:00:00Z');
