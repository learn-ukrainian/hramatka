"""The stable engine boundary consumed by the durable-job runner."""

from __future__ import annotations

from typing import Any, Protocol


class LessonBaker(Protocol):
    """Future engines must return a ``lu.lesson.v1`` document template."""

    def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]: ...


class BakeError(RuntimeError):
    """A safe, teacher-visible failure raised by a lesson baker."""


class ProviderUnavailable(BakeError):
    """A transient provider outage eligible for one fresh bake-level retry."""


class FloorUnmetError(BakeError):
    """Floor failure (lesson cannot reach 45-min density target).

    Carries blames_source so the runner can classify without string inspection:
    - blames_source=True (thin anchor): surface THIN_SOURCE_UA_MESSAGE
    - blames_source=False (sufficient anchor): surface non-blaming FLOOR_SHORTFALL_UA_MESSAGE
    """

    def __init__(self, message: str, *, blames_source: bool) -> None:
        super().__init__(message)
        self.blames_source: bool = blames_source
