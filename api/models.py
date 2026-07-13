"""Strict HTTP input models for the frozen teacher-pilot wire contract."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteRedeem(FrozenModel):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")


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
