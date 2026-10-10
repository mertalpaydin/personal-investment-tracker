"""Automated rolling online backups using SQLite's backup API.

SQLite's ``Connection.backup`` copies pages while the database is live,
without deadlocking concurrent readers or leaving locks held.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import suppress
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


def restore_latest_healthy_backup(
    db_path: Path | str,
    *,
    backups_dir: Path | str | None = None,
) -> tuple[Path, Path] | None:
    """Find the newest valid backup in `backups_dir`, archive the current file, and restore it.

    Returns (archived_corrupt_path, restored_backup_path) on success, or None if
    no healthy backup could be found.
    """
    target = Path(db_path)
    bdir = Path(backups_dir) if backups_dir else target.parent / "backups"
    if not bdir.exists():
        return None

    # Search newest to oldest
    candidates = list_backups(bdir, prefix=f"{target.stem}_")
    selected: Path | None = None
    for cand in candidates:
        try:
            with sqlite3.connect(f"file:{cand}?mode=ro", uri=True) as conn:
                res = conn.execute("PRAGMA quick_check(1);").fetchall()
                if len(res) == 1 and res[0][0] == "ok":
                    selected = cand
                    break
        except Exception:
            continue

    if not selected:
        return None

    # Archive the existing database if present
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    archive_path = target.parent / f"{target.stem}.corrupt-{ts}{target.suffix}"
    if target.exists():
        try:
            shutil.move(target, archive_path)
            log.warning("archived corrupted database %s to %s", target, archive_path)
        except OSError as exc:
            log.error("could not archive corrupted database %s: %s", target, exc)
            return None

    # Remove lingering WAL/SHM and OneDrive conflict files
    for stray_suffix in [
        f"{target.name}-wal",
        f"{target.name}-shm",
        f"{target.stem} (1).db-wal",
        f"{target.stem} (1).db-shm",
    ]:
        stray = target.parent / stray_suffix
        with suppress(OSError):
            stray.unlink(missing_ok=True)

    # Copy the healthy backup over
    shutil.copy2(selected, target)
    log.info("successfully restored %s from healthy backup %s", target, selected)
    return archive_path, selected
