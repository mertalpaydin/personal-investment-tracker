# ADR 0011: Daily updates, scheduled from the page

**Status:** accepted
**Date:** 2026-08-16

## Context

Prices only ever arrived when somebody typed `pt fetch-prices`. There was no
cron, no scheduled workflow, no startup fetch and no lifespan hook;
`.github/workflows/ci.yml` is lint, typecheck, test and PII only, and never
touches the network for prices. `rebuild.run()` recomputes but does not fetch,
so opening the app on a fresh day re-valued yesterday's prices across a longer
date grid and produced a figure that looked current.

The database this was written against was two to three days behind, and nothing
on the page said so. That is the worst shape a data problem can take: every
number stays plausible, and nothing is obviously wrong.

The owner does not use a terminal, so "remember to run the fetch command" was
never going to be the answer.

## Decision

**A Windows Scheduled Task, registered by a button on the settings page.**

`jobs/update.py` does the work and is importable and testable on its own.
`jobs/daily_update.py` is the `-m` entry point the task runs.
`jobs/windows_task.py` wraps `schtasks.exe`. `POST /api/scheduler` registers it.

Four implementation choices, each with an obvious-looking alternative that
fails quietly:

**`/XML`, not `/TR`.** Embedding a quoted command inside a quoted argument is
fragile the moment a path contains a space, and `/TR` cannot express
`<WorkingDirectory>`, `<Hidden>`, or — the one that matters —
`<StartWhenAvailable>true</StartWhenAvailable>`, which is what makes the task
run after a laptop that was closed at the scheduled time. The XML file must be
written UTF-16LE with a BOM; `schtasks` rejects UTF-8 with "The task XML is
malformed", which is a misleading message for a well-formed document.

**`.venv\Scripts\pythonw.exe`, not `uv run`.** `uv run` re-resolves the lockfile
and can reach the network, so a task built on it fails exactly when the machine
is offline — one of the times a catch-up matters most. `pythonw` also means no
console window appears every evening. It has a sharp edge: under `pythonw`,
`sys.stdout` is `None` and `print()` raises `AttributeError`, so
`jobs/daily_update.py` must never print. A test parses the module's AST and
fails if it ever does.

**The window resumes from the last stored observation, not from yesterday.**
`prices.window_for(..., incremental=True)` starts five days before
`max(price.date)`. Combined with `<StartWhenAvailable>`, a machine that was off
for a fortnight comes back and fills the fortnight. Asking for "yesterday" would
leave a permanent hole.

**Registration state is read from `/Query /XML`, never from `/FO LIST`.** Those
field names are localised — a German Windows prints `Letzte Laufzeit` where the
parser would look for `Last Run Time`. Element names in the XML are not
localised. Run *history* comes from our own `update_run` table, which is more
informative anyway.

**Concurrency.** `PRAGMA busy_timeout = 15000` on every connection handles the
millisecond-scale collision between the job and an open browser tab. It is the
wrong tool for a job that takes minutes, so `core/locking.py` adds a
cross-process advisory lock, and the rule is *refuse, never queue*: a second
update writes a `SKIPPED` row and exits, because waiting would only mean doing
the same work later. Both lock backends release on process death, so there is
no stale-lock recovery to get wrong. The rebuild runs inside one transaction, so
under WAL a reader keeps the previous snapshot until the new one lands whole
rather than seeing `position_daily` emptied mid-rebuild.

**Reporting.** `update_run` records every run, including the ones that did
nothing. `PARTIAL` — some instruments updated, one provider was down — is a
distinct status from `FAILED`, because reporting a good day as a failure trains
the reader to ignore the status. A row left `RUNNING` is how a killed process
becomes detectable at all.

## Consequences

**Good.** The data advances without anyone remembering to advance it, and the
page says how current it is and when it last tried.

**A real widening of authority, accepted.** An HTTP request can now cause
`schtasks.exe` to run. This surface previously could not spawn anything. The
mitigations:

- the server binds to loopback only;
- the subprocess is invoked with an argv list and `shell=False`, so there is no
  shell to inject into;
- the only user-supplied value that reaches it is a time of day, matched
  against `^([01]\d|2[0-3]):[0-5]\d$` *before* being formatted into the XML,
  with tests for `25:00`, `18:30; shutdown` and similar;
- `InteractiveToken` / `LeastPrivilege`, so no `/RU SYSTEM` and no stored
  password.

**Bad.** This is the first Windows-specific code in the repository. It is
isolated to `jobs/windows_task.py`, guarded by `sys.platform`, and the settings
page degrades to an explanation on any other platform. `core/locking.py` has an
`fcntl` branch so CI stays green on Linux.

**Bad.** A scheduled task is state outside the repository. It survives a `git
clean` and points at an absolute path, so moving the checkout breaks it
silently. `GET /api/scheduler` reports the recorded command so a wrong one is at
least visible, and re-registering fixes it.
