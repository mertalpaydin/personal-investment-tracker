"""Database connections, configured identically everywhere.

Two of these settings are not optional and are easy to omit:

``foreign_keys=ON``
    SQLite ignores foreign keys unless asked, *per connection*. A schema full of
    REFERENCES clauses that are never enforced is worse than none, because it
    reads as if referential integrity is guaranteed.

``PARSE_DECLTYPES``
    Without it the Decimal converter never runs and every monetary column comes
    back as ``str``, which then compares and sorts lexically. ``'9' > '10'``.

``busy_timeout``
    WAL allows one writer and many readers, but a reader that arrives mid-write
    still gets ``database is locked`` *immediately* unless told to wait. Once a
    scheduled task can update prices while the page is open, that collision
    stops being hypothetical.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from portfolio_tracker.money import register_decimal_support

DEFAULT_DB_PATH = Path("data/portfolio.db")

#: How long a statement waits for a lock before giving up. Fifteen seconds is
#: far longer than any single write here takes, and far shorter than a user
#: will wait before deciding the page is broken. The long-running case -- a
#: whole daily update -- is guarded by `core.locking` instead, because a busy
#: timeout is the wrong tool for something that takes minutes.
BUSY_TIMEOUT_MS = 15_000


def connect(
    path: Path | str = DEFAULT_DB_PATH,
    *,
    read_only: bool = False,
    same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a configured connection, creating the parent directory if needed.

    ``same_thread=False`` is for the web server and nothing else. A sync FastAPI
    dependency and the endpoint it feeds both run on anyio's worker pool, which
    reuses threads but does not pin them -- so the connection opened by the
    dependency is routinely handed to a *different* thread than the one that
    queries it. sqlite3's default guard rejects that, and the failure surfaces as
    a 500 on every request rather than at startup. Relaxing it is safe only
    because each request owns its connection outright: the threads take turns,
    they never share.
    """
    register_decimal_support()

    if str(path) == ":memory:":
        conn = sqlite3.connect(
            ":memory:", detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=same_thread
        )
    else:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            conn = sqlite3.connect(
                f"file:{target}?mode=ro",
                uri=True,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=same_thread,
            )
        else:
            conn = sqlite3.connect(
                target, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=same_thread
            )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Set on read-only connections too: a reader is exactly what collides with
    # the update job's commit, and it is the reader that must be patient.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not read_only:
        # WAL survives across connections and is a property of the file, so this
        # is a no-op after the first write connection. In-memory databases do
        # not support it and silently stay in "memory" journal mode.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction, rolling back on any exception.

    Python's sqlite3 autocommits DDL and leaves implicit transaction handling
    surprising enough that being explicit is worth the four lines.
    """
    try:
        conn.execute("BEGIN")
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
