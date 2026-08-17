"""Reading `.env`, for the settings that are secrets.

A twenty-line parser rather than a dependency. `python-dotenv` would be a
fifth runtime package for something with no edge cases worth outsourcing, and
`pydantic-settings` -- which this project already declares and has never used
-- brings a whole settings framework to read two strings.

Precedence, highest first:

1. a real environment variable, so a scheduled task or a shell can override
   anything without editing a file;
2. `.env` in the repository root, which is gitignored and is where a key
   belongs on a single-user machine;
3. the database, which is what the settings page writes.

The database losing to the file is deliberate. Somebody who has put a key in
`.env` has said where they want it kept, and a settings page that silently
overrode that would be the more surprising behaviour of the two.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

#: The repository root: core/ -> portfolio_tracker/ -> src/ -> here.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV = REPO_ROOT / ".env"


def parse(text: str) -> dict[str, str]:
    """Parse `.env` contents. Blank lines and `#` comments are skipped.

    Handles `export KEY=value` and strips one layer of matching quotes. It does
    not do interpolation, multi-line values or anything else that would make a
    reader unsure what a line means -- a secrets file should be boring.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, str]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        return parse(target.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


def get(name: str, *, default: str | None = None, path: Path | None = None) -> str | None:
    """The value of ``name`` from the environment or `.env`."""
    from_process = os.environ.get(name)
    if from_process:
        return from_process
    value = _load(str(path or DEFAULT_ENV)).get(name)
    return value if value else default


def present(name: str, *, path: Path | None = None) -> bool:
    """Whether the environment supplies this at all."""
    return bool(get(name, path=path))


def reload() -> None:
    """Forget the cached file. For tests, and for a key edited while running."""
    _load.cache_clear()
