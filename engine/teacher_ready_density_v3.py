"""Live authority for the atomic ``TeacherReadyDensity.v3`` production path."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from hramatka.contracts import PILOT_ACTIVITY_TYPES

TEACHER_READY_DENSITY_VERSION: Final = "TeacherReadyDensity.v4"

# Allocation owns the operation identity used by its immutable ≤2 evidence
# reuse rule.  Keeping it beside the v3 floor authority preserves the
# cutover's self-contained preflight path.
COGNITIVE_OPERATION: Final[Mapping[str, str]] = MappingProxyType(
    {
        "true-false": "evaluate",
        "quiz": "recall",
        "cloze": "long-form-reconstruction",
        "fill-in": "form",
        "error-correction": "form",
        "mark-the-words": "identify",
        "match-up": "associate",
        "text-questions": "discuss",
        "short-writing": "write",
    }
)

# Source comprehension may share a carrier only as a genuine interpretive or
# transfer task. Literal fact recovery is reserved for undrilled propositions;
# the final inventory labels any fallback overlap as anchored application.
EVIDENCE_CAPACITY_OVERLAYS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("text-questions", "source-comprehension"),
        ("cloze", "long-form-reconstruction"),
    }
)


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


TEXT_QUESTION_COMPREHENSION_FLOOR: Final = TextQuestionBudget(3, 0, 0)


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
        if self.category_minima is not None and self.category_minima.total > self.minimum_units:
            raise ValueError("Text-question category minima cannot exceed the activity floor.")


# The one v4 per-type density authority.  Do not copy these values into future
# builders, evaluators, receipt code, or phase planners: import ``floor_for``.
FLOOR_TABLE: Final[Mapping[str, ActivityFloor]] = MappingProxyType(
    {
        "true-false": ActivityFloor("true-false", 5, "distinct_statement"),
        "quiz": ActivityFloor("quiz", 5, "distinct_question"),
        "cloze": ActivityFloor("cloze", 5, "distinct_gap_position"),
        "match-up": ActivityFloor("match-up", 6, "unique_atlas_pass_pair"),
        "fill-in": ActivityFloor("fill-in", 5, "distinct_sentence_item"),
        "error-correction": ActivityFloor(
            "error-correction", 5, "distinct_sentence_item", certified_errors_per_unit=1
        ),
        "text-questions": ActivityFloor(
            "text-questions",
            5,
            "distinct_question",
            category_minima=TEXT_QUESTION_COMPREHENSION_FLOOR,
        ),
        "mark-the-words": ActivityFloor("mark-the-words", 5, "unique_certified_target_token"),
        "short-writing": ActivityFloor(
            "short-writing", 1, "productive_task", minimum_registered_constraints=2
        ),
    }
)

if set(FLOOR_TABLE) != set(PILOT_ACTIVITY_TYPES):  # pragma: no cover - import-time contract guard
    raise RuntimeError("TeacherReadyDensity.v4 must cover every pilot activity type exactly once.")


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
        expressly reserves a complete floor-sized question block.
        """
        floor_for(activity_type)
        if phase not in self.phase_slots:
            return False
        if self.duration_minutes != 45 or phase != 3 or activity_type != "text-questions":
            return True
        budget = self.text_question_budget_by_phase.get(phase)
        minima = floor_for("text-questions").category_minima
        return (
            budget is not None
            and minima is not None
            and budget.total == floor_for("text-questions").minimum_units
            and budget.comprehension >= minima.comprehension
            and budget.explanation_inference >= minima.explanation_inference
            and budget.anchored_application >= minima.anchored_application
        )


# These are v3 contract values only; they intentionally do not alter the live
# sizing policy until the later atomic cutover.
PHASE_SHAPES: Final[Mapping[int, PhaseShape]] = MappingProxyType(
    {
        45: PhaseShape(45, {1: 2, 2: 3, 3: 1}, {}),
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
    """Apply the central v3 phase-shape rule for a live lesson duration."""
    return phase_shape_for(duration_minutes).allows(phase, activity_type)


def density_floor_fingerprint() -> str:
    """Return the content-free identity of the locked v3 floor authority.

    Qualification records this value rather than copying individual floor
    values into a receipt.  It deliberately lives next to the live authority.
    """
    payload = {
        "version": TEACHER_READY_DENSITY_VERSION,
        "phase_shapes": {
            str(duration): {
                "phase_slots": {
                    str(phase): slots for phase, slots in sorted(shape.phase_slots.items())
                },
                "text_question_budget_by_phase": {
                    str(phase): budget
                    for phase, budget in sorted(shape.text_question_budget_by_phase.items())
                },
            }
            for duration, shape in sorted(PHASE_SHAPES.items())
        },
        "floors": [
            {
                "type": activity_type,
                "minimum_units": floor.minimum_units,
                "unit_kind": floor.unit_kind,
                "category_minima": (
                    None
                    if floor.category_minima is None
                    else {
                        "comprehension": floor.category_minima.comprehension,
                        "explanation_inference": floor.category_minima.explanation_inference,
                        "anchored_application": floor.category_minima.anchored_application,
                    }
                ),
                "minimum_registered_constraints": floor.minimum_registered_constraints,
                "certified_errors_per_unit": floor.certified_errors_per_unit,
            }
            for activity_type, floor in sorted(FLOOR_TABLE.items())
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
