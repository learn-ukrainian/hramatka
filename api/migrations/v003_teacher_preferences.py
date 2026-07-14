"""Migration v003: Add teacher_preferences table for per-teacher defaults.

Supports default lesson duration persisting per authenticated teacher (P2-6).
Additive table only; does not alter pilot_teachers or lesson_jobs.
Mirrors v002's simple additive migration pattern from #110.
"""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS teacher_preferences (
            teacher_id TEXT PRIMARY KEY REFERENCES pilot_teachers(id),
            default_duration INTEGER NOT NULL DEFAULT 60
                CHECK (default_duration IN (45, 60, 90)),
            updated_at TEXT NOT NULL
        )
        """
    )
