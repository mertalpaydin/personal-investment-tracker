"""Automated rolling online backups using SQLite's backup API.

SQLite's ``Connection.backup`` copies pages while the database is live,
without deadlocking concurrent readers or leaving locks held.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MAX_BACKUPS = 7


def backup_path(db_path: Path, *, timestamp: datetime | None = None) -> Path:
    """Generate the timestamped path for a backup file in a 'backups' directory."""
    ts = (timestamp or datetime.now(UTC)).strftime("%Y%m%d_%H%M%S")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir / f"{db_path.stem}_{ts}.db"


def prune_backups(
    backup_dir: Path, *, prefix: str = "portfolio_", keep: int = DEFAULT_MAX_BACKUPS
) -> list[Path]:
    """Remove older backups in `backup_dir` beyond `keep` count."""
    if not backup_dir.exists():
        return []
    existing = sorted(
        [p for p in backup_dir.glob(f"{prefix}*.db") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for path in existing[keep:]:
        try:
            path.unlink(missing_ok=True)
            removed.append(path)
        except OSError as exc:
            log.warning("could not prune old backup %s: %s", path, exc)
    return removed


def list_backups(backup_dir: Path, *, prefix: str = "portfolio_") -> list[Path]:
    """Return all available backups in `backup_dir` ordered newest-first."""
    if not backup_dir.exists():
        return []
    return sorted(
        [p for p in backup_dir.glob(f"{prefix}*.db") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def create_backup(
    conn: sqlite3.Connection,
    target_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
    keep: int = DEFAULT_MAX_BACKUPS,
) -> Path | None:
    """Take an online atomic snapshot of `conn` into a backup file.

    Returns the backup path on success, or None if skipped (e.g. in-memory database).
    Prunes older backups to `keep` count.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    current_file = row[2] if row else ""
    if not current_file and not db_path:
        return None

    source_path = Path(db_path) if db_path else Path(current_file)
    destination = Path(target_path) if target_path else backup_path(source_path)

    log.info("creating database backup at %s", destination)
    dest_conn = sqlite3.connect(destination)
    try:
        with dest_conn:
            conn.backup(dest_conn)
    finally:
        dest_conn.close()

    prune_backups(destination.parent, prefix=f"{source_path.stem}_", keep=keep)
    return destination
