"""The entry point the Windows Scheduled Task runs. Never call print().

Under ``pythonw.exe`` -- which is what runs this, so no console window flashes
up every evening -- ``sys.stdout`` and ``sys.stderr`` are ``None``, and
``print()`` raises ``AttributeError: 'NoneType' object has no attribute
'write'``. A stray print here would turn a successful update into a task that
reports failure and a database that quietly stops advancing. Everything goes to
the log file instead. ``tests/test_jobs.py`` asserts this module contains no
``print(``.

Exit codes are the other half of the contract, because Task Scheduler's "Last
Result" column is the one piece of feedback visible without opening anything:

    0  everything updated
    1  updated, with problems (a provider was down)
    2  failed
    3  skipped -- another update was already running
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import date
from pathlib import Path

from portfolio_tracker.jobs import update

EXIT_CODES = {"OK": 0, "PARTIAL": 1, "FAILED": 2, "SKIPPED": 3}

#: Who asked for this run. Recorded on the `update_run` row so a reader can
#: tell an unattended catch-up from a button press.
TRIGGERS = ("SCHEDULED", "MANUAL", "CATCHUP", "BACKFILL")

#: Rotating, because this runs every day forever and an unbounded log on a
#: personal laptop is a slow leak nobody notices until it matters.
LOG_BYTES = 1_000_000
LOG_BACKUPS = 5


def _configure_logging(db_path: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(
        db_path.parent / "update.log",
        maxBytes=LOG_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch prices and recompute, unattended.")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--trigger", default="SCHEDULED", choices=TRIGGERS)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--no-rebuild", action="store_true")
    args = parser.parse_args(argv)

    db_path = args.db or update.env_db_path()
    if not db_path.exists():
        # Nothing to do and nothing to log to; the folder may not exist either.
        return EXIT_CODES["FAILED"]

    _configure_logging(db_path)
    log = logging.getLogger("portfolio_tracker.jobs.daily_update")
    log.info("starting %s update against %s", args.trigger, db_path)

    try:
        outcome = update.run_update(
            db_path,
            trigger=args.trigger,
            end=args.end,
            rebuild_after=not args.no_rebuild,
        )
    except BaseException as exc:
        log.exception("unexpected failure during %s update: %s", args.trigger, exc)
        return EXIT_CODES["FAILED"]

    log.info(
        "finished %s: %s prices, %s rates, %s instruments, rebuilt=%s, problems=%s",
        outcome.status,
        outcome.price_rows,
        outcome.fx_rows,
        outcome.instruments,
        outcome.rebuilt,
        len(outcome.problems),
    )
    for problem in outcome.problems:
        log.warning("problem: %s", problem)
    if outcome.error:
        log.error("error: %s", outcome.error)

    return EXIT_CODES.get(outcome.status, 2)


if __name__ == "__main__":
    sys.exit(main())
