# ADR 0010: The page leads, and the HTTP surface writes

**Status:** accepted
**Date:** 2026-08-16
**Supersedes in part:** ADR 0003 (scope cut), and the read-only invariant stated
in `0006_settings.sql` and `cli.py::serve`

## Context

Two earlier decisions, each defensible, combined into a tool its owner could
not use.

ADR 0003 cut the inherited plan to a shippable core and deferred the
presentation layer to "a minimal server-rendered debug UI". That page grew into
887 lines of hand-concatenated HTML in `api/render.py`: one long scrolling
document, no charts of any kind, and no way to reach a single holding.

Separately, `0006_settings.sql` recorded that `setting` was "the ONLY table the
HTTP surface may write", and `cli.py::serve` stated it plainly: *"Read-only.
Every write in this system goes through a reviewed CLI command, so there is
deliberately no HTTP path to the ledger."* The reasoning was sound. A dividend
inferred from a price gap is a guess, and a guess reaching the ledger without
somebody reading its derivation first is how a tracker quietly becomes wrong.

The consequence was not sound. The owner of this repository does not use a
command line — `claude.md` says so, `run.py`'s docstring says so at length —
and the result was a tool where:

- new prices arrived only when someone typed `pt fetch-prices`, so the database
  sat two to three days stale with nothing on the page saying so;
- a trade made on Tuesday could not be recorded at all until the broker
  published an export containing it, because there was no `POST
  /api/transactions` *and* no CLI command to enter a single trade either;
- a holding's performance over time was computed, stored, served by
  `GET /api/positions/{key}` — and drawn nowhere;
- everything sold was invisible: `valuation.py` writes a `position_daily` row
  only while quantity is positive, so the detail endpoint 404'd for any closed
  position;
- German statutory terms appeared untranslated throughout, to a reader who does
  not read German.

## Decision

**Everything reachable from the command line is also reachable from the page.**
The CLI stays — it is how this gets tested and debugged, and two ways to do a
thing is only a problem when they are two *implementations*. Every new endpoint
is a thin adapter over the same `core.*` function the CLI command already calls.

Concretely:

- A real front end under `api/static/`: plain HTML, CSS and ES modules with a
  hash router, and uPlot vendored unminified. No npm, no build step, no CDN;
  `python run.py` remains the only thing anyone runs. `render.py` is kept
  unchanged at `/debug` as the server-rendered ground truth.
- `POST /api/transactions` and friends, in `api/transactions.py`, with the row
  writing and the euro-leg freezing in `core/ledger.py` where ADR 0004's
  conversion rule can be enforced by module.
- A daily update job (`jobs/`) that fetches what is missing and can be
  scheduled from a button. See ADR 0011.
- `core/glossary.py`: plain English first, the German term in parentheses,
  never the German alone.

**What replaces the safety the CLI provided.** The old rule's real content was
*a person reads the derivation before it is committed*. Three things carry that
forward:

1. `POST /api/transactions/validate` is the primary endpoint of the write
   surface. Before a sale is saved it names the FIFO lots it will consume,
   their cost, the gain after advance-charge relief and partial exemption, and
   the allowance remaining afterwards. It writes nothing — it takes the
   read-only connection, so that is true by construction — and it returns 200
   even when it objects, because a form that 422s on every keystroke cannot
   validate as you type.
2. The preview and the save share one code path. Both go through
   `core.scenario.sell()`, the same function that prices the read-only scenario
   endpoint and drives the harvest planner. Two implementations of a tax chain
   is how a preview comes to disagree with the thing it previews.
3. **Inferences still do not reach the ledger unasked.** Nothing changed there.
   Dividends, interest and advance charges are still proposals in
   `pending_entry`, still show their full derivation, and still require a
   person to confirm or reject them. `VORABPAUSCHALE` is deliberately not an
   enterable transaction type.

## Consequences

**Good.** The tool answers its own questions. "How is this holding doing over
time" is a page rather than a JSON endpoint nobody calls. A trade is recorded
in the app that tracks it. A refusal — a missing base rate, an unavailable
exchange rate — arrives with the form that fixes it instead of a command to
type.

**Good, less obviously.** Several features were already built and had simply
never been drawn: Cariño attribution, look-through exposure, the goal
projection, the harvest planner. A meaningful share of this work was wiring, not
building.

**Bad.** The HTTP surface is larger and can now damage the ledger. The
mitigations are that every write is a decision a person made on the page, every
write recomputes the derived layer synchronously so nothing is left half-stated,
and `manual_entry_log` records what was done to a table nothing derives from.

**Bad.** `api/app.py` was already long and the front end adds files rather than
removing them. Split into routers (`transactions.py`, `admin.py`) rather than
allowed to keep growing.

**Accepted risk.** Deleting a hand-entered trade is a *hard* delete. A
`voided_at` flag would have to be honoured by `lots.py`, `cash.py`,
`returns.py`, `tax.py`, `attribution.py`, `vorabpauschale.py`, `valuation.py`
and every ad-hoc query in the API and the debug page; one missed filter is not a
visible bug but a tax figure that is quietly wrong. The ledger stays "what is
true", the audit trail lives where nothing reads it, and the delete response
returns the row so the page can offer an undo.

## Notes

A bug fixed on the way, worth recording because it was invisible rather than
subtle: `GET /api/positions/{import_key}` resolved through `position_daily` and
therefore 404'd for every holding ever sold. The performance of something you no
longer own is a completed fact, not a missing one. It now resolves the
instrument first and reports `closed: true` with the full history, realised gain
and income.
