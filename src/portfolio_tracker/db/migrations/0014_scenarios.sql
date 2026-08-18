-- =====================================================================
-- 0014 -- scenarios worth keeping.
--
-- A scenario is a question, not an answer: "what if I put aside another
-- EUR 300 a month". The answer changes every time the portfolio moves, so
-- only the question is stored and it is re-run against whatever is true
-- now. Storing the outcome would produce a page of figures that were
-- right in March.
--
-- `levers` is JSON rather than a column each because the set of levers is
-- a product decision that will change, and a migration per lever is a
-- tax on changing one's mind about a screen. Nothing derives from it --
-- it is read, turned into a dataclass, and handed to the simulation.
-- =====================================================================

CREATE TABLE scenario (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    person_id  INTEGER NOT NULL REFERENCES person (id),
    levers     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 1 CHECK (pinned IN (0, 1))
);

CREATE INDEX idx_scenario_person ON scenario (person_id, pinned);
