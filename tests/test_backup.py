"""Unit tests for the automated database backup module."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from portfolio_tracker.db import backup
from portfolio_tracker.db.connection import connect


def test_create_backup_copies_data_and_schema(tmp_path: Path) -> None:
    source_db = tmp_path / "portfolio.db"
    conn = connect(source_db)
    conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO test_table (value) VALUES ('hello'), ('world')")
    conn.commit()

    backup_file = backup.create_backup(conn, db_path=source_db)
    assert backup_file is not None
    assert backup_file.exists()
    assert backup_file.parent == tmp_path / "backups"

    # Verify backup contents
    b_conn = sqlite3.connect(backup_file)
    rows = b_conn.execute("SELECT value FROM test_table ORDER BY id").fetchall()
    assert rows == [("hello",), ("world",)]
    b_conn.close()
    conn.close()


def test_prune_backups_keeps_specified_limit(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    files: list[Path] = []
    for i in range(10):
        p = backup_dir / f"portfolio_2026090{i}_000000.db"
        p.write_bytes(b"dummy")
        # Ensure distinct mtimes
        files.append(p)

    # Prune to 5
    removed = backup.prune_backups(backup_dir, keep=5)
    assert len(removed) == 5
    remaining = list(backup_dir.glob("portfolio_*.db"))
    assert len(remaining) == 5


def test_create_backup_skips_in_memory(tmp_path: Path) -> None:
    conn = connect(":memory:")
    res = backup.create_backup(conn)
    assert res is None
    conn.close()
