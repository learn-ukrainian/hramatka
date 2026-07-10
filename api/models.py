"""HTTP input models for the intentionally small, poll-first API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    anchor: AnchorInput
    duration: Literal[45, 60, 90]
    focus: str | None = Field(default=None, max_length=500)


class StatusResponse(BaseModel):
    id: str
    status: Literal["draft", "baking", "ready", "failed"]
    step: Literal["текст отримано", "завдання складено", "перевірка", "готово"]
    last_error: str | None
