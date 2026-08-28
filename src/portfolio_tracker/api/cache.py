"""Remembering answers that cannot have changed.

The projection is deterministic -- fixed seed, fixed blocks -- so the same
question against the same ledger has the same answer every time. Recomputing
it on every visit to the plan page is a few hundred milliseconds of simulation
per panel, three panels deep, for a figure that only moves when the data does.

**Keyed on the data, not on a clock.** A time-to-live would either serve a
stale plan after a trade was entered or expire a good one for nothing. The key
carries a fingerprint of the ledger instead, so a write invalidates every
cached answer the moment it lands and nothing else does. Getting this wrong in
the other direction is what makes a tracker untrustworthy: a page that quietly
shows figures from before the trade you just recorded is worse than a slow one.

The fingerprint is deliberately cheap -- counts and maxima over indexed
columns -- because it runs on every request. It does not attempt to notice an
in-place edit that changes no count and no maximum; `updated_at`-style columns
would be needed for that, and the tables here are append-mostly.
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from typing import Any

#: Entries kept before the oldest is dropped. The plan page uses a handful of
#: parameter combinations; this is sized so ordinary use never evicts.
MAX_ENTRIES = 64

#: Only endpoint bodies go in here, and they are all JSON objects.
Key = tuple[Any, ...]
Body = dict[str, Any]

_CACHE: OrderedDict[Key, Body] = OrderedDict()

#: Tables whose contents can change an answer. Each contributes a count and a
#: maximum, which is enough to notice an insert, a delete, or a rebuild.
_WATCHED: tuple[tuple[str, str], ...] = (
    ("transaction_", "id"),
    ("portfolio_daily", "date"),
    ("position_daily", "date"),
    ("opening_balance", "set_at"),
    ("setting", "updated_at"),
)


def fingerprint(conn: sqlite3.Connection) -> str:
    """A short string that changes whenever a cached answer could.

    Missing tables count as absent rather than raising: this runs before any
    endpoint's own error handling, and a half-migrated database should still
    reach the message that says so.
    """
    parts: list[str] = []
    for table, column in _WATCHED:
        try:
            row = conn.execute(f"SELECT count(*) AS n, max({column}) AS hi FROM {table}").fetchone()
        except sqlite3.Error:
            parts.append(f"{table}:-")
            continue
        parts.append(f"{table}:{row['n']}:{row['hi']}")
    return "|".join(parts)


def get(key: Key) -> Body | None:
    """The stored answer for this key, or None. Refreshes its recency."""
    if key not in _CACHE:
        return None
    _CACHE.move_to_end(key)
    return _CACHE[key]


def put(key: Key, value: Body) -> Body:
    """Store an answer, evicting the least recently used if full."""
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > MAX_ENTRIES:
        _CACHE.popitem(last=False)
    return value


def clear() -> None:
    """Forget everything. For tests, and for a rebuild that swaps the file."""
    _CACHE.clear()
