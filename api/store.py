"""Durable, owner-scoped SQLite persistence for the frozen Hramatka pilot.

This module intentionally contains no HTTP concerns.  It exposes only durable
operations and semantic exceptions; the FastAPI layer maps those exceptions to
the frozen error envelope.  Raw invite and session secrets never enter a
record, database row, exception message, or loggable return value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .migrations import (
    EXPECTED_SCHEMA_VERSION,
    MigrationError,
    apply_migrations,
    current_schema_version,
)

_OPAQUE_TOKEN_BYTES = 32
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
_FAILURE_CODES = frozenset(
    {
        "bake_timeout",
        "worker_restarted",
        "provider_unavailable",
        "engine_unavailable",
        "lesson_schema_invalid",
        "unknown_safe_failure",
    }
)
_STEP_RECEIVED = "текст отримано"
_STEP_COMPOSED = "завдання складено"
_STEP_CHECKING = "перевірка"
_STEP_COMPLETE = "готово"


class PersistenceUnavailable(RuntimeError):
    """SQLite could not durably perform a read, write, or transaction commit."""


class TokenFormatError(ValueError):
    """An opaque secret is not canonical unpadded base64url for exactly 32 bytes."""


class InviteUnavailable(ValueError):
    """A schema-valid invite is unknown, used, expired, revoked, or inactive."""


class SessionUnavailable(ValueError):
    """A previously authenticated teacher became inactive before a mutation committed."""


class IdempotencyConflict(ValueError):
    """A teacher reused a lesson UUID for a different canonical request."""


class LessonNotFound(KeyError):
    """An owner-scoped lesson lookup had no row (including another owner's row)."""


class RevisionConflict(ValueError):
    """A mutation was made against a stale aggregate revision."""


class LessonStateConflict(ValueError):
    """The aggregate exists but its current state disallows that transition."""


class WarningBlockNotFound(ValueError):
    """A present ready lesson has no visible warning block with that id."""


class WarningAcknowledgementsRequired(ValueError):
    """Acceptance requires every visible warning acknowledgement."""

    def __init__(self, block_ids: list[str]) -> None:
        self.block_ids = list(block_ids)
        super().__init__("Visible warning blocks require acknowledgement.")


# Kept as a compatibility import for callers still using the prototype spelling.
WarningBlocksUnacknowledged = WarningAcknowledgementsRequired


@dataclass(frozen=True)
class TeacherRecord:
    id: str
    display_name: str
    created_at: str
    deactivated_at: str | None


@dataclass(frozen=True)
class InviteRecord:
    id: str
    teacher_id: str
    created_at: str
    expires_at: str
    redeemed_at: str | None
    revoked_at: str | None


@dataclass(frozen=True)
class SessionRecord:
    id: str
    teacher_id: str
    teacher_display_name: str
    invite_id: str
    created_at: str
    expires_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class RedeemedSession:
    """Committed session metadata and the one transient browser credential."""

    session: SessionRecord
    raw_secret: bytes


@dataclass(frozen=True)
class JobRecord:
    teacher_id: str
    id: str
    request_json: str
    request_hash: bytes
    status: str
    step: str
    failure_code: str | None
    failure_message: str | None
    lesson: dict[str, Any] | None
    warning_acknowledgements: frozenset[str]
    accepted: bool
    accepted_at: str | None
    accepted_revision: int | None
    revision: int
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None
    progress: dict[str, Any] | None = None

    @property
    def request(self) -> dict[str, Any]:
        return _decode_json_object(self.request_json)

    @property
    def anchor_text(self) -> str:
        return self.request["anchor"]["text"]

    @property
    def anchor_source(self) -> str:
        return self.request["anchor"]["source"]

    @property
    def duration(self) -> int:
        return self.request["duration"]

    @property
    def level(self) -> str:
        return self.request["level"]

    @property
    def focus(self) -> str | None:
        return self.request["focus"]

    @property
    def last_error(self) -> str | None:
        """Prototype-compatible name for the safe durable failure message."""
        return self.failure_message


@dataclass(frozen=True)
class CatalogRecord:
    id: str
    title: str | None
    status: str
    duration: int
    focus: str | None
    revision: int
    accepted: bool
    accepted_at: str | None
    accepted_revision: int | None
    failure_code: str | None
    created_at: str
    updated_at: str


def now_iso() -> str:
    """A UTC RFC 3339 timestamp with stable SQLite lexical ordering."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def encode_opaque_token(raw: bytes) -> str:
    """Encode one 32-byte opaque browser or invite credential canonically."""
    if len(raw) != _OPAQUE_TOKEN_BYTES:
        raise ValueError("Opaque credentials must be exactly 32 bytes.")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_opaque_token(token: str) -> bytes:
    """Strictly decode a canonical 43-character unpadded base64url credential.

    Checking both the final alphabet character and re-encoding rejects aliases
    with non-zero base64 padding bits that permissive decoders would accept.
    """
    if not isinstance(token, str) or _OPAQUE_TOKEN_RE.fullmatch(token) is None:
        raise TokenFormatError("Opaque credential format is invalid.")
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (ValueError, UnicodeEncodeError) as error:
        raise TokenFormatError("Opaque credential format is invalid.") from error
    if len(raw) != _OPAQUE_TOKEN_BYTES or encode_opaque_token(raw) != token:
        raise TokenFormatError("Opaque credential format is invalid.")
    return raw


def invite_token_digest(raw_token: bytes) -> bytes:
    """Return the only invite-token value that is permitted in SQLite."""
    return hashlib.sha256(b"hramatka-invite\0" + raw_token).digest()


def session_secret_digest(raw_secret: bytes) -> bytes:
    """Return the frozen domain-separated session-secret digest."""
    return hashlib.sha256(b"hramatka-session\0" + raw_secret).digest()


def canonical_json(value: Any) -> str:
    """Canonical compact UTF-8-compatible JSON used for durable request values."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_request_json(
    *,
    anchor_text: str,
    anchor_source: str = "teacher-paste",
    level: str = "B1",
    duration: int,
    focus: str | None,
) -> str:
    """Build the complete canonical request defined by the frozen OpenAPI contract."""
    if anchor_source != "teacher-paste":
        raise ValueError("The pilot accepts only teacher-paste anchors.")
    if level != "B1":
        raise ValueError("The pilot accepts only B1 lessons.")
    if duration not in {45, 60, 90}:
        raise ValueError("Unsupported lesson duration.")
    if not isinstance(anchor_text, str) or not anchor_text.strip():
        raise ValueError("Anchor text must not be blank.")
    if not isinstance(focus, str | type(None)):
        raise ValueError("Focus must be a string or null.")
    return canonical_json(
        {
            "anchor": {"source": anchor_source, "text": anchor_text},
            "duration": duration,
            "focus": focus,
            "level": level,
        }
    )


def request_hash(
    *,
    anchor_text: str,
    anchor_source: str = "teacher-paste",
    level: str = "B1",
    duration: int,
    focus: str | None,
) -> bytes:
    """SHA-256 over the canonical UTF-8 request JSON, stored as a BLOB."""
    return hashlib.sha256(
        canonical_request_json(
            anchor_text=anchor_text,
            anchor_source=anchor_source,
            level=level,
            duration=duration,
            focus=focus,
        ).encode("utf-8")
    ).digest()


class JobStore:
    """The sole SQLite source of truth for pilot identities and lesson jobs."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """Create the parent directory and atomically apply ordered migrations."""
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PersistenceUnavailable("The SQLite data directory is unavailable.") from error
        with self._read_connection() as connection:
            try:
                apply_migrations(connection)
            except sqlite3.Error as error:
                raise PersistenceUnavailable("SQLite migration failed.") from error
            except RuntimeError as error:
                raise MigrationError(str(error)) from error

    def current_schema_version(self) -> int:
        with self._read_connection() as connection:
            return current_schema_version(connection)

    def is_ready(self) -> bool:
        """Check the DB-side readiness conditions without exposing database details."""
        try:
            with self._write_transaction() as connection:
                foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
                if foreign_keys is None or int(foreign_keys[0]) != 1:
                    return False
                if current_schema_version(connection) != EXPECTED_SCHEMA_VERSION:
                    return False
            return True
        except (PersistenceUnavailable, MigrationError):
            return False

    def readiness_check(self) -> bool:
        """Compatibility spelling for the API readiness probe."""
        return self.is_ready()

    # -- Operator lifecycle -------------------------------------------------

    def create_teacher(self, display_name: str) -> TeacherRecord:
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 100:
            raise ValueError("Teacher display_name must be 1–100 characters.")
        teacher = TeacherRecord(
            id=str(uuid.uuid4()),
            display_name=display_name,
            created_at=now_iso(),
            deactivated_at=None,
        )
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO pilot_teachers (id, display_name, created_at, deactivated_at)
                VALUES (?, ?, ?, NULL)
                """,
                (teacher.id, teacher.display_name, teacher.created_at),
            )
        return teacher

    def get_teacher(self, teacher_id: str) -> TeacherRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT id, display_name, created_at, deactivated_at
                FROM pilot_teachers WHERE id = ?
                """,
                (teacher_id,),
            ).fetchone()
        return self._teacher_record(row) if row is not None else None

    def create_invite(
        self, teacher_id: str, *, expires_in_hours: int = 72
    ) -> tuple[InviteRecord, str]:
        if not isinstance(expires_in_hours, int) or not 1 <= expires_in_hours <= 168:
            raise ValueError("Invite expiry must be between 1 and 168 hours.")
        raw_token = secrets.token_bytes(_OPAQUE_TOKEN_BYTES)
        token = encode_opaque_token(raw_token)
        created_at = now_iso()
        expires_at = _add_hours(created_at, expires_in_hours)
        invite = InviteRecord(
            id=str(uuid.uuid4()),
            teacher_id=teacher_id,
            created_at=created_at,
            expires_at=expires_at,
            redeemed_at=None,
            revoked_at=None,
        )
        with self._write_transaction() as connection:
            active_teacher = connection.execute(
                """
                SELECT 1 FROM pilot_teachers WHERE id = ? AND deactivated_at IS NULL
                """,
                (teacher_id,),
            ).fetchone()
            if active_teacher is None:
                raise ValueError("Teacher does not exist or is deactivated.")
            connection.execute(
                """
                INSERT INTO pilot_invites (
                    id, teacher_id, token_hash, created_at, expires_at, redeemed_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    invite.id,
                    invite.teacher_id,
                    invite_token_digest(raw_token),
                    invite.created_at,
                    invite.expires_at,
                ),
            )
        return invite, token

    def get_invite(self, invite_id: str) -> InviteRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT id, teacher_id, created_at, expires_at, redeemed_at, revoked_at
                FROM pilot_invites WHERE id = ?
                """,
                (invite_id,),
            ).fetchone()
        return self._invite_record(row) if row is not None else None

    def revoke_invite(self, invite_id: str) -> InviteRecord | None:
        timestamp = now_iso()
        with self._write_transaction() as connection:
            connection.execute(
                """
                UPDATE pilot_invites SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ? AND redeemed_at IS NULL
                """,
                (timestamp, invite_id),
            )
            row = connection.execute(
                """
                SELECT id, teacher_id, created_at, expires_at, redeemed_at, revoked_at
                FROM pilot_invites WHERE id = ?
                """,
                (invite_id,),
            ).fetchone()
        return self._invite_record(row) if row is not None else None

    def deactivate_teacher(self, teacher_id: str) -> TeacherRecord | None:
        """Deactivate one teacher and revoke usable credentials in the same transaction."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE pilot_teachers SET deactivated_at = COALESCE(deactivated_at, ?)
                WHERE id = ?
                """,
                (timestamp, teacher_id),
            )
            if cursor.rowcount == 0:
                return None
            connection.execute(
                """
                UPDATE pilot_invites SET revoked_at = COALESCE(revoked_at, ?)
                WHERE teacher_id = ? AND redeemed_at IS NULL
                """,
                (timestamp, teacher_id),
            )
            connection.execute(
                """
                UPDATE pilot_sessions SET revoked_at = COALESCE(revoked_at, ?)
                WHERE teacher_id = ?
                """,
                (timestamp, teacher_id),
            )
            row = connection.execute(
                """
                SELECT id, display_name, created_at, deactivated_at
                FROM pilot_teachers WHERE id = ?
                """,
                (teacher_id,),
            ).fetchone()
        assert row is not None
        return self._teacher_record(row)

    def revoke_session(self, session_id: str) -> SessionRecord | None:
        timestamp = now_iso()
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE pilot_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (timestamp, session_id),
            )
            row = connection.execute(
                """
                SELECT s.id, s.teacher_id, t.display_name AS teacher_display_name,
                       s.invite_id, s.created_at, s.expires_at, s.revoked_at
                FROM pilot_sessions AS s
                JOIN pilot_teachers AS t ON t.id = s.teacher_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._session_record(row) if row is not None else None

    # -- Browser session lifecycle -----------------------------------------

    def redeem_invite(self, token: str) -> RedeemedSession:
        """Consume one usable invite and create one absolute seven-day session."""
        raw_token = decode_opaque_token(token)
        timestamp = now_iso()
        session_secret = secrets.token_bytes(_OPAQUE_TOKEN_BYTES)
        session = SessionRecord(
            id=str(uuid.uuid4()),
            teacher_id="",
            teacher_display_name="",
            invite_id="",
            created_at=timestamp,
            expires_at=_add_hours(timestamp, 24 * 7),
            revoked_at=None,
        )
        with self._write_transaction() as connection:
            invite_row = connection.execute(
                """
                SELECT i.id, i.teacher_id, t.display_name
                FROM pilot_invites AS i
                JOIN pilot_teachers AS t ON t.id = i.teacher_id
                WHERE i.token_hash = ?
                  AND i.redeemed_at IS NULL
                  AND i.revoked_at IS NULL
                  AND i.expires_at > ?
                  AND t.deactivated_at IS NULL
                """,
                (invite_token_digest(raw_token), timestamp),
            ).fetchone()
            if invite_row is None:
                raise InviteUnavailable("This invite is no longer available.")
            session = SessionRecord(
                id=session.id,
                teacher_id=invite_row["teacher_id"],
                teacher_display_name=invite_row["display_name"],
                invite_id=invite_row["id"],
                created_at=session.created_at,
                expires_at=session.expires_at,
                revoked_at=None,
            )
            consumed = connection.execute(
                """
                UPDATE pilot_invites SET redeemed_at = ?
                WHERE id = ? AND redeemed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                """,
                (timestamp, session.invite_id, timestamp),
            )
            if consumed.rowcount != 1:
                # The immediate transaction makes this defensive guard normally unreachable.
                raise InviteUnavailable("This invite is no longer available.")
            connection.execute(
                """
                INSERT INTO pilot_sessions (
                    id, teacher_id, invite_id, secret_hash, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    session.id,
                    session.teacher_id,
                    session.invite_id,
                    session_secret_digest(session_secret),
                    session.created_at,
                    session.expires_at,
                ),
            )
        return RedeemedSession(session=session, raw_secret=session_secret)

    def lookup_session(self, raw_secret: bytes | str) -> SessionRecord | None:
        """Make the full expiry/revocation/deactivation authorization decision in SQL."""
        raw = _coerce_opaque_secret(raw_secret)
        if raw is None:
            return None
        timestamp = now_iso()
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT s.id, s.teacher_id, t.display_name AS teacher_display_name,
                       s.invite_id, s.created_at, s.expires_at, s.revoked_at
                FROM pilot_sessions AS s
                JOIN pilot_teachers AS t ON t.id = s.teacher_id
                WHERE s.secret_hash = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND t.deactivated_at IS NULL
                """,
                (session_secret_digest(raw), timestamp),
            ).fetchone()
        return self._session_record(row) if row is not None else None

    def logout_session(self, raw_secret: bytes | str) -> bool:
        """Revoke the presented current session before an HTTP 204 is returned."""
        raw = _coerce_opaque_secret(raw_secret)
        if raw is None:
            return False
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE pilot_sessions SET revoked_at = ?
                WHERE secret_hash = ? AND revoked_at IS NULL AND expires_at > ?
                  AND EXISTS (
                    SELECT 1 FROM pilot_teachers AS t
                    WHERE t.id = pilot_sessions.teacher_id AND t.deactivated_at IS NULL
                  )
                """,
                (timestamp, session_secret_digest(raw), timestamp),
            )
        return cursor.rowcount == 1

    # -- Owner-scoped job creation and reads -------------------------------

    def create_or_get(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        anchor_text: str,
        level: str = "B1",
        duration: int,
        focus: str | None,
        anchor_source: str = "teacher-paste",
    ) -> tuple[JobRecord, bool]:
        request_json = canonical_request_json(
            anchor_text=anchor_text,
            anchor_source=anchor_source,
            level=level,
            duration=duration,
            focus=focus,
        )
        digest = hashlib.sha256(request_json.encode("utf-8")).digest()
        timestamp = now_iso()
        with self._write_transaction() as connection:
            active_teacher = connection.execute(
                """
                SELECT 1 FROM pilot_teachers
                WHERE id = ? AND deactivated_at IS NULL
                """,
                (teacher_id,),
            ).fetchone()
            if active_teacher is None:
                # A request can pass cookie lookup just before an operator
                # deactivates that teacher.  This second check is deliberately
                # inside the writer transaction, so it cannot create a new job
                # after that lifecycle decision commits.
                raise SessionUnavailable("The teacher session is no longer active.")
            try:
                connection.execute(
                    """
                    INSERT INTO lesson_jobs (
                        teacher_id, id, request_json, request_hash, status, step,
                        failure_code, failure_message, lesson_json,
                        warning_acknowledgements_json, accepted, accepted_at,
                        accepted_revision, revision, created_at, updated_at,
                        started_at, completed_at
                    ) VALUES (
                        ?, ?, ?, ?, 'draft', ?, NULL, NULL, NULL, '[]', 0,
                        NULL, NULL, 1, ?, ?, NULL, NULL
                    )
                    """,
                    (
                        teacher_id,
                        lesson_id,
                        request_json,
                        digest,
                        _STEP_RECEIVED,
                        timestamp,
                        timestamp,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = connection.execute(
                "SELECT * FROM lesson_jobs WHERE teacher_id = ? AND id = ?",
                (teacher_id, lesson_id),
            ).fetchone()
            if row is None:
                # This can only be a foreign-key rejection for a no-longer-valid teacher.
                raise PersistenceUnavailable("The lesson owner is unavailable.")
            job = self._record(row)
            if job.request_hash != digest:
                raise IdempotencyConflict("This lesson ID is already bound to different inputs.")
        return job, created

    def get(self, teacher_id: str, lesson_id: str) -> JobRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM lesson_jobs WHERE teacher_id = ? AND id = ?",
                (teacher_id, lesson_id),
            ).fetchone()
        return self._record(row) if row is not None else None

    def list_catalog(self, teacher_id: str) -> list[CatalogRecord]:
        """Return metadata only, using exactly the owner-scoped catalog predicate."""
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT id,
                       json_extract(lesson_json, '$.title') AS title,
                       json_extract(request_json, '$.duration') AS duration,
                       json_extract(request_json, '$.focus') AS focus,
                       status, revision, accepted, accepted_at, accepted_revision,
                       failure_code, created_at, updated_at
                FROM lesson_jobs
                WHERE teacher_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (teacher_id,),
            ).fetchall()
        return [self._catalog_record(row) for row in rows]

    # -- Runner transitions -------------------------------------------------

    def claim_next_draft(self) -> JobRecord | None:
        """Claim the next queued job for the one in-process worker, durably."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM lesson_jobs WHERE status = 'draft'
                ORDER BY created_at, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'baking', step = ?, started_at = ?, updated_at = ?,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'draft' AND revision = ?
                """,
                (
                    _STEP_COMPOSED,
                    timestamp,
                    timestamp,
                    row["teacher_id"],
                    row["id"],
                    row["revision"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM lesson_jobs WHERE teacher_id = ? AND id = ?",
                (row["teacher_id"], row["id"]),
            ).fetchone()
        return self._record(claimed) if claimed is not None else None

    def set_step(self, teacher_id: str, lesson_id: str, step: str) -> bool:
        if step not in {_STEP_COMPOSED, _STEP_CHECKING, _STEP_COMPLETE}:
            raise ValueError("Unknown frozen lesson step.")
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs SET step = ?, updated_at = ?, revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                """,
                (step, timestamp, teacher_id, lesson_id),
            )
        return cursor.rowcount == 1

    def complete(self, teacher_id: str, lesson_id: str, lesson: Mapping[str, Any]) -> bool:
        """Persist one complete validated lesson; partial documents are never written."""
        materialized = dict(lesson)
        if materialized.get("accepted") is not False:
            raise ValueError("A newly materialized lesson must be unaccepted.")
        lesson_json = canonical_json(materialized)
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'ready', step = ?, failure_code = NULL, failure_message = NULL,
                    lesson_json = ?, accepted = 0, accepted_at = NULL, accepted_revision = NULL,
                    completed_at = ?, updated_at = ?, revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                """,
                (_STEP_COMPLETE, lesson_json, timestamp, timestamp, teacher_id, lesson_id),
            )
        return cursor.rowcount == 1

    def update_progress(self, lesson_id: str, progress: dict[str, Any]) -> bool:
        """Update progress telemetry for a lesson job."""
        timestamp = now_iso()
        progress = dict(progress)
        if "updated_at" not in progress:
            progress["updated_at"] = timestamp
        progress_json = canonical_json(progress)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs SET progress_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (progress_json, timestamp, lesson_id),
            )
        return cursor.rowcount == 1

    def fail(
        self,
        teacher_id: str,
        lesson_id: str,
        failure_code: str,
        failure_message: str,
        *,
        step: str = _STEP_COMPLETE,
    ) -> bool:
        """Durably fail a baking job with an allowlisted safe code/message only."""
        _validate_failure(failure_code, failure_message)
        if step not in {_STEP_COMPOSED, _STEP_CHECKING, _STEP_COMPLETE}:
            raise ValueError("Unknown frozen lesson step.")
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', step = ?, failure_code = ?, failure_message = ?,
                    lesson_json = NULL, accepted = 0, accepted_at = NULL, accepted_revision = NULL,
                    completed_at = ?, updated_at = ?, revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                """,
                (
                    step,
                    failure_code,
                    failure_message,
                    timestamp,
                    timestamp,
                    teacher_id,
                    lesson_id,
                ),
            )
        return cursor.rowcount == 1

    def recover_baking_jobs(self, hard_timeout_seconds: int | None = None) -> int:
        """Fail orphaned baking rows at startup; queued drafts remain queued."""
        del hard_timeout_seconds  # Recovery is a restart boundary, not a timeout calculation.
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', step = ?, failure_code = 'worker_restarted',
                    failure_message = ?, lesson_json = NULL, accepted = 0,
                    accepted_at = NULL, accepted_revision = NULL, completed_at = ?,
                    updated_at = ?, revision = revision + 1
                WHERE status = 'baking'
                """,
                (
                    _STEP_COMPLETE,
                    (
                        "Складання уроку перервалося через перезапуск сервісу. "
                        "Спробуйте, будь ласка, ще раз."
                    ),
                    timestamp,
                    timestamp,
                ),
            )
        return cursor.rowcount

    def sweep_expired_bakes(self, hard_timeout_seconds: int) -> int:
        if hard_timeout_seconds <= 0:
            raise ValueError("Bake timeout must be positive.")
        cutoff = _add_seconds(now_iso(), -hard_timeout_seconds)
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', step = ?, failure_code = 'bake_timeout',
                    failure_message = ?, lesson_json = NULL, accepted = 0,
                    accepted_at = NULL, accepted_revision = NULL, completed_at = ?,
                    updated_at = ?, revision = revision + 1
                WHERE status = 'baking' AND started_at < ?
                """,
                (
                    _STEP_COMPLETE,
                    "Час на складання уроку вичерпано. Спробуйте, будь ласка, ще раз.",
                    timestamp,
                    timestamp,
                    cutoff,
                ),
            )
        return cursor.rowcount

    def fail_queued_drafts(
        self,
        failure_message: str,
        *,
        failure_code: str = "unknown_safe_failure",
    ) -> int:
        """Fail drafts when the sole worker is deliberately quarantined."""
        _validate_failure(failure_code, failure_message)
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', step = ?, failure_code = ?, failure_message = ?,
                    lesson_json = NULL, accepted = 0, accepted_at = NULL,
                    accepted_revision = NULL, completed_at = ?, updated_at = ?,
                    revision = revision + 1
                WHERE status = 'draft'
                """,
                (_STEP_COMPLETE, failure_code, failure_message, timestamp, timestamp),
            )
        return cursor.rowcount

    # -- Owner-scoped review and acceptance transitions --------------------

    def acknowledge_warning(
        self, teacher_id: str, lesson_id: str, block_id: str, expected_revision: int
    ) -> JobRecord:
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            self._require_expected_revision(job, expected_revision)
            self._require_ready(job)
            lesson = self._require_lesson(job)
            visible_warning_ids = {
                block["id"] for block in self._visible_blocks(lesson) if block.get("mark") == "warn"
            }
            if block_id not in visible_warning_ids:
                raise WarningBlockNotFound("Warning block not found.")
            if block_id in job.warning_acknowledgements:
                return job
            acknowledgements = sorted({*job.warning_acknowledgements, block_id})
            timestamp = now_iso()
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET warning_acknowledgements_json = ?, updated_at = ?, revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'ready' AND revision = ?
                """,
                (
                    canonical_json(acknowledgements),
                    timestamp,
                    teacher_id,
                    lesson_id,
                    expected_revision,
                ),
            )
            self._resolve_mutation(
                cursor.rowcount, connection, teacher_id, lesson_id, expected_revision
            )
            return self._require_owned(connection, teacher_id, lesson_id)

    def accept_lesson(self, teacher_id: str, lesson_id: str, expected_revision: int) -> JobRecord:
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            self._require_expected_revision(job, expected_revision)
            self._require_ready(job)
            if job.accepted:
                raise LessonStateConflict("The lesson is already accepted.")
            lesson = self._require_lesson(job)
            required = {
                block["id"] for block in self._visible_blocks(lesson) if block.get("mark") == "warn"
            }
            acknowledged = set(job.warning_acknowledgements)
            if acknowledged != required:
                raise WarningAcknowledgementsRequired(sorted(required - acknowledged))
            timestamp = now_iso()
            accepted_lesson = dict(lesson)
            accepted_lesson["accepted"] = True
            accepted_lesson["updated_at"] = timestamp
            new_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET accepted = 1, accepted_at = ?, accepted_revision = ?, lesson_json = ?,
                    updated_at = ?, revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'ready'
                  AND revision = ? AND accepted = 0
                """,
                (
                    timestamp,
                    new_revision,
                    canonical_json(accepted_lesson),
                    timestamp,
                    teacher_id,
                    lesson_id,
                    expected_revision,
                ),
            )
            self._resolve_mutation(
                cursor.rowcount, connection, teacher_id, lesson_id, expected_revision
            )
            return self._require_owned(connection, teacher_id, lesson_id)

    def return_to_draft(self, teacher_id: str, lesson_id: str, expected_revision: int) -> JobRecord:
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            self._require_expected_revision(job, expected_revision)
            self._require_ready(job)
            if not job.accepted:
                raise LessonStateConflict("The lesson is not currently accepted.")
            lesson = self._require_lesson(job)
            timestamp = now_iso()
            draft_lesson = dict(lesson)
            draft_lesson["accepted"] = False
            draft_lesson["updated_at"] = timestamp
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET accepted = 0, accepted_at = NULL, accepted_revision = NULL, lesson_json = ?,
                    updated_at = ?, revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'ready'
                  AND revision = ? AND accepted = 1
                """,
                (
                    canonical_json(draft_lesson),
                    timestamp,
                    teacher_id,
                    lesson_id,
                    expected_revision,
                ),
            )
            self._resolve_mutation(
                cursor.rowcount, connection, teacher_id, lesson_id, expected_revision
            )
            return self._require_owned(connection, teacher_id, lesson_id)

    # -- SQLite plumbing and row mapping -----------------------------------

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        except sqlite3.Error as error:
            raise PersistenceUnavailable("SQLite is unavailable.") from error
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise PersistenceUnavailable("SQLite could not persist the request.") from error
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except sqlite3.Error as error:
            try:
                connection.close()  # type: ignore[possibly-undefined]
            except (UnboundLocalError, sqlite3.Error):
                pass
            raise PersistenceUnavailable("SQLite is unavailable.") from error

    @staticmethod
    def _teacher_record(row: sqlite3.Row) -> TeacherRecord:
        return TeacherRecord(
            id=row["id"],
            display_name=row["display_name"],
            created_at=row["created_at"],
            deactivated_at=row["deactivated_at"],
        )

    @staticmethod
    def _invite_record(row: sqlite3.Row) -> InviteRecord:
        return InviteRecord(
            id=row["id"],
            teacher_id=row["teacher_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            redeemed_at=row["redeemed_at"],
            revoked_at=row["revoked_at"],
        )

    @staticmethod
    def _session_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            teacher_id=row["teacher_id"],
            teacher_display_name=row["teacher_display_name"],
            invite_id=row["invite_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        try:
            lesson = json.loads(row["lesson_json"]) if row["lesson_json"] is not None else None
            acknowledgements = json.loads(row["warning_acknowledgements_json"])
            progress = (
                json.loads(row["progress_json"])
                if (
                    "progress_json" in row.keys()
                    and row["progress_json"] is not None
                )
                else None
            )
        except (TypeError, json.JSONDecodeError) as error:
            raise PersistenceUnavailable("SQLite contains unreadable durable data.") from error
        if not isinstance(acknowledgements, list) or any(
            not isinstance(value, str) for value in acknowledgements
        ):
            raise PersistenceUnavailable("SQLite contains unreadable durable data.")
        return JobRecord(
            teacher_id=row["teacher_id"],
            id=row["id"],
            request_json=row["request_json"],
            request_hash=bytes(row["request_hash"]),
            status=row["status"],
            step=row["step"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            lesson=lesson,
            warning_acknowledgements=frozenset(acknowledgements),
            accepted=bool(row["accepted"]),
            accepted_at=row["accepted_at"],
            accepted_revision=row["accepted_revision"],
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            progress=progress,
        )

    @staticmethod
    def _catalog_record(row: sqlite3.Row) -> CatalogRecord:
        return CatalogRecord(
            id=row["id"],
            title=row["title"] if isinstance(row["title"], str) else None,
            status=row["status"],
            duration=row["duration"],
            focus=row["focus"],
            revision=row["revision"],
            accepted=bool(row["accepted"]),
            accepted_at=row["accepted_at"],
            accepted_revision=row["accepted_revision"],
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _visible_blocks(lesson: Mapping[str, Any]) -> list[dict[str, Any]]:
        sizes = {
            45: {1: 2, 2: 3, 3: 1},
            60: {1: 3, 2: 4, 3: 2},
            90: {1: 4, 2: 5, 3: 3},
        }
        try:
            budget = sizes[lesson["duration"]]
            blocks = lesson["blocks"]
        except (KeyError, TypeError) as error:
            raise PersistenceUnavailable("SQLite contains an invalid ready lesson.") from error
        seen = {1: 0, 2: 0, 3: 0}
        visible: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise PersistenceUnavailable("SQLite contains an invalid ready lesson.")
            phase = block.get("phase")
            if phase not in seen:
                raise PersistenceUnavailable("SQLite contains an invalid ready lesson.")
            if seen[phase] < budget[phase]:
                visible.append(block)
                seen[phase] += 1
        return visible

    @staticmethod
    def _require_lesson(job: JobRecord) -> dict[str, Any]:
        if job.lesson is None:
            raise PersistenceUnavailable("A ready lesson has no durable document.")
        return job.lesson

    @staticmethod
    def _require_expected_revision(job: JobRecord, expected_revision: int) -> None:
        if job.revision != expected_revision:
            raise RevisionConflict("The lesson changed; reload it before trying again.")

    @staticmethod
    def _require_ready(job: JobRecord) -> None:
        if job.status != "ready":
            raise LessonStateConflict("The lesson is not in a state that allows this change.")

    def _require_owned(
        self, connection: sqlite3.Connection, teacher_id: str, lesson_id: str
    ) -> JobRecord:
        row = connection.execute(
            "SELECT * FROM lesson_jobs WHERE teacher_id = ? AND id = ?", (teacher_id, lesson_id)
        ).fetchone()
        if row is None:
            raise LessonNotFound(lesson_id)
        return self._record(row)

    def _resolve_mutation(
        self,
        rowcount: int,
        connection: sqlite3.Connection,
        teacher_id: str,
        lesson_id: str,
        expected_revision: int,
    ) -> None:
        if rowcount == 1:
            return
        current = self._require_owned(connection, teacher_id, lesson_id)
        if current.revision != expected_revision:
            raise RevisionConflict("The lesson changed; reload it before trying again.")
        raise LessonStateConflict("The lesson is not in a state that allows this change.")


def _coerce_opaque_secret(value: bytes | str) -> bytes | None:
    if isinstance(value, bytes):
        return value if len(value) == _OPAQUE_TOKEN_BYTES else None
    try:
        return decode_opaque_token(value)
    except TokenFormatError:
        return None


def _decode_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise PersistenceUnavailable("SQLite contains unreadable durable data.") from error
    if not isinstance(value, dict):
        raise PersistenceUnavailable("SQLite contains unreadable durable data.")
    return value


def _validate_failure(failure_code: str, failure_message: str) -> None:
    if failure_code not in _FAILURE_CODES:
        raise ValueError("Unknown durable failure code.")
    if (
        not isinstance(failure_message, str)
        or not failure_message.strip()
        or len(failure_message) > 500
    ):
        raise ValueError("Failure messages must be safe non-empty text up to 500 characters.")


def _add_hours(timestamp: str, hours: int) -> str:
    return _format_timestamp(_parse_timestamp(timestamp) + timedelta(hours=hours))


def _add_seconds(timestamp: str, seconds: int) -> str:
    return _format_timestamp(_parse_timestamp(timestamp) + timedelta(seconds=seconds))


def _parse_timestamp(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
