"""Deterministic, pre-generation capacity for live TeacherReadyDensity.v3.

It receives only already-certified v3 plans, reserves their shared inventory
claims across the complete lesson, and returns either immutable scheduled
substrate or a recoverable capacity event.  It never calls a model.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

from .closed_class_policy import CLOSED_CLASS_ALLOWED_SHAPES, is_closed_class_target
from .lesson_workload_45_v1 import (
    PHASE_PACE,
    interaction_minutes,
    planned_interactions,
)
from .teacher_ready_density_v3 import (
    COGNITIVE_OPERATION,
    EVIDENCE_CAPACITY_OVERLAYS,
    floor_for,
    phase_shape_for,
    type_allowed_in_phase,
)
from .unit_builders_v3 import BUILDERS, CertificationInventory
from .unit_plan_v3 import CertifiedUnit, UnitPlan, certify_unit_plan, normalize_distinctness_key


@dataclass(frozen=True)
class LessonSlot:
    """One scheduled lesson position and its ordered t=0 replacement types."""

    slot_id: str
    phase: int
    requested_type: str
    replacement_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.slot_id.strip() or self.phase < 1:
            raise ValueError("Lesson slots need a stable ID and positive phase.")
        floor_for(self.requested_type)
        replacements = tuple(self.replacement_types)
        if len(replacements) != len(set(replacements)):
            raise ValueError("Lesson slot replacement types must be distinct.")
        if self.requested_type in replacements:
            raise ValueError("A lesson slot cannot replace a type with itself.")
        for activity_type in replacements:
            floor_for(activity_type)
        object.__setattr__(self, "replacement_types", replacements)


@dataclass(frozen=True)
class SlotPlans:
    """All certified-or-unavailable plans considered for one scheduled slot."""

    slot: LessonSlot
    primary: UnitPlan
    replacements: tuple[UnitPlan, ...] = ()

    def __post_init__(self) -> None:
        plans = (self.primary, *self.replacements)
        if any(
            plan.slot_id != self.slot.slot_id or plan.phase != self.slot.phase for plan in plans
        ):
            raise ValueError("Capacity plans must belong to their declared lesson slot.")
        if self.primary.activity_type != self.slot.requested_type:
            raise ValueError("A primary plan must match the slot's requested type.")
        replacements = tuple(self.replacements)
        replacement_types = tuple(plan.activity_type for plan in replacements)
        if replacement_types != self.slot.replacement_types:
            raise ValueError("Replacement plans must match the declared replacement order exactly.")
        object.__setattr__(self, "replacements", replacements)


@dataclass(frozen=True)
class AnchorParagraph:
    """One stable-document-order paragraph with canonical v3 inventory."""

    paragraph_id: str
    inventory: CertificationInventory

    def __post_init__(self) -> None:
        if not self.paragraph_id.strip():
            raise ValueError("Anchor paragraphs need stable IDs.")


@dataclass(frozen=True)
class AnchorWindow:
    """A contiguous initial anchor and its configured lesson boundary."""

    paragraphs: tuple[AnchorParagraph, ...]
    initial_start: int
    initial_end: int
    lesson_start: int = 0
    lesson_end: int | None = None

    def __post_init__(self) -> None:
        paragraphs = tuple(self.paragraphs)
        if not paragraphs:
            raise ValueError("Anchor windows need at least one document paragraph.")
        paragraph_ids = [paragraph.paragraph_id for paragraph in paragraphs]
        if len(paragraph_ids) != len(set(paragraph_ids)):
            raise ValueError("Anchor paragraph IDs must be distinct in document order.")
        lesson_end = len(paragraphs) - 1 if self.lesson_end is None else self.lesson_end
        if not (
            0 <= self.lesson_start <= self.initial_start <= self.initial_end <= lesson_end
            and lesson_end < len(paragraphs)
        ):
            raise ValueError("Initial anchor and lesson boundary must be ordered document indexes.")
        object.__setattr__(self, "paragraphs", paragraphs)
        object.__setattr__(self, "lesson_end", lesson_end)

    def widened_paragraphs(self) -> tuple[tuple[AnchorParagraph, ...], ...]:
        """Return deterministic symmetric windows, each in document order."""
        start = self.initial_start
        end = self.initial_end
        windows = [self.paragraphs[start : end + 1]]
        assert self.lesson_end is not None
        while start > self.lesson_start or end < self.lesson_end:
            if start > self.lesson_start:
                start -= 1
            if end < self.lesson_end:
                end += 1
            windows.append(self.paragraphs[start : end + 1])
        return tuple(windows)


@dataclass(frozen=True)
class ConditionalReplacement:
    """One alternate slot plan proven safe against the chosen lesson remainder."""

    activity_type: str
    plan: UnitPlan

    def __post_init__(self) -> None:
        if self.plan.activity_type != self.activity_type or not self.plan.floor_met:
            raise ValueError("Conditional replacements must carry a certified matching plan.")

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.activity_type,
            "unit_ids": [unit.unit_id for unit in self.plan.units],
        }


@dataclass(frozen=True)
class AllocatedSlot:
    """The floor-sized immutable substrate scheduled for one lesson slot."""

    slot_id: str
    phase: int
    requested_type: str
    scheduled_type: str
    plan: UnitPlan
    substitution_reason: (
        Literal["preflight_unavailable", "capacity", "serialization_exhausted"] | None
    ) = None
    conditional_replacements: tuple[ConditionalReplacement, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.plan.slot_id != self.slot_id
            or self.plan.phase != self.phase
            or self.plan.activity_type != self.scheduled_type
            or not self.plan.floor_met
        ):
            raise ValueError("Allocated slots must hold their certified scheduled plan.")
        if self.scheduled_type == self.requested_type and self.substitution_reason is not None:
            raise ValueError("Requested types cannot carry a substitution reason.")
        if self.scheduled_type != self.requested_type and self.substitution_reason is None:
            raise ValueError("A substituted type needs a deterministic provenance reason.")
        replacements = tuple(self.conditional_replacements)
        if len({replacement.activity_type for replacement in replacements}) != len(replacements):
            raise ValueError("Conditional replacement types must be distinct.")
        if self.scheduled_type in {replacement.activity_type for replacement in replacements}:
            raise ValueError("A conditional replacement must differ from the scheduled type.")
        object.__setattr__(self, "conditional_replacements", replacements)

    @property
    def substituted_at_t0(self) -> bool:
        return self.scheduled_type != self.requested_type

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_id": self.slot_id,
            "phase": self.phase,
            "requested_type": self.requested_type,
            "scheduled_type": self.scheduled_type,
            "substitution_reason": self.substitution_reason,
            "unit_ids": [unit.unit_id for unit in self.plan.units],
            "conditional_replacements": [
                replacement.to_dict() for replacement in self.conditional_replacements
            ],
        }


@dataclass(frozen=True)
class LessonAllocation:
    """One complete exact-cover allocation over a single anchor window."""

    paragraph_ids: tuple[str, ...]
    slots: tuple[AllocatedSlot, ...]

    def __post_init__(self) -> None:
        paragraph_ids = tuple(self.paragraph_ids)
        slots = tuple(self.slots)
        if not paragraph_ids or not slots:
            raise ValueError("Lesson allocations need a non-empty window and scheduled slots.")
        if len({slot.slot_id for slot in slots}) != len(slots):
            raise ValueError("Lesson allocation slot IDs must be distinct.")
        object.__setattr__(self, "paragraph_ids", paragraph_ids)
        object.__setattr__(self, "slots", slots)

    def to_dict(self) -> dict[str, object]:
        return {
            "paragraph_ids": list(self.paragraph_ids),
            "slots": [slot.to_dict() for slot in self.slots],
        }

    def canonical_bytes(self) -> bytes:
        """Serialize deterministic allocation decisions for byte-level replay checks."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


@dataclass(frozen=True)
class CapacityAttempt:
    """One no-generation allocation attempt for a deterministic anchor window."""

    paragraph_ids: tuple[str, ...]
    allocation_found: bool


@dataclass(frozen=True)
class InsufficientAnchorCapacityEvent:
    """Recoverable pre-generation outcome after the lesson boundary is exhausted."""

    paragraph_ids: tuple[str, ...]
    code: Literal["insufficient_anchor_capacity", "closed_class_shape_mismatch"] = (
        "insufficient_anchor_capacity"
    )
    recoverable: bool = True

    def __post_init__(self) -> None:
        if not self.paragraph_ids or self.code not in {
            "insufficient_anchor_capacity",
            "closed_class_shape_mismatch",
        }:
            raise ValueError("Capacity events need a window and the locked failure code.")
        if not self.recoverable:
            raise ValueError("Insufficient anchor capacity must remain recoverable.")


@dataclass(frozen=True)
class LessonPreflightResult:
    """The no-model boundary between capacity certification and serialization."""

    allocation: LessonAllocation | None
    event: InsufficientAnchorCapacityEvent | None
    attempts: tuple[CapacityAttempt, ...]

    def __post_init__(self) -> None:
        attempts = tuple(self.attempts)
        if not attempts:
            raise ValueError("Capacity preflight must record every anchor-window attempt.")
        if (self.allocation is None) == (self.event is None):
            raise ValueError("Preflight returns exactly one allocation or capacity event.")
        object.__setattr__(self, "attempts", attempts)

    @property
    def generation_authorization(self) -> GenerationAuthorization | None:
        """Expose, but never invoke, the post-preflight serialization seam."""
        return GenerationAuthorization(self.allocation) if self.allocation is not None else None

    @property
    def generation_authorized(self) -> bool:
        return self.generation_authorization is not None


@dataclass(frozen=True)
class GenerationAuthorization:
    """An explicit post-preflight handoff; serialization occurs only on this call."""

    allocation: LessonAllocation

    def serialize_with(self, serializer: Callable[[LessonAllocation], object]) -> object:
        return serializer(self.allocation)


@dataclass(frozen=True)
class _SourceUse:
    phase: int
    operation: str


@dataclass(frozen=True)
class _Reservation:
    claims: frozenset[tuple[str, str]]
    source_usage: Mapping[str, tuple[_SourceUse, ...]]

    @classmethod
    def empty(cls) -> _Reservation:
        return cls(frozenset(), {})


@dataclass(frozen=True)
class _Choice:
    slot_plans: SlotPlans
    plan: UnitPlan
    substitution_reason: Literal["preflight_unavailable", "capacity"] | None


def _operation_for(plan: UnitPlan) -> str:
    """Return the certified cognitive operation used for source-reuse checks."""
    alignments = {
        unit.distinctness.get("focus_alignment")
        for unit in plan.units
        if isinstance(unit.distinctness.get("focus_alignment"), str)
    }
    if len(alignments) == 1:
        return next(iter(alignments))
    return COGNITIVE_OPERATION.get(plan.activity_type, plan.activity_type)


def _non_evidence_claims(plan: UnitPlan) -> tuple[tuple[str, str], ...]:
    """Return every shared kit/stem/gap/token/pair claim except source evidence."""
    explicit_claims = tuple(
        (claim.kind, claim.resource_id)
        for unit in plan.units
        for claim in unit.resource_claims
        if claim.kind not in {"sentence", "context_sentence"}
    )
    distinctness_kind = {
        "cloze": "gap",
        "mark-the-words": "target_token",
        "match-up": "pair",
    }.get(plan.activity_type, "stem")
    distinctness_claims = tuple(
        (distinctness_kind, normalize_distinctness_key(plan.activity_type, unit.distinctness))
        for unit in plan.units
    )
    return (*explicit_claims, *distinctness_claims)


def _source_evidence_ids(plan: UnitPlan) -> frozenset[str]:
    """Count an evidence source once per slot, never once per constituent unit."""
    if plan.activity_type == "cloze" and _operation_for(plan) == "long-form-reconstruction":
        # The final contiguous passage is the lesson's integration substrate,
        # not a third independent drill on every sentence it contains. Gap and
        # candidate claims still stay exclusive through _non_evidence_claims.
        return frozenset()
    if (plan.activity_type, _operation_for(plan)) in EVIDENCE_CAPACITY_OVERLAYS:
        # Literal comprehension remains exclusive evidence. Only the two
        # interpretive/application rows are true overlays and may reuse a
        # source carrier already drilled elsewhere in the lesson.
        return frozenset(
            unit.anchor.anchor_id
            for unit in plan.units
            if unit.anchor.kind == "evidence"
            and unit.distinctness.get("question_category") == "comprehension"
        )
    return frozenset(
        (
            *(unit.anchor.anchor_id for unit in plan.units if unit.anchor.kind == "evidence"),
            *(
                f"{unit.anchor.anchor_id.rsplit(':', 1)[0]}:{claim.resource_id}"
                for unit in plan.units
                for claim in unit.resource_claims
                if unit.anchor.kind == "evidence" and claim.kind == "context_sentence"
            ),
        )
    )


def _all_source_evidence_ids(plan: UnitPlan) -> frozenset[str]:
    """Return source carriers without applying the read/discuss overlay exemption."""
    return frozenset(
        (
            *(unit.anchor.anchor_id for unit in plan.units if unit.anchor.kind == "evidence"),
            *(
                f"{unit.anchor.anchor_id.rsplit(':', 1)[0]}:{claim.resource_id}"
                for unit in plan.units
                for claim in unit.resource_claims
                if unit.anchor.kind == "evidence" and claim.kind == "context_sentence"
            ),
        )
    )


def _narrative_drill_plan_overlap_ok(plans: Sequence[UnitPlan]) -> bool:
    """Keep quiz/fill-in carriers distinct in the 45-minute narrative shape."""
    if not any(
        (plan.activity_type, _operation_for(plan)) in EVIDENCE_CAPACITY_OVERLAYS for plan in plans
    ):
        return True
    quiz_plans = [plan for plan in plans if plan.activity_type == "quiz"]
    fill_plans = [plan for plan in plans if plan.activity_type == "fill-in"]
    return all(
        len(_all_source_evidence_ids(quiz) & _all_source_evidence_ids(fill)) <= 2
        for quiz in quiz_plans
        for fill in fill_plans
    )


def _narrative_drill_overlap_ok(choices: Sequence[_Choice]) -> bool:
    return _narrative_drill_plan_overlap_ok(tuple(choice.plan for choice in choices))


def _can_reserve(reservation: _Reservation, plan: UnitPlan) -> bool:
    claims = _non_evidence_claims(plan)
    if len(claims) != len(set(claims)):
        return False
    if not reservation.claims.isdisjoint(claims):
        return False
    operation = _operation_for(plan)
    for source_id in _source_evidence_ids(plan):
        prior_uses = reservation.source_usage.get(source_id, ())
        if len(prior_uses) >= 2:
            return False
        if any(use.operation == operation for use in prior_uses):
            return False
        if any(
            use.phase == plan.phase
            and not (
                (use.operation.startswith("degree-") or use.operation == "anchor-comprehension")
                and (operation.startswith("degree-") or operation == "anchor-comprehension")
            )
            for use in prior_uses
        ):
            return False
    return True


def _reserve(reservation: _Reservation, plan: UnitPlan) -> _Reservation:
    claims = reservation.claims | frozenset(_non_evidence_claims(plan))
    source_usage = {source_id: tuple(uses) for source_id, uses in reservation.source_usage.items()}
    use = _SourceUse(phase=plan.phase, operation=_operation_for(plan))
    for source_id in _source_evidence_ids(plan):
        source_usage[source_id] = (*source_usage.get(source_id, ()), use)
    return _Reservation(claims=frozenset(claims), source_usage=source_usage)


def replacement_plan_can_coexist(
    allocation: LessonAllocation,
    *,
    target_slot_id: str,
    candidate: UnitPlan,
) -> bool:
    """Whether one fresh same-type plan can replace a slot without claim reuse.

    Regeneration removes the target plan before considering its replacement,
    but every retained slot keeps the exact allocator reservations it had at
    lesson creation.  The candidate must also use different non-evidence
    claims from the old target; otherwise shuffled choices or relabelled stems
    could masquerade as a new activity.
    """
    targets = tuple(slot for slot in allocation.slots if slot.slot_id == target_slot_id)
    if len(targets) != 1:
        return False
    target = targets[0]
    if (
        not candidate.floor_met
        or candidate.slot_id != target.slot_id
        or candidate.phase != target.phase
        or candidate.activity_type != target.scheduled_type
    ):
        return False
    old_claims = frozenset(_non_evidence_claims(target.plan))
    candidate_claims = _non_evidence_claims(candidate)
    if (
        len(candidate_claims) != len(set(candidate_claims))
        or not old_claims.isdisjoint(candidate_claims)
        or not {unit.unit_id for unit in target.plan.units}.isdisjoint(
            unit.unit_id for unit in candidate.units
        )
    ):
        return False
    retained_plans = tuple(slot.plan for slot in allocation.slots if slot.slot_id != target_slot_id)
    if not _narrative_drill_plan_overlap_ok((*retained_plans, candidate)):
        return False
    reservation = _Reservation.empty()
    for slot in allocation.slots:
        if slot.slot_id == target_slot_id:
            continue
        if not _can_reserve(reservation, slot.plan):
            return False
        reservation = _reserve(reservation, slot.plan)
    return _can_reserve(reservation, candidate)


def _floor_subplans(plan: UnitPlan) -> Iterator[UnitPlan]:
    """Choose stable substrate sized for the scheduled learner workload."""
    if not plan.floor_met:
        return
    minimum_units = floor_for(plan.activity_type).minimum_units
    operation = _operation_for(plan)
    planned_units = (
        None
        if operation.startswith("degree-")
        else planned_interactions(plan.slot_id, plan.activity_type)
    )
    selected_units = (
        len(plan.units)
        if plan.activity_type == "cloze" and _operation_for(plan) == "long-form-reconstruction"
        else planned_units
        if planned_units is not None and len(plan.units) >= planned_units
        else len(plan.units)
        if planned_units is not None
        else 8
        if planned_units is None and len(plan.units) >= 8
        else minimum_units
    )
    for units in _unit_subsets(plan, selected_units):
        subplan = _subplan(plan, units)
        if subplan.floor_met:
            yield subplan


def _qualified_45_workload_ok(choices: Sequence[_Choice]) -> bool:
    """Require plausible phase pacing only for the exact qualified profile."""
    expected = {
        "P1-A1": frozenset({"match-up", "true-false"}),
        "P1-A2": frozenset({"quiz"}),
        "P2-A1": frozenset({"fill-in"}),
        "P2-A2": frozenset({"error-correction"}),
        "P2-A3": frozenset({"mark-the-words"}),
        "P3-A1": frozenset({"cloze"}),
    }
    actual = {choice.slot_plans.slot.slot_id: choice.plan.activity_type for choice in choices}
    if set(actual) != set(expected) or any(
        actual[slot_id] not in allowed for slot_id, allowed in expected.items()
    ):
        return True
    minutes = {phase: 0.0 for phase in PHASE_PACE}
    for choice in choices:
        minutes[choice.plan.phase] = minutes.get(choice.plan.phase, 0.0) + interaction_minutes(
            choice.plan.activity_type,
            len(choice.plan.units),
        )
    return all(
        pace.minimum_minutes <= minutes[phase] <= pace.maximum_minutes
        for phase, pace in PHASE_PACE.items()
    )


def _unit_subsets(plan: UnitPlan, selected_units: int) -> Iterator[tuple[CertifiedUnit, ...]]:
    if plan.activity_type != "text-questions":
        yield from combinations(plan.units, selected_units)
        return
    minima = floor_for("text-questions").category_minima
    assert minima is not None
    preferred = {
        "comprehension": 3,
        "explanation_inference": 3,
        "anchored_application": 2,
    }
    indexed_categories = {category: [] for category in preferred}
    for index, unit in enumerate(plan.units):
        category = unit.distinctness.get("question_category")
        if not isinstance(category, str) or category not in indexed_categories:
            return
        indexed_categories[category].append(index)

    if selected_units == sum(preferred.values()) and all(
        len(indexed_categories[category]) >= count for category, count in preferred.items()
    ):
        targets = preferred
    else:
        targets = None
    for indexes in combinations(range(len(plan.units)), selected_units):
        counts = Counter(
            str(plan.units[index].distinctness["question_category"]) for index in indexes
        )
        if targets is not None:
            if any(counts[category] != count for category, count in targets.items()):
                continue
        elif counts["comprehension"] < minima.comprehension:
            continue
        yield tuple(plan.units[index] for index in indexes)


def _subplan(plan: UnitPlan, units: Sequence[CertifiedUnit]) -> UnitPlan:
    selected_units = tuple(units)
    target_positions = {
        (
            str(unit.distinctness["target"]["sentence_id"]),
            str(unit.distinctness["target"]["token_id"]),
        )
        for unit in selected_units
        if plan.activity_type == "mark-the-words"
    }
    targets = tuple(
        target
        for target in plan.certified_target_tokens
        if (target.sentence_id, target.token_id) in target_positions
    )
    return certify_unit_plan(
        slot_id=plan.slot_id,
        phase=plan.phase,
        activity_type=plan.activity_type,
        units=selected_units,
        registered_constraints=plan.registered_constraints,
        certified_target_tokens=targets,
    )


def _candidate_choices(slot_plans: SlotPlans) -> tuple[_Choice, ...]:
    primary_ready = slot_plans.primary.floor_met
    choices: list[_Choice] = []
    if primary_ready:
        choices.append(_Choice(slot_plans, slot_plans.primary, None))
    replacement_reason: Literal["preflight_unavailable", "capacity"] = (
        "capacity" if primary_ready else "preflight_unavailable"
    )
    for replacement in slot_plans.replacements:
        if replacement.floor_met:
            choices.append(_Choice(slot_plans, replacement, replacement_reason))
    return tuple(choices)


def _conditional_replacements(
    choices: Sequence[_Choice],
) -> tuple[tuple[ConditionalReplacement, ...], ...]:
    """Certify each configured alternate plan against the chosen lesson remainder."""
    result: list[tuple[ConditionalReplacement, ...]] = []
    for index, choice in enumerate(choices):
        remainder = _Reservation.empty()
        for other_index, other in enumerate(choices):
            if other_index != index:
                remainder = _reserve(remainder, other.plan)
        replacements: list[ConditionalReplacement] = []
        for replacement in choice.slot_plans.replacements:
            if replacement.activity_type == choice.plan.activity_type:
                continue
            for subplan in _floor_subplans(replacement):
                replacement_choices = tuple(
                    _Choice(
                        current.slot_plans,
                        subplan if current_index == index else current.plan,
                        current.substitution_reason,
                    )
                    for current_index, current in enumerate(choices)
                )
                if _can_reserve(remainder, subplan) and _narrative_drill_overlap_ok(
                    replacement_choices
                ):
                    replacements.append(
                        ConditionalReplacement(activity_type=subplan.activity_type, plan=subplan)
                    )
                    break
        result.append(tuple(replacements))
    return tuple(result)


def _slot_sort_key(slot_plans: SlotPlans) -> tuple[int, str]:
    return (slot_plans.slot.phase, slot_plans.slot.slot_id)


def allocate_exact_cover(
    slot_plans: Sequence[SlotPlans], *, paragraph_ids: Sequence[str] = ("direct-plans",)
) -> LessonAllocation | None:
    """Allocate every scheduled slot or return ``None`` without partial output.

    Candidate types retain caller-declared replacement order and every plan
    retains its builder-certified unit order. The recursive search therefore
    returns one stable exact-cover allocation.
    """
    ordered = tuple(sorted(slot_plans, key=_slot_sort_key))
    if not ordered or len({item.slot.slot_id for item in ordered}) != len(ordered):
        raise ValueError("Exact-cover allocation needs non-empty unique lesson slots.")
    paragraph_ids_tuple = tuple(paragraph_ids)
    if not paragraph_ids_tuple:
        raise ValueError("Exact-cover allocation needs the attempted anchor window IDs.")

    def search(
        index: int, reservation: _Reservation, choices: tuple[_Choice, ...]
    ) -> tuple[_Choice, ...] | None:
        if index == len(ordered):
            return (
                choices
                if _narrative_drill_overlap_ok(choices) and _qualified_45_workload_ok(choices)
                else None
            )
        for candidate in _candidate_choices(ordered[index]):
            for subplan in _floor_subplans(candidate.plan):
                if _can_reserve(reservation, subplan):
                    found = search(
                        index + 1,
                        _reserve(reservation, subplan),
                        (
                            *choices,
                            _Choice(
                                candidate.slot_plans,
                                subplan,
                                candidate.substitution_reason,
                            ),
                        ),
                    )
                    if found is not None:
                        return found
        return None

    chosen = search(0, _Reservation.empty(), ())
    if chosen is None:
        return None
    replacements = _conditional_replacements(chosen)
    return LessonAllocation(
        paragraph_ids=paragraph_ids_tuple,
        slots=tuple(
            AllocatedSlot(
                slot_id=choice.slot_plans.slot.slot_id,
                phase=choice.slot_plans.slot.phase,
                requested_type=choice.slot_plans.slot.requested_type,
                scheduled_type=choice.plan.activity_type,
                plan=choice.plan,
                substitution_reason=choice.substitution_reason,
                conditional_replacements=replacements[index],
            )
            for index, choice in enumerate(chosen)
        ),
    )


def _merged_inventory(paragraphs: Sequence[AnchorParagraph]) -> CertificationInventory:
    """Canonicalize one contiguous document window into the builders' inventory."""
    source_ids = {paragraph.inventory.source_id for paragraph in paragraphs}
    if len(source_ids) != 1:
        raise ValueError("Anchor-window paragraphs must share one source inventory ID.")
    source_id = next(iter(source_ids))
    return CertificationInventory(
        source_id=source_id,
        sentences=tuple(
            sentence for paragraph in paragraphs for sentence in paragraph.inventory.sentences
        ),
        candidates=tuple(
            candidate for paragraph in paragraphs for candidate in paragraph.inventory.candidates
        ),
        true_false_facts=tuple(
            fact for paragraph in paragraphs for fact in paragraph.inventory.true_false_facts
        ),
        atlas_pairs=tuple(
            pair for paragraph in paragraphs for pair in paragraph.inventory.atlas_pairs
        ),
        mark_requests=tuple(
            request for paragraph in paragraphs for request in paragraph.inventory.mark_requests
        ),
        writing_tasks=tuple(
            task for paragraph in paragraphs for task in paragraph.inventory.writing_tasks
        ),
    )


def _unavailable_plan(slot: LessonSlot, activity_type: str) -> UnitPlan:
    return certify_unit_plan(
        slot_id=slot.slot_id,
        phase=slot.phase,
        activity_type=activity_type,
        units=(),
    )


def _build_plan(
    builder: Callable[..., UnitPlan] | None,
    inventory: CertificationInventory,
    slot: LessonSlot,
    activity_type: str,
) -> UnitPlan:
    if builder is None:
        return _unavailable_plan(slot, activity_type)
    try:
        plan = builder(inventory, slot_id=slot.slot_id, phase=slot.phase)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return _unavailable_plan(slot, activity_type)
    if (
        not isinstance(plan, UnitPlan)
        or plan.slot_id != slot.slot_id
        or plan.phase != slot.phase
        or plan.activity_type != activity_type
    ):
        return _unavailable_plan(slot, activity_type)
    return plan


def _slot_plans_for_window(
    inventory: CertificationInventory,
    slots: Sequence[LessonSlot],
    builders: Mapping[str, Callable[..., UnitPlan]],
    duration_minutes: int,
) -> tuple[SlotPlans, ...]:
    result: list[SlotPlans] = []
    for slot in slots:
        primary = _build_plan(
            builders.get(slot.requested_type), inventory, slot, slot.requested_type
        )
        if not type_allowed_in_phase(duration_minutes, slot.phase, slot.requested_type):
            primary = _unavailable_plan(slot, slot.requested_type)
        replacements = []
        for activity_type in slot.replacement_types:
            replacement = _build_plan(builders.get(activity_type), inventory, slot, activity_type)
            if not type_allowed_in_phase(duration_minutes, slot.phase, activity_type):
                replacement = _unavailable_plan(slot, activity_type)
            replacements.append(replacement)
        result.append(SlotPlans(slot=slot, primary=primary, replacements=tuple(replacements)))
    return tuple(result)


def preflight_lesson(
    anchor_window: AnchorWindow,
    *,
    duration_minutes: int,
    slots: Sequence[LessonSlot],
    builders: Mapping[str, Callable[..., UnitPlan]] = BUILDERS,
) -> LessonPreflightResult:
    """Certify full lesson capacity before any model serialization is permitted."""
    slots = tuple(slots)
    if not slots or len({slot.slot_id for slot in slots}) != len(slots):
        raise ValueError("Preflight needs at least one lesson slot with a unique ID.")
    phase_shape = phase_shape_for(duration_minutes)
    if Counter(slot.phase for slot in slots) != Counter(phase_shape.phase_slots):
        raise ValueError("Preflight must receive the complete configured lesson phase shape.")
    attempts: list[CapacityAttempt] = []
    for paragraphs in anchor_window.widened_paragraphs():
        paragraph_ids = tuple(paragraph.paragraph_id for paragraph in paragraphs)
        allocation = allocate_exact_cover(
            _slot_plans_for_window(
                _merged_inventory(paragraphs), slots, builders, duration_minutes
            ),
            paragraph_ids=paragraph_ids,
        )
        attempts.append(
            CapacityAttempt(paragraph_ids=paragraph_ids, allocation_found=allocation is not None)
        )
        if allocation is not None:
            return LessonPreflightResult(
                allocation=allocation,
                event=None,
                attempts=tuple(attempts),
            )
    last_slot_plans = _slot_plans_for_window(
        _merged_inventory(anchor_window.widened_paragraphs()[-1]), slots, builders, duration_minutes
    )
    has_mismatch = any(
        plan.activity_type not in CLOSED_CLASS_ALLOWED_SHAPES
        and any(
            is_closed_class_target(
                {"expected_key": u.expected_key_or_rule.value, "allowed_forms": u.allowed_forms}
            )
            for u in plan.units
        )
        for sp in last_slot_plans
        for plan in (sp.primary, *sp.replacements)
    )
    event_code: Literal["insufficient_anchor_capacity", "closed_class_shape_mismatch"] = (
        "closed_class_shape_mismatch" if has_mismatch else "insufficient_anchor_capacity"
    )
    event = InsufficientAnchorCapacityEvent(
        paragraph_ids=attempts[-1].paragraph_ids, code=event_code
    )
    return LessonPreflightResult(
        allocation=None,
        event=event,
        attempts=tuple(attempts),
    )
