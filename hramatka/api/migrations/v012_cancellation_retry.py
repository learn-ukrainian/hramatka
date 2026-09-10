"""Migration v012: durable cancellation and retry-in-place attempt history."""

from __future__ import annotations

import sqlite3

from .v011_anchor_capacity_failure_code import (
    _TARGET_LESSON_COLUMNS as _V011_LESSON_COLUMNS,
)
from .v011_anchor_capacity_failure_code import _create_activity_regenerations_table

_TARGET_LESSON_COLUMNS = (
    *_V011_LESSON_COLUMNS,
    "attempt",
    "attempt_history_json",
    "retry_requested_revision",
)


def apply(connection: sqlite3.Connection) -> None:
    """Rebuild the job parent while preserving durable regeneration children."""
    lesson_jobs = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lesson_jobs'"
    ).fetchone()
    if lesson_jobs is None:
        return
    source_columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(lesson_jobs)").fetchall()
    )
    if frozenset(source_columns) != frozenset(_V011_LESSON_COLUMNS):
        raise sqlite3.DatabaseError(
            "Unsupported lesson_jobs shape for v012; refusing a lossy migration."
        )

    has_regenerations = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'activity_regenerations'"
    ).fetchone()
    _create_lesson_jobs_table(connection)
    connection.execute(
        "INSERT INTO lesson_jobs_new ("
        + ", ".join(_quote_identifier(column) for column in _TARGET_LESSON_COLUMNS)
        + ") SELECT "
        + ", ".join(
            [
                *(_quote_identifier(column) for column in _V011_LESSON_COLUMNS),
                "1",
                _backfilled_attempt_history_expression(),
                "NULL",
            ]
        )
        + " FROM lesson_jobs"
    )

    if has_regenerations is not None:
        _create_activity_regenerations_table(
            connection,
            table_name="activity_regenerations_new",
            parent_table="lesson_jobs_new",
        )
        connection.execute(
            "INSERT INTO activity_regenerations_new SELECT * FROM activity_regenerations"
        )
        connection.execute("DROP TABLE activity_regenerations")

    connection.execute("DROP TABLE lesson_jobs")
    connection.execute("ALTER TABLE lesson_jobs_new RENAME TO lesson_jobs")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lesson_jobs_claim_idx ON lesson_jobs(status, created_at, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lesson_jobs_catalog_idx "
        "ON lesson_jobs(teacher_id, updated_at DESC, id DESC)"
    )
    if has_regenerations is not None:
        connection.execute(
            "ALTER TABLE activity_regenerations_new RENAME TO activity_regenerations"
        )
        connection.execute(
            "CREATE UNIQUE INDEX activity_regenerations_one_active_lesson "
            "ON activity_regenerations(teacher_id, lesson_id) "
            "WHERE status IN ('queued', 'running')"
        )
        connection.execute(
            "CREATE INDEX activity_regenerations_queue "
            "ON activity_regenerations(status, created_at, id)"
        )
        connection.execute(
            "CREATE INDEX activity_regenerations_lesson_history "
            "ON activity_regenerations(teacher_id, lesson_id, created_at DESC, id DESC)"
        )


def _quote_identifier(identifier: str) -> str:
    if identifier not in _TARGET_LESSON_COLUMNS:
        raise ValueError("Unexpected lesson_jobs column")
    return f'"{identifier}"'


def _backfilled_attempt_history_expression() -> str:
    """Make existing terminal attempts visible without inventing provider detail."""
    return """
        CASE WHEN "status" IN ('ready', 'failed') THEN json_array(json_object(
            'attempt', 1,
            'status', "status",
            'failure_code', "failure_code",
            'started_at', "started_at",
            'completed_at', COALESCE("completed_at", "updated_at", "created_at"),
            'retry_requested_revision', NULL
        )) ELSE '[]' END
    """.strip()


def _create_lesson_jobs_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE lesson_jobs_new (
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            id TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_hash BLOB NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('draft', 'baking', 'ready', 'failed', 'cancelled')
            ),
            step TEXT NOT NULL CHECK (step IN (
                'текст отримано', 'завдання складено', 'перевірка', 'готово'
            )),
            failure_code TEXT NULL CHECK (failure_code IS NULL OR failure_code IN (
                'bake_timeout', 'worker_restarted', 'provider_unavailable',
                'engine_unavailable', 'lesson_schema_invalid', 'unknown_safe_failure',
                'lesson_floor_unmet', 'generation_failed', 'no_eligible_activities',
                'insufficient_anchor_capacity', 'cancelled'
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
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            attempt_history_json TEXT NOT NULL DEFAULT '[]',
            retry_requested_revision INTEGER NULL CHECK (
                retry_requested_revision IS NULL OR retry_requested_revision >= 1
            ),
            PRIMARY KEY (teacher_id, id)
        )
        """
    )
