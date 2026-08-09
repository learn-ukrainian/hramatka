"""#402 flag-don't-drop: engine-side ``attempted_record`` boundary proofs.

The engine's honesty semantics must be byte-identical to before: ``dropped``
stays ``dropped``, ``accepted`` stays ``False``, receipts and the qualification
bar stay receipt-driven.  ``attempted_record`` only preserves the last
shape-valid attempt so the wire layer can ship it flagged.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from hramatka.engine.density_evaluator_v3 import (
    BlockEvaluation,
    evaluate_phase_response,
    evaluate_phase_with_repair,
)
from hramatka.engine.density_receipt_v3 import BlockDensityReceipt
from hramatka.engine.lesson_capacity_v3 import AllocatedSlot, LessonAllocation
from hramatka.engine.prompt_pack_v3 import build_phase_context
from hramatka.engine.tests.fixtures.density_v3_regression_fixture import complete_inventory
from hramatka.engine.unit_builders_v3 import BUILDERS


def _allocation(*activity_types: str) -> LessonAllocation:
    slots = []
    for index, activity_type in enumerate(activity_types, start=1):
        slot_id = f"P1-A{index}"
        plan = BUILDERS[activity_type](complete_inventory(), slot_id=slot_id, phase=1)
        assert plan.floor_met
        slots.append(
            AllocatedSlot(
                slot_id=slot_id,
                phase=1,
                requested_type=activity_type,
                scheduled_type=activity_type,
                plan=plan,
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
                "serialized_units": [
                    {"unit_id": unit_id} for unit_id in kit["scheduled_unit_ids"]
                ][: counts.get(kit["slot_id"])],
            }
            for kit in context["type_kits"]
        ]
    }


def _passing_gate(_activity: dict, _kit: dict) -> None:
    return None


def _passing_raw_contract(_activity: dict) -> None:
    return None


def test_attempted_record_is_forbidden_on_accepted_dispositions() -> None:
    """The accepted-content invariant is untouched: ready/tray never carry it."""
    receipt = BlockDensityReceipt(1, "quiz", "ready", 8, True)
    with pytest.raises(ValueError, match="non-accepted"):
        BlockEvaluation(
            slot_id="P1-A1",
            phase=1,
            activity_type="quiz",
            disposition="ready",
            observed_units=8,
            receipt=receipt,
            activity={"payload": {}, "answer_key": {}},
            attempted_record={"slot_id": "P1-A1"},
        )


def test_shortfall_captures_this_attempts_record_and_ready_does_not() -> None:
    allocation = _allocation("quiz", "cloze")
    payload = _payload(allocation, unit_counts={"P1-A2": 6})

    evaluated = evaluate_phase_response(
        allocation,
        phase=1,
        payload=payload,
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
    )

    ready, shortfall = evaluated.blocks
    assert ready.disposition == "ready"
    assert ready.attempted_record is None
    assert shortfall.disposition == "density_shortfall"
    assert shortfall.activity is None  # the hidden-shortfall invariant holds
    assert shortfall.attempted_record is not None
    assert shortfall.attempted_record["slot_id"] == "P1-A2"
    assert shortfall.attempted_record["activity"] == {"type": "cloze", "instruction": "fixture"}


def test_dropped_block_stays_dropped_and_unaccepted_with_the_record_attached() -> None:
    """Boundary proofs 1+2: flagging adds data, never a semantic change.

    Mutation anchor: making ``accepted`` True for a dropped-with-record block,
    or renaming its disposition, must fail this test.
    """
    allocation = _allocation("quiz")

    def failing_repair(request: object) -> dict:
        record = {
            "slot_id": request.slot_id,
            "type": "quiz",
            "activity": {"type": "quiz", "instruction": "fixture"},
            "serialized_units": [],
        }
        return record

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=_payload(allocation, unit_counts={"P1-A1": 0}),
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=failing_repair,
    )

    block = evaluated.blocks[0]
    assert block.disposition == "dropped"
    assert block.accepted is False
    assert block.activity is None
    assert block.attempted_record is not None
    assert block.receipt is not None
    assert block.receipt.to_dict() == {
        "phase": 1,
        "type": "quiz",
        "disposition": "dropped",
        "units": 0,
        "floor_met": False,
        "contract_version": "TeacherReadyDensity.v3",
    }
    # The lesson-level bar is unchanged: any non-accepted block keeps the
    # lesson a recoverable draft, record or no record.
    assert evaluated.disposition == "recoverable_draft"
    # The wrapper cause is last; the granular classifying cause sits before it.
    assert block.errors[-1].cause.startswith("repair_exhausted:")
    assert block.errors[-2].cause.startswith("density_shortfall:")


def test_dropped_tolerates_multi_error_terminal_attempt() -> None:
    """OPEN QUESTION (PR #402 stage 2): the design's v4 delta asked for
    ``assert len(previous.errors) == 1`` inside ``_dropped()``.  That invariant
    is contradicted by this pinned live path: three same-slot records produce
    TWO pre-wrapper errors in one ``_evaluate_payload`` pass (see
    ``test_duplicate_scheduled_records_remain_fail_closed``), and a crash here
    would turn today's graceful per-slot drop into a whole-bake failure.  The
    engine therefore stays total; this test pins the counterexample.

    Since #406 every rule-named cause is repair-eligible, so the duplicate
    response consumes its bounded renderer rounds and drops via
    ``repair_exhausted`` instead of ``not_repairable`` — the multi-error
    premise itself is unchanged.
    """
    allocation = _allocation("quiz")
    payload = _payload(allocation)
    payload["slots"].extend((deepcopy(payload["slots"][0]), deepcopy(payload["slots"][0])))

    def no_repair(_request: object) -> dict:
        raise AssertionError("this path exhausts its repair rounds unrepaired")

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=payload,
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=no_repair,
    )

    block = evaluated.blocks[0]
    assert block.disposition == "dropped"
    assert [error.cause for error in block.errors] == [
        "response_shape: exact_scheduled_slot_response",
        "response_shape: exact_scheduled_slot_response",
        "repair_renderer: slot_response_unavailable",
        "repair_renderer: slot_response_unavailable",
        "repair_renderer: slot_response_unavailable",
        "repair_renderer: slot_response_unavailable",
        "repair_exhausted: no certified replacement serialized successfully",
    ]
    # No shape-valid record ever existed for the poisoned slot.
    assert block.attempted_record is None


def test_renderer_failure_rounds_stack_errors_but_keep_the_shape_valid_record() -> None:
    """The second live multi-error path: renderer failures append across rounds
    via ``_with_renderer_errors`` while ``replace()`` carries the last
    shape-valid attempt forward untouched.
    """
    allocation = _allocation("quiz")

    def always_raising_repair(_request: object) -> dict:
        raise RuntimeError("renderer unavailable")

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=_payload(allocation, unit_counts={"P1-A1": 6}),
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=always_raising_repair,
    )

    block = evaluated.blocks[0]
    assert block.disposition == "dropped"
    causes = [error.cause for error in block.errors]
    assert causes[0].startswith("density_shortfall:")
    assert causes.count("repair_renderer: slot_response_unavailable") == 4
    assert causes[-1].startswith("repair_exhausted:")
    assert len(block.errors) == 6  # 1 shortfall + 4 renderer rounds + wrapper
    # The initial attempt's record survives every renderer-failure round.
    assert block.attempted_record is not None
    assert block.attempted_record["slot_id"] == "P1-A1"
