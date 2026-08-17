# SCHEDULED TASK DISABLED

**The daily automatic update is switched off.** It was removed on 2026-08-17 at
your request.

Nothing will fetch prices on its own. Every figure in the app is computed from
the newest price it has, so while this stays off the whole portfolio is valued
at whatever day you last updated it — and the app will say so rather than
pretend otherwise.

## What the task actually did

Worth being precise, because it is easy to assume from the name that this was
the AI feature. It was not.

The task ran once a day and did three things:

1. fetched any exchange rates missing since the last run,
2. fetched any share and fund prices missing since the last run,
3. recomputed the derived figures — positions, returns, tax state.

**It never called a language model.** Narration only happens when you press a
button on the page, and it is switched off by default in its own settings. The
two features are unrelated; removing this task does not affect narration, and
turning narration on does not schedule anything.

The task was named `PortfolioTrackerDailyUpdate` and ran
`.venv\Scripts\pythonw.exe -m portfolio_tracker.jobs.daily_update`, with no
console window, at 08:00 daily.

## Updating prices while it is off

Open the app and press **Update prices now** on the Settings page. It fetches
everything missing since the last time — not just today — so a gap of any
length is caught up in one press.

The Settings page also shows how old the prices are, and the alert strip at the
top of every page warns when holdings are being valued at carried-forward
prices.

## Turning it back on

**From the app** — Settings → *Update automatically every day* → pick a time →
**Turn it on**. That is all; nothing else is needed.

Evening suits it: European markets close at 17:30 CET and closing prices appear
shortly after. If the machine is off at that time the task runs at the next
opportunity and still picks up every day it missed.

Once registered the same panel offers **Test it now**, which asks Windows to run
it immediately so you can confirm it works rather than waiting a day. The result
appears in *Recent updates* underneath.

**Without the app**, if you ever want to:

```powershell
uv run python -c "from pathlib import Path; from portfolio_tracker.jobs import windows_task as w; print(w.register(repo_root=Path.cwd(), db_path=Path('data/portfolio.db'), at_time='18:30'))"
```

**To remove it again**, either use *Turn it off* on the Settings page, or:

```powershell
schtasks /Delete /TN PortfolioTrackerDailyUpdate /F
```

## Checking whether it is registered

Settings shows this at the top of the *Update automatically every day* panel.
From a terminal:

```powershell
schtasks /Query /TN PortfolioTrackerDailyUpdate
```

`ERROR: The system cannot find the file specified` means it is not registered,
which is the current state.

## Notes worth keeping

- The task is **state outside this repository**. It survives `git clean`, and it
  records an absolute path — so moving or renaming this folder breaks it
  silently. Re-register from Settings after a move.
- It runs **only when you are logged in** (`InteractiveToken`), and needs no
  password and no administrator rights.
- Every run is recorded in the `update_run` table and shown under *Recent
  updates*, including runs that did nothing. A run that fetched some prices and
  not others is reported as `PARTIAL`, which is a normal outcome when one data
  provider is briefly unavailable — not an error.
- Deleting this file changes nothing. It is a reminder, not configuration.

See `docs/adr/0011-scheduled-updates.md` for why it is built the way it is.
