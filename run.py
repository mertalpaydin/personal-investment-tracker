"""Start the server and open the dashboard. Run this file; that is the whole UI.

Meant to be launched straight from an IDE -- the green arrow in VS Code or
PyCharm, or ``python run.py`` -- with no arguments and nothing to remember:

1. applies any pending schema migrations;
2. rebuilds the derived layer if a migration or a decision made it stale;
3. starts uvicorn on a port that is actually free;
4. waits until the server answers, *then* opens the browser.

Steps 1 and 2 used to be an error message telling the reader to go and type two
commands. That was wrong. The owner of this tool does not use the command line,
and a program that knows exactly what needs doing, is capable of doing it, and
instead prints instructions has simply moved its own work onto a person.

Both steps are safe to do unasked, which is the only reason they are done
unasked. A migration adds structure and never edits a figure; a rebuild deletes
and recomputes tables that are pure functions of the ledger. Neither can touch a
trade, a price, or anything a person confirmed. Anything that *could* -- booking
an inference, importing a file -- stays a decision and lives on the page.

Point 4 is the reason this is a script rather than a shell alias. Opening the
browser immediately races the server's startup, and the race is usually lost on
a cold start -- the user gets a connection-refused page and reloads by hand,
every single time. Waiting for a real HTTP response costs a few hundred
milliseconds and removes the failure entirely.

Ctrl-C stops it.
"""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "data" / "portfolio.db"
HOST = "127.0.0.1"
PREFERRED_PORT = 8000

#: How long to wait for the server to answer before opening the browser anyway.
#: Generous: a cold import of the app pulls in FastAPI and sqlite3 converters.
STARTUP_TIMEOUT_SECONDS = 20.0


def _free_port(preferred: int) -> int:
    """The preferred port if it is free, otherwise one the OS picks.

    A tracker that refuses to start because something else is on 8000 is a
    tracker that gets started less often.
    """
    with socket.socket() as probe:
        try:
            probe.bind((HOST, preferred))
        except OSError:
            pass
        else:
            return preferred
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def _wait_then_open(url: str, deadline: float) -> None:
    """Poll until the server answers, then open the browser exactly once."""
    while time.monotonic() < deadline:
        try:
            # Fixed scheme and host, both literals above -- not user input.
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    break
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.2)
    else:
        print(f"server did not answer within {STARTUP_TIMEOUT_SECONDS:.0f}s; opening anyway")
    webbrowser.open(url)


def _prepare() -> str | None:
    """Migrate, and rebuild if that left the derived layer behind.

    Returns None on success, or a message explaining what could not be fixed
    automatically. The distinction that matters: a migration failing is a
    genuine problem worth stopping for, while *needing* one is not.
    """
    from datetime import date

    from portfolio_tracker.core import inbox, rebuild
    from portfolio_tracker.db import migrate as migrations
    from portfolio_tracker.db.connection import connect

    with contextlib.closing(connect(DB_PATH)) as conn:
        try:
            applied = migrations.migrate(conn)
        except migrations.MigrationError as exc:
            # The checksum guard, almost always. Not something to paper over:
            # it means this database and this checkout disagree about history.
            return f"The database cannot be upgraded automatically.\n\n{exc}\n"

        if applied:
            print(f"applied schema update(s): {', '.join(f'{v:04d}' for v in applied)}")

        # A migration that adds a derived column leaves every existing row with
        # it empty, and the page would show blanks with no hint why. The same
        # is true after a decision has been confirmed.
        needs = bool(applied) or inbox.stale_after_decision(conn)
        if needs and conn.execute("SELECT count(*) FROM transaction_").fetchone()[0]:
            print("recomputing the derived figures...", flush=True)
            result = rebuild.run(conn, end=date.today())
            for problem in result.breaks:
                print(f"  {problem}", file=sys.stderr)
    return None


def main() -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "The API extras are not installed. Run:\n\n    uv sync --extra api\n", file=sys.stderr
        )
        return 1

    if not DB_PATH.exists():
        print(
            f"No database at {DB_PATH}.\n\nBuild one first:\n\n"
            "    uv run pt init-db\n"
            "    uv run pt import --open data/open.csv --closed data/closed.csv --account 1\n"
            "    uv run pt rebuild\n",
            file=sys.stderr,
        )
        return 1

    from portfolio_tracker.api import app as api

    if (problem := _prepare()) is not None:
        print(problem, file=sys.stderr)
        return 1

    api.DB_PATH = DB_PATH
    port = _free_port(PREFERRED_PORT)
    url = f"http://{HOST}:{port}/"
    if port != PREFERRED_PORT:
        print(f"port {PREFERRED_PORT} is busy; using {port}")

    # The opener runs alongside uvicorn.run(), which blocks. A daemon thread so
    # Ctrl-C during startup does not leave anything waiting on a dead server.
    threading.Thread(
        target=_wait_then_open,
        args=(url, time.monotonic() + STARTUP_TIMEOUT_SECONDS),
        daemon=True,
    ).start()

    print(f"Portfolio Tracker: {url}   (Ctrl-C to stop)", flush=True)
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl-C is how you stop it
        uvicorn.run(api.app, host=HOST, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
