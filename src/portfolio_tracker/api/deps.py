"""Request-scoped database connections.

Split out of ``app.py`` so the routers added alongside it can depend on the
*same* two functions rather than each defining its own. FastAPI's
``dependency_overrides`` is keyed on function identity, so a second definition
of ``get_db`` would look identical, behave identically, and silently ignore
every override the tests install.

``app.py`` re-exports both names for the same reason: existing code and tests
import them from there.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from portfolio_tracker.api import cache
from portfolio_tracker.core import incoming
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect

#: Rebound by `run.py` and `pt serve` before the app starts serving, so the
#: page and the database always describe the same installation. Module-level
#: mutable state, which is unusual here and deliberate: the alternative is
#: threading a settings object through every route signature for one value
#: that is fixed before the first request and never changes after.
DB_PATH = Path(DEFAULT_DB_PATH)

#: The drop folder, set alongside DB_PATH for the same reason.
INCOMING_DIR = Path(incoming.DEFAULT_INCOMING)


def configure(*, db_path: Path | str, incoming_dir: Path | str | None = None) -> None:
    """Point the API at an installation. Call before serving the first request.

    The drop folder defaults to sitting beside the database, so a second
    database in another directory does not quietly share the first one's
    incoming files.
    """
    global DB_PATH, INCOMING_DIR
    DB_PATH = Path(db_path)
    INCOMING_DIR = Path(incoming_dir) if incoming_dir is not None else DB_PATH.parent / "incoming"
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)


def get_db() -> Iterator[Any]:
    """One connection per request, read-only, closed on the way out.

    The default, so that a route which only reports cannot write by accident.
    The endpoints that do write take ``get_writable_db`` instead, and that
    choice is visible in the signature rather than buried in the body.
    """
    conn = connect(DB_PATH, read_only=True, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def get_writable_db() -> Iterator[Any]:
    """The writable connection, taken only by endpoints that record something.

    Keeping the two separate means write access is something a route asks for,
    not something it inherits.
    """
    conn = connect(DB_PATH, same_thread=False)
    try:
        yield conn
    finally:
        # Anything that asked for a writable connection may have changed an
        # answer, so cached ones are dropped here rather than at each call
        # site. The alternative -- remembering to invalidate in every write
        # endpoint -- is a rule that holds until the day somebody adds one,
        # and a plan page showing figures from before the trade you just
        # entered is worse than a slow one. An in-place edit is also
        # invisible to the content fingerprint, which makes this the only
        # place that reliably notices.
        cache.clear()
        conn.close()
