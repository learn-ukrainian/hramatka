"""Deterministic contracts for the pre-cutover gemma-phase-pack.v3.2."""

from __future__ import annotations

from copy import deepcopy

import pytest

from hramatka.engine import paths
from hramatka.engine.lesson_capacity_v3 import AllocatedSlot, LessonAllocation
from hramatka.engine.linguistics import verify_word
from hramatka.engine.prompt_pack_v3 import (
    PROMPT_PACK_VERSION,
    TEMPLATE_VERSION,
    TYPE_KIT_IDENTITY,
    PromptPackV3Error,
    build_phase_context,
    full_density_exemplars,
    render_phase_prompt,
    six_item_negative_exemplar,
    validate_response,
)
from hramatka.engine.tests.fixtures.density_v3_regression_fixture import complete_inventory
from hramatka.engine.unit_builders_v3 import BUILDERS


def _context(*activity_types: str) -> dict:
    requested_types = activity_types or ("quiz",)
    slots = []
    for index, activity_type in enumerate(requested_types, start=1):
        plan = BUILDERS[activity_type](
            complete_inventory(), slot_id=f"P1-A{index}", phase=1
        )
        assert plan.floor_met
        slots.append(
            AllocatedSlot(
                slot_id=f"P1-A{index}",
                phase=1,
                requested_type=activity_type,
                scheduled_type=activity_type,
                plan=plan,
            )
        )
    allocation = LessonAllocation(
        paragraph_ids=("fixture-paragraph",),
        slots=tuple(slots),
    )
    return build_phase_context(allocation, phase=1)


def _payload(context: dict, *, activity: dict | None = None) -> dict:
    return {
        "slots": [
            {
                "slot_id": kit["slot_id"],
                "type": kit["type"],
                "activity": activity
                if index == 0 and activity is not None
                else {"type": kit["type"], "instruction": "…"},
                "serialized_units": deepcopy(kit["certified_units"]),
            }
            for index, kit in enumerate(context["type_kits"])
        ],
    }


def _passing_gate(_activity: dict, _kit: dict) -> None:
    return None


def _passing_raw_contract(_activity: dict) -> None:
    return None


def _validate(payload: dict, context: dict, **overrides: object) -> list[dict]:
    return validate_response(
        payload,
        context,
        deterministic_gates=overrides.get("deterministic_gates", (_passing_gate,)),
        raw_contract_validator=overrides.get("raw_contract_validator", _passing_raw_contract),
    )


def test_v32_context_uses_the_new_template_and_type_kit_identity() -> None:
    context = _context()

    assert context["pack_version"] == PROMPT_PACK_VERSION == "PromptPackInput.v3"
    assert context["template_version"] == TEMPLATE_VERSION == "gemma-phase-pack.v3.2"
    assert context["type_kit_identity"] == TYPE_KIT_IDENTITY
    kit = context["type_kits"][0]
    assert kit["identity"] == TYPE_KIT_IDENTITY
    assert kit["scheduled_unit_count"] == 8
    assert len(kit["scheduled_unit_ids"]) == 8
    assert kit["scheduled_unit_ids"] == [unit["unit_id"] for unit in kit["certified_units"]]


def test_full_density_exemplars_are_requested_type_only_and_negative_is_six_items() -> None:
    context = _context()

    exemplars = full_density_exemplars(context["type_kits"])
    assert [row["type"] for row in exemplars] == ["quiz"]
    assert len(exemplars[0]["serialized_units"]) == 8
    negative = six_item_negative_exemplar()
    assert len(negative["serialized_units"]) == 6
    prompt = render_phase_prompt(context)
    assert "…, ніж …" in prompt
    assert "зі вікон" in prompt
    assert "(True)" in prompt and "(False)" in prompt


def test_benchmark_surfaces_are_in_the_offline_vesum_regression_bundle() -> None:
    database = paths.vesum_db()

    assert any(row["pos"] == "conj" for row in verify_word("ніж", db_path=database))
    assert any(row["pos"] == "prep" for row in verify_word("зі", db_path=database))
    assert any(
        row["lemma"] == "вікно" and row["tags"] == "noun:inanim:p:v_rod"
        for row in verify_word("вікон", db_path=database)
    )


def test_full_density_exemplar_rejects_any_count_below_the_locked_floor() -> None:
    context = _context()
    underfilled_kit = deepcopy(context["type_kits"][0])
    underfilled_kit["scheduled_unit_count"] = 6

    with pytest.raises(PromptPackV3Error, match="locked v3 type floor"):
        full_density_exemplars([underfilled_kit])


@pytest.mark.parametrize("mutation", ("extra", "missing", "duplicate", "altered"))
def test_immutable_plan_rejects_extra_missing_duplicated_or_altered_substrate(
    mutation: str,
) -> None:
    context = _context()
    payload = _payload(context)
    units = payload["slots"][0]["serialized_units"]
    if mutation == "extra":
        units.append(deepcopy(units[-1]))
    elif mutation == "missing":
        units.pop()
    elif mutation == "duplicate":
        units[-1] = deepcopy(units[0])
    else:
        units[0]["expected_key_or_rule"]["value"] = "altered"

    with pytest.raises(PromptPackV3Error, match="serialization failure"):
        _validate(payload, context)


def test_context_mutation_after_allocation_is_a_serialization_failure() -> None:
    context = _context()
    context["type_kits"][0]["certified_units"][0]["expected_key_or_rule"]["value"] = "altered"

    with pytest.raises(PromptPackV3Error, match="immutable context changed"):
        _validate(_payload(context), context)


def test_exact_count_runs_after_deterministic_gates_and_before_raw_contract_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    payload = _payload(context)
    events: list[str] = []

    def deterministic_gate(_activity: dict, _kit: dict) -> None:
        events.append("deterministic")

    def raw_contract(_activity: dict) -> None:
        events.append("raw")

    from hramatka.engine import prompt_pack_v3

    original = prompt_pack_v3._validate_exact_serialization

    def counted(record: dict, kit: dict) -> None:
        events.append("count")
        original(record, kit)

    monkeypatch.setattr(prompt_pack_v3, "_validate_exact_serialization", counted)
    assert validate_response(
        payload,
        context,
        deterministic_gates=(deterministic_gate,),
        raw_contract_validator=raw_contract,
    ) == [{"type": "quiz", "instruction": "…"}]
    assert events == ["deterministic", "count", "raw"]


def test_failed_exact_count_in_any_slot_never_reaches_raw_contract_validation() -> None:
    context = _context("quiz", "cloze")
    payload = _payload(context)
    payload["slots"][1]["serialized_units"].pop()
    calls: list[str] = []

    with pytest.raises(PromptPackV3Error, match="exact scheduled unit count"):
        _validate(
            payload,
            context,
            raw_contract_validator=lambda _activity: calls.append("raw"),
        )
    assert calls == []


def test_v32_validation_requires_bound_always_on_and_raw_contract_gates() -> None:
    context = _context()
    payload = _payload(context)

    with pytest.raises(PromptPackV3Error, match="always-on deterministic gate runner"):
        validate_response(
            payload,
            context,
            deterministic_gates=(),
            raw_contract_validator=_passing_raw_contract,
        )
    with pytest.raises(PromptPackV3Error, match="raw-contract validator"):
        validate_response(
            payload,
            context,
            deterministic_gates=(_passing_gate,),
            raw_contract_validator=None,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("learner_text", ("(True)", "(False)"))
def test_true_false_parentheticals_are_banned_from_all_learner_facing_fields(
    learner_text: str,
) -> None:
    context = _context()
    payload = _payload(
        context,
        activity={
            "type": "quiz",
            "instruction": "…",
            "items": [{"question": learner_text}],
        },
    )

    with pytest.raises(PromptPackV3Error, match=r"\(True\) or \(False\)"):
        _validate(payload, context)
