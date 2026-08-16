-- =====================================================================
-- 0011 -- the operational log: unattended updates, and calls to a model.
--
-- Both tables here record things that happen *to* the database rather than
-- facts it derives, which is why neither belongs in DERIVED_TABLES: throwing
-- them away on a rebuild would erase the only evidence of what ran and what
-- was sent. They are append-only in practice, and nothing computes a figure
-- from either.
--
-- The reason `update_run` exists at all: until now prices arrived only when
-- somebody typed `pt fetch-prices`, and nothing recorded that they had.
-- A tracker whose data quietly stops advancing, with no place to look, is
-- worse than one that never fetched -- the numbers stay plausible while
-- becoming wrong.
-- =====================================================================

CREATE TABLE update_run (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    -- SCHEDULED: the Windows task. MANUAL: the button on the page.
    -- CATCHUP: the app noticed on startup that data was behind.
    -- BACKFILL: history for one newly watched instrument.
    trigger         TEXT NOT NULL
                    CHECK (trigger IN ('SCHEDULED', 'MANUAL', 'CATCHUP', 'BACKFILL')),
    -- RUNNING is written before any work and updated at the end. A row left
    -- RUNNING is how a killed process is detectable at all; the alternative is
    -- a gap in the history that looks identical to "nothing was scheduled".
    -- SKIPPED means the advisory lock was held: correct, and not a failure.
    status          TEXT NOT NULL
                    CHECK (status IN ('RUNNING', 'OK', 'PARTIAL', 'FAILED', 'SKIPPED')),
    fx_rows         INTEGER NOT NULL DEFAULT 0,
    price_rows      INTEGER NOT NULL DEFAULT 0,
    instruments     INTEGER NOT NULL DEFAULT 0,
    -- Business days between the last price before the run and the last after.
    days_covered    INTEGER,
    last_price_date TEXT,
    rebuilt         INTEGER NOT NULL DEFAULT 0 CHECK (rebuilt IN (0, 1)),
    -- JSON array of strings, one per instrument or quote that failed. PARTIAL
    -- exists so that one dead provider does not present as a failed update:
    -- thirteen of fourteen instruments updating is a good day, not an outage.
    problems        TEXT,
    error           TEXT,
    host            TEXT
);

CREATE INDEX idx_update_run_started ON update_run (started_at DESC);

-- ---------------------------------------------------------------------
-- Calls to a language model.
--
-- `input_json` is stored verbatim (with the API key redacted, and nothing
-- else) because it is what makes "the model only narrates figures this tool
-- computed" a checkable claim rather than a promise. A reader can open a run
-- and confirm every number in the prose appears in the JSON it was given.
--
-- `input_sha256` is the cache key. The payload carries its own as_of, so the
-- hash changes exactly when the underlying data changes and not otherwise.
-- On a free tier that cache is not an optimisation -- a brief rendered on
-- every page load would exhaust the quota within a day without it.
-- ---------------------------------------------------------------------

CREATE TABLE llm_run (
    id            INTEGER PRIMARY KEY,
    task          TEXT NOT NULL
                  CHECK (task IN ('daily_brief', 'peer_scan', 'tax_explainer',
                                  'harvest_plan', 'verify')),
    -- What the task was about: an import_key, a tax year. Free text.
    subject       TEXT,
    created_at    TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    -- CAPPED and CACHED are recorded like any other outcome: a user asking
    -- "why is there no narrative today" deserves an answer, and "you have
    -- used 50 of 50 calls" is one.
    status        TEXT NOT NULL
                  CHECK (status IN ('OK', 'CACHED', 'UNAVAILABLE', 'REFUSED',
                                    'ERROR', 'CAPPED')),
    input_sha256  TEXT NOT NULL,
    input_json    TEXT NOT NULL,
    output_text   TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    latency_ms    INTEGER,
    http_status   INTEGER,
    error         TEXT
);

CREATE INDEX idx_llm_run_task_created ON llm_run (task, created_at DESC);
CREATE INDEX idx_llm_run_cache ON llm_run (input_sha256, status, created_at DESC);

-- ---------------------------------------------------------------------
-- Settings this migration introduces.
--
-- `llm_enabled` defaults to off. Sending portfolio totals and tax figures to
-- a third party is a decision the owner makes deliberately, not one they
-- discover has already been made for them.
-- ---------------------------------------------------------------------

-- A literal timestamp, not a function: this file is SHA-256 pinned and every
-- copy of the database must apply byte-identical statements. `updated_at`
-- here means "shipped with this migration", which is exactly true.
INSERT OR IGNORE INTO setting (key, value, updated_at) VALUES
    ('auto_catchup',        '1',                 '2026-08-16T00:00:00Z'),
    ('llm_enabled',         '0',                 '2026-08-16T00:00:00Z'),
    ('llm_model',           'gemini-3.6-flash',  '2026-08-16T00:00:00Z'),
    ('llm_daily_call_cap',  '50',                '2026-08-16T00:00:00Z'),
    ('llm_send_amounts',    '1',                 '2026-08-16T00:00:00Z');
