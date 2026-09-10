"""Migration v013: bind worker effects and cancellation quiescence to an attempt."""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Add additive attempt-lease columns without rebuilding regeneration FKs."""
    lesson_jobs = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lesson_jobs'"
    ).fetchone()
    if lesson_jobs is None:
        return
    columns = {row[1] for row in connection.execute("PRAGMA table_info(lesson_jobs)").fetchall()}
    required = {
        "attempt",
        "attempt_history_json",
        "retry_requested_revision",
    }
    if not required <= columns:
        raise sqlite3.DatabaseError(
            "Unsupported lesson_jobs shape for v013; refusing a lossy migration."
        )
    for column, definition in (
        ("attempt_token", "TEXT NULL"),
        ("provider_started_at", "TEXT NULL"),
        ("cancelled_acknowledged_at", "TEXT NULL"),
    ):
        if column not in columns:
            connection.execute(f"ALTER TABLE lesson_jobs ADD COLUMN {column} {definition}")
    # Pre-v013 cancellations cannot have a live v013-bound worker after this
    # process boundary.  They are immediately safe to delete after migration.
    connection.execute(
        """
        UPDATE lesson_jobs
        SET cancelled_acknowledged_at = COALESCE(
            cancelled_acknowledged_at, completed_at, updated_at, created_at
        )
        WHERE status = 'cancelled'
        """
    )
