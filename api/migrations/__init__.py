"""Ordered, transactional SQLite migrations for the frozen pilot schema."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .v001_pilot_schema import apply as apply_v001


class MigrationError(RuntimeError):
    """The database cannot be safely brought to the frozen pilot schema."""


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

MIGRATIONS: tuple[Migration, ...] = ((1, "pilot_schema", apply_v001),)
EXPECTED_SCHEMA_VERSION = MIGRATIONS[-1][0]


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply every missing migration in order, one ``BEGIN IMMEDIATE`` at a time."""
    for version, name, apply in MIGRATIONS:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            already_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if already_applied is None:
                apply(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, _now_iso()),
                )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Return zero before bootstrap and the highest applied version otherwise."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if table is None:
        return 0
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()
    assert row is not None
    return int(row["version"])


def _now_iso() -> str:
    # Kept local to prevent the migration package depending on the store package.
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
