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
from typing import Any, Literal

from .migrations import (
    EXPECTED_SCHEMA_VERSION,
    MigrationError,
    apply_migrations,
    current_schema_version,
)
from .validation import validate_lesson

_OPAQUE_TOKEN_BYTES = 32
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
_GOOGLE_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
_LOCAL_STATIC_TEACHER_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "https://hramatka.local/static-teacher/v1")
)
_LOCAL_STATIC_TEACHER_NAME = "Локальний викладач"
_SESSION_ABSOLUTE_HOURS = 24 * 7
_SESSION_IDLE_HOURS = 24
_GOOGLE_NONCE_SECONDS = 10 * 60
_FAILURE_CODES = frozenset(
    {
        "bake_timeout",
        "worker_restarted",
        "provider_unavailable",
        "engine_unavailable",
        "lesson_schema_invalid",
        "unknown_safe_failure",
        "insufficient_anchor_capacity",
        "lesson_floor_unmet",
        "generation_failed",
        "no_eligible_activities",
        "cancelled",
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


class GoogleNonceUnavailable(ValueError):
    """A Google browser ceremony nonce is unknown, expired, or already consumed."""


class GoogleIdentityUnavailable(ValueError):
    """A verified Google account is not linked to one active pilot teacher."""


StaffRole = Literal["admin", "teacher"]


class IdempotencyConflict(ValueError):
    """A teacher reused a lesson UUID for a different canonical request."""


class LessonNotFound(KeyError):
    """An owner-scoped lesson lookup had no row (including another owner's row)."""


class RevisionConflict(ValueError):
    """A mutation was made against a stale aggregate revision."""


class LessonStateConflict(ValueError):
    """The aggregate exists but its current state disallows that transition."""


class AttemptQuiescing(LessonStateConflict):
    """A terminal attempt still has a worker/provider call in flight."""


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


class FeedbackNotApplicable(ValueError):
    """Teacher feedback targets a block that is not currently engine-flagged (#402)."""


class ActivityRegenerationNotFound(KeyError):
    """An owner-scoped one-block regeneration job was not found."""


class ActivityRegenerationInProgress(ValueError):
    """The lesson already has queued or running regeneration work."""


class ActivityRegenerationStateConflict(ValueError):
    """A regeneration job cannot perform the requested state transition."""


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
    is ``warn``, ``provenance.external_options`` is strictly true, **or** the
    engine flagged it (``quality == "engine_flagged"``, #402) — flagged blocks
    join the existing acknowledge-then-accept flow rather than a novel block.

    This is the single server-side definition used by both
    ``acknowledge_warning`` (which ids may be acked) and ``accept_lesson``
    (which ids are required before acceptance). Keep the two sites in lockstep
    by routing them through this helper only.
    """
    if block.get("mark") == "warn":
        return True
    if block.get("quality") == "engine_flagged":
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
class StaffOwnerRecord:
    """Safe owner metadata, exposed only to an authenticated administrator."""

    id: str
    display_name: str
    role: StaffRole | None


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
class GoogleLoginNonce:
    """One short-lived nonce, never returned to logs or durable public API data."""

    id: str
    raw_nonce: str
    kind: str
    teacher_id: str | None
    session_id: str | None


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
    attempt: int
    attempt_history: tuple[dict[str, object], ...]
    retry_requested_revision: int | None
    attempt_token: str | None
    provider_started_at: str | None
    cancelled_acknowledged_at: str | None
    attempt_quiesced_at: str | None
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
class ActivityRegenerationRecord:
    id: str
    teacher_id: str
    lesson_id: str
    block_id: str
    request_hash: bytes
    base_revision: int
    status: str
    feedback: str | None
    attempt: int
    failure_code: str | None
    failure_message: str | None
    old_block_hash: str
    old_block: dict[str, Any]
    new_block_hash: str | None
    new_block: dict[str, Any] | None
    logical_model_id: str
    prompt_version: str
    prompt_sha256: str
    applied_revision: int | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


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


def normalize_google_email(email: str) -> str:
    """Canonicalize only a verified Google email for a preauthorization lookup."""
    if (
        not isinstance(email, str)
        or not 3 <= len(email) <= 320
        or email != email.strip()
        or not email.isascii()
        or _GOOGLE_EMAIL_RE.fullmatch(email) is None
    ):
        raise ValueError("Google email is invalid.")
    return email.casefold()


def google_email_preauthorization_digest(email: str) -> bytes:
    """Return a domain-separated digest for the sole preauthorization lookup."""
    normalized = normalize_google_email(email)
    return hashlib.sha256(
        b"hramatka-google-email-preauthorization\0" + normalized.encode("ascii")
    ).digest()


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


def _attempt_history_json(
    job: JobRecord,
    *,
    status: str,
    failure_code: str | None,
    completed_at: str,
    retry_requested_revision: int | None = None,
) -> str:
    """Append this attempt's terminal disposition exactly once."""
    history = [dict(item) for item in job.attempt_history]
    for item in history:
        if item["attempt"] == job.attempt:
            if retry_requested_revision is not None:
                item["retry_requested_revision"] = retry_requested_revision
            return canonical_json(history)
    history.append(
        {
            "attempt": job.attempt,
            "status": status,
            "failure_code": failure_code,
            "started_at": job.started_at,
            "completed_at": completed_at,
            "retry_requested_revision": retry_requested_revision,
        }
    )
    return canonical_json(history)


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
    """Build canonical durable request JSON, including retained legacy rows."""
    if anchor_source not in {"teacher-paste", "teacher-url"}:
        raise ValueError("A stored lesson request has an unsupported anchor source.")
    if anchor_source == "teacher-url" and not anchor_source_url:
        raise ValueError("teacher-url anchors require source_url.")
    if anchor_source == "teacher-paste" and anchor_source_url is not None:
        raise ValueError("teacher-paste anchors must not include source_url.")
    if level != "B1":
        raise ValueError("The pilot accepts only B1 lessons.")
    # This canonicalizer also validates retained historical request JSON, so it
    # continues to understand 60/90 rows. ``create_or_get`` is the creation gate.
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

    def grant_staff_role(
        self, teacher_id: str, role: StaffRole, *, require_google_identity: bool = False
    ) -> StaffRole:
        """Grant one explicit staff role; operator CLI is the only caller.

        An administrator bootstrap must already be bound to Google. A teacher
        may be granted before first Google binding so the one-time invite-based
        linking ceremony has no implicit account provisioning path.
        """
        if role not in {"admin", "teacher"}:
            raise ValueError("Unsupported staff role.")
        timestamp = now_iso()
        with self._write_transaction() as connection:
            teacher = connection.execute(
                "SELECT 1 FROM pilot_teachers WHERE id = ? AND deactivated_at IS NULL",
                (teacher_id,),
            ).fetchone()
            if teacher is None:
                raise ValueError("Teacher does not exist or is deactivated.")
            if require_google_identity:
                identity = connection.execute(
                    "SELECT 1 FROM google_teacher_identities "
                    "WHERE teacher_id = ? AND revoked_at IS NULL",
                    (teacher_id,),
                ).fetchone()
                if identity is None:
                    raise ValueError("Teacher has no active Google identity.")
            connection.execute(
                """
                INSERT INTO staff_authorization_grants (teacher_id, role, created_at, revoked_at)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(teacher_id) DO UPDATE SET role = excluded.role, revoked_at = NULL
                """,
                (teacher_id, role, timestamp),
            )
        return role

    def revoke_staff_role(self, teacher_id: str) -> bool:
        """Revoke a grant and every extant browser session in one transaction."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            changed = connection.execute(
                "UPDATE staff_authorization_grants SET revoked_at = COALESCE(revoked_at, ?) "
                "WHERE teacher_id = ? AND revoked_at IS NULL",
                (timestamp, teacher_id),
            )
            if changed.rowcount == 1:
                # Do not leave a valid-looking pre-revocation cookie that could
                # become usable again after a later regrant.
                connection.execute(
                    "UPDATE pilot_sessions SET revoked_at = COALESCE(revoked_at, ?) "
                    "WHERE teacher_id = ?",
                    (timestamp, teacher_id),
                )
                # Regranting a role must not revive a stale first-login email
                # reservation. An operator explicitly creates a fresh one.
                connection.execute(
                    "DELETE FROM google_email_preauthorizations "
                    "WHERE teacher_id = ? AND consumed_at IS NULL",
                    (teacher_id,),
                )
        return changed.rowcount == 1

    def active_staff_role(self, teacher_id: str) -> StaffRole | None:
        """Return only an active durable role for an active teacher."""
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT g.role FROM staff_authorization_grants AS g
                JOIN pilot_teachers AS t ON t.id = g.teacher_id
                WHERE g.teacher_id = ? AND g.revoked_at IS NULL AND t.deactivated_at IS NULL
                """,
                (teacher_id,),
            ).fetchone()
        return None if row is None else row["role"]

    def list_staff_owners(self) -> list[StaffOwnerRecord]:
        """List every durable lesson owner, including historical/inactive ones."""
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT t.id, t.display_name,
                       CASE WHEN g.revoked_at IS NULL AND t.deactivated_at IS NULL
                            THEN g.role ELSE NULL END AS role
                FROM pilot_teachers AS t
                LEFT JOIN staff_authorization_grants AS g ON g.teacher_id = t.id
                ORDER BY t.display_name COLLATE NOCASE, t.id
                """
            ).fetchall()
        return [
            StaffOwnerRecord(id=row["id"], display_name=row["display_name"], role=row["role"])
            for row in rows
        ]

    def owner_exists(self, teacher_id: str) -> bool:
        """Resolve a historical owner for an administrator without lesson-id lookup."""
        with self._read_connection() as connection:
            return connection.execute(
                "SELECT 1 FROM pilot_teachers WHERE id = ?", (teacher_id,)
            ).fetchone() is not None

    def preauthorize_google_email(self, teacher_id: str, email: str, role: StaffRole) -> StaffRole:
        """Atomically grant an existing teacher and reserve one verified Google email."""
        if role not in {"admin", "teacher"}:
            raise ValueError("Unsupported staff role.")
        email_digest = google_email_preauthorization_digest(email)
        timestamp = now_iso()
        with self._write_transaction() as connection:
            teacher = connection.execute(
                "SELECT 1 FROM pilot_teachers WHERE id = ? AND deactivated_at IS NULL",
                (teacher_id,),
            ).fetchone()
            if teacher is None:
                raise ValueError("Teacher does not exist or is deactivated.")
            identity = connection.execute(
                "SELECT 1 FROM google_teacher_identities WHERE teacher_id = ?", (teacher_id,)
            ).fetchone()
            if identity is not None:
                raise ValueError("Teacher already has a Google identity.")
            connection.execute(
                """
                INSERT INTO staff_authorization_grants (teacher_id, role, created_at, revoked_at)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(teacher_id) DO UPDATE SET role = excluded.role, revoked_at = NULL
                """,
                (teacher_id, role, timestamp),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO google_email_preauthorizations
                    (id, teacher_id, email_digest, created_at, consumed_at)
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (str(uuid.uuid4()), teacher_id, email_digest, timestamp),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("Google email preauthorization is unavailable.") from error
        return role

    def create_preauthorized_google_teacher(
        self, display_name: str, email: str, role: StaffRole
    ) -> TeacherRecord:
        """Create an operator-approved staff record and one-use Google email reservation."""
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 100:
            raise ValueError("Teacher display_name must be 1–100 characters.")
        if role not in {"admin", "teacher"}:
            raise ValueError("Unsupported staff role.")
        email_digest = google_email_preauthorization_digest(email)
        timestamp = now_iso()
        teacher = TeacherRecord(
            id=str(uuid.uuid4()),
            display_name=display_name,
            created_at=timestamp,
            deactivated_at=None,
        )
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO pilot_teachers (id, display_name, created_at, deactivated_at) "
                "VALUES (?, ?, ?, NULL)",
                (teacher.id, teacher.display_name, timestamp),
            )
            connection.execute(
                "INSERT INTO staff_authorization_grants "
                "(teacher_id, role, created_at, revoked_at) VALUES (?, ?, ?, NULL)",
                (teacher.id, role, timestamp),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO google_email_preauthorizations
                    (id, teacher_id, email_digest, created_at, consumed_at)
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (str(uuid.uuid4()), teacher.id, email_digest, timestamp),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("Google email preauthorization is unavailable.") from error
        return teacher

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

    def redeem_invite(
        self, token: str, nonce: str, *, require_active_staff_grant: bool = False
    ) -> RedeemedSession:
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
                  AND (
                    ? = 0 OR EXISTS (
                        SELECT 1 FROM staff_authorization_grants AS grant_row
                        WHERE grant_row.teacher_id = i.teacher_id
                          AND grant_row.revoked_at IS NULL
                    )
                  )
                """,
                (invite_token_digest(raw_token), int(require_active_staff_grant)),
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

    def authorized_staff_role(self, teacher_id: str) -> StaffRole | None:
        """Authorize a normal session on every request, fail-closed by SQL join."""
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT grant_row.role
                FROM staff_authorization_grants AS grant_row
                JOIN pilot_teachers AS teacher ON teacher.id = grant_row.teacher_id
                JOIN google_teacher_identities AS google
                    ON google.teacher_id = teacher.id
                WHERE grant_row.teacher_id = ?
                  AND grant_row.revoked_at IS NULL
                  AND teacher.deactivated_at IS NULL
                  AND google.revoked_at IS NULL
                """,
                (teacher_id,),
            ).fetchone()
        return None if row is None else row["role"]

    def may_bootstrap_google_link(self, record: SessionRecord) -> bool:
        """Allow exactly a grant-backed invite session to bind its first Google identity."""
        if record.auth_method != "invite":
            return False
        has_grant = self.active_staff_role(record.teacher_id) is not None
        return has_grant and not self.teacher_has_google_identity(record.teacher_id)

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

    # -- Google identity lifecycle ----------------------------------------

    def issue_google_login_nonce(
        self,
        *,
        kind: str,
        teacher_id: str | None = None,
        session_id: str | None = None,
    ) -> GoogleLoginNonce:
        """Persist a one-use GIS nonce before the browser starts its redirect.

        A link ceremony is bound to the currently authenticated teacher session;
        an ordinary sign-in ceremony intentionally has no teacher hint.  The raw
        nonce leaves this method once and only its SHA-256 digest is retained.
        """
        if kind not in {"signin", "link"}:
            raise ValueError("Unsupported Google login ceremony.")
        if kind == "link" and (teacher_id is None or session_id is None):
            raise ValueError("Google link requires the current teacher session.")
        if kind == "signin" and (teacher_id is not None or session_id is not None):
            raise ValueError("Google sign-in cannot carry a teacher hint.")
        raw_nonce = encode_opaque_token(secrets.token_bytes(_OPAQUE_TOKEN_BYTES))
        timestamp = now_iso()
        record = GoogleLoginNonce(
            id=str(uuid.uuid4()),
            raw_nonce=raw_nonce,
            kind=kind,
            teacher_id=teacher_id,
            session_id=session_id,
        )
        with self._write_transaction() as connection:
            if kind == "link":
                active_session = connection.execute(
                    """
                    SELECT 1 FROM pilot_sessions AS s
                    JOIN pilot_teachers AS t ON t.id = s.teacher_id
                    WHERE s.id = ? AND s.teacher_id = ? AND s.revoked_at IS NULL
                      AND s.expires_at > ? AND s.idle_expires_at > ?
                      AND t.deactivated_at IS NULL
                    """,
                    (session_id, teacher_id, timestamp, timestamp),
                ).fetchone()
                if active_session is None:
                    raise SessionUnavailable("The teacher session is no longer active.")
            connection.execute(
                """
                INSERT INTO google_login_nonces
                (id, nonce_hash, kind, teacher_id, session_id, created_at, expires_at, used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    record.id,
                    hashlib.sha256(raw_nonce.encode("ascii")).digest(),
                    kind,
                    teacher_id,
                    session_id,
                    timestamp,
                    _add_seconds(timestamp, _GOOGLE_NONCE_SECONDS),
                ),
            )
        return record

    def consume_google_login_nonce(self, raw_nonce: str) -> GoogleLoginNonce:
        """Atomically consume a GIS nonce before a session or identity is minted."""
        if not isinstance(raw_nonce, str) or _OPAQUE_TOKEN_RE.fullmatch(raw_nonce) is None:
            raise GoogleNonceUnavailable("The Google sign-in is no longer available.")
        timestamp = now_iso()
        digest = hashlib.sha256(raw_nonce.encode("ascii")).digest()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT id, kind, teacher_id, session_id
                FROM google_login_nonces
                WHERE nonce_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (digest, timestamp),
            ).fetchone()
            if row is None:
                raise GoogleNonceUnavailable("The Google sign-in is no longer available.")
            used = connection.execute(
                "UPDATE google_login_nonces SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (timestamp, row["id"]),
            )
            if used.rowcount != 1:
                raise GoogleNonceUnavailable("The Google sign-in is no longer available.")
        return GoogleLoginNonce(
            id=row["id"],
            raw_nonce=raw_nonce,
            kind=row["kind"],
            teacher_id=row["teacher_id"],
            session_id=row["session_id"],
        )

    def link_google_identity(
        self,
        *,
        teacher_id: str,
        session_id: str,
        subject: str,
        email: str,
        require_active_grant: bool = False,
    ) -> None:
        """Bind one verified Google subject to the already signed-in teacher.

        Email is an audit snapshot only. Future login routing is strictly the
        immutable Google ``sub`` claim, never a mutable email address.
        """
        timestamp = now_iso()
        with self._write_transaction() as connection:
            session = connection.execute(
                """
                SELECT 1 FROM pilot_sessions AS s
                JOIN pilot_teachers AS t ON t.id = s.teacher_id
                WHERE s.id = ? AND s.teacher_id = ? AND s.revoked_at IS NULL
                  AND s.expires_at > ? AND s.idle_expires_at > ?
                  AND t.deactivated_at IS NULL
                """,
                (session_id, teacher_id, timestamp, timestamp),
            ).fetchone()
            if session is None:
                raise SessionUnavailable("The teacher session is no longer active.")
            if require_active_grant:
                grant = connection.execute(
                    "SELECT 1 FROM staff_authorization_grants "
                    "WHERE teacher_id = ? AND revoked_at IS NULL",
                    (teacher_id,),
                ).fetchone()
                if grant is None:
                    raise SessionUnavailable("The teacher session is no longer active.")
            existing_subject = connection.execute(
                "SELECT teacher_id FROM google_teacher_identities "
                "WHERE subject = ?",
                (subject,),
            ).fetchone()
            if existing_subject is not None and existing_subject["teacher_id"] != teacher_id:
                raise GoogleIdentityUnavailable("This Google account is unavailable.")
            existing_teacher = connection.execute(
                "SELECT subject FROM google_teacher_identities "
                "WHERE teacher_id = ? AND revoked_at IS NULL",
                (teacher_id,),
            ).fetchone()
            if existing_teacher is not None and existing_teacher["subject"] != subject:
                raise GoogleIdentityUnavailable("This teacher already has a Google sign-in.")
            if existing_subject is not None:
                connection.execute(
                    "UPDATE google_teacher_identities SET last_used_at = ? WHERE subject = ?",
                    (timestamp, subject),
                )
                return
            connection.execute(
                """
                INSERT INTO google_teacher_identities
                (id, teacher_id, subject, email_at_link, created_at, last_used_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (str(uuid.uuid4()), teacher_id, subject, email, timestamp, timestamp),
            )

    def authenticate_google_identity(
        self, subject: str, *, require_active_grant: bool = False
    ) -> str | None:
        """Resolve only an already-linked active teacher from a Google subject."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT g.teacher_id FROM google_teacher_identities AS g
                JOIN pilot_teachers AS t ON t.id = g.teacher_id
                WHERE g.subject = ? AND g.revoked_at IS NULL AND t.deactivated_at IS NULL
                  AND (
                    ? = 0 OR EXISTS (
                        SELECT 1 FROM staff_authorization_grants AS grant_row
                        WHERE grant_row.teacher_id = t.id AND grant_row.revoked_at IS NULL
                    )
                  )
                """,
                (subject, int(require_active_grant)),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE google_teacher_identities SET last_used_at = ? WHERE subject = ?",
                (timestamp, subject),
            )
        return str(row["teacher_id"])

    def bind_google_email_preauthorization(
        self, *, subject: str, email: str
    ) -> RedeemedSession | None:
        """Consume exactly one approved verified email and establish its first Google session.

        The preauthorization, identity binding, and opaque browser session are
        one SQLite transaction.  A subject/email mismatch, unknown email, or
        replay returns no principal and leaves no partially created identity or
        session behind.
        """
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            return None
        try:
            email_digest = google_email_preauthorization_digest(email)
        except ValueError:
            return None
        timestamp = now_iso()
        raw_secret = secrets.token_bytes(_OPAQUE_TOKEN_BYTES)
        session = SessionRecord(
            id=str(uuid.uuid4()),
            teacher_id="",
            teacher_display_name="",
            invite_id=None,
            created_at=timestamp,
            expires_at=_add_hours(timestamp, _SESSION_ABSOLUTE_HOURS),
            revoked_at=None,
            auth_method="google",
        )
        with self._write_transaction() as connection:
            # A provider subject is immutable. It cannot be rebound—even if a
            # historic identity was revoked—by presenting a matching email.
            if connection.execute(
                "SELECT 1 FROM google_teacher_identities WHERE subject = ?", (subject,)
            ).fetchone() is not None:
                return None
            row = connection.execute(
                """
                SELECT p.id, p.teacher_id, t.display_name
                FROM google_email_preauthorizations AS p
                JOIN pilot_teachers AS t ON t.id = p.teacher_id
                JOIN staff_authorization_grants AS g ON g.teacher_id = t.id
                WHERE p.email_digest = ? AND p.consumed_at IS NULL
                  AND t.deactivated_at IS NULL AND g.revoked_at IS NULL
                """,
                (email_digest,),
            ).fetchone()
            if row is None:
                return None
            consumed = connection.execute(
                "UPDATE google_email_preauthorizations SET consumed_at = ? "
                "WHERE id = ? AND consumed_at IS NULL",
                (timestamp, row["id"]),
            )
            if consumed.rowcount != 1:
                return None
            connection.execute(
                """
                INSERT INTO google_teacher_identities
                (id, teacher_id, subject, email_at_link, created_at, last_used_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (str(uuid.uuid4()), row["teacher_id"], subject, email, timestamp, timestamp),
            )
            session = SessionRecord(
                **{
                    **session.__dict__,
                    "teacher_id": row["teacher_id"],
                    "teacher_display_name": row["display_name"],
                }
            )
            connection.execute(
                """
                INSERT INTO pilot_sessions
                (id, teacher_id, invite_id, secret_hash, redeem_nonce_hash, created_at, expires_at,
                 idle_expires_at, last_seen_at, revoked_at, auth_method)
                VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, NULL, 'google')
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

    def teacher_has_google_identity(self, teacher_id: str) -> bool:
        """Return whether this active teacher already has one usable Google link."""
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM google_teacher_identities
                WHERE teacher_id = ? AND revoked_at IS NULL
                """,
                (teacher_id,),
            ).fetchone()
        return row is not None

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

    def record_webauthn_use_and_mint_session(
        self,
        credential_id: bytes,
        sign_count: int,
        *,
        require_active_staff_authorization: bool = False,
    ) -> RedeemedSession | None:
        """Advance a passkey once and mint only for an atomically authorized teacher."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT c.teacher_id, t.display_name
                FROM webauthn_credentials AS c
                JOIN pilot_teachers AS t ON t.id = c.teacher_id
                WHERE c.credential_id = ? AND c.revoked_at IS NULL AND t.deactivated_at IS NULL
                  AND (
                    ? = 0 OR (
                      EXISTS (
                        SELECT 1 FROM staff_authorization_grants AS g
                        WHERE g.teacher_id = t.id AND g.revoked_at IS NULL
                      )
                      AND EXISTS (
                        SELECT 1 FROM google_teacher_identities AS google
                        WHERE google.teacher_id = t.id AND google.revoked_at IS NULL
                      )
                    )
                  )
                """,
                (credential_id, int(require_active_staff_authorization)),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = ? "
                "WHERE credential_id = ? AND ((sign_count = 0 AND ? = 0) OR ? > sign_count)",
                (sign_count, timestamp, credential_id, sign_count, sign_count),
            )
            if changed.rowcount != 1:
                return None
            return self._insert_reentry_session(
                connection,
                teacher_id=row["teacher_id"],
                teacher_display_name=row["display_name"],
                auth_method="passkey",
                timestamp=timestamp,
            )

    def _insert_reentry_session(
        self,
        connection: sqlite3.Connection,
        *,
        teacher_id: str,
        teacher_display_name: str,
        auth_method: str,
        timestamp: str,
    ) -> RedeemedSession:
        """Insert the opaque session only after the caller's writer-side authorization check."""
        raw_secret = secrets.token_bytes(_OPAQUE_TOKEN_BYTES)
        session = SessionRecord(
            id=str(uuid.uuid4()),
            teacher_id=teacher_id,
            teacher_display_name=teacher_display_name,
            invite_id=None,
            created_at=timestamp,
            expires_at=_add_hours(timestamp, _SESSION_ABSOLUTE_HOURS),
            revoked_at=None,
            auth_method=auth_method,
        )
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
        return RedeemedSession(session=session, raw_secret=raw_secret)

    def mint_reentry_session(
        self,
        teacher_id: str,
        *,
        auth_method: str,
        require_active_staff_authorization: bool = False,
    ) -> RedeemedSession:
        """The shared opaque-session seam, with production authorization checked in its write tx."""
        if auth_method not in {"passkey", "recovery", "google"}:
            raise ValueError("Unsupported re-entry method.")
        timestamp = now_iso()
        with self._write_transaction() as connection:
            teacher = connection.execute(
                """
                SELECT t.display_name FROM pilot_teachers AS t
                WHERE t.id = ? AND t.deactivated_at IS NULL
                  AND (
                    ? = 0 OR (
                      EXISTS (
                        SELECT 1 FROM staff_authorization_grants AS g
                        WHERE g.teacher_id = t.id AND g.revoked_at IS NULL
                      )
                      AND EXISTS (
                        SELECT 1 FROM google_teacher_identities AS google
                        WHERE google.teacher_id = t.id AND google.revoked_at IS NULL
                      )
                    )
                  )
                """,
                (teacher_id, int(require_active_staff_authorization)),
            ).fetchone()
            if teacher is None:
                raise SessionUnavailable("The teacher session is no longer active.")
            return self._insert_reentry_session(
                connection,
                teacher_id=teacher_id,
                teacher_display_name=teacher["display_name"],
                auth_method=auth_method,
                timestamp=timestamp,
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

    def redeem_recovery_code_and_mint_session(
        self, code: str, *, require_active_staff_authorization: bool = False
    ) -> RedeemedSession | None:
        """Consume recovery proof and mint only when staff authorization is still current."""
        timestamp = now_iso()
        digest = token_digest(b"hramatka-recovery-code", code.encode())
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT r.teacher_id, t.display_name
                FROM recovery_codes AS r
                JOIN pilot_teachers AS t ON t.id = r.teacher_id
                WHERE r.code_hash = ? AND r.used_at IS NULL AND r.superseded_at IS NULL
                  AND t.deactivated_at IS NULL
                  AND (
                    ? = 0 OR (
                      EXISTS (
                        SELECT 1 FROM staff_authorization_grants AS g
                        WHERE g.teacher_id = t.id AND g.revoked_at IS NULL
                      )
                      AND EXISTS (
                        SELECT 1 FROM google_teacher_identities AS google
                        WHERE google.teacher_id = t.id AND google.revoked_at IS NULL
                      )
                    )
                  )
                """,
                (digest, int(require_active_staff_authorization)),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE recovery_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                (timestamp, digest),
            )
            if changed.rowcount != 1:
                return None
            return self._insert_reentry_session(
                connection,
                teacher_id=row["teacher_id"],
                teacher_display_name=row["display_name"],
                auth_method="recovery",
                timestamp=timestamp,
            )

    # -- Teacher preferences (P2-6) ----------------------------------------

    def get_teacher_default_duration(self, teacher_id: str) -> int:
        """Return the qualified new-lesson duration, ignoring legacy rows."""
        del teacher_id  # Historical rows are retained but never become a choice again.
        return 45

    def set_teacher_default_duration(self, teacher_id: str, duration: int) -> None:
        """Upsert default_duration for owner; validates and commits before return."""
        if duration != 45:
            raise ValueError("default_duration must be the qualified 45-minute duration")
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
        require_active_staff_authorization: bool = False,
    ) -> tuple[JobRecord, bool]:
        if duration != 45:
            raise ValueError("New lesson generation is qualified for 45 minutes only.")
        # The 45-minute pilot creates lessons from text the teacher pasted into
        # the form.  Keep the canonicalizer able to decode historical URL
        # records so they stay readable, but never create another such record.
        if anchor_source != "teacher-paste" or anchor_source_url is not None:
            raise ValueError("New lesson generation accepts teacher-paste anchors only.")
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
                SELECT 1 FROM pilot_teachers AS t
                WHERE t.id = ? AND t.deactivated_at IS NULL
                  AND (
                    ? = 0 OR (
                      EXISTS (
                        SELECT 1 FROM staff_authorization_grants AS g
                        WHERE g.teacher_id = t.id AND g.revoked_at IS NULL
                      )
                      AND EXISTS (
                        SELECT 1 FROM google_teacher_identities AS google
                        WHERE google.teacher_id = t.id AND google.revoked_at IS NULL
                      )
                    )
                  )
                """,
                (teacher_id, int(require_active_staff_authorization)),
            ).fetchone()
            if active_teacher is None:
                # A request can pass HTTP scope resolution just before an
                # operator revokes/deactivates the target. This second check is
                # in the writer transaction, so it cannot create a new job
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
        """Delete only an inactive owner-scoped job; active work must be cancelled."""
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM lesson_jobs
                WHERE teacher_id = ? AND id = ? AND status NOT IN ('draft', 'baking')
                  AND attempt_quiesced_at IS NOT NULL
                """,
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
        attempt_token = str(uuid.uuid4())
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
                    attempt_token = ?, provider_started_at = NULL,
                    cancelled_acknowledged_at = NULL, attempt_quiesced_at = NULL,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'draft' AND revision = ?
                """,
                (
                    _STEP_COMPOSED,
                    timestamp,
                    timestamp,
                    attempt_token,
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

    def is_current_attempt(
        self, teacher_id: str, lesson_id: str, *, attempt: int, attempt_token: str
    ) -> bool:
        """Return whether this exact worker claim may still publish its attempt."""
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM lesson_jobs
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                  AND attempt = ? AND attempt_token = ?
                """,
                (teacher_id, lesson_id, attempt, attempt_token),
            ).fetchone()
        return row is not None

    def begin_provider_call(
        self, teacher_id: str, lesson_id: str, *, attempt: int, attempt_token: str
    ) -> bool:
        """Atomically lease one provider call for the current worker attempt.

        The transaction is the check/use boundary: cancellation either commits
        first and no provider call is leased, or the lease commits first and a
        concurrently requested cancellation truthfully reports work already
        started.  The lease is internal state, so it does not revise browser
        aggregate state.
        """
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET provider_started_at = COALESCE(provider_started_at, ?)
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                  AND attempt = ? AND attempt_token = ?
                """,
                (timestamp, teacher_id, lesson_id, attempt, attempt_token),
            )
        return cursor.rowcount == 1

    def acknowledge_cancelled_attempt(
        self, teacher_id: str, lesson_id: str, *, attempt: int, attempt_token: str
    ) -> bool:
        """Backward-compatible cancelled-attempt alias for generic quiescence."""
        return self.acknowledge_attempt_quiescence(
            teacher_id, lesson_id, attempt=attempt, attempt_token=attempt_token
        )

    def acknowledge_attempt_quiescence(
        self, teacher_id: str, lesson_id: str, *, attempt: int, attempt_token: str
    ) -> bool:
        """Confirm one terminal worker attempt cannot make another durable write."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs SET
                    attempt_quiesced_at = COALESCE(attempt_quiesced_at, ?),
                    cancelled_acknowledged_at = CASE
                        WHEN status = 'cancelled'
                        THEN COALESCE(cancelled_acknowledged_at, ?)
                        ELSE cancelled_acknowledged_at
                    END
                WHERE teacher_id = ? AND id = ? AND status IN ('cancelled', 'failed')
                  AND attempt = ? AND attempt_token = ?
                """,
                (timestamp, timestamp, teacher_id, lesson_id, attempt, attempt_token),
            )
        return cursor.rowcount == 1

    def cancel(
        self, teacher_id: str, lesson_id: str, *, expected_revision: int
    ) -> tuple[JobRecord, bool]:
        """Terminally cancel draft/baking work with owner-scoped CAS semantics.

        Repeating a cancellation acknowledgement at the returned revision is an
        idempotent no-op.  A stale revision remains a conflict, so a browser
        cannot accidentally cancel a later retry attempt.
        """
        timestamp = now_iso()
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            self._require_expected_revision(job, expected_revision)
            if job.status == "cancelled":
                return job, False
            if job.status not in {"draft", "baking"}:
                raise LessonStateConflict("The lesson is not in a state that allows cancellation.")
            history_json = _attempt_history_json(
                job,
                status="cancelled",
                failure_code="cancelled",
                completed_at=timestamp,
            )
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'cancelled', step = ?, failure_code = 'cancelled',
                    failure_message = ?, lesson_json = NULL, accepted = 0,
                    accepted_at = NULL, accepted_revision = NULL,
                    attempt_history_json = ?, completed_at = ?, updated_at = ?,
                    cancelled_acknowledged_at = ?,
                    attempt_quiesced_at = ?,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status IN ('draft', 'baking')
                  AND revision = ?
                """,
                (
                    _STEP_COMPLETE,
                    (
                        "Складання уроку скасовано. Уже запущена відповідь може завершитися "
                        "в сервісі, але урок не буде опубліковано."
                    ),
                    history_json,
                    timestamp,
                    timestamp,
                    timestamp if job.status == "draft" or job.attempt_token is None else None,
                    timestamp if job.status == "draft" or job.attempt_token is None else None,
                    teacher_id,
                    lesson_id,
                    expected_revision,
                ),
            )
            self._resolve_mutation(
                cursor.rowcount, connection, teacher_id, lesson_id, expected_revision
            )
            row = connection.execute(
                "SELECT * FROM lesson_jobs WHERE teacher_id = ? AND id = ?",
                (teacher_id, lesson_id),
            ).fetchone()
        assert row is not None
        return self._record(row), True

    def retry(
        self, teacher_id: str, lesson_id: str, *, expected_revision: int
    ) -> tuple[JobRecord, bool]:
        """Queue a failed/cancelled aggregate in place without duplicating its identity.

        The retry source revision is durable.  Replaying the same request after
        a lost response returns that same attempt even if a worker has already
        claimed or completed it; a later terminal revision starts a new attempt.
        """
        timestamp = now_iso()
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            if job.retry_requested_revision == expected_revision and job.attempt > 1:
                return job, False
            self._require_expected_revision(job, expected_revision)
            if job.status not in {"failed", "cancelled"}:
                raise LessonStateConflict("The lesson is not in a state that allows retry.")
            if job.attempt_quiesced_at is None:
                raise AttemptQuiescing(
                    "The terminal provider attempt has not yet acknowledged quiescence."
                )
            history_json = _attempt_history_json(
                job,
                status=job.status,
                failure_code=job.failure_code,
                completed_at=job.completed_at or timestamp,
                retry_requested_revision=expected_revision,
            )
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'draft', step = ?, failure_code = NULL, failure_message = NULL,
                    lesson_json = NULL, accepted = 0, accepted_at = NULL,
                    accepted_revision = NULL, progress_json = NULL, started_at = NULL,
                    completed_at = NULL, attempt = attempt + 1,
                    attempt_history_json = ?, retry_requested_revision = ?,
                    attempt_token = NULL, provider_started_at = NULL,
                    cancelled_acknowledged_at = NULL, attempt_quiesced_at = NULL,
                    updated_at = ?, revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status IN ('failed', 'cancelled')
                  AND revision = ?
                """,
                (
                    _STEP_RECEIVED,
                    history_json,
                    expected_revision,
                    timestamp,
                    teacher_id,
                    lesson_id,
                    expected_revision,
                ),
            )
            self._resolve_mutation(
                cursor.rowcount, connection, teacher_id, lesson_id, expected_revision
            )
            row = connection.execute(
                "SELECT * FROM lesson_jobs WHERE teacher_id = ? AND id = ?",
                (teacher_id, lesson_id),
            ).fetchone()
        assert row is not None
        return self._record(row), True

    def set_step(
        self,
        teacher_id: str,
        lesson_id: str,
        step: str,
        *,
        attempt: int,
        attempt_token: str,
    ) -> bool:
        if step not in {_STEP_COMPOSED, _STEP_CHECKING, _STEP_COMPLETE}:
            raise ValueError("Unknown frozen lesson step.")
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE lesson_jobs SET step = ?, updated_at = ?, revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                  AND attempt = ? AND attempt_token = ?
                """,
                (step, timestamp, teacher_id, lesson_id, attempt, attempt_token),
            )
        return cursor.rowcount == 1

    def complete(
        self,
        teacher_id: str,
        lesson_id: str,
        lesson: Mapping[str, Any],
        *,
        attempt: int,
        attempt_token: str,
    ) -> bool:
        """Persist one complete validated lesson; partial documents are never written."""
        materialized = dict(lesson)
        if materialized.get("accepted") is not False:
            raise ValueError("A newly materialized lesson must be unaccepted.")
        lesson_json = canonical_json(materialized)
        timestamp = now_iso()
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            if (
                job.status != "baking"
                or job.attempt != attempt
                or job.attempt_token != attempt_token
            ):
                return False
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'ready', step = ?, failure_code = NULL, failure_message = NULL,
                    lesson_json = ?, accepted = 0, accepted_at = NULL, accepted_revision = NULL,
                    attempt_history_json = ?, completed_at = ?, updated_at = ?,
                    attempt_quiesced_at = ?,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                  AND attempt = ? AND attempt_token = ?
                """,
                (
                    _STEP_COMPLETE,
                    lesson_json,
                    _attempt_history_json(
                        job, status="ready", failure_code=None, completed_at=timestamp
                    ),
                    timestamp,
                    timestamp,
                    timestamp,
                    teacher_id,
                    lesson_id,
                    attempt,
                    attempt_token,
                ),
            )
        return cursor.rowcount == 1

    def update_progress(
        self,
        teacher_id: str,
        lesson_id: str,
        progress: dict[str, Any],
        *,
        attempt: int,
        attempt_token: str | None,
    ) -> bool:
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
                WHERE teacher_id = ? AND id = ? AND attempt = ?
                  AND (
                    (status = 'baking' AND attempt_token = ?)
                    OR (status = 'draft' AND attempt_token IS NULL AND ? IS NULL)
                  )
                """,
                (
                    progress_json,
                    timestamp,
                    teacher_id,
                    lesson_id,
                    attempt,
                    attempt_token,
                    attempt_token,
                ),
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
        attempt: int,
        attempt_token: str,
    ) -> bool:
        """Durably fail a baking job with an allowlisted safe code/message only."""
        _validate_failure(failure_code, failure_message)
        if step not in {_STEP_COMPOSED, _STEP_CHECKING, _STEP_COMPLETE}:
            raise ValueError("Unknown frozen lesson step.")
        timestamp = now_iso()
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            if (
                job.status != "baking"
                or job.attempt != attempt
                or job.attempt_token != attempt_token
            ):
                return False
            cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', step = ?, failure_code = ?, failure_message = ?,
                    lesson_json = NULL, accepted = 0, accepted_at = NULL, accepted_revision = NULL,
                    attempt_history_json = ?, completed_at = ?, updated_at = ?,
                    attempt_quiesced_at = ?,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                  AND attempt = ? AND attempt_token = ?
                """,
                (
                    step,
                    failure_code,
                    failure_message,
                    _attempt_history_json(
                        job,
                        status="failed",
                        failure_code=failure_code,
                        completed_at=timestamp,
                    ),
                    timestamp,
                    timestamp,
                    timestamp,
                    teacher_id,
                    lesson_id,
                    attempt,
                    attempt_token,
                ),
            )
        return cursor.rowcount == 1

    def recover_baking_jobs(self, hard_timeout_seconds: int | None = None) -> int:
        """Fail orphaned baking rows at startup; queued drafts remain queued."""
        del hard_timeout_seconds  # Recovery is a restart boundary, not a timeout calculation.
        timestamp = now_iso()
        with self._write_transaction() as connection:
            # A fresh process has no inherited worker/provider call. Any
            # cancelled claim left without an acknowledgement is quiescent at
            # this restart boundary and may become deletable.
            connection.execute(
                """
                UPDATE lesson_jobs
                SET attempt_quiesced_at = COALESCE(attempt_quiesced_at, ?),
                    cancelled_acknowledged_at = CASE
                        WHEN status = 'cancelled'
                        THEN COALESCE(cancelled_acknowledged_at, ?)
                        ELSE cancelled_acknowledged_at
                    END
                WHERE status IN ('cancelled', 'failed') AND attempt_quiesced_at IS NULL
                """,
                (timestamp, timestamp),
            )
            rows = connection.execute(
                "SELECT * FROM lesson_jobs WHERE status = 'baking'"
            ).fetchall()
            for row in rows:
                job = self._record(row)
                connection.execute(
                    """
                UPDATE lesson_jobs
                SET status = 'failed', step = ?, failure_code = 'worker_restarted',
                    failure_message = ?, lesson_json = NULL, accepted = 0,
                    accepted_at = NULL, accepted_revision = NULL, completed_at = ?,
                    attempt_history_json = ?, updated_at = ?, attempt_quiesced_at = ?,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'baking'
                  AND attempt = ? AND attempt_token IS ?
                """,
                    (
                        _STEP_COMPLETE,
                        (
                            "Складання уроку перервалося через перезапуск сервісу. "
                            "Спробуйте, будь ласка, ще раз."
                        ),
                        timestamp,
                        _attempt_history_json(
                            job,
                            status="failed",
                            failure_code="worker_restarted",
                            completed_at=timestamp,
                        ),
                        timestamp,
                        timestamp,
                        job.teacher_id,
                        job.id,
                        job.attempt,
                        job.attempt_token,
                    ),
                )
        return len(rows)

    def sweep_expired_bakes(self, hard_timeout_seconds: int) -> int:
        if hard_timeout_seconds <= 0:
            raise ValueError("Bake timeout must be positive.")
        cutoff = _add_seconds(now_iso(), -hard_timeout_seconds)
        timestamp = now_iso()
        with self._write_transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM lesson_jobs WHERE status = 'baking' AND started_at < ?", (cutoff,)
            ).fetchall()
            for row in rows:
                job = self._record(row)
                connection.execute(
                    """
                UPDATE lesson_jobs
                SET status = 'failed', step = ?, failure_code = 'bake_timeout',
                    failure_message = ?, lesson_json = NULL, accepted = 0,
                    accepted_at = NULL, accepted_revision = NULL, completed_at = ?,
                    attempt_history_json = ?, updated_at = ?, attempt_quiesced_at = NULL,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'baking' AND started_at < ?
                  AND attempt = ? AND attempt_token IS ?
                """,
                    (
                        _STEP_COMPLETE,
                        "Час на складання уроку вичерпано. Спробуйте, будь ласка, ще раз.",
                        timestamp,
                        _attempt_history_json(
                            job,
                            status="failed",
                            failure_code="bake_timeout",
                            completed_at=timestamp,
                        ),
                        timestamp,
                        job.teacher_id,
                        job.id,
                        cutoff,
                        job.attempt,
                        job.attempt_token,
                    ),
                )
        return len(rows)

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
            rows = connection.execute("SELECT * FROM lesson_jobs WHERE status = 'draft'").fetchall()
            for row in rows:
                job = self._record(row)
                connection.execute(
                    """
                UPDATE lesson_jobs
                SET status = 'failed', step = ?, failure_code = ?, failure_message = ?,
                    lesson_json = NULL, accepted = 0, accepted_at = NULL,
                    accepted_revision = NULL, completed_at = ?, attempt_history_json = ?,
                    updated_at = ?, attempt_quiesced_at = ?,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'draft'
                """,
                    (
                        _STEP_COMPLETE,
                        failure_code,
                        failure_message,
                        timestamp,
                        _attempt_history_json(
                            job,
                            status="failed",
                            failure_code=failure_code,
                            completed_at=timestamp,
                        ),
                        timestamp,
                        # A queued draft has neither a claimed token nor a
                        # provider lease. Quarantine is therefore its terminal
                        # process boundary: no acknowledgement can arrive and
                        # retry/delete must not remain blocked forever.
                        timestamp,
                        job.teacher_id,
                        job.id,
                    ),
                )
        return len(rows)

    # -- Durable one-block regeneration jobs (#418) -----------------------

    def create_or_get_activity_regeneration(
        self,
        teacher_id: str,
        regeneration_id: str,
        *,
        lesson_id: str,
        block_id: str,
        expected_revision: int,
        feedback: str | None,
        prompt_version: str,
        prompt_sha256: str,
    ) -> tuple[ActivityRegenerationRecord, bool]:
        """Create one idempotent queued replacement while leaving the lesson usable."""
        if isinstance(feedback, str):
            feedback = feedback.strip() or None
        if feedback is not None and (not isinstance(feedback, str) or len(feedback) > 1000):
            raise ValueError("Regeneration feedback must be non-empty text up to 1000 characters.")
        if not prompt_version or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256):
            raise ValueError("Regeneration prompt provenance is invalid.")
        timestamp = now_iso()
        with self._write_transaction() as connection:
            existing_row = connection.execute(
                "SELECT * FROM activity_regenerations WHERE id = ?", (regeneration_id,)
            ).fetchone()
            if existing_row is not None:
                existing = self._activity_regeneration_record(existing_row)
                request_hash = _activity_regeneration_request_hash(
                    lesson_id=lesson_id,
                    block_id=block_id,
                    base_revision=expected_revision,
                    feedback=feedback,
                    old_block_hash=existing.old_block_hash,
                    logical_model_id=existing.logical_model_id,
                    prompt_version=prompt_version,
                    prompt_sha256=prompt_sha256,
                )
                if existing.teacher_id != teacher_id or existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "This regeneration ID is already bound to different inputs."
                    )
                return existing, False
            lesson_job = self._require_owned(connection, teacher_id, lesson_id)
            self._require_expected_revision(lesson_job, expected_revision)
            self._require_ready(lesson_job)
            lesson = self._require_lesson(lesson_job)
            block = self._regenerable_block(lesson, block_id)
            logical_model_id = lesson_job.logical_model_id
            if logical_model_id is None:
                raise LessonStateConflict("The lesson has no durable model route.")
            old_block_json = canonical_json(block)
            old_block_hash = _canonical_hash_hex(block)
            request_hash = _activity_regeneration_request_hash(
                lesson_id=lesson_id,
                block_id=block_id,
                base_revision=expected_revision,
                feedback=feedback,
                old_block_hash=old_block_hash,
                logical_model_id=logical_model_id,
                prompt_version=prompt_version,
                prompt_sha256=prompt_sha256,
            )
            try:
                connection.execute(
                    """
                    INSERT INTO activity_regenerations (
                        id, teacher_id, lesson_id, block_id, request_hash, base_revision,
                        status, feedback, attempt, failure_code, failure_message,
                        old_block_hash, old_block_json, new_block_hash, new_block_json,
                        logical_model_id, prompt_version, prompt_sha256, applied_revision,
                        created_at, updated_at, started_at, completed_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, 'queued', ?, 1, NULL, NULL,
                        ?, ?, NULL, NULL, ?, ?, ?, NULL, ?, ?, NULL, NULL
                    )
                    """,
                    (
                        regeneration_id,
                        teacher_id,
                        lesson_id,
                        block_id,
                        request_hash,
                        expected_revision,
                        feedback,
                        old_block_hash,
                        old_block_json,
                        logical_model_id,
                        prompt_version,
                        prompt_sha256,
                        timestamp,
                        timestamp,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = connection.execute(
                "SELECT * FROM activity_regenerations WHERE id = ?", (regeneration_id,)
            ).fetchone()
            if row is None:
                raise ActivityRegenerationInProgress(
                    "The lesson already has active regeneration work."
                )
            record = self._activity_regeneration_record(row)
            if record.teacher_id != teacher_id or record.request_hash != request_hash:
                raise IdempotencyConflict(
                    "This regeneration ID is already bound to different inputs."
                )
            return record, created

    def get_activity_regeneration(
        self, teacher_id: str, regeneration_id: str
    ) -> ActivityRegenerationRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM activity_regenerations
                WHERE teacher_id = ? AND id = ?
                """,
                (teacher_id, regeneration_id),
            ).fetchone()
        return self._activity_regeneration_record(row) if row is not None else None

    def list_activity_regenerations(
        self, teacher_id: str, lesson_id: str, *, limit: int = 50
    ) -> list[ActivityRegenerationRecord]:
        if not 1 <= limit <= 100:
            raise ValueError("Regeneration history limit must be between 1 and 100.")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM activity_regenerations
                WHERE teacher_id = ? AND lesson_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (teacher_id, lesson_id, limit),
            ).fetchall()
        return [self._activity_regeneration_record(row) for row in rows]

    def claim_next_activity_regeneration(self) -> ActivityRegenerationRecord | None:
        """Atomically claim the oldest queued replacement for the shared worker pool."""
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM activity_regenerations
                WHERE status = 'queued' ORDER BY created_at, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE activity_regenerations
                SET status = 'running', started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued' AND attempt = ?
                """,
                (timestamp, timestamp, row["id"], row["attempt"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM activity_regenerations WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._activity_regeneration_record(claimed) if claimed is not None else None

    def complete_activity_regeneration(
        self,
        teacher_id: str,
        regeneration_id: str,
        replacement_block: Mapping[str, Any],
    ) -> bool:
        """Apply one checked block and its lesson revision in the same transaction."""
        replacement = copy.deepcopy(dict(replacement_block))
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM activity_regenerations
                WHERE teacher_id = ? AND id = ?
                """,
                (teacher_id, regeneration_id),
            ).fetchone()
            if row is None:
                raise ActivityRegenerationNotFound(regeneration_id)
            regeneration = self._activity_regeneration_record(row)
            if regeneration.status != "running":
                return False
            lesson_job = self._require_owned(connection, teacher_id, regeneration.lesson_id)
            if lesson_job.status != "ready" or lesson_job.revision != regeneration.base_revision:
                self._fail_activity_regeneration_in_connection(
                    connection,
                    regeneration_id,
                    code="revision_conflict",
                    message="Урок змінився. Оновіть його й спробуйте створити варіант ще раз.",
                    timestamp=timestamp,
                )
                return False
            lesson = copy.deepcopy(self._require_lesson(lesson_job))
            try:
                blocks = lesson["blocks"]
                index = next(
                    position
                    for position, block in enumerate(blocks)
                    if isinstance(block, Mapping) and block.get("id") == regeneration.block_id
                )
                current = blocks[index]
            except (KeyError, TypeError, StopIteration):
                self._fail_activity_regeneration_in_connection(
                    connection,
                    regeneration_id,
                    code="block_changed",
                    message="Вибраний блок змінився. Оновіть урок і спробуйте ще раз.",
                    timestamp=timestamp,
                )
                return False
            if _canonical_hash_hex(current) != regeneration.old_block_hash:
                self._fail_activity_regeneration_in_connection(
                    connection,
                    regeneration_id,
                    code="block_changed",
                    message="Вибраний блок змінився. Оновіть урок і спробуйте ще раз.",
                    timestamp=timestamp,
                )
                return False
            if (
                replacement.get("id") != regeneration.block_id
                or replacement.get("phase") != current.get("phase")
                or replacement.get("type") != current.get("type")
                or _activity_learning_content(replacement.get("activity"))
                == _activity_learning_content(current.get("activity"))
            ):
                raise ReviewMutationInvalid("Regenerated block does not preserve its slot.")
            blocks[index] = replacement
            lesson["accepted"] = False
            lesson["updated_at"] = timestamp
            acknowledgements = set(lesson_job.warning_acknowledgements)
            acknowledgements.discard(regeneration.block_id)
            validate_lesson(lesson)
            new_revision = lesson_job.revision + 1
            lesson_cursor = connection.execute(
                """
                UPDATE lesson_jobs
                SET lesson_json = ?, warning_acknowledgements_json = ?, accepted = 0,
                    accepted_at = NULL, accepted_revision = NULL, updated_at = ?,
                    revision = revision + 1
                WHERE teacher_id = ? AND id = ? AND status = 'ready' AND revision = ?
                """,
                (
                    canonical_json(lesson),
                    canonical_json(sorted(acknowledgements)),
                    timestamp,
                    teacher_id,
                    regeneration.lesson_id,
                    regeneration.base_revision,
                ),
            )
            if lesson_cursor.rowcount != 1:
                raise RevisionConflict("The lesson changed during regeneration completion.")
            new_block_json = canonical_json(replacement)
            new_block_hash = _canonical_hash_hex(replacement)
            cursor = connection.execute(
                """
                UPDATE activity_regenerations
                SET status = 'succeeded', failure_code = NULL, failure_message = NULL,
                    new_block_hash = ?, new_block_json = ?, applied_revision = ?,
                    completed_at = ?, updated_at = ?
                WHERE teacher_id = ? AND id = ? AND status = 'running'
                """,
                (
                    new_block_hash,
                    new_block_json,
                    new_revision,
                    timestamp,
                    timestamp,
                    teacher_id,
                    regeneration_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PersistenceUnavailable("Regeneration completion lost its durable job.")
            return True

    def fail_activity_regeneration(
        self,
        teacher_id: str,
        regeneration_id: str,
        failure_code: str,
        failure_message: str,
    ) -> bool:
        _validate_activity_regeneration_failure(failure_code, failure_message)
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE activity_regenerations
                SET status = 'failed', failure_code = ?, failure_message = ?,
                    completed_at = ?, updated_at = ?
                WHERE teacher_id = ? AND id = ? AND status IN ('queued', 'running')
                """,
                (
                    failure_code,
                    failure_message,
                    timestamp,
                    timestamp,
                    teacher_id,
                    regeneration_id,
                ),
            )
        return cursor.rowcount == 1

    def retry_activity_regeneration(
        self,
        teacher_id: str,
        regeneration_id: str,
        *,
        expected_revision: int,
        prompt_version: str,
        prompt_sha256: str,
    ) -> ActivityRegenerationRecord:
        """Retry a terminal failure against the teacher's current unchanged target block."""
        if not prompt_version or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256):
            raise ValueError("Regeneration prompt provenance is invalid.")
        timestamp = now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM activity_regenerations
                WHERE teacher_id = ? AND id = ?
                """,
                (teacher_id, regeneration_id),
            ).fetchone()
            if row is None:
                raise ActivityRegenerationNotFound(regeneration_id)
            regeneration = self._activity_regeneration_record(row)
            if regeneration.status != "failed":
                if (
                    regeneration.status in {"queued", "running", "succeeded"}
                    and regeneration.base_revision == expected_revision
                ):
                    # The browser may retry after losing the first HTTP response.
                    # Return that durable attempt instead of reporting a false
                    # failure or creating duplicate work.
                    return regeneration
                raise ActivityRegenerationStateConflict("Only failed regeneration can be retried.")
            lesson_job = self._require_owned(connection, teacher_id, regeneration.lesson_id)
            self._require_expected_revision(lesson_job, expected_revision)
            self._require_ready(lesson_job)
            block = self._regenerable_block(self._require_lesson(lesson_job), regeneration.block_id)
            active = connection.execute(
                """
                SELECT 1 FROM activity_regenerations
                WHERE teacher_id = ? AND lesson_id = ? AND id != ?
                  AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (
                    teacher_id,
                    regeneration.lesson_id,
                    regeneration_id,
                ),
            ).fetchone()
            if active is not None:
                raise ActivityRegenerationInProgress(
                    "The lesson already has active regeneration work."
                )
            old_block_hash = _canonical_hash_hex(block)
            old_block_json = canonical_json(block)
            request_hash = _activity_regeneration_request_hash(
                lesson_id=regeneration.lesson_id,
                block_id=regeneration.block_id,
                base_revision=expected_revision,
                feedback=regeneration.feedback,
                old_block_hash=old_block_hash,
                logical_model_id=regeneration.logical_model_id,
                prompt_version=prompt_version,
                prompt_sha256=prompt_sha256,
            )
            cursor = connection.execute(
                """
                UPDATE activity_regenerations
                SET status = 'queued', base_revision = ?, attempt = attempt + 1,
                    failure_code = NULL, failure_message = NULL,
                    request_hash = ?, old_block_hash = ?, old_block_json = ?,
                    prompt_version = ?, prompt_sha256 = ?,
                    new_block_hash = NULL, new_block_json = NULL,
                    applied_revision = NULL, started_at = NULL, completed_at = NULL,
                    updated_at = ?
                WHERE teacher_id = ? AND id = ? AND status = 'failed'
                """,
                (
                    expected_revision,
                    request_hash,
                    old_block_hash,
                    old_block_json,
                    prompt_version,
                    prompt_sha256,
                    timestamp,
                    teacher_id,
                    regeneration_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ActivityRegenerationStateConflict("Regeneration retry lost its state.")
            retried = connection.execute(
                "SELECT * FROM activity_regenerations WHERE id = ?", (regeneration_id,)
            ).fetchone()
        assert retried is not None
        return self._activity_regeneration_record(retried)

    def recover_activity_regenerations(self) -> int:
        """Fail only interrupted running replacements; queued work remains claimable."""
        timestamp = now_iso()
        message = "Створення нового варіанта перервалося. Спробуйте ще раз."
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE activity_regenerations
                SET status = 'failed', failure_code = 'worker_restarted',
                    failure_message = ?, completed_at = ?, updated_at = ?
                WHERE status = 'running'
                """,
                (message, timestamp, timestamp),
            )
        return cursor.rowcount

    def sweep_expired_activity_regenerations(self, hard_timeout_seconds: int) -> int:
        """Fail running replacements whose worker can no longer finish them.

        This is the periodic counterpart to startup recovery. It also closes
        the double-fault case where generation fails and the first persistence
        attempt to record that failure is itself temporarily unavailable.
        """
        if hard_timeout_seconds <= 0:
            raise ValueError("Regeneration timeout must be positive.")
        cutoff = _add_seconds(now_iso(), -hard_timeout_seconds)
        timestamp = now_iso()
        message = "Час на створення нового варіанта вичерпано. Спробуйте ще раз."
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE activity_regenerations
                SET status = 'failed', failure_code = 'engine_unavailable',
                    failure_message = ?, completed_at = ?, updated_at = ?
                WHERE status = 'running' AND started_at < ?
                """,
                (message, timestamp, timestamp, cutoff),
            )
        return cursor.rowcount

    def fail_queued_activity_regenerations(self, failure_message: str) -> int:
        """Fail queued replacements only when the shared worker pool is unavailable."""
        _validate_activity_regeneration_failure("engine_unavailable", failure_message)
        timestamp = now_iso()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE activity_regenerations
                SET status = 'failed', failure_code = 'engine_unavailable',
                    failure_message = ?, completed_at = ?, updated_at = ?
                WHERE status = 'queued'
                """,
                (failure_message, timestamp, timestamp),
            )
        return cursor.rowcount

    @staticmethod
    def _regenerable_block(lesson: Mapping[str, Any], block_id: str) -> dict[str, Any]:
        blocks = lesson.get("blocks")
        if not isinstance(blocks, list):
            raise PersistenceUnavailable("A ready lesson has no valid blocks.")
        block = next(
            (
                candidate
                for candidate in blocks
                if isinstance(candidate, dict) and candidate.get("id") == block_id
            ),
            None,
        )
        if block is None:
            raise LessonBlockNotFound(block_id)
        provenance = block.get("provenance")
        if (
            re.fullmatch(r"block-[1-9][0-9]*", block_id) is None
            or block.get("edited") is not False
            or not isinstance(provenance, Mapping)
            or provenance.get("source") != "generated"
        ):
            raise LessonStateConflict(
                "Only an unchanged engine-generated lesson block can be regenerated."
            )
        return copy.deepcopy(block)

    @staticmethod
    def _fail_activity_regeneration_in_connection(
        connection: sqlite3.Connection,
        regeneration_id: str,
        *,
        code: str,
        message: str,
        timestamp: str,
    ) -> None:
        _validate_activity_regeneration_failure(code, message)
        connection.execute(
            """
            UPDATE activity_regenerations
            SET status = 'failed', failure_code = ?, failure_message = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (code, message, timestamp, timestamp, regeneration_id),
        )

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

    # -- Teacher feedback on engine-flagged blocks (#402) ------------------

    def put_activity_feedback(
        self,
        teacher_id: str,
        lesson_id: str,
        *,
        slot_id: str,
        verdict: str,
        comment: str | None,
    ) -> JobRecord:
        """Record the teacher's verdict on a flagged block.

        State machine (design of record, issue #402): a PUT against a block
        that is not currently ``engine_flagged`` is refused; the first PUT
        inserts; a same-hash PUT revises in place; a hash mismatch (rebake or
        re-flag produced new content under the same slot) soft-closes the old
        row via ``superseded_at`` — never deletes it — and inserts a fresh
        current row.  The comparison hash is always the block's flag-time
        ``flagged_content_hash``, never recomputed and never client-supplied.
        """
        if verdict not in {"good", "bad"}:
            raise ValueError("Feedback verdict must be 'good' or 'bad'.")
        if comment is not None and (
            not isinstance(comment, str) or not comment.strip() or len(comment) > 2000
        ):
            raise ValueError("Feedback comments must be non-empty text up to 2000 characters.")
        with self._write_transaction() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            self._require_ready(job)
            lesson = self._require_lesson(job)
            block = next(
                (
                    candidate
                    for candidate in lesson.get("blocks", [])
                    if isinstance(candidate, Mapping) and candidate.get("id") == slot_id
                ),
                None,
            )
            if block is None:
                raise LessonBlockNotFound(slot_id)
            if block.get("quality") != "engine_flagged":
                raise FeedbackNotApplicable("The block is not currently engine-flagged.")
            wire_hash = block.get("flagged_content_hash")
            if not isinstance(wire_hash, str) or not wire_hash:
                raise PersistenceUnavailable("A flagged block has no frozen content hash.")
            timestamp = now_iso()
            current = connection.execute(
                """
                SELECT * FROM activity_feedback
                WHERE lesson_id = ? AND slot_id = ? AND superseded_at IS NULL
                """,
                (lesson_id, slot_id),
            ).fetchone()
            if current is not None and current["activity_payload_hash"] == wire_hash:
                connection.execute(
                    """
                    UPDATE activity_feedback
                    SET teacher_verdict = ?, comment = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (verdict, comment, timestamp, current["id"]),
                )
                return job
            if current is not None:
                connection.execute(
                    "UPDATE activity_feedback SET superseded_at = ? WHERE id = ?",
                    (timestamp, current["id"]),
                )
            connection.execute(
                """
                INSERT INTO activity_feedback (
                    id, lesson_id, slot_id, activity_payload_hash,
                    engine_reason_class, engine_reason_uk, teacher_verdict,
                    comment, created_at, updated_at, superseded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    str(uuid.uuid4()),
                    lesson_id,
                    slot_id,
                    wire_hash,
                    str(block.get("engine_reason_class") or ""),
                    str(block.get("flag_reason_uk") or ""),
                    verdict,
                    comment,
                    timestamp,
                    timestamp,
                ),
            )
            return job

    def applicable_activity_feedback(
        self, teacher_id: str, lesson_id: str
    ) -> dict[str, dict[str, Any]]:
        """Current-judgment feedback rows keyed by block id.

        Binds to the design's ``applicable`` definition: ``superseded_at IS
        NULL`` **and** the row's hash equals the live block's frozen
        ``flagged_content_hash``.  A history-current row whose content has
        since changed is presented as "no feedback yet", never as the
        teacher's opinion of what is on screen; it stays queryable unfiltered
        for the out-of-scope analytics step.
        """
        with self._read_connection() as connection:
            job = self._require_owned(connection, teacher_id, lesson_id)
            if job.lesson is None:
                return {}
            live_hashes = {
                block.get("id"): block.get("flagged_content_hash")
                for block in job.lesson.get("blocks", [])
                if isinstance(block, Mapping)
                and block.get("quality") == "engine_flagged"
                and isinstance(block.get("flagged_content_hash"), str)
            }
            if not live_hashes:
                return {}
            rows = connection.execute(
                """
                SELECT slot_id, activity_payload_hash, teacher_verdict, comment, updated_at
                FROM activity_feedback
                WHERE lesson_id = ? AND superseded_at IS NULL
                """,
                (lesson_id,),
            ).fetchall()
            applicable: dict[str, dict[str, Any]] = {}
            for row in rows:
                if live_hashes.get(row["slot_id"]) == row["activity_payload_hash"]:
                    applicable[row["slot_id"]] = {
                        "verdict": row["teacher_verdict"],
                        "comment": row["comment"],
                        "updated_at": row["updated_at"],
                    }
            return applicable

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
            attempt_history = json.loads(row["attempt_history_json"])
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
        if not isinstance(attempt_history, list) or any(
            not isinstance(item, dict)
            or set(item)
            != {
                "attempt",
                "status",
                "failure_code",
                "started_at",
                "completed_at",
                "retry_requested_revision",
            }
            or not isinstance(item["attempt"], int)
            or item["attempt"] < 1
            or item["status"] not in {"ready", "failed", "cancelled"}
            or item["failure_code"] not in {*_FAILURE_CODES, None}
            or item["started_at"] is not None
            and not isinstance(item["started_at"], str)
            or not isinstance(item["completed_at"], str)
            or item["retry_requested_revision"] is not None
            and (
                not isinstance(item["retry_requested_revision"], int)
                or item["retry_requested_revision"] < 1
            )
            for item in attempt_history
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
            attempt=row["attempt"],
            attempt_history=tuple(attempt_history),
            retry_requested_revision=row["retry_requested_revision"],
            attempt_token=row["attempt_token"],
            provider_started_at=row["provider_started_at"],
            cancelled_acknowledged_at=row["cancelled_acknowledged_at"],
            attempt_quiesced_at=row["attempt_quiesced_at"],
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
    def _activity_regeneration_record(row: sqlite3.Row) -> ActivityRegenerationRecord:
        try:
            old_block = json.loads(row["old_block_json"])
            new_block = (
                json.loads(row["new_block_json"]) if row["new_block_json"] is not None else None
            )
        except (TypeError, json.JSONDecodeError) as error:
            raise PersistenceUnavailable("SQLite contains unreadable regeneration data.") from error
        if not isinstance(old_block, dict) or (
            new_block is not None and not isinstance(new_block, dict)
        ):
            raise PersistenceUnavailable("SQLite contains unreadable regeneration data.")
        return ActivityRegenerationRecord(
            id=row["id"],
            teacher_id=row["teacher_id"],
            lesson_id=row["lesson_id"],
            block_id=row["block_id"],
            request_hash=bytes(row["request_hash"]),
            base_revision=row["base_revision"],
            status=row["status"],
            feedback=row["feedback"],
            attempt=row["attempt"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            old_block_hash=row["old_block_hash"],
            old_block=old_block,
            new_block_hash=row["new_block_hash"],
            new_block=new_block,
            logical_model_id=row["logical_model_id"],
            prompt_version=row["prompt_version"],
            prompt_sha256=row["prompt_sha256"],
            applied_revision=row["applied_revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
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


def _canonical_hash_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _activity_regeneration_request_hash(
    *,
    lesson_id: str,
    block_id: str,
    base_revision: int,
    feedback: str | None,
    old_block_hash: str,
    logical_model_id: str,
    prompt_version: str,
    prompt_sha256: str,
) -> bytes:
    request_json = canonical_json(
        {
            "lesson_id": lesson_id,
            "block_id": block_id,
            "base_revision": base_revision,
            "feedback": feedback,
            "old_block_hash": old_block_hash,
            "logical_model_id": logical_model_id,
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256,
        }
    )
    return hashlib.sha256(request_json.encode("utf-8")).digest()


def _activity_learning_content(activity: Any) -> tuple[Any, Any]:
    if not isinstance(activity, Mapping):
        return None, None
    return activity.get("payload"), activity.get("answer_key")


def _validate_activity_regeneration_failure(failure_code: str, failure_message: str) -> None:
    allowed = {
        "provider_unavailable",
        "generation_failed",
        "engine_unavailable",
        "revision_conflict",
        "block_changed",
        "worker_restarted",
    }
    if failure_code not in allowed:
        raise ValueError("Unknown regeneration failure code.")
    if (
        not isinstance(failure_message, str)
        or not failure_message.strip()
        or len(failure_message) > 500
    ):
        raise ValueError(
            "Regeneration failure messages must be safe non-empty text up to 500 characters."
        )


def _add_hours(timestamp: str, hours: int) -> str:
    return _format_timestamp(_parse_timestamp(timestamp) + timedelta(hours=hours))


def _add_seconds(timestamp: str, seconds: int) -> str:
    return _format_timestamp(_parse_timestamp(timestamp) + timedelta(seconds=seconds))


def _parse_timestamp(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
