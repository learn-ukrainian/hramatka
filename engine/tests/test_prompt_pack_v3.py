"""Deterministic contracts for the v3.3 binding-contract redesign (#375)."""

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
    validate_distractor_adjacency,
    validate_elicitation_shape,
    validate_exemplar_contamination,
    validate_response,
    validate_verbatim_answer_ban,
)
from hramatka.engine.tests.fixtures.density_v3_regression_fixture import complete_inventory
from hramatka.engine.unit_builders_v3 import BUILDERS


def _context(*activity_types: str) -> dict:
    requested_types = activity_types or ("quiz",)
    slots = []
    for index, activity_type in enumerate(requested_types, start=1):
        plan = BUILDERS[activity_type](complete_inventory(), slot_id=f"P1-A{index}", phase=1)
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
                "serialized_units": [{"unit_id": unit_id} for unit_id in kit["scheduled_unit_ids"]],
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


def test_v33_context_uses_the_new_template_and_type_kit_identity() -> None:
    context = _context()

    assert context["pack_version"] == PROMPT_PACK_VERSION == "PromptPackInput.v3.2"
    assert context["template_version"] == TEMPLATE_VERSION == "gemma-phase-pack.v3.4"
    assert context["type_kit_identity"] == TYPE_KIT_IDENTITY
    kit = context["type_kits"][0]
    assert kit["identity"] == TYPE_KIT_IDENTITY
    assert kit["scheduled_unit_count"] == 8
    assert len(kit["scheduled_unit_ids"]) == 8
    assert kit["scheduled_unit_ids"] == [unit["unit_id"] for unit in kit["certified_units"]]


def test_serializer_temperature_is_bound_into_the_prompt_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_context = _context()
    monkeypatch.setenv("HRAMATKA_GEN_TEMPERATURE", "0.7")
    overridden_context = _context()

    assert default_context["serializer_temperature"] == 0.0
    assert overridden_context["serializer_temperature"] == 0.7
    assert default_context["context_sha256"] != overridden_context["context_sha256"]


def test_full_density_exemplars_are_requested_type_only_and_negative_is_six_items() -> None:
    context = _context()

    exemplars = full_density_exemplars(context["type_kits"])
    assert [row["type"] for row in exemplars] == ["quiz"]
    assert exemplars[0]["slot_id"] == "<synthetic-quiz-slot>"
    assert exemplars[0]["activity"]["payload"]["type"] == "quiz"
    assert len(exemplars[0]["serialized_units"]) == 8
    negative = six_item_negative_exemplar()
    assert len(negative["serialized_units"]) == 6
    prompt = render_phase_prompt(context)
    assert "SYNTHETIC-QUIZ-STEM" in prompt
    assert "v3.4" in prompt


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
        units[0]["unit_id"] = "altered"

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


def test_v33_validation_requires_bound_always_on_and_raw_contract_gates() -> None:
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


def _primary_forms(kit: dict) -> tuple[str, ...]:
    return tuple(
        unit["allowed_forms"][0]
        for unit in kit.get("certified_units", [])
        if isinstance(unit, dict)
        and isinstance(unit.get("allowed_forms"), list)
        and unit["allowed_forms"]
    )


@pytest.mark.parametrize(
    ("activity_type", "contaminated_field", "contaminated_value"),
    [
        ("cloze", "text", "SYNTHETIC-CLOZE-TEXT"),
        ("fill-in", "instruction", "SYNTHETIC-FILLIN-STEM"),
        ("true-false", "instruction", "Синтетична вказівка."),
        ("match-up", "instruction", "SYNTHETIC-MATCH-LEFT"),
        ("text-questions", "instruction", "SYNTHETIC-OPEN-QUESTION"),
        ("short-writing", "prompt", "SYNTHETIC-WRITING-PROMPT"),
    ],
)
def test_exemplar_contamination_gate_rejects_literal_exemplar_fragments(
    activity_type: str,
    contaminated_field: str,
    contaminated_value: str,
) -> None:
    context = _context(activity_type)
    kit = context["type_kits"][0]
    activity: dict = {"payload": {"type": activity_type, contaminated_field: contaminated_value}}

    with pytest.raises(PromptPackV3Error, match="synthetic exemplar fragment"):
        validate_exemplar_contamination(activity, kit)


def test_exemplar_contamination_rejects_raw_certified_forms_in_text_questions() -> None:
    context = _context("text-questions")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": list(_primary_forms(kit)),
        },
        "answer_key": {"guidance": "x"},
    }

    with pytest.raises(PromptPackV3Error, match="raw certified forms"):
        validate_exemplar_contamination(activity, kit)


def test_verbatim_answer_ban_rejects_answer_in_question() -> None:
    context = _context("quiz")
    kit = context["type_kits"][0]
    form = _primary_forms(kit)[0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": f"Яке слово тут потрібно: {form}?",
                    "options": [form, "інший"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    with pytest.raises(PromptPackV3Error, match="contains answer form"):
        validate_verbatim_answer_ban(activity, kit)


def test_verbatim_answer_ban_rejects_bare_form_question() -> None:
    context = _context("quiz")
    kit = context["type_kits"][0]
    form = _primary_forms(kit)[0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": form,
                    "options": [form, "інший"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    with pytest.raises(PromptPackV3Error, match="bare answer form or template"):
        validate_verbatim_answer_ban(activity, kit)


def test_verbatim_answer_ban_accepts_composed_question() -> None:
    context = _context("quiz")
    kit = context["type_kits"][0]
    form = _primary_forms(kit)[0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": "Яка форма слова потрібна в цьому реченні?",
                    "options": [form, "інший"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    validate_verbatim_answer_ban(activity, kit)


def test_elicitation_shape_rejects_bare_form() -> None:
    context = _context("quiz")
    kit = context["type_kits"][0]
    form = _primary_forms(kit)[0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": form,
                    "options": [form, "інший"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    with pytest.raises(PromptPackV3Error, match="must be a composed sentence"):
        validate_elicitation_shape(activity, kit)


def test_elicitation_shape_accepts_composed_item() -> None:
    context = _context("cloze")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "cloze",
            "instruction": "Заповніть пропуск.",
            "text": "У місті сьогодні холодно, тому ми йдемо у кавʼярню.",
            "blanks": [{"id": 1, "answer": "кавʼярню", "options": ["кавʼярню", "кавʼярня"]}],
        },
        "answer_key": {"blanks": [{"id": 1, "answer": "кавʼярню"}]},
    }

    validate_elicitation_shape(activity, kit)


def test_distractor_adjacency_rejects_non_vesum_distractor() -> None:
    context = _context("quiz")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": "Яка форма іменника потрібна тут?",
                    "options": ["книги", "вигаданеСлово123", "книг"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    with pytest.raises(PromptPackV3Error, match="not in VESUM"):
        validate_distractor_adjacency(activity, kit)


def test_distractor_adjacency_rejects_unrelated_lemma_distractor() -> None:
    context = _context("quiz")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": "Яка форма іменника потрібна тут?",
                    "options": ["книги", "телевізор", "зошит"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    with pytest.raises(PromptPackV3Error, match="does not share a lemma"):
        validate_distractor_adjacency(activity, kit)


def test_distractor_adjacency_accepts_same_lemma_form() -> None:
    context = _context("quiz")
    kit = context["type_kits"][0]
    # Use a noun form pair known to exist in the offline fixture VESUM.
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": "Яка форма іменника потрібна тут?",
                    "options": ["книги", "книга", "книг"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    # Both forms share the lemma "книга".
    validate_distractor_adjacency(activity, kit)


def test_distractor_adjacency_requires_answer_at_declared_index() -> None:
    context = _context("quiz")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": "Яка форма іменника потрібна тут?",
                    "options": ["книг", "книги", "книга"],
                    "correct": 1,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 1}]},
    }

    validate_distractor_adjacency(activity, kit)


def test_distractor_adjacency_accepts_same_class_prep_distractor() -> None:
    """Uninflectable preposition answer accepts another real preposition."""
    context = _context("quiz")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний прийменник.",
            "items": [
                {
                    "question": "Який прийменник потрібен тут?",
                    "options": ["на", "про", "під"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    validate_distractor_adjacency(activity, kit)


def test_distractor_adjacency_rejects_wrong_class_uninflectable_distractor() -> None:
    """Uninflectable preposition answer rejects a real word of a different class."""
    context = _context("quiz")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний прийменник.",
            "items": [
                {
                    "question": "Який прийменник потрібен тут?",
                    "options": ["на", "нате", "під"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    with pytest.raises(PromptPackV3Error, match="same POS class"):
        validate_distractor_adjacency(activity, kit)


def test_distractor_adjacency_rejects_identical_uninflectable_distractor() -> None:
    """Uninflectable answer rejects a distractor equal to itself."""
    context = _context("quiz")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний прийменник.",
            "items": [
                {
                    "question": "Який прийменник потрібен тут?",
                    "options": ["на", "на", "під"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    with pytest.raises(PromptPackV3Error, match="equals answer"):
        validate_distractor_adjacency(activity, kit)


def test_error_correction_valid_adjacent_error_passes() -> None:
    """Valid adjacent-error item (same lemma inflectable wrong form) passes."""
    context = _context("error-correction")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "error-correction",
            "instruction": "Виправте помилку в реченні.",
            "items": ["У бібліотеці є багато цікаві книжки для читання."],
        },
        "answer_key": {"items": ["книжок"]},
    }
    validate_distractor_adjacency(activity, kit)


def test_error_correction_token_duplication_fails() -> None:
    """Token-duplication corruptions fail explicitly."""
    context = _context("error-correction")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "error-correction",
            "instruction": "Виправте помилку в реченні.",
            "items": ["думку думку вчених, читання є важливим."],
        },
        "answer_key": {"items": ["На"]},
    }
    with pytest.raises(PromptPackV3Error, match="repeated token corruption"):
        validate_distractor_adjacency(activity, kit)


def test_error_correction_correct_form_present_verbatim_fails() -> None:
    """Error-correction item containing the correct form verbatim fails."""
    kit = _context("error-correction")["type_kits"][0]
    kit["certified_units"][0]["allowed_forms"] = ["читання"]
    kit["certified_units"][0]["expected_key_or_rule"]["value"] = "читання"
    activity = {
        "payload": {
            "type": "error-correction",
            "instruction": "Виправте помилку в реченні.",
            "items": ["На думку вчених, читання є важливим."],
        },
        "answer_key": {"items": ["читання"]},
    }
    with pytest.raises(PromptPackV3Error, match="contains answer form"):
        validate_verbatim_answer_ban(activity, kit)


def test_error_correction_wrong_class_adjacent_form_fails() -> None:
    """Uninflectable target with a wrong-class word fails."""
    context = _context("error-correction")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "error-correction",
            "instruction": "Виправте помилку в реченні.",
            "items": ["Нате думку вчених, читання є важливим."],
        },
        "answer_key": {"items": ["на"]},
    }
    with pytest.raises(PromptPackV3Error, match="does not contain an adjacent wrong form"):
        validate_distractor_adjacency(activity, kit)


def test_error_correction_uninflectable_target_with_same_class_wrong_word_passes() -> None:
    """Uninflectable target with a same-class wrong word passes."""
    context = _context("error-correction")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "error-correction",
            "instruction": "Виправте помилку в реченні.",
            "items": ["Про думку вчених, читання є важливим."],
        },
        "answer_key": {"items": ["на"]},
    }
    validate_distractor_adjacency(activity, kit)


def test_short_writing_elicitation_prompt_and_guidance_naming_passes() -> None:
    """Elicitation prompt + guidance naming target form passes validation."""
    from hramatka.api.baking.engine_adapter_v3 import _activity_gate

    context = _context("short-writing")
    kit = context["type_kits"][0]
    target_forms = [
        fragment for unit in kit["certified_units"] for fragment in unit["allowed_forms"]
    ]
    activity = {
        "payload": {
            "type": "short-writing",
            "prompt": "Напишіть короткий текст про ваш щоденний розклад дня.",
        },
        "answer_key": {
            "guidance": (
                "Текст має бути коротким і обов'язково містити цільові вимоги: "
                + ", ".join(target_forms)
                + "."
            ),
        },
    }
    _activity_gate(activity, kit)
    validate_verbatim_answer_ban(activity, kit)
    validate_elicitation_shape(activity, kit)
    validate_exemplar_contamination(activity, kit)


def test_short_writing_prompt_containing_certified_form_fails() -> None:
    """Prompt containing a certified target form verbatim fails validation."""
    from hramatka.api.baking.engine_adapter_v3 import _activity_gate

    context = _context("short-writing")
    kit = context["type_kits"][0]
    target_form = kit["certified_units"][0]["allowed_forms"][0]
    activity = {
        "payload": {
            "type": "short-writing",
            "prompt": f"Напишіть короткий текст, використовуючи форму {target_form}.",
        },
        "answer_key": {
            "guidance": f"Текст має містити форму {target_form}.",
        },
    }
    with pytest.raises(ValueError, match="contains certified form .* verbatim"):
        _activity_gate(activity, kit)


def test_short_writing_guidance_missing_form_fails() -> None:
    """Guidance missing a certified target form fails validation."""
    from hramatka.api.baking.engine_adapter_v3 import _activity_gate

    context = _context("short-writing")
    kit = context["type_kits"][0]
    activity = {
        "payload": {
            "type": "short-writing",
            "prompt": "Напишіть короткий текст про ваш щоденний розклад дня.",
        },
        "answer_key": {
            "guidance": "Текст має бути коротким і описовим.",
        },
    }
    with pytest.raises(ValueError, match="missing certified form"):
        _activity_gate(activity, kit)


def test_short_writing_concatenation_prompt_fails() -> None:
    """Short-writing prompt that is a raw concatenation of forms fails contamination."""
    context = _context("short-writing")
    kit = context["type_kits"][0]
    primary_forms = [unit["allowed_forms"][0] for unit in kit["certified_units"]]
    activity = {
        "payload": {
            "type": "short-writing",
            "prompt": " ".join(primary_forms),
        },
        "answer_key": {
            "guidance": f"Текст має містити {' '.join(primary_forms)}.",
        },
    }
    with pytest.raises(PromptPackV3Error, match="concatenation of raw certified forms"):
        validate_exemplar_contamination(activity, kit)
