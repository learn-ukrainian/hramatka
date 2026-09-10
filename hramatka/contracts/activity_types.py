"""Single source of truth for the pilot's player-backed activity types."""

from __future__ import annotations

import json
from pathlib import Path

_REGISTRY_PATH = Path(__file__).with_name("pilot_activity_types.schema.json")


def _load_registry() -> tuple[str, ...]:
    contract = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    raw = contract["$defs"]["pilotActivityType"]["enum"]
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) for item in raw):
        raise RuntimeError("PILOT_ACTIVITY_TYPES must be a non-empty string array")
    if len(raw) != len(set(raw)):
        raise RuntimeError("PILOT_ACTIVITY_TYPES must not contain duplicates")
    return tuple(raw)


PILOT_ACTIVITY_TYPES = _load_registry()
