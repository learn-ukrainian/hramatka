"""Durable SQLite store for one lesson job per lesson id."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class IdempotencyConflict(ValueError):
    """A caller reused a lesson id for different bake inputs."""


@dataclass(frozen=True)
class JobRecord:
    id: str
    anchor_text: str
    anchor_source: str
    duration: int
    focus: str | None
    request_hash: str
    status: str
    step: str
    last_error: str | None
    lesson: dict[str, Any] | None
    warning_acknowledgements: frozenset[str]
    created_at: str
    updated_at: str
    started_at: str | None


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request_hash(*, anchor_text: str, anchor_source: str, duration: int, focus: str | None) -> str:
    body = json.dumps(
        {
            "anchor": {"text": anchor_text, "source": anchor_source},
            "duration": duration,
            "focus": focus,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class JobStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lesson_jobs (
                    id TEXT PRIMARY KEY,
                    anchor_text TEXT NOT NULL,
                    anchor_source TEXT NOT NULL,
                    duration INTEGER NOT NULL,
                    focus TEXT,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('draft', 'baking', 'ready', 'failed')),
                    step TEXT NOT NULL,
                    last_error TEXT,
                    lesson_json TEXT,
                    warning_acknowledgements_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT
                )
                """
            )

    def create_or_get(
        self, *, id: str, anchor_text: str, anchor_source: str, duration: int, focus: str | None
    ) -> tuple[JobRecord, bool]:
        digest = request_hash(
            anchor_text=anchor_text,
            anchor_source=anchor_source,
            duration=duration,
            focus=focus,
        )
        timestamp = now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO lesson_jobs (
                    id, anchor_text, anchor_source, duration, focus, request_hash, status,
                    step, last_error, lesson_json, warning_acknowledgements_json,
                    created_at, updated_at, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', 'текст отримано', NULL, NULL, '[]', ?, ?, NULL)
                """,
                (
                    id,
                    anchor_text,
                    anchor_source,
                    duration,
                    focus,
                    digest,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM lesson_jobs WHERE id = ?", (id,)).fetchone()

        if row is None:  # pragma: no cover - guarded by the primary key insert/select sequence
            raise RuntimeError("Unable to create or read durable lesson job.")
        record = self._record(row)
        if record.request_hash != digest:
            raise IdempotencyConflict("That lesson id is already bound to different bake inputs.")
        return record, cursor.rowcount == 1

    def get(self, id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM lesson_jobs WHERE id = ?", (id,)).fetchone()
        return self._record(row) if row is not None else None

    def set_step(self, id: str, step: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs SET step = ?, updated_at = ?
                WHERE id = ? AND status = 'baking'
                """,
                (step, now_iso(), id),
            )
        return cursor.rowcount == 1

    def claim_next_draft(self) -> JobRecord | None:
        """Atomically claim the oldest queued job for the one local bake worker."""
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM lesson_jobs WHERE status = 'draft'
                ORDER BY created_at, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            lesson_id = row["id"]
            connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'baking', step = 'завдання складено',
                    started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'draft'
                """,
                (timestamp, timestamp, lesson_id),
            )
            claimed = connection.execute(
                "SELECT * FROM lesson_jobs WHERE id = ?", (lesson_id,)
            ).fetchone()
            connection.execute("COMMIT")
        return self._record(claimed) if claimed is not None else None

    def complete(self, id: str, lesson: dict[str, Any]) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'ready', step = 'готово', last_error = NULL,
                    lesson_json = ?, updated_at = ?
                WHERE id = ? AND status = 'baking'
                """,
                (json.dumps(lesson, ensure_ascii=False, separators=(",", ":")), now_iso(), id),
            )
        return cursor.rowcount == 1

    def fail(self, id: str, error: str, *, step: str | None = None) -> bool:
        if not error:
            raise ValueError("A failed job requires an honest last_error.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', step = COALESCE(?, step), last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'baking'
                """,
                (step, error, now_iso(), id),
            )
        return cursor.rowcount == 1

    def recover_baking_jobs(self, hard_timeout_seconds: int) -> int:
        """Fail interrupted/stale bakes on process startup; no work is fabricated."""
        timed_out = self.sweep_expired_bakes(hard_timeout_seconds)
        with self._connect() as connection:
            interrupted = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', last_error = ?, updated_at = ?
                WHERE status = 'baking'
                """,
                (
                    "Bake was interrupted by process restart and was marked failed "
                    "rather than left baking.",
                    now_iso(),
                ),
            ).rowcount
            queued = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', last_error = ?, updated_at = ?
                WHERE status = 'draft'
                """,
                (
                    "Bake remained queued when the process restarted and was marked failed "
                    "rather than left waiting.",
                    now_iso(),
                ),
            ).rowcount
        return timed_out + interrupted + queued

    def sweep_expired_bakes(self, hard_timeout_seconds: int) -> int:
        """Fail long-running work during normal operation, not only after a restart."""
        cutoff = (
            (datetime.now(UTC) - timedelta(seconds=hard_timeout_seconds))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', last_error = ?, updated_at = ?
                WHERE status = 'baking' AND started_at < ?
                """,
                (
                    "Bake exceeded the configured hard timeout and was marked failed "
                    "during timeout sweep.",
                    now_iso(),
                    cutoff,
                ),
            )
        return cursor.rowcount

    def fail_queued_drafts(self, error: str) -> int:
        """Fail unclaimed jobs when the only bake worker is quarantined."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs SET status = 'failed', last_error = ?, updated_at = ?
                WHERE status = 'draft'
                """,
                (error, now_iso()),
            )
        return cursor.rowcount

    def acknowledge_warning(self, lesson_id: str, block_id: str) -> JobRecord:
        job = self._require_ready_job(lesson_id)
        lesson = self._require_lesson(job)
        visible_warning_ids = {
            block["id"] for block in self._visible_blocks(lesson) if block.get("mark") == "warn"
        }
        if block_id not in visible_warning_ids:
            raise ValueError("That block is not a visible warning requiring acknowledgement.")
        acknowledgements = set(job.warning_acknowledgements)
        acknowledgements.add(block_id)
        self._write_acknowledgements(lesson_id, acknowledgements)
        updated = self.get(lesson_id)
        if updated is None:  # pragma: no cover - job cannot disappear
            raise RuntimeError("Durable lesson job disappeared while acknowledging a warning.")
        return updated

    def accept_lesson(self, lesson_id: str) -> JobRecord:
        job = self._require_ready_job(lesson_id)
        lesson = self._require_lesson(job)
        required = {
            block["id"] for block in self._visible_blocks(lesson) if block.get("mark") == "warn"
        }
        missing = sorted(required - set(job.warning_acknowledgements))
        if missing:
            raise ValueError(
                "Visible warning blocks still need acknowledgement: " + ", ".join(missing)
            )

        lesson["accepted"] = True
        lesson["updated_at"] = now_iso()
        self._write_lesson(lesson_id, lesson)
        updated = self.get(lesson_id)
        if updated is None:  # pragma: no cover - job cannot disappear
            raise RuntimeError("Durable lesson job disappeared while accepting.")
        return updated

    def return_to_draft(self, lesson_id: str) -> JobRecord:
        job = self._require_ready_job(lesson_id)
        lesson = self._require_lesson(job)
        lesson["accepted"] = False
        lesson["updated_at"] = now_iso()
        self._write_lesson(lesson_id, lesson)
        updated = self.get(lesson_id)
        if updated is None:  # pragma: no cover - job cannot disappear
            raise RuntimeError("Durable lesson job disappeared while returning to draft.")
        return updated

    def _write_acknowledgements(self, lesson_id: str, acknowledgements: set[str]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE lesson_jobs
                SET warning_acknowledgements_json = ?, updated_at = ?
                WHERE id = ? AND status = 'ready'
                """,
                (json.dumps(sorted(acknowledgements)), now_iso(), lesson_id),
            )

    def _write_lesson(self, lesson_id: str, lesson: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE lesson_jobs SET lesson_json = ?, updated_at = ?
                WHERE id = ? AND status = 'ready'
                """,
                (
                    json.dumps(lesson, ensure_ascii=False, separators=(",", ":")),
                    now_iso(),
                    lesson_id,
                ),
            )

    @staticmethod
    def _visible_blocks(lesson: dict[str, Any]) -> list[dict[str, Any]]:
        sizes = {
            45: {1: 2, 2: 3, 3: 1},
            60: {1: 3, 2: 4, 3: 2},
            90: {1: 4, 2: 5, 3: 3},
        }
        budget = sizes[lesson["duration"]]
        seen = {1: 0, 2: 0, 3: 0}
        visible: list[dict[str, Any]] = []
        for block in lesson["blocks"]:
            phase = block["phase"]
            if seen[phase] < budget[phase]:
                visible.append(block)
                seen[phase] += 1
        return visible

    def _require_ready_job(self, lesson_id: str) -> JobRecord:
        job = self.get(lesson_id)
        if job is None:
            raise KeyError(lesson_id)
        if job.status != "ready":
            raise RuntimeError("Lesson is not ready.")
        return job

    @staticmethod
    def _require_lesson(job: JobRecord) -> dict[str, Any]:
        if job.lesson is None:  # pragma: no cover - invariant of ready jobs
            raise RuntimeError("Ready lesson has no emitted document.")
        return job.lesson

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            anchor_text=row["anchor_text"],
            anchor_source=row["anchor_source"],
            duration=row["duration"],
            focus=row["focus"],
            request_hash=row["request_hash"],
            status=row["status"],
            step=row["step"],
            last_error=row["last_error"],
            lesson=json.loads(row["lesson_json"]) if row["lesson_json"] else None,
            warning_acknowledgements=frozenset(json.loads(row["warning_acknowledgements_json"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
        )
