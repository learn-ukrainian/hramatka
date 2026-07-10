"""Deterministic mock lesson baker used until the private engine is wired in."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .port import BakeError

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "canonical-lesson.v1.json"


class MockLessonBaker:
    """Return the local canonical fixture after a deliberately visible delay."""

    def __init__(
        self,
        *,
        delay_seconds: int,
        fail: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 20 <= delay_seconds <= 240:
            raise ValueError("Mock bake delay must be between 20 and 240 seconds.")
        self._delay_seconds = delay_seconds
        self._fail = fail
        self._sleeper = sleeper

    def bake(self, anchor: str, duration: int, focus: str | None) -> dict[str, Any]:
        """Match the future real-engine interface exactly.

        The durable job layer, rather than the engine adapter, sets the authoritative
        lesson id, request metadata, lifecycle state, and timestamps.
        """
        del anchor, duration, focus
        self._sleeper(self._delay_seconds)
        if self._fail:
            raise BakeError("Mock bake failure was injected.")
        return copy.deepcopy(json.loads(_FIXTURE_PATH.read_text(encoding="utf-8")))
