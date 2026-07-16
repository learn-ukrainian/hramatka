"""Migration v005: distinguish durable generation failures from engine outages.

The two additions are intentionally narrow: ``generation_failed`` covers
allowlisted non-provider generation outcomes, and ``no_eligible_activities``
covers a completed generation that supplied no usable activity. SQLite needs a
table recreation to extend a CHECK constraint; every existing row is copied.
"""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Recreate lesson_jobs with the extended failure_code allowlist."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_jobs_new (
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            id TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_hash BLOB NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('draft', 'baking', 'ready', 'failed')),
            step TEXT NOT NULL CHECK (step IN (
                'текст отримано', 'завдання складено', 'перевірка', 'готово'
            )),
            failure_code TEXT NULL CHECK (failure_code IS NULL OR failure_code IN (
                'bake_timeout', 'worker_restarted', 'provider_unavailable',
                'engine_unavailable', 'lesson_schema_invalid', 'unknown_safe_failure',
                'lesson_floor_unmet', 'generation_failed', 'no_eligible_activities'
            )),
            failure_message TEXT NULL,
            lesson_json TEXT NULL,
            warning_acknowledgements_json TEXT NOT NULL DEFAULT '[]',
            accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
            accepted_at TEXT NULL,
            accepted_revision INTEGER NULL,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT NULL,
            completed_at TEXT NULL,
            progress_json TEXT NULL,
            PRIMARY KEY (teacher_id, id)
        )
        """
    )
    connection.execute("INSERT INTO lesson_jobs_new SELECT * FROM lesson_jobs")
    connection.execute("DROP TABLE lesson_jobs")
    connection.execute("ALTER TABLE lesson_jobs_new RENAME TO lesson_jobs")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lesson_jobs_claim_idx ON lesson_jobs(status, created_at, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lesson_jobs_catalog_idx "
        "ON lesson_jobs(teacher_id, updated_at DESC, id DESC)"
    )
