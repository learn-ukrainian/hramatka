"""Initial frozen Hramatka pilot schema.

There is deliberately no lossy upgrade path from the pre-pilot prototype's
``lesson_jobs`` table: it has no teacher owner and its primary key cannot be
made owner-scoped without inventing ownership.  Refusing that database is the
only durable, non-disclosing migration behaviour.
"""

from __future__ import annotations

import sqlite3

_FROZEN_LESSON_COLUMNS = {
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
}


def apply(connection: sqlite3.Connection) -> None:
    _refuse_incompatible_legacy_lesson_table(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_teachers (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
            created_at TEXT NOT NULL,
            deactivated_at TEXT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_invites (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            token_hash BLOB NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            redeemed_at TEXT NULL,
            revoked_at TEXT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_sessions (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            invite_id TEXT NOT NULL UNIQUE REFERENCES pilot_invites(id),
            secret_hash BLOB NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_jobs (
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
                'engine_unavailable', 'lesson_schema_invalid', 'unknown_safe_failure'
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
            PRIMARY KEY (teacher_id, id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lesson_jobs_claim_idx ON lesson_jobs(status, created_at, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lesson_jobs_catalog_idx "
        "ON lesson_jobs(teacher_id, updated_at DESC, id DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS pilot_invites_teacher_idx ON pilot_invites(teacher_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS pilot_sessions_teacher_idx ON pilot_sessions(teacher_id)"
    )


def _refuse_incompatible_legacy_lesson_table(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'lesson_jobs'"
    ).fetchone()
    if row is None:
        return
    columns = {
        item["name"] for item in connection.execute("PRAGMA table_info(lesson_jobs)").fetchall()
    }
    table_info = connection.execute("PRAGMA table_info(lesson_jobs)").fetchall()
    primary_key = [
        item["name"] for item in sorted(table_info, key=lambda item: item["pk"]) if item["pk"]
    ]
    if _FROZEN_LESSON_COLUMNS.issubset(columns) and primary_key == ["teacher_id", "id"]:
        return
    raise RuntimeError(
        "Existing lesson_jobs table is incompatible with the owner-scoped pilot schema; "
        "refusing a lossy migration."
    )
