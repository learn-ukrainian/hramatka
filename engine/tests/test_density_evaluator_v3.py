"""Acceptance coverage for density-v3 slice 5's isolated evaluator boundary."""

from __future__ import annotations

from copy import deepcopy

import pytest

from hramatka.api.baking.engine_adapter_v3 import _activity_gate, _raw_activity_contract
from hramatka.engine.density_evaluator_v3 import (
    CAUSE_VOCABULARY,
    MAX_REPAIR_ROUNDS,
    _replacement_slot,
    evaluate_phase_response,
    evaluate_phase_with_repair,
)
from hramatka.engine.lesson_capacity_v3 import (
    AllocatedSlot,
    ConditionalReplacement,
    LessonAllocation,
)
from hramatka.engine.prompt_pack_v3 import (
    PromptPackV3Error,
    build_phase_context,
    render_phase_prompt,
    validate_gap_construction,
)
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
                "serialized_units": [{"unit_id": unit_id} for unit_id in kit["scheduled_unit_ids"]][
                    : counts.get(kit["slot_id"])
                ],
            }
            for kit in context["type_kits"]
        ]
    }


def _record_from_context(context: object, *, slot_id: str | None = None) -> dict:
    assert isinstance(context, dict) or hasattr(context, "get")
    kit = next(
        item for item in context["type_kits"] if slot_id is None or item["slot_id"] == slot_id
    )

    return {
        "slot_id": kit["slot_id"],
        "type": kit["type"],
        "activity": {"type": kit["type"], "instruction": "fixture"},
        "serialized_units": [{"unit_id": unit_id} for unit_id in kit["scheduled_unit_ids"]],
    }


def _passing_gate(_activity: dict, _kit: dict) -> None:
    return None


def _passing_raw_contract(_activity: dict) -> None:
    return None


def test_cause_vocabulary_covers_every_bake_path_rejection_stage() -> None:
    """Removing any production cause mapping fails this explicit rule-pair pin."""
    assert CAUSE_VOCABULARY == {
        "response_shape": "response_shape: exact_scheduled_slot_response",
        "learner_facing_fields": "learner_facing_fields: ukrainian_boolean_labels",
        "activity_binding": "activity_binding: certified_unit_binding",
        "exemplar_contamination": "exemplar_contamination: no_synthetic_or_raw_certified_content",
        "verbatim_answer_ban": "verbatim_answer_ban: answer_forms_excluded_from_learner_prose",
        "elicitation_shape": "elicitation_shape: composed_learner_context",
        "gap_construction": "gap_construction",
        "distractor_adjacency": "distractor_adjacency: vesum_adjacent_distractors",
        "distractor_repeated_token": "distractor_adjacency: error_correction_no_repeated_tokens",
        "non_revealing_sequence": "non_revealing_sequence: distinct_learner_stems",
        "activity_purpose": "activity_purpose: source_grounded_meaning",
        "short_writing_visible_constraints": (
            "short_writing_visible_constraints: learner_facing_requirements"
        ),
        "serialization_exactness": "serialization_exactness: scheduled_unit_references",
        "raw_contract": "raw_contract: pilot_activity_schema",
        "repair_renderer": "repair_renderer: slot_response_unavailable",
        "replacement_renderer": "replacement_renderer: slot_response_unavailable",
        "unregistered_deterministic_gate": "deterministic_gate: unregistered_rule",
    }


def test_rendered_reference_response_reaches_the_production_evaluator() -> None:
    """The prompt-requested response shape resolves real kit references to a ready block."""
    allocation = _allocation("quiz")
    context = build_phase_context(allocation, phase=1)
    prompt = render_phase_prompt(context)
    kit = context["type_kits"][0]
    forms = [unit["allowed_forms"][0] for unit in kit["certified_units"]]
    units = kit["certified_units"]
    response = {
        "slots": [
            {
                "slot_id": kit["slot_id"],
                "type": "quiz",
                "activity": {
                    "payload": {
                        "type": "quiz",
                        "instruction": "Оберіть правильний варіант.",
                        "items": [
                            {
                                "question": unit["gapped_rendering_surface"],
                                "options": unit["distinctness"]["choice_bank"],
                                "correct": unit["distinctness"]["choice_bank"].index(form),
                            }
                            for form, unit in zip(forms, units, strict=True)
                        ],
                    },
                    "answer_key": {
                        "items": [
                            {
                                "index": index,
                                "correct": units[index]["distinctness"]["choice_bank"].index(
                                    form
                                ),
                            }
                            for index, form in enumerate(forms)
                        ]
                    },
                },
                "serialized_units": [{"unit_id": unit_id} for unit_id in kit["scheduled_unit_ids"]],
            }
        ]
    }

    evaluated = evaluate_phase_response(
        allocation,
        phase=1,
        payload=response,
        deterministic_gates=(_activity_gate,),
        raw_contract_validator=_raw_activity_contract,
    )

    assert "serialized_units is an ordered reference list" in prompt
    assert "exact one-to-one cover" in prompt
    assert 'activity contains exactly {"payload":{...},"answer_key":{...}}' in prompt
    assert "gapped_rendering_surface" in prompt
    assert "eight questions reveal successive words" in prompt
    assert [(block.disposition, block.observed_units) for block in evaluated.blocks] == [
        ("ready", 8)
    ]


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
        {
            "phase": 1,
            "type": "quiz",
            "disposition": "ready",
            "units": 8,
            "floor_met": True,
            "contract_version": "TeacherReadyDensity.v3",
        },
        {
            "phase": 1,
            "type": "cloze",
            "disposition": "tray",
            "units": 8,
            "floor_met": True,
            "contract_version": "TeacherReadyDensity.v3",
        },
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
        "contract_version": "TeacherReadyDensity.v3",
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
        "cause": "deterministic_gate: unregistered_rule",
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


def test_gate_dropped_cloze_names_the_gap_construction_rule_for_repair() -> None:
    """A context-free rule name, rather than learner text, reaches bounded repair."""
    allocation = _allocation("cloze")
    payload = _payload(allocation)
    payload["slots"][0]["activity"] = {
        "payload": {
            "type": "cloze",
            "instruction": "Заповніть пропуски.",
            "text": "{1} {2} {3} {4} {5} кілька {6} {7} {8}.",
            "blanks": [{"id": index} for index in range(1, 9)],
        },
        "answer_key": {"blanks": []},
    }
    repair_causes: list[tuple[str, ...]] = []

    def repair(request: object) -> dict:
        repair_causes.append(tuple(error.cause for error in request.prior_errors))
        return deepcopy(payload["slots"][0])

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=payload,
        deterministic_gates=(validate_gap_construction,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
        max_repair_rounds=1,
    )

    assert repair_causes == [("gap_construction: cloze_local_context",)]
    assert evaluated.blocks[0].disposition == "dropped"
    assert [error.cause for error in evaluated.blocks[0].errors] == [
        "gap_construction: cloze_local_context",
        "repair_exhausted: no certified replacement serialized successfully",
    ]


def test_gap_construction_rejects_fill_in_items_from_one_carrier_sentence() -> None:
    activity = {
        "payload": {
            "type": "fill-in",
            "instruction": "Вставте слово.",
            "items": [
                {
                    "sentence": "Це ___ для перевірки.",
                    "answer": "слово",
                    "options": [],
                }
                for _ in range(8)
            ],
        },
        "answer_key": {"items": []},
    }

    with pytest.raises(ValueError, match="fill_in_distinct_carrier_sentences"):
        validate_gap_construction(activity, {})


def test_gap_construction_rejects_missing_gap_markers() -> None:
    cloze = {
        "payload": {
            "type": "cloze",
            "text": "Цей контекст не має позначеного пропуску.",
            "blanks": [{"id": 1}],
        }
    }
    fill_in = {
        "payload": {
            "type": "fill-in",
            "items": [{"sentence": "У реченні немає пропуску.", "answer": "слово"}],
        }
    }

    with pytest.raises(ValueError, match="cloze_markers_match_blanks"):
        validate_gap_construction(cloze, {})
    with pytest.raises(ValueError, match="fill_in_single_gap_marker"):
        validate_gap_construction(fill_in, {})


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


def test_missing_scheduled_slots_repair_only_the_missing_ids() -> None:
    allocation = _allocation("quiz", "cloze", "fill-in")
    subset = _payload(allocation)
    subset["slots"] = [record for record in subset["slots"] if record["slot_id"] == "P1-A1"]
    repair_calls: list[tuple[int, str]] = []

    def repair(request: object) -> dict:
        repair_calls.append((request.round, request.slot_id))
        return _record_from_context(request.prompt_context, slot_id=request.slot_id)

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=subset,
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
    )

    assert repair_calls == [(1, "P1-A2"), (1, "P1-A3")]
    assert [
        (block.slot_id, block.disposition, block.observed_units) for block in evaluated.blocks
    ] == [
        ("P1-A1", "ready", 8),
        ("P1-A2", "ready", 8),
        ("P1-A3", "ready", 8),
    ]
    assert evaluated.disposition == "teacher_ready"


def test_text_question_item_repairs_cannot_regress_other_questions() -> None:
    allocation = _allocation("text-questions")
    context = build_phase_context(allocation, phase=1)
    record = _record_from_context(context)
    original_items = [f"Добре питання {index}?" for index in range(8)]
    original_items[3] = "Погане питання 3?"
    original_items[6] = "Погане питання 6?"
    record["activity"] = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": original_items,
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }
    repair_targets: list[tuple[int, ...]] = []

    def validate_activity_purpose(activity: dict, _kit: dict) -> None:
        for index, item in enumerate(activity["payload"]["items"]):
            if item.startswith("Погане"):
                raise PromptPackV3Error(
                    f"text question consumes its source answer at items[{index}]"
                )

    def repair(request: object) -> dict:
        repair_targets.append(request.target_item_indexes)
        return {
            "repair_items": [
                {"index": index, "question": f"Виправлене питання {index}?"}
                for index in request.target_item_indexes
            ]
        }

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload={"slots": [record]},
        deterministic_gates=(validate_activity_purpose,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
    )

    assert repair_targets == [(3, 6)]
    assert evaluated.disposition == "teacher_ready"
    assert evaluated.blocks[0].activity["payload"]["items"] == [
        "Добре питання 0?",
        "Добре питання 1?",
        "Добре питання 2?",
        "Виправлене питання 3?",
        "Добре питання 4?",
        "Добре питання 5?",
        "Виправлене питання 6?",
        "Добре питання 7?",
    ]


def test_targeted_text_question_repair_discards_non_target_mutations() -> None:
    allocation = _allocation("text-questions")
    context = build_phase_context(allocation, phase=1)
    record = _record_from_context(context)
    record["activity"] = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Погане питання 0?", *[f"Добре питання {index}?" for index in range(1, 8)]],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }
    repair_targets: list[tuple[int, ...]] = []

    def validate_activity_purpose(activity: dict, _kit: dict) -> None:
        for index, item in enumerate(activity["payload"]["items"]):
            if item.startswith("Погане"):
                raise PromptPackV3Error(
                    f"text question consumes its source answer at items[{index}]"
                )

    def repair(request: object) -> dict:
        repair_targets.append(request.target_item_indexes)
        if request.target_item_indexes:
            candidate = request.prior_record_copy
            assert candidate is not None
            candidate["activity"]["payload"]["items"][0] = "Виправлене питання 0?"
            candidate["activity"]["payload"]["items"][1] = "Погане повернення 1?"
            return candidate
        clean = deepcopy(record)
        clean["activity"]["payload"]["items"][0] = "Виправлене питання 0?"
        return clean

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload={"slots": [record]},
        deterministic_gates=(validate_activity_purpose,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
    )

    assert repair_targets == [(0,), ()]
    assert evaluated.disposition == "teacher_ready"
    assert evaluated.blocks[0].activity["payload"]["items"][1] == "Добре питання 1?"


def test_exhausted_text_question_repairs_get_one_clean_same_plan_regeneration() -> None:
    allocation = _allocation("text-questions")
    context = build_phase_context(allocation, phase=1)
    record = _record_from_context(context)
    record["activity"] = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Погане питання 0?", *[f"Добре питання {index}?" for index in range(1, 8)]],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }
    repair_rounds: list[int] = []
    replacement_plans: list[object] = []

    def validate_activity_purpose(activity: dict, _kit: dict) -> None:
        for index, item in enumerate(activity["payload"]["items"]):
            if item.startswith("Погане"):
                raise PromptPackV3Error(
                    f"text question consumes its source answer at items[{index}]"
                )

    def repair(request: object) -> dict:
        repair_rounds.append(request.round)
        candidate = request.prior_record_copy
        assert candidate is not None
        return candidate

    def replacement(request: object) -> dict:
        replacement_plans.append(request.plan)
        clean = deepcopy(record)
        clean["activity"]["payload"]["items"][0] = "Виправлене питання 0?"
        return clean

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload={"slots": [record]},
        deterministic_gates=(validate_activity_purpose,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
        replacement_renderer=replacement,
    )

    assert repair_rounds == list(range(1, MAX_REPAIR_ROUNDS + 1))
    assert replacement_plans == [allocation.slots[0].plan]
    assert evaluated.disposition == "teacher_ready"
    assert evaluated.blocks[0].activity_type == "text-questions"


def test_missing_scheduled_slot_exhausts_bounded_repairs_then_fails_closed() -> None:
    allocation = _allocation("quiz", "cloze", replacements=("fill-in",))
    subset = _payload(allocation)
    subset["slots"] = [record for record in subset["slots"] if record["slot_id"] == "P1-A1"]
    repair_calls: list[tuple[int, str]] = []
    replacement_calls: list[str] = []

    def repair(request: object) -> dict:
        repair_calls.append((request.round, request.slot_id))
        return {"slots": []}

    def replacement(request: object) -> dict:
        replacement_calls.append(request.slot_id)
        return _record_from_context(request.prompt_context)

    evaluated = evaluate_phase_with_repair(
        allocation,
        phase=1,
        payload=subset,
        deterministic_gates=(_passing_gate,),
        raw_contract_validator=_passing_raw_contract,
        repair_renderer=repair,
        replacement_renderer=replacement,
    )

    assert repair_calls == [
        (repair_round, "P1-A2")
        for repair_round in range(1, MAX_REPAIR_ROUNDS + 1)
    ]
    assert [(block.slot_id, block.disposition) for block in evaluated.blocks] == [
        ("P1-A1", "ready"),
        ("P1-A2", "dropped"),
    ]
    assert replacement_calls == []
    assert evaluated.disposition == "recoverable_draft"


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
        {"slot_id": "unassigned", "cause": "response_shape: exact_scheduled_slot_response"},
        {"slot_id": "unassigned", "cause": "response_shape: exact_scheduled_slot_response"},
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
        {"slot_id": "unassigned", "cause": "response_shape: exact_scheduled_slot_response"},
        {"slot_id": "unassigned", "cause": "response_shape: exact_scheduled_slot_response"},
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
        "response_shape: exact_scheduled_slot_response",
        "response_shape: exact_scheduled_slot_response",
    ]


def test_bounded_repairs_share_the_original_plan_and_context_then_use_exact_replacement() -> None:
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
        if request.round == MAX_REPAIR_ROUNDS:
            return {"slot_id": request.slot_id}
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

    assert [call[0] for call in calls] == list(range(1, MAX_REPAIR_ROUNDS + 1))
    assert calls[0][1] == calls[1][1] == calls[2][1] == calls[3][1]
    assert calls[0][2] == calls[1][2] == calls[2][2] == calls[3][2]
    assert [(call[3], call[4]) for call in calls] == [
        ("quiz", 8)
    ] * MAX_REPAIR_ROUNDS
    assert len(frozen_prompts) == 1
    assert replacement_calls == [("cloze", 8)]
    assert [(block.activity_type, block.disposition) for block in evaluated.blocks] == [
        ("cloze", "ready")
    ]
    assert evaluated.disposition == "teacher_ready"
    assert all(
        set(receipt.to_dict())
        == {"phase", "type", "disposition", "units", "floor_met", "contract_version"}
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

    assert repair_calls == list(range(1, MAX_REPAIR_ROUNDS + 1))
    assert replacement_calls == ["cloze"]
    assert evaluated.blocks[0].activity_type == "cloze"
    assert evaluated.blocks[0].disposition == "ready"
    assert any(
        error.cause == "repair_renderer: slot_response_unavailable"
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
    bad_payload["slots"][0]["serialized_units"][0]["unit_id"] = "changed"

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

    assert calls == list(range(1, MAX_REPAIR_ROUNDS + 1))
    assert evaluated.disposition == "recoverable_draft"
    assert evaluated.blocks[0].disposition == "dropped"
    assert evaluated.blocks[0].receipt is not None
    assert evaluated.blocks[0].receipt.to_dict() == {
        "phase": 1,
        "type": "quiz",
        "disposition": "dropped",
        "units": 8,
        "floor_met": True,
        "contract_version": "TeacherReadyDensity.v3",
    }
    assert all(error.slot_id == "P1-A1" for error in evaluated.errors)


def test_receipt_rejects_underfloor_ready_or_tray_dispositions() -> None:
    from hramatka.engine.density_receipt_v3 import BlockDensityReceipt

    with pytest.raises(ValueError, match="independently meet"):
        BlockDensityReceipt(1, "quiz", "ready", 6, False)
    with pytest.raises(ValueError, match="independently meet"):
        BlockDensityReceipt(1, "quiz", "tray", 6, False)
