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

    Carries source attribution so the runner can classify without string
    inspection:
    - blames_source=True: the supplied anchor cannot support the certified
      lesson plan, so surface ``insufficient_anchor_capacity``.
    - blames_source=False: a post-generation, validation, or serialization
      floor was not met, so retain the retry-compatible ``lesson_floor_unmet``.

    ``blames_source`` describes the failure cause, not whether retry is safe.
    The durable failure code is the teacher-facing projection of that typed
    cause; raw engine messages never cross the API boundary.
    """

    def __init__(self, message: str, *, blames_source: bool) -> None:
        super().__init__(message)
        self.blames_source: bool = blames_source
