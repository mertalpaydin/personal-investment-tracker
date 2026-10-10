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


def test_restore_latest_healthy_backup_restores_and_archives(tmp_path: Path) -> None:
    source_db = tmp_path / "portfolio.db"
    conn = connect(source_db)
    conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO test_table (value) VALUES ('original')")
    conn.commit()

    backup_file = backup.create_backup(conn, db_path=source_db)
    assert backup_file is not None
    conn.close()

    # Corrupt the main db by writing garbage
    source_db.write_bytes(b"corrupted garbage bytes that are not sqlite")

    # Restore
    restored = backup.restore_latest_healthy_backup(source_db)
    assert restored is not None
    archive_path, backup_used = restored
    assert archive_path.exists()
    assert "corrupt" in archive_path.name
    assert backup_used == backup_file

    # Verify restored db is healthy and readable
    conn2 = connect(source_db, auto_heal=False)
    rows = conn2.execute("SELECT value FROM test_table").fetchall()
    assert rows[0][0] == "original"
    conn2.close()


def test_connect_auto_heals_corrupted_database(tmp_path: Path) -> None:
    source_db = tmp_path / "portfolio.db"
    conn = connect(source_db)
    conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO test_table (value) VALUES ('safe')")
    conn.commit()

    backup.create_backup(conn, db_path=source_db)
    conn.close()

    # Corrupt the database file
    source_db.write_bytes(b"corrupted database header")

    # connect with auto_heal=True should seamlessly restore
    conn_recovered = connect(source_db, auto_heal=True)
    rows = conn_recovered.execute("SELECT value FROM test_table").fetchall()
    assert rows[0][0] == "safe"
    conn_recovered.close()
