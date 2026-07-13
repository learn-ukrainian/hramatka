"""Offline validation against the private, contract-derived interim schema.

The schema is loaded through the digest-verifying vendor loader (review-p46
nit 1): the API must never trust vendored bytes the engine would refuse.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from hramatka.contracts import PILOT_ACTIVITY_TYPES
from hramatka.engine import vendoring


@lru_cache(maxsize=1)
def lesson_validator() -> Draft202012Validator:
    schema = vendoring.read_json(vendoring.LU_LESSON, "lu.lesson.v1.schema.json")
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_lesson(lesson: dict[str, Any]) -> None:
    """Validate against the digest-pinned public schema and pilot type subset.

    The public lesson schema intentionally supports more activity shapes than the
    pilot player.  Checking the private registry here, before the runner commits,
    prevents an otherwise valid but unsupported activity from becoming durable.
    """
    lesson_validator().validate(lesson)
    allowed = set(PILOT_ACTIVITY_TYPES)
    for collection in ("blocks", "rejected"):
        for item in lesson.get(collection, []):
            item_type = item.get("type")
            activity_type = (item.get("activity") or {}).get("type")
            if item_type not in allowed or activity_type != item_type:
                raise ValueError("Lesson contains an activity outside the frozen pilot registry.")
