"""Recording every call, and not making most of them.

The cache here is not an optimisation. A daily brief rendered whenever the
overview is opened would exhaust a free tier's daily quota before lunch, and
the feature would appear to work for a day and then stop. Keyed on a hash of
the exact payload -- which carries its own ``as_of`` -- a cached answer is
returned whenever the underlying figures have not changed, and a fresh one
whenever they have. That is the same condition under which the prose would
have been identical anyway.

`llm_run` stores the input JSON verbatim, redacted only of the API key. That
is what turns "the model only narrates figures this tool computed" from a
promise into something a reader can check: open a run, and every number in the
prose is either in the JSON above it or it was invented.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

#: How long a cached narration stays good. Twelve hours, so a brief written
#: this morning is reused this afternoon but never survives a day's new prices.
CACHE_HOURS = 12.0

#: Shortest gap between two live calls. Short on purpose: this is a
#: double-click guard, not a rate limiter. Every narration here is a deliberate
#: click, the provider already backs off on a real 429, and the daily cap
#: bounds total spend -- so refusing somebody who asked for a brief and then a
#: tax explanation would cost more than it saves.
MIN_INTERVAL_S = 2.0


@dataclass(frozen=True)
class Usage:
    calls_today: int
    daily_cap: int
    seconds_since_last: float | None

    @property
    def capped(self) -> bool:
        return self.calls_today >= self.daily_cap

    @property
    def too_soon(self) -> bool:
        return self.seconds_since_last is not None and self.seconds_since_last < MIN_INTERVAL_S


def fingerprint(payload: dict[str, Any]) -> str:
    """A stable hash of a payload, whatever order its keys arrived in."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cached(
    conn: sqlite3.Connection, *, task: str, input_sha256: str, max_age_hours: float = CACHE_HOURS
) -> sqlite3.Row | None:
    """The most recent good answer for this exact input, if it is still fresh."""
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(  # type: ignore[no-any-return]
        "SELECT * FROM llm_run WHERE task = ? AND input_sha256 = ? "
        "AND status IN ('OK', 'CACHED') AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (task, input_sha256, cutoff),
    ).fetchone()


def usage(conn: sqlite3.Connection, *, daily_cap: int) -> Usage:
    """How much of today's allowance has gone, and how recently."""
    today = date.today().isoformat()
    calls = conn.execute(
        "SELECT count(*) AS n FROM llm_run WHERE status = 'OK' AND created_at >= ?",
        (f"{today}T00:00:00Z",),
    ).fetchone()["n"]

    row = conn.execute("SELECT max(created_at) AS last FROM llm_run WHERE status = 'OK'").fetchone()
    since: float | None = None
    if row is not None and row["last"]:
        try:
            last = datetime.strptime(str(row["last"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            since = (datetime.now(UTC) - last).total_seconds()
        except ValueError:
            since = None

    return Usage(calls_today=int(calls), daily_cap=daily_cap, seconds_since_last=since)


def record(
    conn: sqlite3.Connection,
    *,
    task: str,
    subject: str | None,
    provider: str,
    model: str,
    status: str,
    input_sha256: str,
    input_json: str,
    output_text: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    http_status: int | None = None,
    error: str | None = None,
) -> int:
    """Write one row. Called for refusals and failures too, not only successes.

    A run that produced nothing is exactly the run somebody will later want to
    understand, so "no narrative today" has an answer beyond a shrug.
    """
    cursor = conn.execute(
        """
        INSERT INTO llm_run (
            task, subject, created_at, provider, model, status, input_sha256,
            input_json, output_text, input_tokens, output_tokens, latency_ms,
            http_status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task,
            subject,
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            provider,
            model,
            status,
            input_sha256,
            input_json,
            output_text,
            input_tokens,
            output_tokens,
            latency_ms,
            http_status,
            error,
        ),
    )
    return int(cursor.lastrowid or 0)


def history(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM llm_run ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
