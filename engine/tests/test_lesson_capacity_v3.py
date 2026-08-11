"""RED-first coverage for the isolated v3 lesson-wide capacity layer."""

from __future__ import annotations

import pytest

from hramatka.engine.lesson_capacity_v3 import (
    AnchorParagraph,
    AnchorWindow,
    LessonSlot,
    SlotPlans,
    allocate_exact_cover,
    preflight_lesson,
)
from hramatka.engine.unit_builders_v3 import AnchorSentence, CertificationInventory
from hramatka.engine.unit_plan_v3 import (
    CertifiedUnit,
    Citation,
    ExpectedKeyRule,
    ResourceClaim,
    UnitAnchor,
    UnitPlan,
    certify_unit_plan,
)


def _plan(
    *,
    slot_id: str,
    phase: int,
    activity_type: str,
    claim_prefix: str,
    evidence_id: str,
    unit_count: int = 8,
    question_categories: tuple[str, ...] | None = None,
    shared_claim: str | None = None,
) -> UnitPlan:
    """Build source-free certified substrate for allocator-only tests."""
    units = tuple(
        CertifiedUnit(
            unit_id=f"{slot_id}:{activity_type}:{index}",
            resource_claims=(
                ResourceClaim("stem", shared_claim or f"{claim_prefix}:{index}"),
            ),
            anchor=UnitAnchor("evidence", evidence_id),
            allowed_forms=(f"form-{index}",),
            expected_key_or_rule=ExpectedKeyRule("key", f"key-{index}"),
            citation_plan=(Citation("capacity-fixture", f"unit:{index}"),),
            distinctness={
                "stem": f"{slot_id}:{activity_type}:{index}",
                **(
                    {
                        "question_category": (
                            question_categories[index]
                            if question_categories is not None
                            else (
                                ("comprehension",) * 3
                                + ("explanation_inference",) * 3
                                + ("anchored_application",) * max(2, unit_count - 6)
                            )[index]
                        )
                    }
                    if activity_type == "text-questions"
                    else {}
                ),
            },
        )
        for index in range(unit_count)
    )
    return certify_unit_plan(
        slot_id=slot_id,
        phase=phase,
        activity_type=activity_type,
        units=units,
    )


def _unavailable(*, slot_id: str, phase: int, activity_type: str) -> UnitPlan:
    return certify_unit_plan(
        slot_id=slot_id,
        phase=phase,
        activity_type=activity_type,
        units=(),
    )


def _empty_inventory(source_id: str = "capacity-fixture") -> CertificationInventory:
    return CertificationInventory(source_id=source_id, sentences=())


def _full_45_slots(*, first_replacements: tuple[str, ...] = ()) -> tuple[LessonSlot, ...]:
    return tuple(
        LessonSlot(
            f"P{phase}-A{position}",
            phase=phase,
            requested_type="quiz",
            replacement_types=first_replacements if (phase, position) == (1, 1) else (),
        )
        for phase, count in ((1, 2), (2, 3), (3, 1))
        for position in range(1, count + 1)
    )


def test_exact_cover_reserves_shared_claims_and_substitutes_before_generation() -> None:
    first = LessonSlot("P1-A1", phase=1, requested_type="quiz")
    second = LessonSlot(
        "P2-A1", phase=2, requested_type="fill-in", replacement_types=("true-false",)
    )
    allocation = allocate_exact_cover(
        (
            SlotPlans(
                slot=first,
                primary=_plan(
                    slot_id=first.slot_id,
                    phase=first.phase,
                    activity_type="quiz",
                    claim_prefix="shared",
                    evidence_id="evidence:first",
                ),
            ),
            SlotPlans(
                slot=second,
                primary=_plan(
                    slot_id=second.slot_id,
                    phase=second.phase,
                    activity_type="fill-in",
                    claim_prefix="shared",
                    evidence_id="evidence:second",
                ),
                replacements=(
                    _plan(
                        slot_id=second.slot_id,
                        phase=second.phase,
                        activity_type="true-false",
                        claim_prefix="replacement",
                        evidence_id="evidence:replacement",
                    ),
                ),
            ),
        )
    )

    assert allocation is not None
    chosen = {slot.slot_id: slot for slot in allocation.slots}
    assert chosen["P2-A1"].scheduled_type == "true-false"
    assert chosen["P2-A1"].substitution_reason == "capacity"
    assert chosen["P2-A1"].plan.floor_met


def test_source_evidence_second_use_needs_a_new_phase_and_operation() -> None:
    first = LessonSlot("P1-A1", phase=1, requested_type="quiz")
    same_phase = LessonSlot("P1-A2", phase=1, requested_type="fill-in")
    same_operation = LessonSlot("P2-A1", phase=2, requested_type="quiz")

    def slot_plans(slot: LessonSlot, claim_prefix: str) -> SlotPlans:
        return SlotPlans(
            slot=slot,
            primary=_plan(
                slot_id=slot.slot_id,
                phase=slot.phase,
                activity_type=slot.requested_type,
                claim_prefix=claim_prefix,
                evidence_id="evidence:shared",
            ),
        )

    assert allocate_exact_cover((slot_plans(first, "one"), slot_plans(same_phase, "two"))) is None
    assert (
        allocate_exact_cover((slot_plans(first, "one"), slot_plans(same_operation, "two")))
        is None
    )


def test_source_evidence_is_never_reused_a_third_time() -> None:
    slots = (
        LessonSlot("P1-A1", phase=1, requested_type="quiz"),
        LessonSlot("P2-A1", phase=2, requested_type="fill-in"),
        LessonSlot("P3-A1", phase=3, requested_type="true-false"),
    )
    plans = tuple(
        SlotPlans(
            slot=slot,
            primary=_plan(
                slot_id=slot.slot_id,
                phase=slot.phase,
                activity_type=slot.requested_type,
                claim_prefix=f"claims:{slot.slot_id}",
                evidence_id="evidence:shared",
            ),
        )
        for slot in slots
    )

    assert allocate_exact_cover(plans) is None


def test_exact_cover_is_byte_deterministic_and_counts_only_floor_sized_substrate() -> None:
    first = LessonSlot("P1-A1", phase=1, requested_type="quiz")
    second = LessonSlot("P2-A1", phase=2, requested_type="fill-in")
    plans = (
        SlotPlans(
            slot=first,
            primary=_plan(
                slot_id=first.slot_id,
                phase=first.phase,
                activity_type="quiz",
                claim_prefix="quiz",
                evidence_id="evidence:one",
                unit_count=9,
            ),
        ),
        SlotPlans(
            slot=second,
            primary=_plan(
                slot_id=second.slot_id,
                phase=second.phase,
                activity_type="fill-in",
                claim_prefix="fill",
                evidence_id="evidence:two",
            ),
        ),
    )

    first_run = allocate_exact_cover(plans)
    second_run = allocate_exact_cover(plans)

    assert first_run is not None and second_run is not None
    assert first_run.canonical_bytes() == second_run.canonical_bytes()
    assert len(first_run.slots[0].plan.units) == 8


def test_exact_cover_preserves_the_text_question_3_3_2_composition() -> None:
    slot = LessonSlot("P1-A1", phase=1, requested_type="text-questions")
    categories = (
        ("comprehension",) * 8
        + ("explanation_inference",) * 3
        + ("anchored_application",) * 2
    )
    allocation = allocate_exact_cover(
        (
            SlotPlans(
                slot=slot,
                primary=_plan(
                    slot_id=slot.slot_id,
                    phase=slot.phase,
                    activity_type="text-questions",
                    claim_prefix="questions",
                    evidence_id="evidence:questions",
                    unit_count=len(categories),
                    question_categories=categories,
                ),
            ),
        )
    )

    assert allocation is not None
    selected_categories = [
        str(unit.distinctness["question_category"])
        for unit in allocation.slots[0].plan.units
    ]
    assert selected_categories.count("comprehension") == 3
    assert selected_categories.count("explanation_inference") == 3
    assert selected_categories.count("anchored_application") == 2


def test_exact_cover_rejects_a_duplicate_resource_inside_one_selected_plan() -> None:
    slot = LessonSlot("P1-A1", phase=1, requested_type="quiz")
    allocation = allocate_exact_cover(
        (
            SlotPlans(
                slot=slot,
                primary=_plan(
                    slot_id=slot.slot_id,
                    phase=slot.phase,
                    activity_type="quiz",
                    claim_prefix="duplicate",
                    evidence_id="evidence:duplicate",
                    shared_claim="same-kit-resource",
                ),
            ),
        )
    )

    assert allocation is None


def test_conditional_replacements_are_certified_against_the_remainder_only() -> None:
    first = LessonSlot("P1-A1", phase=1, requested_type="quiz", replacement_types=("true-false",))
    second = LessonSlot("P2-A1", phase=2, requested_type="fill-in")
    allocation = allocate_exact_cover(
        (
            SlotPlans(
                slot=first,
                primary=_plan(
                    slot_id=first.slot_id,
                    phase=first.phase,
                    activity_type="quiz",
                    claim_prefix="primary",
                    evidence_id="evidence:first",
                ),
                replacements=(
                    _plan(
                        slot_id=first.slot_id,
                        phase=first.phase,
                        activity_type="true-false",
                        claim_prefix="remainder-conflict",
                        evidence_id="evidence:replacement",
                    ),
                ),
            ),
            SlotPlans(
                slot=second,
                primary=_plan(
                    slot_id=second.slot_id,
                    phase=second.phase,
                    activity_type="fill-in",
                    claim_prefix="remainder-conflict",
                    evidence_id="evidence:second",
                ),
            ),
        )
    )

    assert allocation is not None
    first_slot = next(slot for slot in allocation.slots if slot.slot_id == first.slot_id)
    assert first_slot.conditional_replacements == ()


def test_conflict_free_conditional_replacement_is_pre_certified() -> None:
    first = LessonSlot("P1-A1", phase=1, requested_type="quiz", replacement_types=("true-false",))
    second = LessonSlot("P2-A1", phase=2, requested_type="fill-in")
    allocation = allocate_exact_cover(
        (
            SlotPlans(
                slot=first,
                primary=_plan(
                    slot_id=first.slot_id,
                    phase=first.phase,
                    activity_type="quiz",
                    claim_prefix="primary",
                    evidence_id="evidence:first",
                ),
                replacements=(
                    _plan(
                        slot_id=first.slot_id,
                        phase=first.phase,
                        activity_type="true-false",
                        claim_prefix="replacement-safe",
                        evidence_id="evidence:replacement",
                    ),
                ),
            ),
            SlotPlans(
                slot=second,
                primary=_plan(
                    slot_id=second.slot_id,
                    phase=second.phase,
                    activity_type="fill-in",
                    claim_prefix="remainder",
                    evidence_id="evidence:second",
                ),
            ),
        )
    )

    assert allocation is not None
    first_slot = next(slot for slot in allocation.slots if slot.slot_id == first.slot_id)
    assert [replacement.activity_type for replacement in first_slot.conditional_replacements] == [
        "true-false"
    ]


def test_sub_floor_primary_is_replaced_at_t0_before_serializer_handoff() -> None:
    slots = _full_45_slots(first_replacements=("true-false",))
    slot = slots[0]
    window = AnchorWindow(
        paragraphs=(AnchorParagraph("p-1", _empty_inventory()),),
        initial_start=0,
        initial_end=0,
    )

    def build_quiz(_inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
        if slot_id == slot.slot_id:
            return _unavailable(slot_id=slot_id, phase=phase, activity_type="quiz")
        return _plan(
            slot_id=slot_id,
            phase=phase,
            activity_type="quiz",
            claim_prefix=f"quiz:{slot_id}",
            evidence_id=f"evidence:{slot_id}",
        )

    def build_true_false(
        _inventory: CertificationInventory, *, slot_id: str, phase: int
    ) -> UnitPlan:
        return _plan(
            slot_id=slot_id,
            phase=phase,
            activity_type="true-false",
            claim_prefix=f"replacement:{slot_id}",
            evidence_id=f"evidence:replacement:{slot_id}",
        )

    result = preflight_lesson(
        window,
        duration_minutes=45,
        slots=slots,
        builders={"quiz": build_quiz, "true-false": build_true_false},
    )

    assert result.allocation is not None
    chosen = result.allocation.slots[0]
    assert chosen.scheduled_type == "true-false"
    assert chosen.substitution_reason == "preflight_unavailable"
    serializer_calls: list[str] = []
    authorization = result.generation_authorization
    assert authorization is not None
    assert serializer_calls == []
    authorization.serialize_with(
        lambda allocation: serializer_calls.append(allocation.slots[0].slot_id)
    )
    assert serializer_calls == ["P1-A1"]


def test_thin_anchor_widens_in_document_order_before_capacity_event() -> None:
    slots = _full_45_slots()
    window = AnchorWindow(
        paragraphs=(
            AnchorParagraph("p-1", _empty_inventory()),
            AnchorParagraph("p-2", _empty_inventory()),
        ),
        initial_start=0,
        initial_end=0,
        lesson_end=1,
    )

    def build_quiz(_inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
        return _unavailable(slot_id=slot_id, phase=phase, activity_type="quiz")

    result = preflight_lesson(
        window,
        duration_minutes=45,
        slots=slots,
        builders={"quiz": build_quiz},
    )

    assert result.allocation is None
    assert [attempt.paragraph_ids for attempt in result.attempts] == [("p-1",), ("p-1", "p-2")]
    assert result.event is not None
    assert result.event.code == "insufficient_anchor_capacity"
    assert result.event.recoverable is True
    assert result.generation_authorized is False


def test_widening_can_certify_the_next_adjacent_paragraph() -> None:
    slots = _full_45_slots()
    source_sentence = AnchorSentence("s-1", "source", ())
    window = AnchorWindow(
        paragraphs=(
            AnchorParagraph("p-1", _empty_inventory()),
            AnchorParagraph(
                "p-2", CertificationInventory("capacity-fixture", (source_sentence,))
            ),
        ),
        initial_start=0,
        initial_end=0,
        lesson_end=1,
    )

    def build_quiz(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
        if not inventory.sentences:
            return _unavailable(slot_id=slot_id, phase=phase, activity_type="quiz")
        return _plan(
            slot_id=slot_id,
            phase=phase,
            activity_type="quiz",
            claim_prefix=f"widened:{slot_id}",
            evidence_id=f"evidence:widened:{slot_id}",
        )

    result = preflight_lesson(
        window,
        duration_minutes=45,
        slots=slots,
        builders={"quiz": build_quiz},
    )

    assert result.allocation is not None
    assert result.allocation.paragraph_ids == ("p-1", "p-2")
    assert [attempt.paragraph_ids for attempt in result.attempts] == [("p-1",), ("p-1", "p-2")]


def test_preflight_rejects_a_partial_lesson_shape_before_any_allocation() -> None:
    window = AnchorWindow(
        paragraphs=(AnchorParagraph("p-1", _empty_inventory()),),
        initial_start=0,
        initial_end=0,
    )

    with pytest.raises(ValueError, match="complete configured lesson phase shape"):
        preflight_lesson(
            window,
            duration_minutes=45,
            slots=(LessonSlot("P1-A1", phase=1, requested_type="quiz"),),
            builders={},
        )
