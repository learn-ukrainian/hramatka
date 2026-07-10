"""Offline validation against the private, contract-derived interim schema.

The schema is loaded through the digest-verifying vendor loader (review-p46
nit 1): the API must never trust vendored bytes the engine would refuse.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from hramatka.engine import vendoring


@lru_cache(maxsize=1)
def lesson_validator() -> Draft202012Validator:
    schema = vendoring.read_json(vendoring.LU_LESSON, "lu.lesson.v1.schema.json")
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_lesson(lesson: dict[str, Any]) -> None:
    """Raise the first structural contract violation from the pinned offline schema."""
    lesson_validator().validate(lesson)
