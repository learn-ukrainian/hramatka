"""Human-facing quality contract for the qualified 45-minute lesson."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from .lesson_capacity_v3 import AllocatedSlot, LessonAllocation
from .lesson_profile_45_v1 import lesson_profile_45
from .lesson_workload_45_v1 import phase_minutes, validate_phase_pace
from .sentence_segmentation_v1 import sentence_spans
from .teacher_ready_density_v3 import floor_for

QUALITY_CONTRACT_VERSION: Final = "HramatkaTeacherLessonQuality.v2"
MIN_DISTINCT_ACTIVITY_TYPES_45: Final = 6
MIN_DISTINCT_COGNITIVE_OPERATIONS_45: Final = 6
MIN_CLOZE_WORDS_45: Final = 350
MAX_CLOZE_WORDS_45: Final = 450
_UKRAINIAN_WORD_RE: Final = re.compile(r"[А-Яа-яІіЇїЄєҐґ][А-Яа-яІіЇїЄєҐґ'’\-]*")
_GAP_MARKER_RE: Final = re.compile(r"\{[1-9]\d*\}")

# The production allocator's coarse operation labels are useful for source
# reservation, but they are not a pedagogical variety measure (quiz and cloze
# are both called ``recall`` there). This contract names the learner action the
# teacher actually sees. Format and learner action are intentionally checked as
# separate contract axes even though the current pilot mapping is one-to-one;
# future activity formats may not silently collapse the action variety.
QUALITY_OPERATION: Final[Mapping[str, str]] = MappingProxyType(
    {
        "quiz": "retrieve-select",
        "cloze": "contextual-reconstruct",
        "match-up": "semantic-associate",
        "fill-in": "guided-form",
        "error-correction": "diagnose-repair",
        "mark-the-words": "identify-target",
    }
)

_SPACE_RE: Final = re.compile(r"\s+")


class LessonQualityError(ValueError):
    """The complete rendered lesson is not ready for a teacher."""


def estimated_active_work_minutes(allocation: LessonAllocation) -> float:
    """Estimate student-active work from certified units in the final plan."""
    try:
        return sum(phase_minutes(allocation.slots).values())
    except ValueError as exc:
        raise LessonQualityError(str(exc)) from exc


def _normalized_prompt(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _SPACE_RE.sub(" ", unicodedata.normalize("NFC", value).casefold()).strip()
    return normalized or None


def _mapping_rows(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        return ()
    return tuple(value)


def _string_rows(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(row, str) for row in value):
        return ()
    return tuple(value)


def learner_surfaces(activity: Mapping[str, object]) -> tuple[str, ...]:
    """Project the answer-bearing learner surfaces from one rendered activity."""
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        raise LessonQualityError("Rendered lesson activity has no learner payload.")
    activity_type = payload.get("type")
    surfaces: tuple[object, ...]
    if activity_type == "quiz":
        surfaces = tuple(row.get("question") for row in _mapping_rows(payload.get("items")))
    elif activity_type == "true-false":
        surfaces = tuple(row.get("statement") for row in _mapping_rows(payload.get("items")))
    elif activity_type == "fill-in":
        surfaces = tuple(row.get("sentence") for row in _mapping_rows(payload.get("items")))
    elif activity_type in {"error-correction", "text-questions"}:
        surfaces = _string_rows(payload.get("items"))
    elif activity_type == "cloze":
        surfaces = (payload.get("text"),)
    elif activity_type == "match-up":
        surfaces = tuple(
            f"{row.get('left')} ↔ {row.get('right')}" for row in _mapping_rows(payload.get("pairs"))
        )
    elif activity_type == "mark-the-words":
        surfaces = (payload.get("text"),)
    elif activity_type == "short-writing":
        surfaces = (payload.get("prompt"),)
    else:
        raise LessonQualityError("Rendered lesson contains an unknown activity type.")
    normalized = tuple(
        item for value in surfaces if (item := _normalized_prompt(value)) is not None
    )
    if not normalized:
        raise LessonQualityError("Rendered lesson activity has no learner work surface.")
    return normalized


def validate_teacher_lesson_plan_quality_45(allocation: LessonAllocation) -> None:
    """Fail closed unless a complete 45-minute plan is dense and varied."""
    profile = lesson_profile_45()
    expected_slots = profile.slots
    if len(allocation.slots) != len(expected_slots):
        raise LessonQualityError("45-minute lesson does not contain every profile slot.")

    scheduled_types: list[str] = []
    source_uses: Counter[str] = Counter()
    scored_claims: set[tuple[str, str, str]] = set()
    for expected, allocated in zip(expected_slots, allocation.slots, strict=True):
        allowed_types = {expected.requested_type, *expected.replacement_types}
        if (
            allocated.slot_id != expected.slot_id
            or allocated.phase != expected.phase
            or allocated.scheduled_type not in allowed_types
            or not allocated.plan.floor_met
            or len(allocated.plan.units) < floor_for(allocated.scheduled_type).minimum_units
        ):
            raise LessonQualityError("45-minute lesson drifted from its certified profile.")
        scheduled_types.append(allocated.scheduled_type)
        operation = QUALITY_OPERATION.get(allocated.scheduled_type)
        if operation is None:
            raise LessonQualityError("45-minute lesson contains an unclassified learner action.")
        slot_source_ids: set[str] = set()
        for unit in allocated.plan.units:
            if unit.anchor.kind != "evidence":
                continue
            slot_source_ids.add(unit.anchor.anchor_id)
            claim = (
                unit.anchor.anchor_id,
                operation,
                (
                    unit.unit_id
                    if allocated.scheduled_type == "mark-the-words"
                    else _normalized_prompt(unit.expected_key_or_rule.value) or ""
                ),
            )
            if claim in scored_claims:
                raise LessonQualityError("45-minute lesson repeats a scored source operation.")
            scored_claims.add(claim)
        if allocated.scheduled_type != "cloze":
            source_uses.update(slot_source_ids)

    if len(set(scheduled_types)) < MIN_DISTINCT_ACTIVITY_TYPES_45:
        raise LessonQualityError("45-minute lesson lacks activity-format variety.")
    if (
        len({QUALITY_OPERATION[activity_type] for activity_type in scheduled_types})
        < MIN_DISTINCT_COGNITIVE_OPERATIONS_45
    ):
        raise LessonQualityError("45-minute lesson lacks cognitive-operation variety.")
    if allocation.slots[-1].phase != 3 or allocation.slots[-1].scheduled_type != "cloze":
        raise LessonQualityError("45-minute lesson does not culminate in long-form reconstruction.")
    if max(source_uses.values(), default=0) > 2:
        raise LessonQualityError("45-minute lesson overuses one source carrier.")
    try:
        validate_phase_pace(allocation.slots)
    except ValueError as exc:
        raise LessonQualityError(str(exc)) from exc


def validate_teacher_rendered_slot_quality_45(
    allocated: AllocatedSlot,
    rendered: Mapping[str, object],
) -> tuple[str, ...]:
    """Validate one rendered slot against its certified 45-minute plan."""
    slot_id = rendered.get("slot_id")
    activity_type = rendered.get("type")
    activity = rendered.get("activity")
    if (
        slot_id != allocated.slot_id
        or activity_type != allocated.scheduled_type
        or not isinstance(activity, Mapping)
    ):
        raise LessonQualityError("Rendered lesson is detached from its certified slot plan.")
    surfaces = learner_surfaces(activity)
    if activity_type not in {"cloze", "mark-the-words"} and len(surfaces) != len(
        allocated.plan.units
    ):
        raise LessonQualityError("Rendered lesson activity lost certified interactions.")
    if len(surfaces) != len(set(surfaces)):
        raise LessonQualityError("Rendered lesson repeats work inside one activity.")
    payload = activity.get("payload")
    assert isinstance(payload, Mapping)
    if activity_type == "quiz":
        if any("___" in surface or _GAP_MARKER_RE.search(surface) for surface in surfaces):
            raise LessonQualityError("Quiz repeats a gap-completion action.")
    elif activity_type == "cloze":
        text = payload.get("text")
        blanks = _mapping_rows(payload.get("blanks"))
        if (
            not isinstance(text, str)
            or len(blanks) != len(allocated.plan.units)
            or len(_GAP_MARKER_RE.findall(text)) != len(blanks)
        ):
            raise LessonQualityError("Long-form reconstruction lost certified gaps.")
        reconstructed = text
        for blank in blanks:
            marker = f"{{{blank.get('id')}}}"
            answer = blank.get("answer")
            if not isinstance(answer, str) or reconstructed.count(marker) != 1:
                raise LessonQualityError("Long-form reconstruction has an invalid answer map.")
            reconstructed = reconstructed.replace(marker, answer)
        word_count = len(_UKRAINIAN_WORD_RE.findall(reconstructed))
        if not MIN_CLOZE_WORDS_45 <= word_count <= MAX_CLOZE_WORDS_45:
            raise LessonQualityError("Long-form reconstruction is outside 350–450 words.")
        gapped_sentences = sentence_spans(text)
        reconstructed_sentences = sentence_spans(reconstructed)
        if (
            len(gapped_sentences) != len(reconstructed_sentences)
            or len(blanks) != len(gapped_sentences)
            or any(len(_GAP_MARKER_RE.findall(sentence)) != 1 for sentence in gapped_sentences)
        ):
            raise LessonQualityError(
                "Long-form reconstruction must contain one interaction in every sentence."
            )
    elif activity_type == "mark-the-words":
        targets = _string_rows(payload.get("target_words"))
        text = payload.get("text")
        normalized_targets = tuple(_normalized_prompt(target) for target in targets)
        if (
            not isinstance(text, str)
            or len(targets) != len(allocated.plan.units)
            or any(
                target is None or len(_UKRAINIAN_WORD_RE.findall(target)) != 1
                for target in normalized_targets
            )
        ):
            raise LessonQualityError("Word identification lost certified targets.")
        target_set = set(normalized_targets)
        markable_instances = sum(
            _normalized_prompt(word) in target_set for word in _UKRAINIAN_WORD_RE.findall(text)
        )
        # The activity UI scores target *occurrences*, not unique lemmas.
        # Repeated forms in different certified token positions therefore
        # remain separate interactions, but the rendered passage must
        # actually contain every planned interaction.
        if markable_instances < len(allocated.plan.units):
            raise LessonQualityError("Word identification lost certified target positions.")
    return surfaces


def validate_teacher_lesson_quality_45(
    allocation: LessonAllocation,
    rendered_slots: Sequence[Mapping[str, object]],
) -> None:
    """Fail closed unless the complete 45-minute lesson is dense and varied."""
    validate_teacher_lesson_plan_quality_45(allocation)

    if len(rendered_slots) != len(allocation.slots):
        raise LessonQualityError("45-minute lesson did not render every certified slot.")
    seen_surfaces: dict[str, str] = {}
    for allocated, rendered in zip(allocation.slots, rendered_slots, strict=True):
        surfaces = validate_teacher_rendered_slot_quality_45(allocated, rendered)
        for surface in surfaces:
            prior_slot = seen_surfaces.get(surface)
            if prior_slot is not None and prior_slot != allocated.slot_id:
                raise LessonQualityError("Rendered lesson repeats learner work across activities.")
            seen_surfaces[surface] = allocated.slot_id
