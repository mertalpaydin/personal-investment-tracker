"""Numbered SQL migrations with a checksum guard.

The inherited `schema.sql` could not be applied twice: no `IF NOT EXISTS`, no
version table, and addenda that used `ALTER TABLE`. It only worked once, against
an empty file, and could not express a second change.

This replaces it with numbered files and a `schema_version` table that records a
SHA-256 of each file *as applied*. Editing an already-applied migration then
makes the runner **refuse** rather than silently diverge -- the failure mode
being guarded against is two databases with the same version number and
different shapes, which is close to impossible to diagnose later.

Not Alembic: SQLite, one developer and no ORM make that pure ceremony.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

#: Tables holding nothing that cannot be recomputed from transactions, prices,
#: FX rates and account terms. `reset_derived` truncates exactly these, which is
#: what makes "store facts, derive everything else" operational: any bug in the
#: derived layer is fixed by rebuilding, never by patching rows.
DERIVED_TABLES: tuple[str, ...] = (
    "lot_disposal",
    "lot",
    "position_daily",
    "portfolio_daily",
    "interest_accrual",
    "allowance_entry",
    "loss_pot",
    "vorabpauschale",
)


class MigrationError(RuntimeError):
    """Raised when the migration state on disk and in the database disagree."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        # Normalise line endings so a Windows checkout and a Linux CI runner
        # agree. Without this the guard fires spuriously on every clone.
        content = self.path.read_bytes().replace(b"\r\n", b"\n")
        return hashlib.sha256(content).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return migrations ordered by version, rejecting duplicates and gaps."""
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(
                f"migration filename must be NNNN_lower_snake.sql, got {path.name!r}"
            )
        found.append(Migration(int(match.group(1)), match.group(2), path))

    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions: {versions}")
    if versions and versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be contiguous from 1, got {versions}")
    return found


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            checksum   TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def applied(conn: sqlite3.Connection) -> dict[int, str]:
    """Map of version -> checksum for migrations already applied."""
    _ensure_version_table(conn)
    rows = conn.execute("SELECT version, checksum FROM schema_version").fetchall()
    return {row["version"]: row["checksum"] for row in rows}


def _verify_history(conn: sqlite3.Connection, migrations: list[Migration]) -> None:
    """Refuse to proceed if an already-applied migration has been edited."""
    known = applied(conn)
    by_version = {m.version: m for m in migrations}

    for version, recorded in sorted(known.items()):
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"database has migration {version:04d} applied, but no such file exists. "
                "The database is newer than this checkout."
            )
        if migration.checksum != recorded:
            raise MigrationError(
                f"migration {version:04d}_{migration.name}.sql has changed since it was "
                f"applied to this database.\n"
                f"  recorded: {recorded[:16]}...\n"
                f"  on disk:  {migration.checksum[:16]}...\n"
                "Applied migrations are immutable. Add a new migration instead; if this "
                "database is disposable, delete it and re-run."
            )


def migrate(conn: sqlite3.Connection, *, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every pending migration. Returns the versions applied, in order.

    Idempotent: a second call with nothing pending returns an empty list.
    Each migration runs in its own transaction -- SQLite DDL is transactional,
    so a failure rolls back cleanly rather than leaving a half-built schema.
    """
    migrations = discover(directory)
    _verify_history(conn, migrations)
    already = applied(conn)

    newly_applied: list[int] = []
    for migration in migrations:
        if migration.version in already:
            continue
        checksum = migration.checksum
        try:
            conn.execute("BEGIN")
            conn.executescript(migration.sql)
            conn.execute(
                "INSERT INTO schema_version (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    checksum,
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        # Mirror into user_version so the schema level is visible to any tool
        # that has never heard of this project.
        conn.execute(f"PRAGMA user_version = {migration.version}")
        newly_applied.append(migration.version)

    return newly_applied


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration, or 0 for an empty database."""
    known = applied(conn)
    return max(known) if known else 0


def reset_derived(conn: sqlite3.Connection) -> dict[str, int]:
    """Empty every recomputable table. Returns rows deleted per table.

    Deliberately does NOT touch `transaction_`, `price`, `fx_rate`,
    `instrument`, `account`, `person` or `tax_constant` -- those are facts.
    Everything else is a cached derivation and is safe to throw away.
    """
    deleted: dict[str, int] = {}
    try:
        conn.execute("BEGIN")
        for table in DERIVED_TABLES:
            before = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            conn.execute(f"DELETE FROM {table}")
            deleted[table] = before
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return deleted
