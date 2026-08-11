"""Deterministic contracts for the v3.3 binding-contract redesign (#375)."""

from __future__ import annotations

from copy import deepcopy

import pytest

from hramatka.engine import anchor_inventory_v3, paths, prompt_pack_v3
from hramatka.engine.lesson_capacity_v3 import AllocatedSlot, LessonAllocation
from hramatka.engine.linguistics import verify_word
from hramatka.engine.prompt_pack_v3 import (
    PROMPT_PACK_VERSION,
    TEMPLATE_VERSION,
    TYPE_KIT_IDENTITY,
    PromptPackV3Error,
    RuleNamedRejection,
    build_phase_context,
    compact_schema_exemplars,
    one_slot_context,
    render_phase_prompt,
    six_item_negative_exemplar,
    validate_activity_purpose,
    validate_distractor_adjacency,
    validate_elicitation_shape,
    validate_exemplar_contamination,
    validate_non_revealing_sequence,
    validate_response,
    validate_slot_deterministic_gates,
    validate_verbatim_answer_ban,
    validate_visible_writing_constraints,
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

    assert context["pack_version"] == PROMPT_PACK_VERSION == "PromptPackInput.v3.3"
    assert context["template_version"] == TEMPLATE_VERSION == "gemma-phase-pack.v3.11"
    assert context["type_kit_identity"] == TYPE_KIT_IDENTITY
    kit = context["type_kits"][0]
    assert kit["identity"] == TYPE_KIT_IDENTITY
    assert kit["scheduled_unit_count"] == 8
    assert len(kit["scheduled_unit_ids"]) == 8
    assert kit["scheduled_unit_ids"] == [unit["unit_id"] for unit in kit["certified_units"]]


def test_pinned_cloze_kit_marks_the_exact_repeated_target_occurrences() -> None:
    from hramatka.qualification.harness import _v3_qualification_allocation

    allocation = _v3_qualification_allocation()
    context = build_phase_context(allocation, phase=1)
    kit = next(
        item
        for item in context["type_kits"]
        if item["slot_id"] == "P1-A2" and item["type"] == "cloze"
    )
    units = kit["certified_units"]
    carrier = " ".join(dict.fromkeys(unit["rendering_surface"] for unit in units))
    assert carrier.count("читання") == 5
    assert carrier.count("українців") == 2

    spans: list[tuple[int, int, int]] = []
    repeated_answer_occurrences: dict[str, list[int]] = {}
    for index, unit in enumerate(units, start=1):
        answer = unit["allowed_forms"][0]
        gap = unit["distinctness"]["gap"]
        start, end = gap["start_offset"], gap["end_offset"]
        assert carrier[start:end] == answer
        spans.append((start, end, index))
        if answer in {"читання", "українців"}:
            repeated_answer_occurrences.setdefault(answer, []).append(
                carrier[:start].count(answer) + 1
            )
    assert repeated_answer_occurrences == {"читання": [2, 5], "українців": [2]}

    independently_rendered = carrier
    for start, end, index in reversed(sorted(spans)):
        independently_rendered = (
            independently_rendered[:start] + f"{{{index}}}" + independently_rendered[end:]
        )
    assert kit["marked_rendering_surface"] == independently_rendered


def test_serializer_temperature_is_bound_into_the_prompt_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_context = _context()
    monkeypatch.setenv("HRAMATKA_GEN_TEMPERATURE", "0.7")
    overridden_context = _context()

    assert default_context["serializer_temperature"] == 0.0
    assert overridden_context["serializer_temperature"] == 0.7
    assert default_context["context_sha256"] != overridden_context["context_sha256"]


def test_focus_is_integrity_bound_and_one_slot_repair_keeps_it() -> None:
    context = _context("quiz", "cloze")
    allocation = LessonAllocation(
        paragraph_ids=("fixture-paragraph",),
        slots=tuple(
            AllocatedSlot(
                slot_id=kit["slot_id"],
                phase=kit["phase"],
                requested_type=kit["type"],
                scheduled_type=kit["type"],
                plan=BUILDERS[kit["type"]](
                    complete_inventory(), slot_id=kit["slot_id"], phase=kit["phase"]
                ),
            )
            for kit in context["type_kits"]
        ),
    )
    focused = build_phase_context(allocation, phase=1, focus="Нічліг на березі")
    narrowed = one_slot_context(focused, slot_id="P1-A2")

    assert focused["lesson_focus"] == "Нічліг на березі"
    assert narrowed["lesson_focus"] == "Нічліг на березі"
    assert narrowed["response_order"] == ["P1-A2"]
    assert [kit["slot_id"] for kit in narrowed["type_kits"]] == ["P1-A2"]
    assert "Нічліг на березі" in render_phase_prompt(narrowed)


def test_compact_exemplars_are_requested_type_only_and_keep_exact_count() -> None:
    context = _context()

    exemplars = compact_schema_exemplars(context["type_kits"])
    assert [row["type"] for row in exemplars] == ["quiz"]
    assert exemplars[0]["required_unit_count"] == 8
    shape = exemplars[0]["one_item_slot_shape"]
    assert shape["slot_id"] == "<synthetic-quiz-slot>"
    assert shape["activity"]["payload"]["type"] == "quiz"
    assert len(shape["activity"]["payload"]["items"]) == 1
    assert len(shape["serialized_units"]) == 1
    negative = six_item_negative_exemplar()
    assert len(negative["serialized_units"]) == 6
    prompt = render_phase_prompt(context)
    assert "SYNTHETIC-QUIZ-STEM" in prompt
    assert "v3.11" in prompt
    assert "APPLICABLE TYPE PURPOSE CONTRACTS" in prompt
    assert "CONTRASTIVE PEDAGOGY FAILURES" in prompt


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
        compact_schema_exemplars([underfilled_kit])


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


@pytest.mark.parametrize("activity_type", ("quiz", "fill-in"))
def test_verbatim_answer_ban_rejects_repeated_answer_away_from_certified_gap(
    activity_type: str,
) -> None:
    """A visible second occurrence leaks the answer even in a certified carrier."""
    sentence = "Такі ___ — це зв'язки в мозку!"
    if activity_type == "quiz":
        payload = {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": sentence,
                    "options": ["зв'язки", "слова", "думки"],
                    "correct": 0,
                }
            ],
        }
        answer_key = {"items": [{"index": 0, "correct": 0}]}
    else:
        payload = {
            "type": "fill-in",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "sentence": sentence,
                    "answer": "зв'язки",
                    "options": ["зв'язки", "слова", "думки"],
                }
            ],
        }
        answer_key = {"items": ["зв'язки"]}

    with pytest.raises(PromptPackV3Error, match="contains answer form"):
        validate_verbatim_answer_ban(
            {"payload": payload, "answer_key": answer_key},
            {
                "certified_units": [
                    {
                        "allowed_forms": ["зв'язки"],
                        "gapped_rendering_surface": sentence,
                    }
                ]
            },
        )


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


def test_distractor_adjacency_accepts_same_case_number_personal_pronouns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "вона": [{"lemma": "вона", "pos": "noun", "tags": "noun:f:v_naz:pron:pers:3"}],
        "він": [{"lemma": "він", "pos": "noun", "tags": "noun:m:v_naz:pron:pers:3"}],
        "воно": [{"lemma": "воно", "pos": "noun", "tags": "noun:n:v_naz:pron:pers:3"}],
        "ми": [{"lemma": "ми", "pos": "noun", "tags": "noun:p:v_naz:pron:pers:1"}],
        "ви": [{"lemma": "ви", "pos": "noun", "tags": "noun:p:v_naz:pron:pers:2"}],
        "вони": [{"lemma": "вони", "pos": "noun", "tags": "noun:p:v_naz:pron:pers:3"}],
    }
    monkeypatch.setattr(
        prompt_pack_v3,
        "_vesum_matches",
        lambda form, _db_path: rows.get(form.casefold(), []),
    )
    activity = {
        "payload": {
            "type": "fill-in",
            "instruction": "Вставте слово.",
            "items": [
                {
                    "sentence": "___ була красива.",
                    "answer": "Вона",
                    "options": ["Він", "Вона", "Воно"],
                }
            ],
        },
        "answer_key": {"items": ["Вона"]},
    }

    validate_distractor_adjacency(activity, _context("fill-in")["type_kits"][0])

    plural_activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть займенник із тексту.",
            "items": [
                {
                    "question": "___ зателефонували того ж вечора.",
                    "options": ["Вони", "Ми", "Ви"],
                    "correct": 1,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 1}]},
    }

    validate_distractor_adjacency(plural_activity, _context("quiz")["type_kits"][0])


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
                    "options": ["на", "На", "під"],
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


def test_error_correction_correct_form_in_instruction_fails() -> None:
    """An instruction must not disclose the hidden correction."""
    kit = _context("error-correction")["type_kits"][0]
    kit["certified_units"][0]["allowed_forms"] = ["читання"]
    kit["certified_units"][0]["expected_key_or_rule"]["value"] = "читання"
    activity = {
        "payload": {
            "type": "error-correction",
            "instruction": "Виправте на «читання».",
            "items": ["На думку вчених, читання є важливим."],
        },
        "answer_key": {"items": ["читання"]},
    }
    with pytest.raises(PromptPackV3Error, match="contains answer form"):
        validate_verbatim_answer_ban(activity, kit)


def test_error_correction_allows_correct_form_elsewhere_in_certified_context() -> None:
    """A natural second occurrence is context, not a correction leak."""
    kit = _context("error-correction")["type_kits"][0]
    item = "Як нам шукали квартиру, коли ми приїхали?"
    kit["certified_units"][0]["allowed_forms"] = [item, "ми"]
    kit["certified_units"][0]["expected_key_or_rule"]["value"] = "ми"
    kit["certified_units"][0]["rendering_surface"] = "Як ми шукали квартиру, коли ми приїхали?"
    activity = {
        "payload": {
            "type": "error-correction",
            "instruction": "Виправте помилку в реченні.",
            "items": [item],
        },
        "answer_key": {"items": ["ми"]},
    }

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


def test_short_writing_visible_constraints_and_generic_guidance_pass() -> None:
    """Every certified constraint belongs in learner-visible prompt prose."""
    from hramatka.api.baking.engine_adapter_v3 import _activity_gate

    context = _context("short-writing")
    kit = context["type_kits"][0]
    target_forms = [
        fragment for unit in kit["certified_units"] for fragment in unit["allowed_forms"]
    ]
    activity = {
        "payload": {
            "type": "short-writing",
            "prompt": ("Напишіть текст про ваш щоденний розклад: " + ", ".join(target_forms) + "."),
        },
        "answer_key": {"guidance": "Перевірте виконання всіх умов."},
    }
    _activity_gate(activity, kit)
    validate_visible_writing_constraints(activity, kit)
    validate_verbatim_answer_ban(activity, kit)
    validate_elicitation_shape(activity, kit)
    validate_exemplar_contamination(activity, kit)


def test_short_writing_prompt_missing_one_constraint_fails() -> None:
    """A learner prompt that omits any certified marker fails binding."""
    from hramatka.api.baking.engine_adapter_v3 import _activity_gate

    context = _context("short-writing")
    kit = context["type_kits"][0]
    target_forms = kit["certified_units"][0]["allowed_forms"]
    activity = {
        "payload": {
            "type": "short-writing",
            "prompt": "Напишіть текст: " + ", ".join(target_forms[:-1]) + ".",
        },
        "answer_key": {"guidance": "Перевірте виконання умов."},
    }
    with pytest.raises(ValueError, match="missing certified constraint"):
        _activity_gate(activity, kit)


def test_short_writing_constraints_hidden_only_in_guidance_fail() -> None:
    """Hidden guidance cannot substitute for learner-visible constraints."""
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
    with pytest.raises(ValueError, match="missing certified constraint"):
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


def test_true_false_statement_text_must_equal_certified_surface() -> None:
    from hramatka.api.baking.engine_adapter_v3 import _activity_gate

    kit = _context("true-false")["type_kits"][0]
    statements = [unit["allowed_forms"][0] for unit in kit["certified_units"]]
    truth = [unit["expected_key_or_rule"]["value"] == "true" for unit in kit["certified_units"]]
    activity = {
        "payload": {
            "type": "true-false",
            "instruction": "Визначте правильність тверджень.",
            "items": [
                {"statement": statement, "correct": correct}
                for statement, correct in zip(statements, truth, strict=True)
            ],
        },
        "answer_key": {
            "items": [{"index": index, "correct": correct} for index, correct in enumerate(truth)]
        },
    }
    _activity_gate(activity, kit)

    activity["payload"]["items"][0]["statement"] += " зайве"
    with pytest.raises(ValueError, match="statement is detached"):
        _activity_gate(activity, kit)


def test_error_correction_source_sentence_must_equal_certified_surface() -> None:
    from hramatka.api.baking.engine_adapter_v3 import _activity_gate

    kit = _context("error-correction")["type_kits"][0]
    sources = [unit["allowed_forms"][0] for unit in kit["certified_units"]]
    answers = [unit["expected_key_or_rule"]["value"] for unit in kit["certified_units"]]
    activity = {
        "payload": {
            "type": "error-correction",
            "instruction": "Виправте одну помилку.",
            "items": list(sources),
        },
        "answer_key": {"items": answers},
    }
    _activity_gate(activity, kit)

    activity["payload"]["items"][0] += " зайве"
    with pytest.raises(ValueError, match="error-correction source items"):
        _activity_gate(activity, kit)


def test_repeated_text_questions_and_token_retrieval_are_rejected() -> None:
    kit = _context("text-questions")["type_kits"][0]
    repeated = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповіді.",
            "items": ["Чому герой залишився на березі?"] * 8,
        },
        "answer_key": {"guidance": "Відповідайте повними реченнями."},
    }
    with pytest.raises(PromptPackV3Error, match="repeats a learner-facing stem"):
        validate_non_revealing_sequence(repeated, kit)

    retrieval = deepcopy(repeated)
    retrieval["payload"]["items"] = [
        "Яке слово стоїть у реченні?",
        *[f"Чому герой діє саме так у ситуації {index}?" for index in range(2, 9)],
    ]
    with pytest.raises(PromptPackV3Error, match="token label instead of meaning"):
        validate_activity_purpose(retrieval, kit)


def test_repeated_true_false_statements_are_rejected() -> None:
    kit = _context("true-false")["type_kits"][0]
    repeated = {
        "payload": {
            "type": "true-false",
            "instruction": "Визначте правильність тверджень.",
            "items": [{"statement": "Те саме твердження.", "correct": True}] * 8,
        },
        "answer_key": {"items": [{"index": index, "correct": True} for index in range(8)]},
    }

    with pytest.raises(PromptPackV3Error, match="repeats a learner-facing stem"):
        validate_non_revealing_sequence(repeated, kit)


def test_quiz_non_revealing_gate_uses_one_certified_source_per_item() -> None:
    """Static source diversity is an inventory invariant, not a model repair target."""
    kit = _context("quiz")["type_kits"][0]
    units = kit["certified_units"]
    surfaces = [unit["rendering_surface"] for unit in units]
    assert len(set(surfaces)) == len(units) == 8
    items = []
    for unit in units:
        answer = unit["allowed_forms"][0]
        items.append(
            {
                "question": unit["rendering_surface"].replace(answer, "___", 1),
                "options": [answer, "альтернатива"],
                "correct": 0,
            }
        )
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": items,
        },
        "answer_key": {"items": [{"index": index, "correct": 0} for index in range(len(items))]},
    }

    validate_non_revealing_sequence(activity, kit)

    activity["payload"]["items"][1]["question"] = activity["payload"]["items"][0]["question"]
    with pytest.raises(PromptPackV3Error, match="repeats a learner-facing stem"):
        validate_non_revealing_sequence(activity, kit)


def test_natural_text_question_categories_need_one_source_content_lemma() -> None:
    kit = _context("text-questions")["type_kits"][0]
    activities = {
        "comprehension": "Що повідомляє уривок про читання?",
        "explanation_inference": "Як можна пояснити користь читання?",
        "anchored_application": "У якій реальній ситуації допоможе читання?",
    }
    for category, question in activities.items():
        single = deepcopy(kit)
        single["certified_units"] = [
            next(
                unit
                for unit in kit["certified_units"]
                if unit["distinctness"]["question_category"] == category
            )
        ]
        single["certified_units"][0]["rendering_surface"] = "Регулярне читання розвиває мозок."
        activity = {
            "payload": {
                "type": "text-questions",
                "instruction": "Дайте відповідь.",
                "items": [question],
            },
            "answer_key": {"guidance": "Відповідайте повними реченнями."},
        }
        validate_activity_purpose(activity, single)

        activity["payload"]["items"] = [question.replace("читання", "подорож")]
        with pytest.raises(PromptPackV3Error, match="detached from its rendering surface"):
            validate_activity_purpose(activity, single)


def test_anchored_application_accepts_natural_learner_experience_cue() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Четвертий поверх без ліфта може бути мінусом."
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["З вашого досвіду, для кого четвертий поверх без ліфта може бути мінусом?"],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    validate_activity_purpose(activity, kit)


def test_anchored_application_accepts_taki_with_explicit_noun_topic() -> None:
    """`такі` is not detached when the question explicitly names its referent."""
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Такі зв'язки виникають у мозку дитини."
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["У якій реальній ситуації важливі такі зв'язки в мозку?"],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    validate_activity_purpose(activity, kit)


def test_anchored_application_rejects_contrast_as_one_causal_reason() -> None:
    """The exact failed canary's `через те, що X, але Y` logic cannot recur."""
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Квартира була красива, але дорога."
    unit["distinctness"]["question_intent"] = "realistic-transfer"
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": [
                "Чи доводилося вам відмовлятися від квартири через те, що вона "
                "красива, але дорога?"
            ],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    with pytest.raises(PromptPackV3Error, match="turns a contrast into one causal reason"):
        validate_activity_purpose(activity, kit)


def test_degree_cue_accepts_only_normative_correlative_pairs() -> None:
    assert prompt_pack_v3._surface_has_degree_cue(
        "Що довший був пошук, то простішими ставали вимоги.", "comparative"
    )
    assert prompt_pack_v3._surface_has_degree_cue(
        "Чим ближче будинок до центру, тим дорожчий він.", "comparative"
    )
    assert not prompt_pack_v3._surface_has_degree_cue(
        "Що ближче будинок до центру, тим дорожчий він.", "comparative"
    )


def test_degree_writing_rejects_literal_base_word_requirement() -> None:
    """`малий` is a lemma cue; correct `менший` must satisfy the learner task."""
    kit = _context("short-writing")["type_kits"][0]
    marker = (
        "утворіть потрібні форми від прикметників «малий», «світлий», «теплий», «близький»"
    )
    unit = kit["certified_units"][0]
    unit["allowed_forms"] = [marker, "від 80 до 110 слів"]
    unit["rendering_surface"] = anchor_inventory_v3.DEGREE_WRITING_SCENARIO
    unit["distinctness"]["attribute_warrants"] = dict(
        anchor_inventory_v3.DEGREE_WRITING_WARRANTS
    )
    kit["focus_alignment"] = "degree-writing"
    activity = {
        "payload": {
            "type": "short-writing",
            "prompt": (
                f"{anchor_inventory_v3.DEGREE_WRITING_SCENARIO}\n"
                "Порівняйте квартири й обґрунтуйте вибір. Ужийте щонайменше 3 "
                "прикметники у вищому або найвищому ступені; "
                f"{marker}; від 80 до 110 слів. "
                "Використайте слова «малий», «світлий», «теплий», «близький»."
            ),
        },
        "answer_key": {"guidance": "Перевірте виконання умов."},
    }

    with pytest.raises(PromptPackV3Error, match="literal base words"):
        validate_visible_writing_constraints(activity, kit)


@pytest.mark.parametrize(
    ("question", "suffix"),
    (
        ("Яке слово стоїть у реченні?", "token_retrieval"),
        ("Чому в уривку згадано читання?", "question_category_mismatch"),
        ("Що повідомляє уривок про подорож?", "source_lemma_overlap_missing"),
        (
            "Що повідомляє уривок про те, як регулярне читання щодня розвиває "
            "мозок дорослої людини вдома?",
            "answer_leak",
        ),
        ("Що повідомляє уривок про читання цих книжок?", "unresolved_reference"),
    ),
)
def test_text_question_repair_receives_safe_actionable_gate_code(
    question: str, suffix: str
) -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "comprehension"
    )
    unit["rendering_surface"] = "Регулярне читання розвиває мозок."
    kit["certified_units"] = [unit]
    record = {
        "activity": {
            "payload": {
                "type": "text-questions",
                "instruction": "Дайте відповідь.",
                "items": [question],
            },
            "answer_key": {"guidance": "Відповідайте повними реченнями."},
        }
    }

    with pytest.raises(RuleNamedRejection) as rejected:
        validate_slot_deterministic_gates(
            record,
            kit,
            deterministic_gates=(validate_activity_purpose,),
        )

    assert rejected.value.rule_key == "activity_purpose"
    assert rejected.value.suffix == f"{suffix}:item=0"


def test_duplicate_options_or_duplicate_answers_fail_closed() -> None:
    """Duplicate answer string or duplicate options in MCQ list fail closed."""
    context = _context("cloze")
    kit = context["type_kits"][0]
    duplicate_answer_activity = {
        "payload": {
            "type": "cloze",
            "instruction": "Заповніть пропуски.",
            "text": "У мене є ___.",
            "blanks": [
                {"id": 1, "answer": "книги", "options": ["книги", "книги", "книг"]},
            ],
        },
        "answer_key": {
            "blanks": [{"id": 1, "answer": "книги"}],
        },
    }
    with pytest.raises(PromptPackV3Error, match="duplicate"):
        validate_distractor_adjacency(duplicate_answer_activity, kit)

    absent_answer_activity = {
        "payload": {
            "type": "cloze",
            "instruction": "Заповніть пропуски.",
            "text": "У мене є ___.",
            "blanks": [
                {"id": 1, "answer": "книги", "options": ["зошит", "ручка", "олівець"]},
            ],
        },
        "answer_key": {
            "blanks": [{"id": 1, "answer": "книги"}],
        },
    }
    with pytest.raises(PromptPackV3Error, match="is not in options"):
        validate_distractor_adjacency(absent_answer_activity, kit)


def test_degree_reinforcement_fill_requires_a_real_degree_contrast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyses = {
        "тепліша": [{"lemma": "тепліший", "tags": "adj:f:nom:compc", "pos": "adj"}],
        "теплішої": [{"lemma": "тепліший", "tags": "adj:f:gen:compc", "pos": "adj"}],
        "теплішій": [{"lemma": "тепліший", "tags": "adj:f:dat:compc", "pos": "adj"}],
    }
    monkeypatch.setattr(
        prompt_pack_v3,
        "_vesum_matches",
        lambda form, _db_path: analyses.get(form, []),
    )
    monkeypatch.setattr(
        prompt_pack_v3,
        "_uninflectable_allowed_pos",
        lambda _answer, _db_path: None,
    )
    kit = deepcopy(_context("fill-in")["type_kits"][0])
    unit = deepcopy(kit["certified_units"][0])
    unit["allowed_forms"] = ["тепліша"]
    unit["distinctness"] = {
        **unit["distinctness"],
        "focus_alignment": "degree-reinforcement",
        "choice_bank": ["тепліша", "теплішої", "теплішій"],
        "exclusion_warrants": {
            "теплішої": "wrong agreement frame",
            "теплішій": "wrong agreement frame",
        },
    }
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "fill-in",
            "instruction": "Оберіть форму, що завершує речення.",
            "items": [
                {
                    "sentence": "Ця квартира ___ за попередню.",
                    "answer": "тепліша",
                    "options": ["тепліша", "теплішої", "теплішій"],
                }
            ],
        },
        "answer_key": {"items": ["тепліша"]},
    }

    with pytest.raises(PromptPackV3Error, match="real degree contrast"):
        validate_distractor_adjacency(activity, kit)


def test_pack_v39_no_options_zero_mandate_and_enforces_varied_placement() -> None:
    """Pack v3.11 contains no options[0] mandate and enforces varied answer placement."""
    from pathlib import Path

    template_path = Path(__file__).parent.parent / "prompts" / "gemma-phase-pack.v3.11.md"
    content = template_path.read_text(encoding="utf-8")
    assert "first option (`options[0]`)" not in content
    assert "options[0]" not in content
    assert "vary the correct-option position" in content

    context = _context("cloze")
    kit = context["type_kits"][0]
    fixed_placement_activity = {
        "payload": {
            "type": "cloze",
            "instruction": "Заповніть пропуски.",
            "text": "У мене є ___, ___ та ___.",
            "blanks": [
                {"id": 1, "answer": "книги", "options": ["книги", "книга", "книг"]},
                {"id": 2, "answer": "книга", "options": ["книга", "книги", "книг"]},
                {"id": 3, "answer": "книг", "options": ["книг", "книга", "книги"]},
            ],
        },
        "answer_key": {
            "blanks": [
                {"id": 1, "answer": "книги"},
                {"id": 2, "answer": "книга"},
                {"id": 3, "answer": "книг"},
            ],
        },
    }
    with pytest.raises(PromptPackV3Error, match="must vary answer placement"):
        validate_distractor_adjacency(fixed_placement_activity, kit)


def test_bare_gap_marker_check_rejects_longer_underscore_runs_and_bracketed() -> None:
    """Quiz stem check accepts bare '___' but rejects '____' and bracketed gap markers."""
    from hramatka.engine.prompt_pack_v3 import _has_bare_gap_marker

    assert _has_bare_gap_marker("«___ думку»") is True
    assert _has_bare_gap_marker("Я іду ___ додому.") is True
    assert _has_bare_gap_marker("«____ думку»") is False
    assert _has_bare_gap_marker("«[___] думку»") is False
    assert _has_bare_gap_marker("«(___) думку»") is False

    context = _context("quiz")
    kit = deepcopy(context["type_kits"][0])
    kit["certified_units"] = [
        {
            "unit_id": "u1",
            "allowed_forms": ["в"],
            "expected_key_or_rule": {"kind": "key", "value": "в"},
        }
    ]

    longer_run_stem_activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": "Я іду ____ школу.",
                    "options": ["в", "на", "під"],
                    "correct": 0,
                },
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }
    with pytest.raises(PromptPackV3Error, match="missing gap marker '___'"):
        validate_verbatim_answer_ban(longer_run_stem_activity, kit)
