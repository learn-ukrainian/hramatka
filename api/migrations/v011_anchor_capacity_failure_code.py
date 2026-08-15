"""Migration v011: expose preflight anchor-capacity failures distinctly.

``lesson_floor_unmet`` remains the non-blaming, retry-compatible outcome for
post-generation validation and serialization failures.  A deterministic source
preflight failure instead persists ``insufficient_anchor_capacity`` so the
teacher UI can truthfully ask for a longer, more varied source.
"""

from __future__ import annotations

import sqlite3

_TARGET_LESSON_COLUMNS = (
    "teacher_id",
    "id",
    "request_json",
    "request_hash",
    "status",
    "step",
    "failure_code",
    "failure_message",
    "lesson_json",
    "warning_acknowledgements_json",
    "accepted",
    "accepted_at",
    "accepted_revision",
    "revision",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
    "progress_json",
)
_FROZEN_LESSON_COLUMNS = frozenset(_TARGET_LESSON_COLUMNS[:-1])
_LEGACY_STORED_LESSON_COLUMNS = frozenset({"teacher_id", "id", "lesson_json"})
_LEGACY_TIMESTAMP = "1970-01-01T00:00:00Z"


def apply(connection: sqlite3.Connection) -> None:
    """Recreate lesson_jobs without cascading durable regeneration history."""
    lesson_jobs = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lesson_jobs'"
    ).fetchone()
    # Historical migration fixtures can intentionally contain only the session
    # tables relevant to their assertion.  There is no lesson data to upgrade.
    if lesson_jobs is None:
        return
    has_regenerations = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'activity_regenerations'"
    ).fetchone()
    insert_columns, select_expressions = _lesson_jobs_projection(connection)
    _create_lesson_jobs_table(connection, table_name="lesson_jobs_new")
    connection.execute(
        "INSERT INTO lesson_jobs_new ("
        + ", ".join(_quote_identifier(column) for column in insert_columns)
        + ") SELECT "
        + ", ".join(select_expressions)
        + " FROM lesson_jobs"
    )

    # v010 adds a composite FK with ON DELETE CASCADE.  Dropping the old
    # parent while that child exists would erase regeneration history under
    # PRAGMA foreign_keys=ON.  Copy the child to a table that instead points at
    # the copied parent, then remove the old pair and rename both replacements.
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
            """
            CREATE UNIQUE INDEX activity_regenerations_one_active_lesson
            ON activity_regenerations(teacher_id, lesson_id)
            WHERE status IN ('queued', 'running')
            """
        )
        connection.execute(
            """
            CREATE INDEX activity_regenerations_queue
            ON activity_regenerations(status, created_at, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX activity_regenerations_lesson_history
            ON activity_regenerations(teacher_id, lesson_id, created_at DESC, id DESC)
            """
        )


def _lesson_jobs_projection(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return an explicit, validated named-column projection into the v011 shape.

    v011 is normally applied to the v010 table (all frozen columns plus
    ``progress_json``).  A long-lived v009 fixture also models the older
    persisted-document-only table.  Both shapes are intentionally supported;
    any other missing or extra column set fails closed before a replacement
    table is created, rather than silently dropping unknown durable state.
    """
    source_columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(lesson_jobs)").fetchall()
    )
    source_set = frozenset(source_columns)
    if source_set == _FROZEN_LESSON_COLUMNS or source_set == frozenset(_TARGET_LESSON_COLUMNS):
        return (
            _TARGET_LESSON_COLUMNS,
            tuple(
                _quote_identifier(column) if column in source_set else "NULL"
                for column in _TARGET_LESSON_COLUMNS
            ),
        )
    if source_set == _LEGACY_STORED_LESSON_COLUMNS:
        return _TARGET_LESSON_COLUMNS, _legacy_stored_lesson_projection()
    raise sqlite3.DatabaseError(
        "Unsupported lesson_jobs shape for v011; refusing a lossy migration."
    )


def _legacy_stored_lesson_projection() -> tuple[str, ...]:
    """Backfill the documented pre-v009 stored-lesson table with safe values."""
    lesson_json = _quote_identifier("lesson_json")
    return (
        _quote_identifier("teacher_id"),
        _quote_identifier("id"),
        "'{}'",
        "X''",
        f"CASE WHEN {lesson_json} IS NULL THEN 'draft' ELSE 'ready' END",
        f"CASE WHEN {lesson_json} IS NULL THEN 'текст отримано' ELSE 'готово' END",
        "NULL",
        "NULL",
        lesson_json,
        "'[]'",
        "0",
        "NULL",
        "NULL",
        "1",
        f"'{_LEGACY_TIMESTAMP}'",
        f"'{_LEGACY_TIMESTAMP}'",
        "NULL",
        "NULL",
        "NULL",
    )


def _quote_identifier(identifier: str) -> str:
    """Quote an internally allowlisted SQLite identifier."""
    if identifier not in _TARGET_LESSON_COLUMNS:
        raise ValueError("Unexpected lesson_jobs column")
    return f'"{identifier}"'


def _create_lesson_jobs_table(connection: sqlite3.Connection, *, table_name: str) -> None:
    """Create the v011 lesson-job shape under a fixed migration-private name."""
    if table_name != "lesson_jobs_new":  # pragma: no cover - migration-internal invariant
        raise ValueError("Unexpected lesson_jobs migration table name")
    connection.execute(
        """
        CREATE TABLE lesson_jobs_new (
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
                'lesson_floor_unmet', 'generation_failed', 'no_eligible_activities',
                'insufficient_anchor_capacity'
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


def _create_activity_regenerations_table(
    connection: sqlite3.Connection, *, table_name: str, parent_table: str
) -> None:
    """Create the v010 child shape against the temporary v011 parent."""
    if (table_name, parent_table) != ("activity_regenerations_new", "lesson_jobs_new"):
        raise ValueError("Unexpected activity_regenerations migration table names")
    connection.execute(
        """
        CREATE TABLE activity_regenerations_new (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL,
            lesson_id TEXT NOT NULL,
            block_id TEXT NOT NULL,
            request_hash BLOB NOT NULL,
            base_revision INTEGER NOT NULL CHECK (base_revision >= 1),
            status TEXT NOT NULL CHECK (status IN (
                'queued', 'running', 'succeeded', 'failed'
            )),
            feedback TEXT NULL,
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            failure_code TEXT NULL CHECK (failure_code IS NULL OR failure_code IN (
                'provider_unavailable', 'generation_failed', 'engine_unavailable',
                'revision_conflict', 'block_changed', 'worker_restarted'
            )),
            failure_message TEXT NULL,
            old_block_hash TEXT NOT NULL CHECK (
                length(old_block_hash) = 64 AND old_block_hash NOT GLOB '*[^0-9a-f]*'
            ),
            old_block_json TEXT NOT NULL,
            new_block_hash TEXT NULL CHECK (
                new_block_hash IS NULL OR (
                    length(new_block_hash) = 64 AND new_block_hash NOT GLOB '*[^0-9a-f]*'
                )
            ),
            new_block_json TEXT NULL,
            logical_model_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            prompt_sha256 TEXT NOT NULL CHECK (
                length(prompt_sha256) = 64 AND prompt_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            applied_revision INTEGER NULL CHECK (
                applied_revision IS NULL OR applied_revision >= 2
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT NULL,
            completed_at TEXT NULL,
            FOREIGN KEY (teacher_id, lesson_id)
                REFERENCES lesson_jobs_new(teacher_id, id) ON DELETE CASCADE
        )
        """
    )
