"""Where the API key lives, and how it is kept out of everything else.

**It comes from `.env` if there is one, and the `setting` table otherwise.**
Putting it in a gitignored file is the more conventional place for a secret and
is what this repository's owner chose; the settings page remains for anyone who
would rather not edit files. `core/env.py` documents the precedence.

Either way the key ends up on the same disk as the database, and that is worth
stating plainly rather than dressing up: `data/portfolio.db` already holds this
household's entire financial history in the clear, in the same folder. A
free-tier API key beside it is not the weak link. Adding a keyring dependency
would buy protection only against an attacker who can read one file but not
another, which is not a threat that applies here.

What does earn its keep is making the key hard to leak *outward*:

- no endpoint ever returns it, only a hint like ``AQ.abcd…4f2b``;
- :func:`redact` runs over anything written to `llm_run` or a log;
- ``scripts/check_no_pii.py`` refuses a commit containing one, in the worktree
  and in the history.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from portfolio_tracker.core import env, settings
from portfolio_tracker.llm.adapter import LLMProvider
from portfolio_tracker.llm.providers.gemini import GeminiProvider
from portfolio_tracker.llm.providers.null import NullProvider

KEY_SETTING = "llm_api_key"
MODEL_SETTING = "llm_model"
ENABLED_SETTING = "llm_enabled"
CAP_SETTING = "llm_daily_call_cap"
VERIFIED_SETTING = "llm_key_verified_at"
SEND_AMOUNTS_SETTING = "llm_send_amounts"

#: Environment names, read from a real variable or from `.env`.
ENV_KEY = "GEMINI_API_KEY"
ENV_MODEL = "GEMINI_MODEL"

DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_DAILY_CAP = 50

#: A sanity check, not a format check. Google has shipped at least two key
#: formats -- an older `AIza…` of 39 characters and a newer `AQ.…` of 53 -- and
#: a rule that knows about today's rejects tomorrow's for a reason the person
#: pasting it can do nothing about. So this checks only for the mistakes a
#: human actually makes: a truncated paste, a URL, stray whitespace. Whether a
#: key works is the API's judgement, and its refusal is reported verbatim.
MIN_KEY_LENGTH = 20
KEY_LOOKS_WRONG = re.compile(r"\s|^https?://", re.I)

#: For scrubbing a key out of text when the exact value is not to hand. Both
#: known prefixes, and open-ended on length so a longer variant of either is
#: still caught.
KEY_IN_TEXT = re.compile(r"\b(?:AIza|AQ\.)[A-Za-z0-9_.\-]{15,}")


class KeyError_(ValueError):
    """The key was not accepted."""


def key_source(conn: sqlite3.Connection) -> str | None:
    """Where the key in use came from: 'env', 'settings', or None."""
    if env.present(ENV_KEY):
        return "env"
    return "settings" if settings.get(conn, KEY_SETTING) else None


def get_key(conn: sqlite3.Connection) -> str | None:
    """The key to use. `.env` wins over the settings page -- see core/env.py."""
    return env.get(ENV_KEY) or settings.get(conn, KEY_SETTING)


def set_key(conn: sqlite3.Connection, key: str) -> None:
    cleaned = key.strip()
    if len(cleaned) < MIN_KEY_LENGTH or KEY_LOOKS_WRONG.search(cleaned):
        raise KeyError_(
            "That does not look like an API key. Paste the whole key from "
            "Google AI Studio, with no spaces around it."
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
    cleaned = KEY_IN_TEXT.sub("…redacted…", text)
    if key:
        # The exact value is the reliable path; the pattern above is the
        # backstop for text that reached here without it.
        cleaned = cleaned.replace(key, "…redacted…")
    return cleaned


def model_source(conn: sqlite3.Connection) -> str:
    return "env" if env.present(ENV_MODEL) else "settings"


def model(conn: sqlite3.Connection) -> str:
    """Which model to ask. `.env` wins, for the same reason the key does."""
    return env.get(ENV_MODEL) or settings.get(conn, MODEL_SETTING, DEFAULT_MODEL) or DEFAULT_MODEL


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
        return NullProvider(
            f"No API key is set. Put one in .env as {ENV_KEY}, or add it in Settings."
        )
    return GeminiProvider(key, model=model(conn))
