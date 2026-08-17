"""Typed access to the key/value `setting` table.

Three modules already read this table with their own hand-written SELECT and
UPSERT, which was fine while there were two settings. The narration layer adds
six, several of them booleans and integers, and "is the string '0' truthy"
is not a question worth answering six times.

Existing callers are left alone deliberately: they work, and rewriting them
would put unrelated churn in a commit about something else.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else default


def get_bool(conn: sqlite3.Connection, key: str, *, default: bool = False) -> bool:
    raw = get(conn, key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_int(conn: sqlite3.Connection, key: str, *, default: int) -> int:
    raw = get(conn, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def set_value(conn: sqlite3.Connection, key: str, value: str | int | bool) -> None:
    """Store a setting. Booleans become '1'/'0', which is what the table holds."""
    stored = ("1" if value else "0") if isinstance(value, bool) else str(value)
    conn.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, stored, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )


def clear(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM setting WHERE key = ?", (key,))
