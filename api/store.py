"""Durable, owner-scoped SQLite persistence for the frozen Hramatka pilot.

This module intentionally contains no HTTP concerns.  It exposes only durable
operations and semantic exceptions; the FastAPI layer maps those exceptions to
the frozen error envelope.  Raw invite and session secrets never enter a
record, database row, exception message, or loggable return value.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
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
from .validation import validate_lesson

_OPAQUE_TOKEN_BYTES = 32
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
_LOCAL_STATIC_TEACHER_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "https://hramatka.local/static-teacher/v1")
)
_LOCAL_STATIC_TEACHER_NAME = "Локальний викладач"
_SESSION_ABSOLUTE_HOURS = 24 * 7
_SESSION_IDLE_HOURS = 24
_FAILURE_CODES = frozenset(
    {
        "bake_timeout",
        "worker_restarted",
        "provider_unavailable",
        "engine_unavailable",
        "lesson_schema_invalid",
        "unknown_safe_failure",
        "lesson_floor_unmet",
        "generation_failed",
        "no_eligible_activities",
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


class LessonBlockNotFound(ValueError):
    """A present ready lesson has no block with that id."""


class RejectedEntryNotFound(ValueError):
    """A present ready lesson has no rejected entry at the requested index."""


class ReviewMutationInvalid(ValueError):
    """A review mutation would produce an invalid frozen lesson document."""


class WarningAcknowledgementsRequired(ValueError):
    """Acceptance requires every visible warning acknowledgement."""

    def __init__(self, block_ids: list[str]) -> None:
        self.block_ids = list(block_ids)
        super().__init__("Visible warning blocks require acknowledgement.")


# Kept as a compatibility import for callers still using the prototype spelling.
WarningBlocksUnacknowledged = WarningAcknowledgementsRequired


FOCUS_STATUS_ACK_ID = "focus-status"
"""Reserved lesson-level pseudo-block id acknowledging an unsupported focus.

Real ids are generated and always carry a prefix — ``block-<n>`` from the
composer, ``restored-<uuid4hex>`` from a teacher restore — so this literal
cannot name an actual block. ``tests/test_pilot_backend_contract.py`` asserts
the namespace stays disjoint.
"""


def block_needs_review(block: Mapping[str, Any]) -> bool:
    """Return whether a visible block requires teacher acknowledgement.

    Mirrors the UI predicate ``blockNeedsReview`` in
    ``hramatka/app/src/review-helpers.ts``: a block needs review when its mark
    is ``warn`` **or** ``provenance.external_options`` is strictly true.

    This is the single server-side definition used by both
    ``acknowledge_warning`` (which ids may be acked) and ``accept_lesson``
    (which ids are required before acceptance). Keep the two sites in lockstep
    by routing them through this helper only.
    """
    if block.get("mark") == "warn":
        return True
    provenance = block.get("provenance")
    return isinstance(provenance, Mapping) and provenance.get("external_options") is True


def focus_status_needs_review(lesson: Mapping[str, Any]) -> bool:
    """Return whether the lesson's focus outcome requires acknowledgement.

    A focus the anchor cannot support is a quality caveat, not an FYI: the
    teacher acknowledges it explicitly, exactly like a warn block. A supported
    focus — and an absent ``focus_status``, meaning none was requested — needs
    nothing.
    """
    focus_status = lesson.get("focus_status")
    return isinstance(focus_status, Mapping) and focus_status.get("supported") is False


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
    invite_id: str | None
    created_at: str
    expires_at: str
    revoked_at: str | None
    auth_method: str = "invite"


@dataclass(frozen=True)
class RedeemedSession:
    """Committed session metadata and the one transient browser credential."""

    session: SessionRecord
    raw_secret: bytes


@dataclass(frozen=True)
class WebAuthnChallenge:
    id: str
    raw_challenge: bytes


@dataclass(frozen=True)
class CredentialRecord:
    id: str
    teacher_id: str
    credential_id: bytes
    public_key: bytes
    sign_count: int


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
    def anchor_source_url(self) -> str | None:
        value = self.request["anchor"].get("source_url")
        return value if isinstance(value, str) else None

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
    def methodology(self) -> str:
        """The durable methodology, defaulting only for pre-field pilot jobs."""
        value = self.request.get("methodology", "ttt")
        if value != "ttt":
            raise ValueError("Durable methodology is invalid.")
        return value

    @property
    def grammar_focus(self) -> str | None:
        """The normalized teacher grammar focus, absent on older job records."""
        value = self.request.get("grammar_focus")
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 120
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("Durable grammar focus is invalid.")
        return value

    @property
    def logical_model_id(self) -> str | None:
        """Selected teacher model, absent only on pre-#244 durable jobs."""
        request = self.request
        if "logical_model_id" not in request:
            return None
        value = request["logical_model_id"]
        if not isinstance(value, str) or not value:
            raise ValueError("Durable logical model ID is invalid.")
        return value

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
    methodology: str
    grammar_focus: str | None
    anchor_snippet: str
    level: str
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


def redeem_nonce_digest(raw_nonce: bytes) -> bytes:
    """Return the only browser-entry nonce value permitted in SQLite."""
    return hashlib.sha256(b"hramatka-redeem-nonce\0" + raw_nonce).digest()


def token_digest(domain: bytes, raw: bytes) -> bytes:
    """Persist a purpose-separated, non-recoverable ceremony proof."""
    return hashlib.sha256(domain + b"\0" + raw).digest()


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
    anchor_source_url: str | None = None,
    level: str = "B1",
    duration: int,
    focus: str | None,
    methodology: str = "ttt",
    grammar_focus: str | None = None,
    logical_model_id: str | None = None,
) -> str:
    """Build the complete canonical request defined by the frozen OpenAPI contract."""
    if anchor_source not in {"teacher-paste", "teacher-url"}:
        raise ValueError("The pilot accepts only teacher-paste or teacher-url anchors.")
    if anchor_source == "teacher-url" and not anchor_source_url:
        raise ValueError("teacher-url anchors require source_url.")
    if anchor_source == "teacher-paste" and anchor_source_url is not None:
        raise ValueError("teacher-paste anchors must not include source_url.")
    if level != "B1":
        raise ValueError("The pilot accepts only B1 lessons.")
    if duration not in {45, 60, 90}:
        raise ValueError("Unsupported lesson duration.")
    if not isinstance(anchor_text, str) or not anchor_text.strip():
        raise ValueError("Anchor text must not be blank.")
    if not isinstance(focus, str | type(None)):
        raise ValueError("Focus must be a string or null.")
    if methodology != "ttt":
        raise ValueError("Unsupported methodology.")
    if grammar_focus is not None:
        if (
            not isinstance(grammar_focus, str)
            or not grammar_focus
            or len(grammar_focus) > 120
            or "\n" in grammar_focus
            or "\r" in grammar_focus
            or grammar_focus != grammar_focus.strip()
        ):
            raise ValueError(
                "Grammar focus must be a trimmed one-line string of at most 120 characters."
            )
    if logical_model_id is not None and (
        not isinstance(logical_model_id, str) or not logical_model_id
    ):
        raise ValueError("Logical model ID must be a non-empty string or null.")
    anchor: dict[str, Any] = {"source": anchor_source, "text": anchor_text}
    if anchor_source_url is not None:
        anchor["source_url"] = anchor_source_url
    request = {
        "anchor": anchor,
        "duration": duration,
        "focus": focus,
        "grammar_focus": grammar_focus,
        "level": level,
        "methodology": methodology,
    }
    if logical_model_id is not None:
        request["logical_model_id"] = logical_model_id
    return canonical_json(request)


def request_hash(
    *,
    anchor_text: str,
    anchor_source: str = "teacher-paste",
    anchor_source_url: str | None = None,
    level: str = "B1",
    duration: int,
    focus: str | None,
    methodology: str = "ttt",
    grammar_focus: str | None = None,
    logical_model_id: str | None = None,
) -> bytes:
    """SHA-256 over the canonical UTF-8 request JSON, stored as a BLOB."""
    return hashlib.sha256(
        canonical_request_json(
            anchor_text=anchor_text,
            anchor_source=anchor_source,
            anchor_source_url=anchor_source_url,
            level=level,
            duration=duration,
            focus=focus,
            methodology=methodology,
            grammar_focus=grammar_focus,
            logical_model_id=logical_model_id,
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

    def create_local_static_session(self) -> RedeemedSession:
        """Create a normal session for the one stable local-only teacher.

        This deliberately has no invite row: the caller is already guarded by
        the loopback-only application factory route.  The browser credential is
        still a fresh opaque value and is stored only as a digest, exactly like
        an invite-redeemed session.
        """
        timestamp = now_iso()
        raw_secret = secrets.token_bytes(_OPAQUE_TOKEN_BYTES)
        session = SessionRecord(
            id=str(uuid.uuid4()),
            teacher_id=_LOCAL_STATIC_TEACHER_ID,
            teacher_display_name=_LOCAL_STATIC_TEACHER_NAME,
            invite_id=None,
            created_at=timestamp,
            expires_at=_add_hours(timestamp, _SESSION_ABSOLUTE_HOURS),
            revoked_at=None,
            auth_method="local",
        )
        with self._write_transaction() as connection:
            teacher_row = connection.execute(
                """
                SELECT deactivated_at FROM pilot_teachers
                WHERE id = ?
                """,
                (_LOCAL_STATIC_TEACHER_ID,),
            ).fetchone()
            if teacher_row is None:
                connection.execute(
                    """
                    INSERT INTO pilot_teachers (id, display_name, created_at, deactivated_at)
                    VALUES (?, ?, ?, NULL)
                    """,
                    (_LOCAL_STATIC_TEACHER_ID, _LOCAL_STATIC_TEACHER_NAME, timestamp),
                )
            elif teacher_row["deactivated_at"] is not None:
                raise SessionUnavailable("The local teacher account is deactivated.")
            connection.execute(
                """
                INSERT INTO pilot_sessions (
                    id, teacher_id, invite_id, secret_hash, redeem_nonce_hash,
                    created_at, expires_at, idle_expires_at, last_seen_at, revoked_at, auth_method
                ) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, NULL, 'local')
                """,
                (
                    session.id,
                    session.teacher_id,
                    session_secret_digest(raw_secret),
                    session.created_at,
                    session.expires_at,
                    _add_hours(timestamp, _SESSION_IDLE_HOURS),
                    timestamp,
                ),
            )
        return RedeemedSession(session=session, raw_secret=raw_secret)

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
                       s.invite_id, s.created_at, s.expires_at, s.revoked_at, s.auth_method
                FROM pilot_sessions AS s
                JOIN pilot_teachers AS t ON t.id = s.teacher_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._session_record(row) if row is not None else None

    def revoke_teacher_sessions(self, teacher_id: str) -> int:
        """Durably revoke every active session for one teacher without deactivation."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE pilot_sessions SET revoked_at = COALESCE(revoked_at, ?)
                WHERE teacher_id = ? AND revoked_at IS NULL AND expires_at > ?
                  AND idle_expires_at > ?
                """,
                (timestamp, teacher_id, timestamp, timestamp),
            )
        return cursor.rowcount

    # -- Browser session lifecycle -----------------------------------------

    def redeem_invite(self, token: str, nonce: str) -> RedeemedSession:
        """Consume an invite once, with same-browser recovery after a lost response."""
        raw_token = decode_opaque_token(token)
        raw_nonce = decode_opaque_token(nonce)
        timestamp = now_iso()
        session_secret = secrets.token_bytes(_OPAQUE_TOKEN_BYTES)
        idle_expires_at = _add_hours(timestamp, _SESSION_IDLE_HOURS)
        session = SessionRecord(
            id=str(uuid.uuid4()),
            teacher_id="",
            teacher_display_name="",
            invite_id="",
            created_at=timestamp,
            expires_at=_add_hours(timestamp, _SESSION_ABSOLUTE_HOURS),
            revoked_at=None,
            auth_method="invite",
        )
        with self._write_transaction() as connection:
            invite_row = connection.execute(
                """
                SELECT i.id, i.teacher_id, i.redeemed_at, i.expires_at,
                       t.display_name, s.id AS session_id, s.created_at AS session_created_at,
                       s.expires_at AS session_expires_at,
                       s.idle_expires_at, s.revoked_at AS session_revoked_at,
                       s.redeem_nonce_hash
                FROM pilot_invites AS i
                JOIN pilot_teachers AS t ON t.id = i.teacher_id
                LEFT JOIN pilot_sessions AS s ON s.invite_id = i.id
                WHERE i.token_hash = ?
                  AND i.revoked_at IS NULL
                  AND t.deactivated_at IS NULL
                """,
                (invite_token_digest(raw_token),),
            ).fetchone()
            if invite_row is None:
                raise InviteUnavailable("This invite is no longer available.")
            if invite_row["redeemed_at"] is not None:
                if (
                    invite_row["session_id"] is None
                    or invite_row["redeem_nonce_hash"] is None
                    or not secrets.compare_digest(
                        invite_row["redeem_nonce_hash"], redeem_nonce_digest(raw_nonce)
                    )
                    or invite_row["session_revoked_at"] is not None
                    or invite_row["session_expires_at"] <= timestamp
                    or invite_row["idle_expires_at"] is None
                ):
                    raise InviteUnavailable("This invite is no longer available.")
                # The first transaction committed, but its Set-Cookie response was
                # lost. Reissue only to the same browser proof, rotating the opaque
                # credential without ever recovering or storing the old secret.
                updated = connection.execute(
                    """
                    UPDATE pilot_sessions
                    SET secret_hash = ?, last_seen_at = ?, idle_expires_at = CASE
                        WHEN expires_at < ? THEN expires_at ELSE ? END
                    WHERE id = ? AND redeem_nonce_hash = ? AND revoked_at IS NULL
                    """,
                    (
                        session_secret_digest(session_secret),
                        timestamp,
                        _add_hours(timestamp, _SESSION_IDLE_HOURS),
                        _add_hours(timestamp, _SESSION_IDLE_HOURS),
                        invite_row["session_id"],
                        redeem_nonce_digest(raw_nonce),
                    ),
                )
                if updated.rowcount != 1:
                    raise InviteUnavailable("This invite is no longer available.")
                return RedeemedSession(
                    session=SessionRecord(
                        id=invite_row["session_id"],
                        teacher_id=invite_row["teacher_id"],
                        teacher_display_name=invite_row["display_name"],
                        invite_id=invite_row["id"],
                        created_at=invite_row["session_created_at"],
                        expires_at=invite_row["session_expires_at"],
                        revoked_at=None,
                        auth_method="invite",
                    ),
                    raw_secret=session_secret,
                )
            if invite_row["expires_at"] <= timestamp:
                raise InviteUnavailable("This invite is no longer available.")
            session = SessionRecord(
                id=session.id,
                teacher_id=invite_row["teacher_id"],
                teacher_display_name=invite_row["display_name"],
                invite_id=invite_row["id"],
                created_at=session.created_at,
                expires_at=session.expires_at,
                revoked_at=None,
                auth_method="invite",
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
                    id, teacher_id, invite_id, secret_hash, redeem_nonce_hash,
                    created_at, expires_at, idle_expires_at, last_seen_at, revoked_at, auth_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'invite')
                """,
                (
                    session.id,
                    session.teacher_id,
                    session.invite_id,
                    session_secret_digest(session_secret),
                    redeem_nonce_digest(raw_nonce),
                    session.created_at,
                    session.expires_at,
                    idle_expires_at,
                    timestamp,
                ),
            )
        return RedeemedSession(session=session, raw_secret=session_secret)

    def lookup_session(self, raw_secret: bytes | str) -> SessionRecord | None:
        """Make the full expiry/revocation/deactivation authorization decision in SQL."""
        raw = _coerce_opaque_secret(raw_secret)
        if raw is None:
            return None
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT s.id, s.teacher_id, t.display_name AS teacher_display_name,
                       s.invite_id, s.created_at, s.expires_at, s.revoked_at, s.auth_method
                FROM pilot_sessions AS s
                JOIN pilot_teachers AS t ON t.id = s.teacher_id
                WHERE s.secret_hash = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND s.idle_expires_at > ?
                  AND t.deactivated_at IS NULL
                """,
                (session_secret_digest(raw), timestamp, timestamp),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE pilot_sessions
                    SET last_seen_at = ?, idle_expires_at = CASE
                        WHEN expires_at < ? THEN expires_at ELSE ? END
                    WHERE id = ?
                    """,
                    (
                        timestamp,
                        _add_hours(timestamp, _SESSION_IDLE_HOURS),
                        _add_hours(timestamp, _SESSION_IDLE_HOURS),
                        row["id"],
                    ),
                )
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
                  AND idle_expires_at > ?
                  AND EXISTS (
                    SELECT 1 FROM pilot_teachers AS t
                    WHERE t.id = pilot_sessions.teacher_id AND t.deactivated_at IS NULL
                  )
                """,
                (timestamp, session_secret_digest(raw), timestamp, timestamp),
            )
        return cursor.rowcount == 1

    # -- WebAuthn and recovery-code lifecycle ------------------------------

    def issue_webauthn_challenge(
        self, *, kind: str, teacher_id: str | None = None, session_id: str | None = None
    ) -> WebAuthnChallenge:
        if kind not in {"enrollment", "assertion"}:
            raise ValueError("Unsupported WebAuthn ceremony.")
        raw = secrets.token_bytes(32)
        record = WebAuthnChallenge(id=str(uuid.uuid4()), raw_challenge=raw)
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO webauthn_challenges
                (id, teacher_id, session_id, kind, challenge_hash, expires_at, used_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    record.id,
                    teacher_id,
                    session_id,
                    kind,
                    token_digest(b"hramatka-webauthn-challenge", raw),
                    _add_hours(now_iso(), 1),
                ),
            )
        return record

    def consume_webauthn_challenge(
        self, *, challenge_id: str, raw_challenge: bytes, kind: str, session_id: str | None = None
    ) -> str | bool | None:
        """Atomically consume one unexpired ceremony and return its bound teacher."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT teacher_id FROM webauthn_challenges
                WHERE id = ? AND kind = ? AND challenge_hash = ? AND expires_at > ?
                  AND used_at IS NULL AND (session_id IS ?)
                """,
                (
                    challenge_id,
                    kind,
                    token_digest(b"hramatka-webauthn-challenge", raw_challenge),
                    timestamp,
                    session_id,
                ),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE webauthn_challenges SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (timestamp, challenge_id),
            )
            if changed.rowcount != 1:
                return None
        return row["teacher_id"] if row["teacher_id"] is not None else True

    def add_webauthn_credential(
        self, *, teacher_id: str, credential_id: bytes, public_key: bytes, sign_count: int
    ) -> None:
        with self._write_transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM pilot_teachers WHERE id = ? AND deactivated_at IS NULL",
                (teacher_id,),
            ).fetchone()
            if active is None:
                raise SessionUnavailable("The teacher session is no longer active.")
            connection.execute(
                """
                INSERT INTO webauthn_credentials
                (id, teacher_id, credential_id, public_key, sign_count, created_at,
                 last_used_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (str(uuid.uuid4()), teacher_id, credential_id, public_key, sign_count, now_iso()),
            )

    def lookup_webauthn_credential(self, credential_id: bytes) -> CredentialRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT c.id, c.teacher_id, c.credential_id, c.public_key, c.sign_count
                FROM webauthn_credentials AS c JOIN pilot_teachers AS t ON t.id = c.teacher_id
                WHERE c.credential_id = ? AND c.revoked_at IS NULL AND t.deactivated_at IS NULL
                """,
                (credential_id,),
            ).fetchone()
        return None if row is None else CredentialRecord(**dict(row))

    def record_webauthn_use(self, credential_id: bytes, sign_count: int) -> str | None:
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT c.teacher_id, t.display_name FROM webauthn_credentials AS c
                JOIN pilot_teachers AS t ON t.id = c.teacher_id
                WHERE c.credential_id = ? AND c.revoked_at IS NULL AND t.deactivated_at IS NULL
                """,
                (credential_id,),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = ? "
                "WHERE credential_id = ? AND ((sign_count = 0 AND ? = 0) OR ? > sign_count)",
                (sign_count, now_iso(), credential_id, sign_count, sign_count),
            )
            if changed.rowcount != 1:
                return None
        return row["teacher_id"]

    def mint_reentry_session(self, teacher_id: str, *, auth_method: str) -> RedeemedSession:
        """The shared, opaque session seam used by all non-invite entry doors."""
        if auth_method not in {"passkey", "recovery"}:
            raise ValueError("Unsupported re-entry method.")
        timestamp = now_iso()
        raw_secret = secrets.token_bytes(_OPAQUE_TOKEN_BYTES)
        session = SessionRecord(
            id=str(uuid.uuid4()),
            teacher_id=teacher_id,
            teacher_display_name="",
            invite_id=None,
            created_at=timestamp,
            expires_at=_add_hours(timestamp, _SESSION_ABSOLUTE_HOURS),
            revoked_at=None,
            auth_method=auth_method,
        )
        with self._write_transaction() as connection:
            teacher = connection.execute(
                "SELECT display_name FROM pilot_teachers WHERE id = ? AND deactivated_at IS NULL",
                (teacher_id,),
            ).fetchone()
            if teacher is None:
                raise SessionUnavailable("The teacher session is no longer active.")
            connection.execute(
                """
                INSERT INTO pilot_sessions
                (id, teacher_id, invite_id, secret_hash, redeem_nonce_hash, created_at, expires_at,
                 idle_expires_at, last_seen_at, revoked_at, auth_method)
                VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    session.id,
                    teacher_id,
                    session_secret_digest(raw_secret),
                    timestamp,
                    session.expires_at,
                    _add_hours(timestamp, _SESSION_IDLE_HOURS),
                    timestamp,
                    auth_method,
                ),
            )
        return RedeemedSession(
            session=SessionRecord(
                **{**session.__dict__, "teacher_display_name": teacher["display_name"]}
            ),
            raw_secret=raw_secret,
        )

    def regenerate_recovery_codes(self, teacher_id: str, *, count: int = 10) -> list[str]:
        codes = [
            base64.urlsafe_b64encode(secrets.token_bytes(12)).decode("ascii").rstrip("=").upper()
            for _ in range(count)
        ]
        timestamp = now_iso()
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE recovery_codes SET superseded_at = ? WHERE teacher_id = ? "
                "AND used_at IS NULL AND superseded_at IS NULL",
                (timestamp, teacher_id),
            )
            connection.executemany(
                "INSERT INTO recovery_codes "
                "(id, teacher_id, code_hash, created_at, used_at, superseded_at) "
                "VALUES (?, ?, ?, ?, NULL, NULL)",
                [
                    (
                        str(uuid.uuid4()),
                        teacher_id,
                        token_digest(b"hramatka-recovery-code", code.encode()),
                        timestamp,
                    )
                    for code in codes
                ],
            )
        return codes

    def redeem_recovery_code(self, code: str) -> str | None:
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT r.teacher_id FROM recovery_codes AS r
                JOIN pilot_teachers AS t ON t.id = r.teacher_id
                WHERE r.code_hash = ? AND r.used_at IS NULL
                  AND r.superseded_at IS NULL AND t.deactivated_at IS NULL
                """,
                (token_digest(b"hramatka-recovery-code", code.encode()),),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE recovery_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                (timestamp, token_digest(b"hramatka-recovery-code", code.encode())),
            )
            return row["teacher_id"] if changed.rowcount == 1 else None

    # -- Teacher preferences (P2-6) ----------------------------------------

    def get_teacher_default_duration(self, teacher_id: str) -> int:
        """Return persisted default_duration or 60 when absent (additive table)."""
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT default_duration FROM teacher_preferences WHERE teacher_id = ?",
                (teacher_id,),
            ).fetchone()
            if row is None:
                return 60
            return int(row["default_duration"])

    def set_teacher_default_duration(self, teacher_id: str, duration: int) -> None:
        """Upsert default_duration for owner; validates and commits before return."""
        if duration not in (45, 60, 90):
            raise ValueError("default_duration must be one of 45, 60, 90")
        ts = now_iso()
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO teacher_preferences (teacher_id, default_duration, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(teacher_id) DO UPDATE SET
                    default_duration = excluded.default_duration,
                    updated_at = excluded.updated_at
                """,
                (teacher_id, duration, ts),
            )

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
        methodology: str = "ttt",
        grammar_focus: str | None = None,
        anchor_source: str = "teacher-paste",
        anchor_source_url: str | None = None,
        logical_model_id: str | None = None,
    ) -> tuple[JobRecord, bool]:
        request_json = canonical_request_json(
            anchor_text=anchor_text,
            anchor_source=anchor_source,
            anchor_source_url=anchor_source_url,
            level=level,
            duration=duration,
            focus=focus,
            methodology=methodology,
            grammar_focus=grammar_focus,
            logical_model_id=logical_model_id,
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

    def seed_ready(
        self,
        teacher_id: str,
        lesson_id: str,
        lesson: Mapping[str, Any],
        *,
        request_json: str,
    ) -> tuple[JobRecord, bool]:
        """Insert one validated ready lesson for an explicitly local dev workflow.

        The production bake lifecycle remains draft -> baking -> ready.  This
        narrowly scoped operation exists for the offline seed CLI, whose input
        has already been selected by a local operator.  It still applies the
        API validator and all durable ownership and idempotency checks.
        """
        materialized = dict(lesson)
        validate_lesson(materialized)
        if materialized.get("id") != lesson_id:
            raise ValueError("Seed lesson ID must match the durable lesson ID.")
        if materialized.get("status") != "ready":
            raise ValueError("A seeded lesson must have ready status.")
        if materialized.get("accepted") is not False:
            raise ValueError("A seeded lesson must be unaccepted.")

        request = _decode_json_object(request_json)
        if canonical_json(request) != request_json:
            raise ValueError("Seed request JSON must be canonical.")
        try:
            request_anchor = request["anchor"]
            lesson_anchor = materialized["anchor"]
            request_matches_lesson = (
                request_anchor["text"] == lesson_anchor["text"]
                and request_anchor["source"] == lesson_anchor["source"]
                and request["duration"] == materialized["duration"]
                and request["level"] == materialized["level"]
                and request["focus"] == materialized["focus"]
                and request["methodology"] == materialized["method"]
            )
        except (KeyError, TypeError):
            request_matches_lesson = False
        if not request_matches_lesson:
            raise ValueError("Seed request JSON must describe the seeded lesson.")

        lesson_json = canonical_json(materialized)
        digest = hashlib.sha256(request_json.encode("utf-8")).digest()
        created_at = materialized["created_at"]
        updated_at = materialized["updated_at"]
        with self._write_transaction() as connection:
            active_teacher = connection.execute(
                """
                SELECT 1 FROM pilot_teachers
                WHERE id = ? AND deactivated_at IS NULL
                """,
                (teacher_id,),
            ).fetchone()
            if active_teacher is None:
                raise SessionUnavailable("The lesson owner is unavailable.")
            try:
                connection.execute(
                    """
                    INSERT INTO lesson_jobs (
                        teacher_id, id, request_json, request_hash, status, step,
                        failure_code, failure_message, lesson_json,
                        warning_acknowledgements_json, accepted, accepted_at,
                        accepted_revision, revision, created_at, updated_at,
                        started_at, completed_at, progress_json
                    ) VALUES (
                        ?, ?, ?, ?, 'ready', ?, NULL, NULL, ?, '[]', 0,
                        NULL, NULL, 1, ?, ?, NULL, ?, NULL
                    )
                    """,
                    (
                        teacher_id,
                        lesson_id,
                        request_json,
                        digest,
                        _STEP_COMPLETE,
                        lesson_json,
                        created_at,
                        updated_at,
                        updated_at,
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
                raise PersistenceUnavailable("The lesson owner is unavailable.")
            job = self._record(row)
            if job.request_hash != digest or job.lesson != materialized:
                raise IdempotencyConflict(
                    "This seed lesson ID is already bound to different content."
                )
        return job, created

    def delete_lesson(self, teacher_id: str, lesson_id: str) -> bool:
        """Delete one owner-scoped lesson job row regardless of status."""
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM lesson_jobs WHERE teacher_id = ? AND id = ?",
                (teacher_id, lesson_id),
            )
        return cursor.rowcount == 1

    def list_catalog(self, teacher_id: str) -> list[CatalogRecord]:
        """Return metadata only, using exactly the owner-scoped catalog predicate."""
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT id,
                       json_extract(lesson_json, '$.title') AS title,
                       COALESCE(
                           json_extract(lesson_json, '$.duration'),
                           json_extract(request_json, '$.duration')
                       ) AS duration,
                       json_extract(request_json, '$.focus') AS focus,
                       COALESCE(json_extract(request_json, '$.methodology'), 'ttt') AS methodology,
                       json_extract(request_json, '$.grammar_focus') AS grammar_focus,
                       json_extract(request_json, '$.level') AS level,
                       substr(
                           trim(
                               replace(
                                   replace(
                                       json_extract(request_json, '$.anchor.text'),
                                       char(10),
                                       ' '
                                   ),
                                   char(13),
                                   ' '
                               )
                           ),
                           1, 140
                       ) AS anchor_snippet,
                       status, revision, accepted, accepted_at, accepted_revision,
                       failure_code, created_at, updated_at
                FROM lesson_jobs
                WHERE teacher_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (teacher_id,),
            ).fetchall()
        return [self._catalog_record(row) for row in rows]

    # -- Runner transitions -------------------------------------------------

    def claim_next_draft(self) -> JobRecord | None:
        """Atomically claim the next queued job for one in-process pool worker."""
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
        """Fail drafts only when the whole in-process worker pool is quarantined."""
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

    def move_block(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        block_id: str,
        direction: str,
        expected_revision: int,
    ) -> JobRecord:
        from . import lesson as lesson_tools

        def mutation(lesson: dict[str, Any], _: set[str]) -> None:
            try:
                lesson_tools.move_block(lesson, block_id, direction)
            except KeyError as error:
                raise LessonBlockNotFound(block_id) from error

        return self._mutate_review_lesson(
            teacher_id, lesson_id, expected_revision=expected_revision, mutation=mutation
        )

    def remove_block(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        block_id: str,
        expected_revision: int,
    ) -> JobRecord:
        from . import lesson as lesson_tools

        def mutation(lesson: dict[str, Any], acknowledgements: set[str]) -> None:
            try:
                lesson_tools.remove_block_to_rejected(lesson, block_id)
            except KeyError as error:
                raise LessonBlockNotFound(block_id) from error
            acknowledgements.discard(block_id)

        return self._mutate_review_lesson(
            teacher_id, lesson_id, expected_revision=expected_revision, mutation=mutation
        )

    def include_reserve_block(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        block_id: str,
        expected_revision: int,
    ) -> JobRecord:
        from . import lesson as lesson_tools

        def mutation(lesson: dict[str, Any], _: set[str]) -> None:
            try:
                lesson_tools.include_reserve_block(lesson, block_id)
            except KeyError as error:
                raise LessonBlockNotFound(block_id) from error

        return self._mutate_review_lesson(
            teacher_id, lesson_id, expected_revision=expected_revision, mutation=mutation
        )

    def replace_block_activity(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        block_id: str,
        activity: Mapping[str, Any],
        expected_revision: int,
    ) -> JobRecord:
        from . import lesson as lesson_tools

        def mutation(lesson: dict[str, Any], acknowledgements: set[str]) -> None:
            try:
                warning_was_edited = lesson_tools.replace_block_activity(
                    lesson, block_id, dict(activity)
                )
            except KeyError as error:
                raise LessonBlockNotFound(block_id) from error
            if warning_was_edited:
                acknowledgements.discard(block_id)

        return self._mutate_review_lesson(
            teacher_id, lesson_id, expected_revision=expected_revision, mutation=mutation
        )

    def restore_rejected_entry(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        rejected_index: int,
        phase: int,
        expected_revision: int,
    ) -> JobRecord:
        from . import lesson as lesson_tools

        def mutation(lesson: dict[str, Any], _: set[str]) -> None:
            try:
                lesson_tools.restore_rejected_entry(lesson, rejected_index, phase)
            except IndexError as error:
                raise RejectedEntryNotFound(rejected_index) from error

        return self._mutate_review_lesson(
            teacher_id, lesson_id, expected_revision=expected_revision, mutation=mutation
        )

    def select_duration(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        duration: int,
        expected_revision: int,
    ) -> JobRecord:
        from . import lesson as lesson_tools

        def mutation(lesson: dict[str, Any], _: set[str]) -> None:
            lesson_tools.select_duration(lesson, duration)

        return self._mutate_review_lesson(
            teacher_id, lesson_id, expected_revision=expected_revision, mutation=mutation
        )

    def _mutate_review_lesson(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        expected_revision: int,
        mutation: Callable[[dict[str, Any], set[str]], None],
    ) -> JobRecord:
        """Apply one validated review edit in the same revision-guarded transaction."""
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            self._require_expected_revision(job, expected_revision)
            self._require_ready(job)
            if job.accepted:
                raise LessonStateConflict(
                    "The accepted lesson must return to draft before editing."
                )
            lesson = copy.deepcopy(self._require_lesson(job))
            acknowledgements = set(job.warning_acknowledgements)
            try:
                mutation(lesson, acknowledgements)
                validate_lesson(lesson)
            except (LessonBlockNotFound, RejectedEntryNotFound):
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise ReviewMutationInvalid("Review mutation is invalid.") from error
            timestamp = now_iso()
            lesson["updated_at"] = timestamp
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET lesson_json = ?, warning_acknowledgements_json = ?, updated_at = ?,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'ready' AND accepted = 0
                  AND revision = ?
                """,
                (
                    canonical_json(lesson),
                    canonical_json(sorted(acknowledgements)),
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

    def acknowledge_warning(
        self, teacher_id: str, lesson_id: str, block_id: str, expected_revision: int
    ) -> JobRecord:
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            self._require_expected_revision(job, expected_revision)
            self._require_ready(job)
            lesson = self._require_lesson(job)
            needs_review_ids = self._visible_needs_review_ids(lesson)
            if block_id not in needs_review_ids:
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
            required = self._visible_needs_review_ids(lesson)
            acknowledged = set(job.warning_acknowledgements)
            missing_acknowledgements = required - acknowledged
            if missing_acknowledgements:
                raise WarningAcknowledgementsRequired(sorted(missing_acknowledgements))
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
            auth_method=row["auth_method"] if "auth_method" in row.keys() else "invite",
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        try:
            lesson = json.loads(row["lesson_json"]) if row["lesson_json"] is not None else None
            acknowledgements = json.loads(row["warning_acknowledgements_json"])
            progress = (
                json.loads(row["progress_json"])
                if ("progress_json" in row.keys() and row["progress_json"] is not None)
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
            methodology=row["methodology"],
            grammar_focus=row["grammar_focus"],
            anchor_snippet=row["anchor_snippet"],
            level=row["level"],
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
        try:
            from .lesson import split_review_blocks

            visible, _ = split_review_blocks(dict(lesson))
            return visible
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceUnavailable("SQLite contains an invalid ready lesson.") from error

    @classmethod
    def _visible_needs_review_ids(cls, lesson: Mapping[str, Any]) -> set[str]:
        """Ids requiring ack: visible warn blocks, plus an unsupported focus.

        Widening the set here is the whole change: ``acknowledge_warning`` will
        admit the reserved id and ``accept_lesson`` will demand it, with no
        condition duplicated at either call site.
        """
        ids = {block["id"] for block in cls._visible_blocks(lesson) if block_needs_review(block)}
        if focus_status_needs_review(lesson):
            ids.add(FOCUS_STATUS_ACK_ID)
        return ids

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
