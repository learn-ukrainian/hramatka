"""Acceptance coverage for density-v3 slice 5's isolated evaluator boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy

import pytest

from hramatka.engine.density_evaluator_v3 import (
    _replacement_slot,
    evaluate_phase_response,
    evaluate_phase_with_repair,
)
from hramatka.engine.lesson_capacity_v3 import (
    AllocatedSlot,
    ConditionalReplacement,
    LessonAllocation,
)
from hramatka.engine.prompt_pack_v3 import build_phase_context, render_phase_prompt
from hramatka.engine.tests.fixtures.density_v3_regression_fixture import complete_inventory
from hramatka.engine.unit_builders_v3 import BUILDERS


def _allocation(*activity_types: str, replacements: tuple[str, ...] = ()) -> LessonAllocation:
    slots = []
    for index, activity_type in enumerate(activity_types, start=1):
        slot_id = f"P1-A{index}"
        plan = BUILDERS[activity_type](complete_inventory(), slot_id=slot_id, phase=1)
        assert plan.floor_met
        conditional = tuple(
            ConditionalReplacement(
                replacement_type,
                BUILDERS[replacement_type](complete_inventory(), slot_id=slot_id, phase=1),
            )
            for replacement_type in replacements
        )
        slots.append(
            AllocatedSlot(
                slot_id=slot_id,
                phase=1,
                requested_type=activity_type,
                scheduled_type=activity_type,
                plan=plan,
                conditional_replacements=conditional,
            )
        )
    return LessonAllocation(paragraph_ids=("fixture-paragraph",), slots=tuple(slots))


def _payload(allocation: LessonAllocation, *, unit_counts: dict[str, int] | None = None) -> dict:
    context = build_phase_context(allocation, phase=1)
    counts = unit_counts or {}
    return {
        "slots": [
            {
                "slot_id": kit["slot_id"],
                "type": kit["type"],
                "activity": {"type": kit["type"], "instruction": "fixture"},
                "serialized_units": deepcopy(kit["certified_units"])[: counts.get(kit["slot_id"])],
            }
            for kit in context["type_kits"]
        ]
    }


def _record_from_context(context: object, *, slot_id: str | None = None) -> dict:
    assert isinstance(context, dict) or hasattr(context, "get")
    kit = next(
        item for item in context["type_kits"] if slot_id is None or item["slot_id"] == slot_id
    )

    def thaw(value: object) -> object:
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            return [thaw(item) for item in value]
        return value

    return {
        "slot_id": kit["slot_id"],
        "type": kit["type"],
        "activity": {"type": kit["type"], "instruction": "fixture"},
        "serialized_units": thaw(kit["certified_units"]),
    }


def _passing_gate(_activity: dict, _kit: dict) -> None:
    return None


def _passing_raw_contract(_activity: dict) -> None:
    return None


def test_ready_and_tray_are_graded_independently_and_emit_only_content_free_receipts() -> None:
    allocation = _allocation("quiz", "cloze")
    evaluated = evaluate_phase_response(
        allocation,
        phase=1,
        payload=_payload(allocation),
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        tray_slot_ids=("P1-A2",),
    )

    assert [(block.slot_id, block.disposition) for block in evaluated.blocks] == [
        ("P1-A1", "ready"),
        ("P1-A2", "tray"),
    ]
    assert [receipt.to_dict() for receipt in evaluated.receipts] == [
        {"phase": 1, "type": "quiz", "disposition": "ready", "units": 8, "floor_met": True},
        {"phase": 1, "type": "cloze", "disposition": "tray", "units": 8, "floor_met": True},
    ]


def test_six_unit_output_is_hidden_density_shortfall_with_no_tray_credit() -> None:
    allocation = _allocation("quiz", "cloze")
    evaluated = evaluate_phase_response(
        allocation,
        phase=1,
        payload=_payload(allocation, unit_counts={"P1-A2": 6}),
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        tray_slot_ids=("P1-A2",),
    )

    ready, shortfall = evaluated.blocks
    assert ready.disposition == "ready"
    assert shortfall.disposition == "density_shortfall"
    assert shortfall.activity is None
    assert shortfall.receipt is not None
    assert shortfall.receipt.to_dict() == {
        "phase": 1,
        "type": "cloze",
        "disposition": "density_shortfall",
        "units": 6,
        "floor_met": False,
    }
    assert [block.slot_id for block in evaluated.accepted] == ["P1-A1"]
    assert all(error.slot_id == "P1-A2" for error in shortfall.errors)


def test_all_deterministic_gates_run_before_any_independent_raw_contract() -> None:
    allocation = _allocation("quiz", "cloze")
    events: list[str] = []

    def gate(_activity: dict, kit: dict) -> None:
        events.append(f"gate:{kit['slot_id']}")

    def raw(activity: dict) -> None:
        events.append(f"raw:{activity['type']}")

    evaluated = evaluate_phase_response(
        allocation,
        phase=1,
        payload=_payload(allocation, unit_counts={"P1-A2": 6}),
        deterministic_gates=(gate,),
        raw_contract_validator=raw,
    )

    assert [block.disposition for block in evaluated.blocks] == ["ready", "density_shortfall"]
    assert events == ["gate:P1-A1", "gate:P1-A2", "raw:quiz"]


def test_gate_failure_is_slot_specific_and_does_not_hide_other_block_grading() -> None:
    allocation = _allocation("quiz", "cloze")

    def gate(_activity: dict, kit: dict) -> None:
        if kit["slot_id"] == "P1-A1":
            raise ValueError("blocked")

    evaluated = evaluate_phase_response(
        allocation,
        phase=1,
        payload=_payload(allocation),
        deterministic_gates=(gate,),
        raw_contract_validator=_passing_raw_contract,
    )

    failed, ready = evaluated.blocks
    assert failed.disposition == "failed"
    assert ready.disposition == "ready"
    assert failed.errors == (failed.errors[0],)
    assert failed.errors[0].to_dict() == {
        "slot_id": "P1-A1",
        "cause": "deterministic_gate: blocked",
    }


def test_non_serialization_failures_do_not_consume_repair_rounds() -> None:
    allocation = _allocation("quiz")
    calls: list[int] = []

    def gate(_activity: dict, _kit: dict) -> None:
        raise ValueError("blocked")

    def repair(request: object) -> dict:
        calls.append(request.round)
        return _record_from_context(request.prompt_context)

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=_payload(allocation),
        deterministic_gates=(gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
    )

    assert calls == []
    assert evaluated.disposition == "recoverable_draft"
    assert evaluated.blocks[0].disposition == "dropped"
    assert any(error.cause.startswith("not_repairable:") for error in evaluated.blocks[0].errors)


def test_ready_tray_does_not_block_repair_for_another_slot() -> None:
    allocation = _allocation("quiz", "cloze")
    calls: list[str] = []

    def repair(request: object) -> dict:
        calls.append(request.slot_id)
        return _record_from_context(request.prompt_context, slot_id=request.slot_id)

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=_payload(allocation, unit_counts={"P1-A2": 6}),
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
        tray_slot_ids=("P1-A1",),
    )

    assert calls == ["P1-A2"]
    assert [(block.slot_id, block.disposition) for block in evaluated.blocks] == [
        ("P1-A1", "tray"),
        ("P1-A2", "ready"),
    ]
    assert evaluated.disposition == "teacher_ready"


def test_stray_responses_are_unassigned_without_poisoning_scheduled_slots() -> None:
    allocation = _allocation("quiz")
    trailing_payload = _payload(allocation)
    trailing_payload["slots"].extend(({"type": "quiz"}, {"slot_id": "P1-unexpected"}))

    trailing = evaluate_phase_response(
        allocation,
        phase=1,
        payload=trailing_payload,
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
    )

    assert trailing.blocks[0].disposition == "ready"
    assert [error.to_dict() for error in trailing.unassigned_errors] == [
        {"slot_id": "unassigned", "cause": "response_shape: slot_id is missing or invalid"},
        {"slot_id": "unassigned", "cause": "response_shape: unscheduled slot response"},
    ]

    leading_payload = _payload(allocation)
    leading_payload["slots"][:0] = [{"type": "quiz"}, {"slot_id": "P1-unexpected"}]
    leading = evaluate_phase_response(
        allocation,
        phase=1,
        payload=leading_payload,
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
    )

    assert leading.blocks[0].disposition == "ready"
    assert [error.to_dict() for error in leading.unassigned_errors] == [
        {"slot_id": "unassigned", "cause": "response_shape: slot_id is missing or invalid"},
        {"slot_id": "unassigned", "cause": "response_shape: unscheduled slot response"},
    ]


def test_duplicate_scheduled_records_remain_fail_closed() -> None:
    allocation = _allocation("quiz")
    payload = _payload(allocation)
    payload["slots"].extend((deepcopy(payload["slots"][0]), deepcopy(payload["slots"][0])))

    evaluated = evaluate_phase_response(
        allocation,
        phase=1,
        payload=payload,
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
    )

    assert evaluated.blocks[0].disposition == "failed"
    assert [error.cause for error in evaluated.blocks[0].errors] == [
        "response_shape: duplicate slot response",
        "response_shape: duplicate slot response",
    ]


def test_two_repairs_share_the_original_plan_and_context_then_use_exact_replacement() -> None:
    allocation = _allocation("quiz", replacements=("cloze",))
    calls: list[tuple[int, int, int, str, int]] = []
    replacement_calls: list[tuple[str, int]] = []
    frozen_prompts: list[str] = []

    def repair(request: object) -> dict:
        calls.append(
            (
                request.round,
                id(request.plan),
                id(request.prompt_context),
                request.activity_type,
                len(request.plan.units),
            )
        )
        if request.round == 1:
            with pytest.raises(TypeError, match="immutable"):
                request.prompt_context["phase"] = 2
            frozen_prompts.append(render_phase_prompt(request.prompt_context))
        record = _record_from_context(request.prompt_context)
        record["serialized_units"] = record["serialized_units"][:6]
        return record

    def replacement(request: object) -> dict:
        replacement_calls.append((request.activity_type, len(request.plan.units)))
        return _record_from_context(request.prompt_context)

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=_payload(allocation, unit_counts={"P1-A1": 6}),
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
        replacement_renderer=replacement,
    )

    assert [call[0] for call in calls] == [1, 2]
    assert calls[0][1] == calls[1][1]
    assert calls[0][2] == calls[1][2]
    assert [(call[3], call[4]) for call in calls] == [("quiz", 8), ("quiz", 8)]
    assert len(frozen_prompts) == 1
    assert replacement_calls == [("cloze", 8)]
    assert [(block.activity_type, block.disposition) for block in evaluated.blocks] == [
        ("cloze", "ready")
    ]
    assert evaluated.disposition == "teacher_ready"
    assert all(
        set(receipt.to_dict()) == {"phase", "type", "disposition", "units", "floor_met"}
        for receipt in evaluated.receipts
    )


def test_repair_renderer_failure_keeps_the_second_round_and_replacement_path() -> None:
    allocation = _allocation("quiz", replacements=("cloze",))
    repair_calls: list[int] = []
    replacement_calls: list[str] = []

    def repair(request: object) -> dict:
        repair_calls.append(request.round)
        if request.round == 1:
            raise RuntimeError("renderer unavailable")
        record = _record_from_context(request.prompt_context)
        record["serialized_units"] = record["serialized_units"][:6]
        return record

    def replacement(request: object) -> dict:
        replacement_calls.append(request.activity_type)
        return _record_from_context(request.prompt_context)

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=_payload(allocation, unit_counts={"P1-A1": 6}),
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
        replacement_renderer=replacement,
    )

    assert repair_calls == [1, 2]
    assert replacement_calls == ["cloze"]
    assert evaluated.blocks[0].activity_type == "cloze"
    assert evaluated.blocks[0].disposition == "ready"
    assert any(
        error.cause == "repair_renderer: renderer unavailable"
        for error in evaluated.attempts[1].blocks[0].errors
    )
    assert not any(
        error.cause.startswith("not_repairable:")
        for attempt in evaluated.attempts
        for block in attempt.blocks
        for error in block.errors
    )


def test_replacement_slot_records_serialization_exhaustion_provenance() -> None:
    allocation = _allocation("quiz", replacements=("cloze",))
    slot = allocation.slots[0]

    replacement_slot = _replacement_slot(slot, slot.conditional_replacements[0])

    assert replacement_slot.substitution_reason == "serialization_exhausted"


def test_repair_exhaustion_without_a_certified_replacement_drops_only_its_slot() -> None:
    allocation = _allocation("quiz")
    calls: list[int] = []
    bad_payload = _payload(allocation)
    bad_payload["slots"][0]["serialized_units"][0]["expected_key_or_rule"]["value"] = "changed"

    def repair(request: object) -> dict:
        calls.append(request.round)
        return deepcopy(bad_payload["slots"][0])

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=bad_payload,
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
    )

    assert calls == [1, 2]
    assert evaluated.disposition == "recoverable_draft"
    assert evaluated.blocks[0].disposition == "dropped"
    assert evaluated.blocks[0].receipt is not None
    assert evaluated.blocks[0].receipt.to_dict() == {
        "phase": 1,
        "type": "quiz",
        "disposition": "dropped",
        "units": 8,
        "floor_met": True,
    }
    assert all(error.slot_id == "P1-A1" for error in evaluated.errors)


def test_receipt_rejects_underfloor_ready_or_tray_dispositions() -> None:
    from hramatka.engine.density_receipt_v3 import BlockDensityReceipt

    with pytest.raises(ValueError, match="independently meet"):
        BlockDensityReceipt(1, "quiz", "ready", 6, False)
    with pytest.raises(ValueError, match="independently meet"):
        BlockDensityReceipt(1, "quiz", "tray", 6, False)
