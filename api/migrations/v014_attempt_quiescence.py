"""Migration v014: retain an attempt's quiescence across terminal transitions."""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Add the attempt quiescence marker without rebuilding lesson-job FKs."""
    lesson_jobs = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lesson_jobs'"
    ).fetchone()
    if lesson_jobs is None:
        return
    columns = {row[1] for row in connection.execute("PRAGMA table_info(lesson_jobs)").fetchall()}
    required = {"attempt_token", "cancelled_acknowledged_at"}
    if not required <= columns:
        raise sqlite3.DatabaseError(
            "Unsupported lesson_jobs shape for v014; refusing a lossy migration."
        )
    if "attempt_quiesced_at" not in columns:
        connection.execute("ALTER TABLE lesson_jobs ADD COLUMN attempt_quiesced_at TEXT NULL")
    # A migration is a process boundary: pre-v014 terminal rows have no live
    # in-process worker to acknowledge, so they are safe to retain/delete.
    connection.execute(
        """
        UPDATE lesson_jobs
        SET attempt_quiesced_at = COALESCE(
            attempt_quiesced_at, cancelled_acknowledged_at, completed_at, updated_at, created_at
        )
        WHERE status NOT IN ('draft', 'baking')
        """
    )
