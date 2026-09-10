"""Canonical visible-block sizing policy for the Hramatka pilot.

Python owns this level × duration policy.  Browser-facing copies are checked
against it in ``tests/test_sizing_policy.py`` so a changed duration cannot
quietly change what teachers see.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

B1: Final = "B1"
# The only duration qualified for newly generated lessons. The legacy 60/90
# maps below remain solely so already-stored lessons can still be read.
DEFAULT_DURATION: Final = 45

# The B1 allocation preserves the established Test → Teach → Test shape while
# raising density above the A1 floor: 45→6, 60→10, 90→12 visible blocks.
_B1_PHASE_BUDGETS: Final[dict[int, dict[int, int]]] = {
    45: {1: 2, 2: 3, 3: 1},
    60: {1: 3, 2: 5, 3: 2},
    90: {1: 4, 2: 5, 3: 3},
}

# The same TTT shape needs a truthful minute plan in the teacher conductor.
# Each row is deliberately complete: its phases must add up to the selected
# duration, rather than leaving an unaccounted-for buffer in the UI.
_B1_PHASE_MINUTES: Final[dict[int, dict[int, int]]] = {
    45: {1: 10, 2: 20, 3: 15},
    60: {1: 15, 2: 30, 3: 15},
    90: {1: 20, 2: 45, 3: 25},
}

# Keep the level dimension explicit even while the pilot serves B1 only.  New
# levels must make their sizing decision here rather than adding local maps.
PHASE_BUDGETS_BY_LEVEL: Final[dict[str, dict[int, dict[int, int]]]] = {
    B1: _B1_PHASE_BUDGETS,
}
PHASE_MINUTES_BY_LEVEL: Final[dict[str, dict[int, dict[int, int]]]] = {
    B1: _B1_PHASE_MINUTES,
}


def phase_budgets(level: str, duration: int) -> Mapping[int, int]:
    """Return the canonical visible-block budgets for one level and duration."""
    try:
        return PHASE_BUDGETS_BY_LEVEL[level][duration]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported lesson sizing: level={level!r}, duration={duration!r}"
        ) from exc


def phase_minutes(level: str, duration: int) -> Mapping[int, int]:
    """Return the canonical teacher-conductor minute budget for one lesson."""
    try:
        minutes = PHASE_MINUTES_BY_LEVEL[level][duration]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported lesson sizing: level={level!r}, duration={duration!r}"
        ) from exc
    if sum(minutes.values()) != duration:  # pragma: no cover - static authority guard
        raise RuntimeError("Phase-minute budgets must total their selected duration.")
    return minutes


def phase_plan(level: str, duration: int) -> list[int]:
    """Expand canonical budgets into the ordered Test → Teach → Test plan."""
    return [
        phase
        for phase, slots in sorted(phase_budgets(level, duration).items())
        for _ in range(slots)
    ]


def planned_block_count(level: str, duration: int) -> int:
    """Return the canonical number of visible blocks for a lesson."""
    return sum(phase_budgets(level, duration).values())


def duration_kind(duration: object) -> str:
    """Classify an invalid caller value without putting the raw value in traces."""
    if duration is None:
        return "missing"
    if isinstance(duration, bool):
        return "boolean"
    if isinstance(duration, int):
        return "unsupported"
    return "invalid"


def resolve_duration(level: str, duration: object) -> tuple[int, str | None]:
    """Resolve a supported duration or loudly fall back to the qualified plan."""
    if isinstance(duration, int) and not isinstance(duration, bool):
        if duration in PHASE_BUDGETS_BY_LEVEL.get(level, {}):
            return duration, None
    return DEFAULT_DURATION, duration_kind(duration)
