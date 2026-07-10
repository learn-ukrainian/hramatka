"""HTTP input models for the intentionally small, poll-first API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnchorInput(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    source: Literal["teacher-paste", "teacher-url"] = "teacher-paste"

    @model_validator(mode="before")
    @classmethod
    def accept_plain_text(cls, value: object) -> object:
        if isinstance(value, str):
            return {"text": value, "source": "teacher-paste"}
        return value


class LessonCreate(BaseModel):
    """``id`` is both the lesson id and the HTTP idempotency key."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    anchor: AnchorInput
    # The API seam is deliberately explicit: v1 is a B1 pilot, not a
    # level-neutral generator that happens to default to B1.  Pydantic emits a
    # structured 422/literal_error for C1, A2, etc. before any durable job or
    # generator call is made.
    level: Literal["B1"] = "B1"
    duration: Literal[45, 60, 90]
    focus: str | None = Field(default=None, max_length=500)


class StatusResponse(BaseModel):
    id: str
    status: Literal["draft", "baking", "ready", "failed"]
    step: Literal["текст отримано", "завдання складено", "перевірка", "готово"]
    last_error: str | None
