"""Locked, pre-cutover authority for ``TeacherReadyDensity.v3``.

This module is deliberately isolated from the live v2 engine.  Later build
slices may consume it, but no production import path may do so before the
atomic v2-to-v3 cutover.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from hramatka.contracts import PILOT_ACTIVITY_TYPES

TEACHER_READY_DENSITY_VERSION: Final = "TeacherReadyDensity.v3"


@dataclass(frozen=True)
class TextQuestionBudget:
    """The complete, indivisible text-question composition."""

    comprehension: int
    explanation_inference: int
    anchored_application: int

    def __post_init__(self) -> None:
        if min(self.comprehension, self.explanation_inference, self.anchored_application) < 0:
            raise ValueError("Text-question budgets cannot be negative.")

    @property
    def total(self) -> int:
        return self.comprehension + self.explanation_inference + self.anchored_application


TEXT_QUESTION_3_3_2: Final = TextQuestionBudget(3, 3, 2)


@dataclass(frozen=True)
class ActivityFloor:
    """One activity family's non-negotiable minimum substrate."""

    activity_type: str
    minimum_units: int
    unit_kind: str
    category_minima: TextQuestionBudget | None = None
    minimum_registered_constraints: int = 0
    certified_errors_per_unit: int = 0

    def __post_init__(self) -> None:
        if not self.activity_type:
            raise ValueError("An activity floor needs an activity type.")
        if self.minimum_units < 1:
            raise ValueError("An activity floor must require at least one unit.")
        if self.minimum_registered_constraints < 0 or self.certified_errors_per_unit < 0:
            raise ValueError("Floor subrequirements cannot be negative.")
        if self.category_minima is not None and self.category_minima.total != self.minimum_units:
            raise ValueError("Text-question category minima must equal the activity floor.")


# The one v3 per-type density authority.  Do not copy these values into future
# builders, evaluators, receipt code, or phase planners: import ``floor_for``.
FLOOR_TABLE: Final[Mapping[str, ActivityFloor]] = MappingProxyType(
    {
        "true-false": ActivityFloor("true-false", 8, "distinct_statement"),
        "quiz": ActivityFloor("quiz", 8, "distinct_question"),
        "cloze": ActivityFloor("cloze", 8, "distinct_gap_position"),
        "match-up": ActivityFloor("match-up", 8, "unique_atlas_pass_pair"),
        "fill-in": ActivityFloor("fill-in", 8, "distinct_sentence_item"),
        "error-correction": ActivityFloor(
            "error-correction", 8, "distinct_sentence_item", certified_errors_per_unit=1
        ),
        "text-questions": ActivityFloor(
            "text-questions", 8, "distinct_question", category_minima=TEXT_QUESTION_3_3_2
        ),
        "mark-the-words": ActivityFloor("mark-the-words", 8, "unique_certified_target_token"),
        "short-writing": ActivityFloor(
            "short-writing", 1, "productive_task", minimum_registered_constraints=2
        ),
    }
)

if set(FLOOR_TABLE) != set(PILOT_ACTIVITY_TYPES):  # pragma: no cover - import-time contract guard
    raise RuntimeError("TeacherReadyDensity.v3 must cover every pilot activity type exactly once.")


def floor_for(activity_type: str) -> ActivityFloor:
    """Return the sole v3 floor for one registered activity type."""
    try:
        return FLOOR_TABLE[activity_type]
    except KeyError as exc:
        raise ValueError(
            f"Unknown TeacherReadyDensity.v3 activity type: {activity_type!r}"
        ) from exc


@dataclass(frozen=True)
class PhaseShape:
    """A v3 phase table and any explicit text-question budget within it."""

    duration_minutes: int
    phase_slots: Mapping[int, int]
    text_question_budget_by_phase: Mapping[int, TextQuestionBudget]

    def __post_init__(self) -> None:
        if self.duration_minutes <= 0:
            raise ValueError("A phase shape needs a positive lesson duration.")
        slots = {int(phase): int(count) for phase, count in self.phase_slots.items()}
        if not slots or any(phase < 1 or count < 1 for phase, count in slots.items()):
            raise ValueError("Phase shapes need positive phases and slot counts.")
        budgets = {
            int(phase): budget for phase, budget in self.text_question_budget_by_phase.items()
        }
        if not set(budgets).issubset(slots):
            raise ValueError("A text-question budget must belong to a declared phase.")
        object.__setattr__(self, "phase_slots", MappingProxyType(slots))
        object.__setattr__(self, "text_question_budget_by_phase", MappingProxyType(budgets))

    def allows(self, phase: int, activity_type: str) -> bool:
        """Return whether this exact phase table can host an activity family.

        The special 45-minute restriction is intentionally table-driven.  The
        single phase-3 slot may not carry text-questions unless the same table
        expressly reserves the full, unsplittable 3+3+2 composition.
        """
        floor_for(activity_type)
        if phase not in self.phase_slots:
            return False
        if self.duration_minutes != 45 or phase != 3 or activity_type != "text-questions":
            return True
        return self.text_question_budget_by_phase.get(phase) == TEXT_QUESTION_3_3_2


# These are v3 contract values only; they intentionally do not alter the live
# sizing policy until the later atomic cutover.
PHASE_SHAPES: Final[Mapping[int, PhaseShape]] = MappingProxyType(
    {
        45: PhaseShape(45, {1: 3, 2: 4, 3: 1}, {}),
        60: PhaseShape(60, {1: 3, 2: 5, 3: 2}, {}),
        90: PhaseShape(90, {1: 4, 2: 5, 3: 3}, {}),
    }
)


def phase_shape_for(duration_minutes: int) -> PhaseShape:
    """Return the v3 phase shape for a supported teacher lesson duration."""
    try:
        return PHASE_SHAPES[duration_minutes]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported TeacherReadyDensity.v3 duration: {duration_minutes!r}"
        ) from exc


def type_allowed_in_phase(duration_minutes: int, phase: int, activity_type: str) -> bool:
    """Apply the central v3 phase-shape rule without touching the v2 scheduler."""
    return phase_shape_for(duration_minutes).allows(phase, activity_type)
