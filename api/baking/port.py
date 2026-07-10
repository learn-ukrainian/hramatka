"""The stable engine boundary consumed by the durable-job runner."""

from __future__ import annotations

from typing import Any, Protocol


class LessonBaker(Protocol):
    """Future engines must return a ``lu.lesson.v1`` document template."""

    def bake(self, anchor: str, duration: int, focus: str | None) -> dict[str, Any]: ...


class BakeError(RuntimeError):
    """A safe, teacher-visible failure raised by a lesson baker."""
