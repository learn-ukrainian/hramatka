"""Migration v002: Add progress_json column to lesson_jobs table.

Supports optional per-call telemetry and honest job progress tracking.
"""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    # Add progress_json column to lesson_jobs
    connection.execute("ALTER TABLE lesson_jobs ADD COLUMN progress_json TEXT NULL")
