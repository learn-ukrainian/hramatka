"""Strict HTTP input models for the frozen teacher-pilot wire contract."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteRedeem(FrozenModel):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")


class WebAuthnCredential(FrozenModel):
    """Opaque browser WebAuthn response; verification is done by the RP library."""

    credential: dict[str, Any]


class WebAuthnAssertion(FrozenModel):
    challenge_id: UUID
    credential: dict[str, Any]


class RecoveryCodeRedeem(FrozenModel):
    code: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class AnchorInput(FrozenModel):
    text: str = Field(min_length=1, max_length=100_000)
    source: Literal["teacher-paste"]

    @field_validator("text")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Paste text must not be whitespace only.")
        return value


class LessonCreate(FrozenModel):
    """The browser UUID is the owner-scoped durable idempotency key."""

    id: UUID
    anchor: AnchorInput
    level: Literal["B1"]
    # New generation is limited to the human-qualified 45-minute lesson path.
    # Stored 60/90-minute documents stay readable in response models.
    duration: Literal[45]

    @field_validator("duration", mode="before")
    @classmethod
    def require_qualified_duration(cls, value: object) -> object:
        if type(value) is not int or value != 45:
            raise ValueError(
                "Нові уроки наразі доступні лише у 45-хвилинному форматі, "
                "доки цей формат проходить перевірку вчителями."
            )
        return value
    # The pilot deliberately offers one methodology only. Keep the literal on
    # the wire now so a future expanded choice cannot silently alter a bake.
    methodology: Literal["ttt"] = "ttt"
    focus: str | None = Field(default=None, max_length=500)
    grammar_focus: str | None = Field(default=None, max_length=120)
    # Optional only for wire compatibility with pre-selector clients.  The
    # production endpoint resolves it fail-closed; newly persisted jobs always
    # carry a qualified logical ID.  Injected legacy bakers retain their test /
    # migration seam while old durable request_json remains readable.
    logical_model_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$",
    )

    @field_validator("grammar_focus", mode="before")
    @classmethod
    def normalize_grammar_focus(cls, value: object) -> object:
        """Trim a teacher-supplied grammar focus without accepting line breaks."""
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("Grammar focus must be one line.")
        trimmed = value.strip()
        return trimmed or None


class RevisionMutation(FrozenModel):
    expected_revision: int = Field(ge=1)


class BlockMoveMutation(FrozenModel):
    expected_revision: int = Field(ge=1)
    direction: Literal["up", "down"]


class ActivityReplacementMutation(FrozenModel):
    expected_revision: int = Field(ge=1)
    activity: dict[str, Any]


class ActivityRegenerationCreate(FrozenModel):
    """Create one idempotent asynchronous replacement for a generated block."""

    id: UUID
    expected_revision: int = Field(ge=1)
    feedback: str | None = Field(default=None, max_length=1000)

    @field_validator("feedback", mode="before")
    @classmethod
    def normalize_feedback(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        trimmed = value.strip()
        return trimmed or None


class RestoreRejectedMutation(FrozenModel):
    expected_revision: int = Field(ge=1)
    phase: Literal[1, 2, 3] = 2


class ActivityFeedbackMutation(FrozenModel):
    """PUT body for teacher feedback on an engine-flagged block (#402).

    Deliberately no ``expected_revision``: staleness is hash-gated against the
    block's flag-time ``flagged_content_hash`` server-side, so a revision race
    cannot attach a verdict to changed content.
    """

    verdict: Literal["good", "bad"]
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("comment", mode="before")
    @classmethod
    def normalize_comment(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        trimmed = value.strip()
        return trimmed or None


class TeacherPreferences(FrozenModel):
    """GET response and PUT body for per-teacher defaults (owner-scoped)."""

    # Legacy preference rows are never surfaced as new-lesson choices.
    default_duration: Literal[45]

    @field_validator("default_duration", mode="before")
    @classmethod
    def require_qualified_default_duration(cls, value: object) -> object:
        if type(value) is not int or value != 45:
            raise ValueError(
                "Для нових уроків зараз доступна лише кваліфікована тривалість 45 хвилин."
            )
        return value
