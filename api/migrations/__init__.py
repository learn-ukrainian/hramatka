"""Ordered, transactional SQLite migrations for the frozen pilot schema."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .v001_pilot_schema import apply as apply_v001
from .v002_add_progress_column import apply as apply_v002
from .v003_teacher_preferences import apply as apply_v003
from .v004_floor_failure_code import apply as apply_v004
from .v005_generation_failure_codes import apply as apply_v005
from .v006_durable_teacher_sessions import apply as apply_v006
from .v007_local_static_sessions import apply as apply_v007
from .v008_teacher_passkeys import apply as apply_v008
from .v009_activity_feedback import apply as apply_v009
from .v010_activity_regenerations import apply as apply_v010
from .v011_anchor_capacity_failure_code import apply as apply_v011


class MigrationError(RuntimeError):
    """The database cannot be safely brought to the frozen pilot schema."""


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

MIGRATIONS: tuple[Migration, ...] = (
    (1, "pilot_schema", apply_v001),
    (2, "add_progress_column", apply_v002),
    (3, "add_teacher_preferences", apply_v003),
    (4, "floor_failure_code", apply_v004),
    (5, "generation_failure_codes", apply_v005),
    (6, "durable_teacher_sessions", apply_v006),
    (7, "local_static_sessions", apply_v007),
    (8, "teacher_passkeys", apply_v008),
    (9, "activity_feedback", apply_v009),
    (10, "activity_regenerations", apply_v010),
    (11, "anchor_capacity_failure_code", apply_v011),
)
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
