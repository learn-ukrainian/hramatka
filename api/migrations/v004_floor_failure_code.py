"""Migration v004: Extend lesson_jobs failure_code CHECK to include 'lesson_floor_unmet'.

This is the dedicated code for floor BakeError paths (thin-source vs. non-blaming
generation/gate shortfall on sufficient source) per #174. The two paths carry
distinct cause-accurate UA messages but share the machine code.

Additive migration only. Uses a safe table-recreate + copy + rename + reindex
to extend the column CHECK constraint (SQLite has no ALTER for CHECK). All
existing data is preserved; no columns dropped, no destructive ops. Mirrors the
additive spirit of v002/v003. Migrations auto-apply on API startup.
"""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Recreate lesson_jobs with the extended failure_code allowlist."""
    # Full column list matches v001 CREATE + v002 progress_json.
    # New code added inside the CHECK IN (...) list.
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
                'lesson_floor_unmet'
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
    # Preserve every row exactly (progress_json may be NULL).
    connection.execute("INSERT INTO lesson_jobs_new SELECT * FROM lesson_jobs")
    connection.execute("DROP TABLE lesson_jobs")
    connection.execute("ALTER TABLE lesson_jobs_new RENAME TO lesson_jobs")

    # Re-create the lesson_jobs indexes (dropped with the old table).
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lesson_jobs_claim_idx ON lesson_jobs(status, created_at, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lesson_jobs_catalog_idx "
        "ON lesson_jobs(teacher_id, updated_at DESC, id DESC)"
    )
