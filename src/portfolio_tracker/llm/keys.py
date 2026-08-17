"""Where the API key lives, and how it is kept out of everything else.

**It goes in the `setting` table**, and the reasoning is worth stating plainly
rather than dressing up. `data/portfolio.db` already holds this household's
entire financial history in the clear, in the same folder. A free-tier API key
sitting beside it is not the weak link, and adding a keyring dependency would
buy protection only against an attacker who can read one file but not another
-- which is not a threat that applies here. It would also add a new way for
the thing to fail on a machine whose owner has already said they will not edit
files.

What does earn its keep is making the key hard to leak *outward*:

- no endpoint ever returns it, only a hint like ``AIza…4f2b``;
- :func:`redact` runs over anything written to `llm_run` or a log;
- ``scripts/check_no_pii.py`` refuses a commit containing one, in the worktree
  and in the history.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from portfolio_tracker.core import settings
from portfolio_tracker.llm.adapter import LLMProvider
from portfolio_tracker.llm.providers.gemini import GeminiProvider
from portfolio_tracker.llm.providers.null import NullProvider

KEY_SETTING = "llm_api_key"
MODEL_SETTING = "llm_model"
ENABLED_SETTING = "llm_enabled"
CAP_SETTING = "llm_daily_call_cap"
VERIFIED_SETTING = "llm_key_verified_at"
SEND_AMOUNTS_SETTING = "llm_send_amounts"

DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_DAILY_CAP = 50

#: Google AI Studio keys. Used to reject a pasted mistake early -- an account
#: id, a URL, half a key -- rather than after a round trip.
KEY_SHAPE = re.compile(r"^AIza[0-9A-Za-z_\-]{35}$")

#: The same shape, unanchored, for scrubbing it out of text.
KEY_IN_TEXT = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")


class KeyError_(ValueError):
    """The key was not accepted."""


def get_key(conn: sqlite3.Connection) -> str | None:
    return settings.get(conn, KEY_SETTING)


def set_key(conn: sqlite3.Connection, key: str) -> None:
    cleaned = key.strip()
    if not KEY_SHAPE.match(cleaned):
        raise KeyError_(
            "That does not look like a Google AI Studio key. They begin 'AIza' "
            "and are 39 characters long."
        )
    settings.set_value(conn, KEY_SETTING, cleaned)
    settings.clear(conn, VERIFIED_SETTING)


def clear_key(conn: sqlite3.Connection) -> None:
    settings.clear(conn, KEY_SETTING)
    settings.clear(conn, VERIFIED_SETTING)


def mark_verified(conn: sqlite3.Connection) -> None:
    settings.set_value(conn, VERIFIED_SETTING, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def hint(key: str | None) -> str | None:
    """Enough to recognise which key is stored, not enough to use it."""
    if not key:
        return None
    return f"{key[:8]}…{key[-4:]}" if len(key) > 14 else "…"


def redact(text: str, key: str | None = None) -> str:
    """Remove any API key from text on its way into a log or a table."""
    if not text:
        return text
    cleaned = KEY_IN_TEXT.sub("AIza…redacted", text)
    if key:
        cleaned = cleaned.replace(key, "AIza…redacted")
    return cleaned


def model(conn: sqlite3.Connection) -> str:
    return settings.get(conn, MODEL_SETTING, DEFAULT_MODEL) or DEFAULT_MODEL


def enabled(conn: sqlite3.Connection) -> bool:
    return settings.get_bool(conn, ENABLED_SETTING, default=False)


def daily_cap(conn: sqlite3.Connection) -> int:
    return settings.get_int(conn, CAP_SETTING, default=DEFAULT_DAILY_CAP)


def send_amounts(conn: sqlite3.Connection) -> bool:
    return settings.get_bool(conn, SEND_AMOUNTS_SETTING, default=True)


def provider_for(conn: sqlite3.Connection) -> LLMProvider:
    """The provider to use right now, or one that politely always refuses.

    Off and unconfigured both return a :class:`NullProvider`, so there is a
    single code path for "no narrative" and it is the one exercised by default
    -- narration ships switched off.
    """
    if not enabled(conn):
        return NullProvider("Narration is switched off. Turn it on in Settings.")
    key = get_key(conn)
    if not key:
        return NullProvider("No API key is set. Add one in Settings.")
    return GeminiProvider(key, model=model(conn))
