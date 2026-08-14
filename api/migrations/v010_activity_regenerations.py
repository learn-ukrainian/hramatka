"""Migration v010: durable one-block lesson regeneration jobs (#418)."""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Add an owner-scoped asynchronous job without changing lesson-job states."""
    connection.execute(
        """
        CREATE TABLE activity_regenerations (
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
                REFERENCES lesson_jobs(teacher_id, id) ON DELETE CASCADE
        )
        """
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
