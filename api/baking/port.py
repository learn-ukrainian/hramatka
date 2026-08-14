"""The stable engine boundary consumed by the durable-job runner."""

from __future__ import annotations

from typing import Any, Protocol

FLOOR_SHORTFALL_UA_MESSAGE = (
    "Цього разу не вдалося скласти повний урок. Спробуйте, будь ласка, ще раз."
)


class LessonBaker(Protocol):
    """Future engines must return a ``lu.lesson.v1`` document template."""

    def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]: ...

    def regenerate_activity(
        self,
        anchor: str | dict,
        duration: int,
        focus: str | None,
        *,
        block: dict[str, Any],
        feedback: str | None,
    ) -> dict[str, Any]: ...


class BakeError(RuntimeError):
    """A safe, teacher-visible failure raised by a lesson baker."""


class ProviderUnavailable(BakeError):
    """A transient provider outage eligible for one fresh bake-level retry."""

    def __init__(self, message: str, *, retry_exhausted: bool = True) -> None:
        super().__init__(message)
        # Preserve the provider transport's typed retry outcome across the
        # private engine/API boundary.  This is in-memory control flow only;
        # it is never written to the durable lesson record.
        self.retry_exhausted = retry_exhausted


class GenerationFailed(BakeError):
    """A non-provider generation failure with a safe, allowlisted type tag."""

    def __init__(self, message: str, *, generation_error_type: str) -> None:
        super().__init__(message)
        self.generation_error_type = generation_error_type
        self.retry_exhausted = False


class FloorUnmetError(BakeError):
    """Floor failure (lesson cannot reach 45-min density target).

    Carries blames_source so the runner can classify without string inspection:
    - blames_source=True (thin anchor): surface THIN_SOURCE_UA_MESSAGE
    - blames_source=False (sufficient anchor): surface non-blaming FLOOR_SHORTFALL_UA_MESSAGE
    """

    def __init__(self, message: str, *, blames_source: bool) -> None:
        super().__init__(message)
        self.blames_source: bool = blames_source
