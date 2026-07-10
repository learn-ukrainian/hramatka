"""Offline validation against the private, contract-derived interim schema."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "vendor" / "lu.lesson.v1.schema.json"


@lru_cache(maxsize=1)
def lesson_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_lesson(lesson: dict[str, Any]) -> None:
    """Raise the first structural contract violation from the pinned offline schema."""
    lesson_validator().validate(lesson)
