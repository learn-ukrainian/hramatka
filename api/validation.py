"""Offline validation against the frozen pilot lesson + activity contract.

Schemas load through the digest-verifying vendor loader and resolve the lesson
schema's external activity `$ref` offline via a pinned `referencing.Registry`
mapping — no network fetch.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from jsonschema import Draft7Validator, FormatChecker
from referencing import Registry, Resource

from hramatka.contracts import PILOT_ACTIVITY_TYPES
from hramatka.engine import vendoring

ACTIVITY_SCHEMA_URI = (
    "https://learn-ukrainian.github.io/packages/activity-kit/lu.activity.v1.schema.json"
)
_CLOZE_MARKER_RE = re.compile(r"(?:\{\{|\[___:)(\d+)(?:\}\}|\])")


@lru_cache(maxsize=1)
def lesson_validator() -> Draft7Validator:
    activity_schema = vendoring.read_json(
        vendoring.PILOT_LU_ACTIVITY, "lu.activity.v1.schema.json"
    )
    lesson_schema = vendoring.read_json(vendoring.PILOT_LU_LESSON, "lu.lesson.v1.schema.json")
    activity_resource = Resource.from_contents(activity_schema)
    registry = Registry().with_resource(ACTIVITY_SCHEMA_URI, activity_resource)
    return Draft7Validator(lesson_schema, registry=registry, format_checker=FormatChecker())


def lesson_validation_errors(lesson: dict[str, Any]) -> list[str]:
    """Return human-readable schema errors (empty when valid)."""
    validator = lesson_validator()
    return sorted(error.message for error in validator.iter_errors(lesson))


def validate_lesson(lesson: dict[str, Any]) -> None:
    """Validate against the pilot pin and frozen nine-type registry.

    The public lesson schema intentionally supports more activity shapes than the
    pilot player.  Checking the private registry here, before the runner commits,
    prevents an otherwise valid but unsupported activity from becoming durable.
    """
    errors = lesson_validation_errors(lesson)
    if errors:
        raise ValueError(errors[0])
    allowed = set(PILOT_ACTIVITY_TYPES)
    for collection in ("blocks", "rejected"):
        for item in lesson.get(collection, []):
            item_type = item.get("type")
            activity_type = (item.get("activity") or {}).get("type")
            if item_type not in allowed or activity_type != item_type:
                raise ValueError("Lesson contains an activity outside the frozen pilot registry.")
    _validate_cloze_marker_invariants(lesson)


def _validate_cloze_marker_invariants(lesson: dict[str, Any]) -> None:
    """Keep numbered internal and rendered cloze markers aligned to blank positions."""
    for collection in ("blocks", "rejected"):
        for item in lesson.get(collection, []):
            activity = item.get("activity") or {}
            if activity.get("type") != "cloze":
                continue
            payload = activity.get("payload") or {}
            text = payload.get("text")
            blanks = payload.get("blanks")
            if not isinstance(text, str) or not isinstance(blanks, list):
                continue  # The frozen schema has already reported malformed shapes.
            marker_numbers = {int(value) for value in _CLOZE_MARKER_RE.findall(text)}
            if not marker_numbers:
                continue
            expected_numbers = set(range(1, len(blanks) + 1))
            if marker_numbers != expected_numbers:
                raise ValueError("Cloze markers must be 1-based and match the blank positions.")
