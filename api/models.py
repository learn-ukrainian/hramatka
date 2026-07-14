"""Strict HTTP input models for the frozen teacher-pilot wire contract."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteRedeem(FrozenModel):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")


class AnchorInput(FrozenModel):
    text: str = Field(min_length=1, max_length=100_000)
    source: Literal["teacher-paste", "teacher-url"]
    source_url: str | None = Field(default=None, max_length=2048)

    @field_validator("text")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Paste text must not be whitespace only.")
        return value

    @field_validator("source_url")
    @classmethod
    def normalize_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None

    @model_validator(mode="after")
    def validate_source_provenance(self) -> AnchorInput:
        if self.source == "teacher-paste":
            if self.source_url is not None:
                raise ValueError("teacher-paste anchors must not include source_url.")
            return self
        if self.source_url is None or not self.source_url.startswith("https://"):
            raise ValueError("teacher-url anchors require an https source_url.")
        return self


class UrlImportRequest(FrozenModel):
    url: str = Field(min_length=1, max_length=2048)


class LessonCreate(FrozenModel):
    """The browser UUID is the owner-scoped durable idempotency key."""

    id: UUID
    anchor: AnchorInput
    level: Literal["B1"]
    duration: Literal[45, 60, 90]
    focus: str | None = Field(default=None, max_length=500)


class RevisionMutation(FrozenModel):
    expected_revision: int = Field(ge=1)


class BlockMoveMutation(FrozenModel):
    expected_revision: int = Field(ge=1)
    direction: Literal["up", "down"]


class ActivityReplacementMutation(FrozenModel):
    expected_revision: int = Field(ge=1)
    activity: dict[str, Any]


class RestoreRejectedMutation(FrozenModel):
    expected_revision: int = Field(ge=1)
    phase: Literal[1, 2, 3] = 2


class DurationMutation(FrozenModel):
    expected_revision: int = Field(ge=1)
    duration: Literal[45, 60, 90]


class TeacherPreferences(FrozenModel):
    """GET response and PUT body for per-teacher defaults (owner-scoped)."""

    default_duration: Literal[45, 60, 90]
