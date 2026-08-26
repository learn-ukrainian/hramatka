"""Human-paced workload policy for the qualified 45-minute lesson."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol

WORKLOAD_VERSION: Final = "Hramatka45MinuteWorkload.v1"


@dataclass(frozen=True)
class PhasePace:
    """Plausible student-active minutes for one lesson phase."""

    minimum_minutes: float
    maximum_minutes: float

    def __post_init__(self) -> None:
        if not 0 < self.minimum_minutes <= self.maximum_minutes:
            raise ValueError("Phase pace needs a positive ordered minute range.")


PHASE_PACE: Final[Mapping[int, PhasePace]] = MappingProxyType(
    {
        1: PhasePace(7.0, 10.0),
        2: PhasePace(15.0, 21.0),
        3: PhasePace(12.0, 18.0),
    }
)

# These are response-time estimates, not universal activity floors. They let
# the exact 45-minute schedule carry different amounts of work for a quick
# matching board, a two-step repair item, and a whole-text reconstruction.
MINUTES_PER_INTERACTION: Final[Mapping[str, float]] = MappingProxyType(
    {
        "match-up": 0.4,
        "true-false": 0.4,
        "quiz": 0.6,
        "fill-in": 0.65,
        "error-correction": 1.1,
        "mark-the-words": 0.5,
        "cloze": 0.5,
    }
)
LONG_READING_BASE_MINUTES: Final = 3.0

# Planning aims sized to the accepted lesson's phase budgets. Quality is
# decided by the phase minute ranges above, so these are not pass/fail counts.
PLANNED_INTERACTIONS: Final[Mapping[tuple[str, str], int]] = MappingProxyType(
    {
        ("P1-A1", "match-up"): 8,
        ("P1-A1", "true-false"): 8,
        ("P1-A2", "quiz"): 8,
        ("P2-A1", "fill-in"): 8,
        ("P2-A2", "error-correction"): 8,
        ("P2-A3", "mark-the-words"): 10,
    }
)


class WorkloadSlot(Protocol):
    phase: int
    scheduled_type: str
    plan: object


def planned_interactions(slot_id: str, activity_type: str) -> int | None:
    """Return the 45-minute planning aim for one fixed-profile slot."""
    return PLANNED_INTERACTIONS.get((slot_id, activity_type))


def interaction_minutes(activity_type: str, count: int) -> float:
    """Estimate active student minutes for one activity."""
    if count < 0:
        raise ValueError("Interaction counts cannot be negative.")
    try:
        minutes = MINUTES_PER_INTERACTION[activity_type] * count
    except KeyError as exc:
        raise ValueError("45-minute workload contains an unweighted activity.") from exc
    return minutes + (LONG_READING_BASE_MINUTES if activity_type == "cloze" else 0.0)


def cloze_interaction_bounds() -> tuple[int, int]:
    """Return the sentence-count range that fits the final-phase pace."""
    pace = PHASE_PACE[3]
    weight = MINUTES_PER_INTERACTION["cloze"]
    return (
        math.ceil((pace.minimum_minutes - LONG_READING_BASE_MINUTES) / weight),
        math.floor((pace.maximum_minutes - LONG_READING_BASE_MINUTES) / weight),
    )


def phase_minutes(slots: Sequence[WorkloadSlot]) -> Mapping[int, float]:
    """Return student-active minutes by phase for an allocated lesson."""
    result: defaultdict[int, float] = defaultdict(float)
    for slot in slots:
        units = getattr(slot.plan, "units", ())
        result[slot.phase] += interaction_minutes(slot.scheduled_type, len(units))
    return MappingProxyType(dict(result))


def validate_phase_pace(slots: Sequence[WorkloadSlot]) -> Mapping[int, float]:
    """Return phase minutes or fail when a complete lesson is implausibly paced."""
    result = phase_minutes(slots)
    for phase, pace in PHASE_PACE.items():
        minutes = result.get(phase, 0.0)
        if not pace.minimum_minutes <= minutes <= pace.maximum_minutes:
            raise ValueError(f"45-minute lesson phase {phase} is outside its active-work pace.")
    return result


def workload_manifest() -> dict[str, object]:
    """Return the content-free pacing identity included in the profile digest."""
    return {
        "version": WORKLOAD_VERSION,
        "phase_pace": {
            str(phase): {
                "minimum_minutes": pace.minimum_minutes,
                "maximum_minutes": pace.maximum_minutes,
            }
            for phase, pace in sorted(PHASE_PACE.items())
        },
        "minutes_per_interaction": dict(sorted(MINUTES_PER_INTERACTION.items())),
        "long_reading_base_minutes": LONG_READING_BASE_MINUTES,
        "planned_interactions": [
            {
                "slot_id": slot_id,
                "activity_type": activity_type,
                "count": count,
            }
            for (slot_id, activity_type), count in sorted(PLANNED_INTERACTIONS.items())
        ],
    }
