"""Migration v009: teacher feedback on engine-flagged blocks (#402).

Creates the durable ``activity_feedback`` table.  Lifecycle follows the
``recovery_codes.superseded_at`` precedent from v008: rows are never deleted,
the current row for a ``(lesson_id, slot_id)`` pair is the one with
``superseded_at IS NULL``, and the partial unique index enforces that at the
database layer rather than in application logic only.

Also backfills the lu.lesson.v1@1.3.0 block quality fields into stored lesson
documents: 1.3.0 makes ``quality``/``flag_reason_uk``/``flagged_content_hash``/
``engine_reason_class`` required on every block, and review mutations
re-validate the whole document against the new pin.  Every block shipped
before this migration passed the gates or was teacher-authored, so
``engine_ok`` with null flag fields is the honest value.
"""

from __future__ import annotations

import json
import sqlite3


def _canonical_json(value: object) -> str:
    # Mirrors ``hramatka.api.store.canonical_json``; kept local so the
    # migration package does not depend on the store package.
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def apply(connection: sqlite3.Connection) -> None:
    # No REFERENCES on lesson_id: lesson_jobs' primary key is the composite
    # (teacher_id, id), so a single-column foreign key would be a "foreign key
    # mismatch" under PRAGMA foreign_keys=ON.  Ownership is enforced by
    # ``_require_owned`` on every read/write path, and feedback rows deliberately
    # survive lesson deletion as analyzable engine-vs-teacher judgment data.
    connection.execute(
        """
        CREATE TABLE activity_feedback (
            id TEXT PRIMARY KEY,
            lesson_id TEXT NOT NULL,
            slot_id TEXT NOT NULL,
            activity_payload_hash TEXT NOT NULL,
            engine_reason_class TEXT NOT NULL,
            engine_reason_uk TEXT NOT NULL,
            teacher_verdict TEXT NOT NULL
                CHECK (teacher_verdict IN ('good', 'bad')),
            comment TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            superseded_at TEXT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX activity_feedback_current_idx
        ON activity_feedback(lesson_id, slot_id)
        WHERE superseded_at IS NULL
        """
    )
    connection.execute(
        "CREATE INDEX activity_feedback_lesson_idx ON activity_feedback(lesson_id)"
    )
    _backfill_block_quality_fields(connection)


def _backfill_block_quality_fields(connection: sqlite3.Connection) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lesson_jobs'"
        ).fetchone()
        is None
    ):
        return
    rows = connection.execute(
        "SELECT id, lesson_json FROM lesson_jobs WHERE lesson_json IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            lesson = json.loads(row["lesson_json"])
        except (TypeError, json.JSONDecodeError):
            continue  # An unreadable document is the store's problem, not v009's.
        if not isinstance(lesson, dict):
            continue
        changed = False
        blocks = lesson.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and "quality" not in block:
                block["quality"] = "engine_ok"
                block["flag_reason_uk"] = None
                block["flagged_content_hash"] = None
                block["engine_reason_class"] = None
                changed = True
        if changed:
            connection.execute(
                "UPDATE lesson_jobs SET lesson_json = ? WHERE id = ?",
                (_canonical_json(lesson), row["id"]),
            )
