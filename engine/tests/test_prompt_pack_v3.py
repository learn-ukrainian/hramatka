"""Deterministic contracts for the current v3 binding and pedagogy pack."""

from __future__ import annotations

from collections.abc import Callable
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
    RuleNamedRejectionGroup,
    build_phase_context,
    compact_schema_exemplars,
    one_slot_context,
    render_phase_prompt,
    six_item_negative_exemplar,
    teacher_sample_review_inputs,
    validate_activity_purpose,
    validate_distractor_adjacency,
    validate_elicitation_shape,
    validate_exemplar_contamination,
    validate_non_revealing_sequence,
    validate_response,
    validate_slot_deterministic_gates,
    validate_teacher_sample_constraints,
    validate_verbatim_answer_ban,
    validate_visible_writing_constraints,
)
from hramatka.engine.teacher_ready_density_v3 import floor_for
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


def test_v34_context_uses_the_new_template_and_type_kit_identity() -> None:
    context = _context()

    assert context["pack_version"] == PROMPT_PACK_VERSION == "PromptPackInput.v3.4"
    assert context["template_version"] == TEMPLATE_VERSION == "gemma-phase-pack.v3.15"
    assert context["type_kit_identity"] == TYPE_KIT_IDENTITY
    kit = context["type_kits"][0]
    assert kit["identity"] == TYPE_KIT_IDENTITY
    assert kit["scheduled_unit_count"] == 8
    assert len(kit["scheduled_unit_ids"]) == 8
    assert kit["scheduled_unit_ids"] == [unit["unit_id"] for unit in kit["certified_units"]]


def test_prompt_states_the_teacher_sample_wire_contract_for_both_open_types() -> None:
    prompt = render_phase_prompt(_context("text-questions", "short-writing"))

    assert "N. Зразок відповіді: <concrete Ukrainian sample>" in prompt
    assert "exactly one `Зразок відповіді:` marker" in prompt
    assert "inside the certified word range" in prompt


def test_pinned_cloze_kit_marks_the_exact_repeated_target_occurrences() -> None:
    from hramatka.qualification.harness import _v3_qualification_allocation

    allocation = _v3_qualification_allocation()
    context = build_phase_context(allocation, phase=3)
    kit = next(
        item
        for item in context["type_kits"]
        if item["slot_id"] == "P3-A1" and item["type"] == "cloze"
    )
    units = kit["certified_units"]
    carrier = units[0]["rendering_surface"]
    assert all(unit["rendering_surface"] == carrier for unit in units)

    spans: list[tuple[int, int, int]] = []
    for index, unit in enumerate(units, start=1):
        answer = unit["allowed_forms"][0]
        gap = unit["distinctness"]["gap"]
        start, end = gap["start_offset"], gap["end_offset"]
        assert carrier[start:end] == answer
        spans.append((start, end, index))

    independently_rendered = carrier
    for start, end, index in reversed(sorted(spans)):
        independently_rendered = (
            independently_rendered[:start] + f"{{{index}}}" + independently_rendered[end:]
        )
    assert kit["marked_rendering_surface"] == independently_rendered


def test_phase_three_cloze_prompt_projects_one_lossless_shared_source() -> None:
    from hramatka.engine.prompt_pack_v3 import _model_facing_type_kits
    from hramatka.qualification.harness import _v3_qualification_allocation

    context = build_phase_context(_v3_qualification_allocation(), phase=3)
    original = deepcopy(context)
    projected = _model_facing_type_kits(context["type_kits"])
    kit = next(item for item in projected if item["type"] == "cloze")
    original_kit = next(item for item in context["type_kits"] if item["type"] == "cloze")
    carrier = original_kit["certified_units"][0]["rendering_surface"]

    assert context == original
    assert kit["source_surface_catalog"] == [carrier]
    assert all(
        unit["source_surface_id"] == 0 and "rendering_surface" not in unit
        for unit in kit["certified_units"]
    )
    assert render_phase_prompt(context).count(carrier) == 1


def test_phase_three_cloze_prompt_keeps_nonshared_surfaces_unprojected() -> None:
    from hramatka.engine.prompt_pack_v3 import _model_facing_type_kits
    from hramatka.qualification.harness import _v3_qualification_allocation

    context = build_phase_context(_v3_qualification_allocation(), phase=3)
    kits = deepcopy(context["type_kits"])
    kit = next(item for item in kits if item["type"] == "cloze")
    kit["certified_units"][1]["rendering_surface"] += " Інший текст."

    assert _model_facing_type_kits(kits) == kits


def test_qualified_quiz_is_source_comprehension_not_another_gap_drill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hramatka.api.baking.engine_adapter_v3 import _activity_gate
    from hramatka.qualification.harness import _v3_qualification_allocation

    context = build_phase_context(_v3_qualification_allocation(), phase=1)
    kit = next(item for item in context["type_kits"] if item["type"] == "quiz")
    items = []
    key_items = []
    topic_lemmas = {}
    for index, unit in enumerate(kit["certified_units"]):
        distinctness = unit["distinctness"]
        prefix = distinctness["question_frame"]["allowed_prefixes"][0]
        topic = distinctness["question_topic"]["surface"]
        topic_lemmas[topic.casefold()] = distinctness["question_topic"]["lemma"]
        options = list(distinctness["choice_bank"])
        shift = index % len(options)
        options = [*options[shift:], *options[:shift]]
        correct = options.index(unit["allowed_forms"][0])
        items.append(
            {
                "question": f"{prefix} повідомляє джерело про {topic}?",
                "options": options,
                "correct": correct,
            }
        )
        key_items.append({"index": index, "correct": correct})
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Дайте відповіді за змістом тексту.",
            "items": items,
        },
        "answer_key": {"items": key_items},
    }
    original_matches = prompt_pack_v3._vesum_matches

    def qualification_matches(word, db_path):
        lemma = topic_lemmas.get(word.casefold())
        return [{"lemma": lemma, "pos": "noun"}] if lemma else original_matches(word, db_path)

    monkeypatch.setattr(prompt_pack_v3, "_vesum_matches", qualification_matches)

    _activity_gate(activity, kit)
    validate_activity_purpose(activity, kit)
    validate_distractor_adjacency(activity, kit)
    validate_teacher_sample_constraints(activity, kit)
    review_inputs = teacher_sample_review_inputs(activity, kit)

    assert len(review_inputs) == len(kit["certified_units"])
    assert all(row["activity_type"] == "quiz" for row in review_inputs)
    assert all(row["category"] == "comprehension" for row in review_inputs)
    assert [row["teacher_sample"] for row in review_inputs] == [
        item["options"][item["correct"]] for item in items
    ]
    assert [row["evidence_segments"] for row in review_inputs] == [
        [unit["rendering_surface"]] for unit in kit["certified_units"]
    ]

    activity["payload"]["items"][0]["question"] = "Що тут ___?"
    with pytest.raises(PromptPackV3Error, match="not bound to its proposition"):
        validate_activity_purpose(activity, kit)


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
    assert len(negative["serialized_units"]) == 4
    prompt = render_phase_prompt(context)
    assert "SYNTHETIC-QUIZ-STEM" in prompt
    assert "v3.15" in prompt
    assert "APPLICABLE TYPE PURPOSE CONTRACTS" in prompt
    assert "CONTRASTIVE PEDAGOGY FAILURES" in prompt


def test_text_question_schema_shape_has_no_copyable_learner_content() -> None:
    context = _context("text-questions")

    exemplar = compact_schema_exemplars(context["type_kits"])[0]
    activity = exemplar["one_item_slot_shape"]["activity"]
    prompt = render_phase_prompt(context)

    assert activity["payload"]["instruction"] == ""
    assert activity["payload"]["items"] == [""]
    assert activity["answer_key"]["guidance"] == ""
    assert "SYNTHETIC-OPEN-QUESTION" not in prompt
    assert "Синтетична вказівка." not in prompt
    assert "never return that value blank" in prompt


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
    underfilled_kit["scheduled_unit_count"] = floor_for("quiz").minimum_units - 1

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


def test_v34_validation_requires_bound_always_on_and_raw_contract_gates() -> None:
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


def test_contextual_cloze_rejects_cross_lemma_semantic_distractors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = {
        "certified_units": [
            {
                "allowed_forms": ["наметі"],
                "distinctness": {
                    "choice_bank": ["наметі", "наметом", "намети"],
                    "frame_family": "contextual-morphology-cloze.v3",
                },
            },
        ]
    }
    activity = {
        "payload": {
            "type": "cloze",
            "instruction": "Заповніть пропуск.",
            "text": "Друзі зупинилися у {1} біля тихої річки.",
            "blanks": [
                {
                    "id": 1,
                    "answer": "наметі",
                    "options": ["наметі", "наметом", "намети"],
                }
            ],
        },
        "answer_key": {"blanks": [{"id": 1, "answer": "наметі"}]},
    }
    lemmas = {
        "наметі": {"намет"},
        "наметом": {"намет"},
        "намети": {"намет"},
        "юшці": {"юшка"},
    }
    monkeypatch.setattr(prompt_pack_v3, "_lemma_set", lambda form, _db: lemmas[form])

    validate_distractor_adjacency(activity, kit)

    activity["payload"]["blanks"][0]["options"][-1] = "юшці"
    kit["certified_units"][0]["distinctness"]["choice_bank"][-1] = "юшці"
    with pytest.raises(PromptPackV3Error, match="cross-lemma semantic distractor"):
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


def _ukrainian_sample(word_count: int) -> str:
    words = "Я уважно читаю текст і послідовно пояснюю власну думку".split()
    return " ".join(words[index % len(words)] for index in range(word_count))


def test_short_writing_visible_constraints_and_bounded_teacher_sample_pass() -> None:
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
        "answer_key": {
            "guidance": "Перевірте виконання всіх умов. Зразок відповіді: " + _ukrainian_sample(60)
        },
    }
    _activity_gate(activity, kit)
    validate_visible_writing_constraints(activity, kit)
    validate_teacher_sample_constraints(activity, kit)
    validate_verbatim_answer_ban(activity, kit)
    validate_elicitation_shape(activity, kit)
    validate_exemplar_contamination(activity, kit)


@pytest.mark.parametrize(
    ("word_count", "passes"), ((59, False), (60, True), (80, True), (81, False))
)
def test_short_writing_teacher_sample_enforces_certified_boundaries(
    word_count: int, passes: bool
) -> None:
    kit = _context("short-writing")["type_kits"][0]
    activity = {
        "payload": {"type": "short-writing", "prompt": "Напишіть короткий текст."},
        "answer_key": {"guidance": "Зразок відповіді: " + _ukrainian_sample(word_count)},
    }

    if passes:
        validate_teacher_sample_constraints(activity, kit)
    else:
        with pytest.raises(RuleNamedRejection) as rejection:
            validate_teacher_sample_constraints(activity, kit)
        assert rejection.value.rule_key == "teacher_sample_constraints"
        assert rejection.value.suffix == "short_writing_word_count"


def test_retained_56_word_short_writing_failure_is_rejected() -> None:
    """Redacted reproduction of the observed 56-word answer for a 60–80 word task."""
    kit = _context("short-writing")["type_kits"][0]
    activity = {
        "payload": {"type": "short-writing", "prompt": "Напишіть короткий текст."},
        "answer_key": {"guidance": "Зразок відповіді: " + _ukrainian_sample(56)},
    }

    with pytest.raises(
        RuleNamedRejection,
        match=r"teacher_sample_constraints: short_writing_word_count",
    ):
        validate_teacher_sample_constraints(activity, kit)


def test_short_writing_punctuation_cannot_pad_a_short_teacher_sample() -> None:
    kit = _context("short-writing")["type_kits"][0]
    activity = {
        "payload": {"type": "short-writing", "prompt": "Напишіть короткий текст."},
        "answer_key": {"guidance": "Зразок відповіді: " + _ukrainian_sample(59) + " ---"},
    }

    with pytest.raises(
        RuleNamedRejection,
        match=r"teacher_sample_constraints: short_writing_word_count",
    ):
        validate_teacher_sample_constraints(activity, kit)


@pytest.mark.parametrize(
    "guidance",
    (
        "Перевірте виконання умов.",
        "Зразок відповіді:",
        "Зразок відповіді: Один. Зразок відповіді: Два.",
    ),
)
def test_short_writing_missing_empty_or_duplicate_teacher_sample_fails(guidance: str) -> None:
    kit = _context("short-writing")["type_kits"][0]
    activity = {
        "payload": {"type": "short-writing", "prompt": "Напишіть короткий текст."},
        "answer_key": {"guidance": guidance},
    }

    with pytest.raises(RuleNamedRejection) as rejection:
        validate_teacher_sample_constraints(activity, kit)
    assert rejection.value.rule_key == "teacher_sample_constraints"
    assert rejection.value.suffix == "short_writing_sample_shape"


def _text_question_activity(kit: dict) -> dict:
    units = kit["certified_units"]
    return {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": [f"Що повідомляє фрагмент {index}?" for index in range(1, len(units) + 1)],
        },
        "answer_key": {
            "guidance": "\n".join(
                f"{index}. Зразок відповіді: {unit['rendering_surface']}"
                for index, unit in enumerate(units, start=1)
            )
        },
    }


def test_text_question_review_inputs_use_only_matching_host_certified_evidence() -> None:
    kit = _context("text-questions")["type_kits"][0]
    first_surface = kit["certified_units"][0]["rendering_surface"]
    certified_segment = first_surface.split(",", maxsplit=1)[0]
    kit["certified_units"][0]["distinctness"]["regeneration_evidence_segments"] = [
        certified_segment
    ]
    activity = _text_question_activity(kit)
    activity["answer_key"]["evidence_quote"] = "Підроблений доказ від генератора."
    frozen_activity = deepcopy(activity)

    validate_teacher_sample_constraints(activity, kit)
    inputs = teacher_sample_review_inputs(activity, kit)

    assert len(inputs) == len(kit["certified_units"])
    assert inputs[0]["evidence_segments"] == [certified_segment]
    assert inputs[1]["evidence_segments"] == [kit["certified_units"][1]["rendering_surface"]]
    assert all("evidence_quote" not in row for row in inputs)
    assert "Підроблений доказ" not in repr(inputs)
    assert activity == frozen_activity
    assert set(activity) == {"payload", "answer_key"}
    assert isinstance(activity["answer_key"]["guidance"], str)


def test_text_question_malformed_host_evidence_segments_fail_closed() -> None:
    kit = _context("text-questions")["type_kits"][0]
    kit["certified_units"][0]["distinctness"]["regeneration_evidence_segments"] = [
        "Рядок, якого немає в сертифікованому фрагменті."
    ]
    activity = _text_question_activity(kit)
    record = {
        "slot_id": kit["slot_id"],
        "type": kit["type"],
        "activity": activity,
        "serialized_units": [{"unit_id": unit_id} for unit_id in kit["scheduled_unit_ids"]],
    }

    with pytest.raises(RuleNamedRejection) as rejection:
        validate_slot_deterministic_gates(
            record,
            kit,
            deterministic_gates=(validate_teacher_sample_constraints,),
        )
    assert rejection.value.rule_key == "teacher_sample_constraints"
    assert rejection.value.suffix is None


@pytest.mark.parametrize(
    "guidance_transform",
    (
        lambda lines: "\n".join(lines[:-1]),
        lambda lines: "\n".join(reversed(lines)),
        lambda lines: "\n".join([lines[0].replace("1.", "перший."), *lines[1:]]),
    ),
    ids=("missing", "misordered", "malformed"),
)
def test_text_question_missing_misordered_or_malformed_samples_fail_closed(
    guidance_transform: Callable[[list[str]], str],
) -> None:
    kit = _context("text-questions")["type_kits"][0]
    activity = _text_question_activity(kit)
    lines = activity["answer_key"]["guidance"].splitlines()
    activity["answer_key"]["guidance"] = guidance_transform(lines)

    with pytest.raises(RuleNamedRejection) as rejection:
        validate_teacher_sample_constraints(activity, kit)
    assert rejection.value.rule_key == "teacher_sample_constraints"
    assert rejection.value.suffix == "text_question_sample_shape"


def test_text_question_duplicate_samples_report_each_item_locally() -> None:
    kit = _context("text-questions")["type_kits"][0]
    activity = _text_question_activity(kit)
    duplicate = kit["certified_units"][0]["rendering_surface"]
    activity["answer_key"]["guidance"] = "\n".join(
        f"{index}. Зразок відповіді: {duplicate}"
        for index in range(1, len(kit["certified_units"]) + 1)
    )

    with pytest.raises(RuleNamedRejectionGroup) as rejection:
        validate_teacher_sample_constraints(activity, kit)
    assert tuple(item.rule_key for item in rejection.value.rejections) == (
        "teacher_sample_constraints",
    ) * len(kit["certified_units"])
    assert tuple(item.suffix for item in rejection.value.rejections) == tuple(
        f"duplicate_sample:item={index}" for index in range(len(kit["certified_units"]))
    )


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


def test_generic_short_writing_rejects_linguistic_jargon_as_the_topic() -> None:
    kit = _context("short-writing")["type_kits"][0]
    target_forms = [
        fragment for unit in kit["certified_units"] for fragment in unit["allowed_forms"]
    ]
    activity = {
        "payload": {
            "type": "short-writing",
            "prompt": "Напишіть про лему й морфологію: " + ", ".join(target_forms) + ".",
        },
        "answer_key": {"guidance": "Перевірте виконання умов."},
    }

    with pytest.raises(PromptPackV3Error, match="linguistic jargon"):
        validate_visible_writing_constraints(activity, kit)


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
        "comprehension": "Що розвиває читання?",
        "explanation_inference": "Як можна пояснити користь читання?",
        "anchored_application": "Як можна застосувати читання?",
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


def test_definition_question_uses_the_poliahati_content_frame(monkeypatch) -> None:
    original_matches = prompt_pack_v3._vesum_matches
    known = {
        "сенс": [{"lemma": "сенс", "pos": "noun"}],
        "гри": [{"lemma": "гра", "pos": "noun"}],
        "полягає": [{"lemma": "полягати", "pos": "verb"}],
        "кожен": [{"lemma": "кожний", "pos": "adj"}],
        "прагне": [{"lemma": "прагнути", "pos": "verb"}],
        "перемоги": [{"lemma": "перемога", "pos": "noun"}],
    }

    def vesum_matches(word, db_path):
        normalized = word.casefold()
        if normalized in known:
            return known[normalized]
        return original_matches(word, db_path)

    monkeypatch.setattr(prompt_pack_v3, "_vesum_matches", vesum_matches)
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "explanation_inference"
    )
    unit["rendering_surface"] = "Сенс гри полягає в тому, що кожен прагне перемоги."
    unit["distinctness"]["question_intent"] = "definition-content.v1"
    unit["distinctness"]["question_topic"] = {"surface": "Сенс", "lemma": "сенс"}
    unit["distinctness"]["question_frame"] = {
        "allowed_prefixes": ["У чому полягає"],
        "category": "explanation_inference",
        "intent": "definition-content.v1",
    }
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["У чому полягає сенс гри?"],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    validate_activity_purpose(activity, kit)


def test_explanation_question_rejects_a_bare_personal_pronoun_topic() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "explanation_inference"
    )
    unit["rendering_surface"] = "Іван читав, коли почався дощ."
    unit["distinctness"]["question_intent"] = "temporal-clause.v1"
    unit["distinctness"]["question_topic"] = {"surface": "читав", "lemma": "читати"}
    unit["distinctness"]["question_frame"] = {
        "allowed_prefixes": ["Коли"],
        "category": "explanation_inference",
        "intent": "temporal-clause.v1",
    }
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Коли він читав?"],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    with pytest.raises(PromptPackV3Error, match="unresolved source deixis"):
        validate_activity_purpose(activity, kit)


def test_licensed_cause_question_accepts_correct_ukrainian_case() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "explanation_inference"
    )
    unit["rendering_surface"] = "Регулярне читання розвиває мозок."
    unit["distinctness"]["question_intent"] = "licensed-vid-cause.v1"
    unit["distinctness"]["question_frame"]["allowed_prefixes"] = ["Через що"]
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Через що корисне читання?"],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    validate_activity_purpose(activity, kit)


def test_text_question_must_leave_source_content_for_the_answer() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "comprehension"
    )
    unit["rendering_surface"] = "Регулярне читання розвиває мозок людини."
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Чи регулярне читання розвиває мозок людини?"],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    with pytest.raises(PromptPackV3Error, match="consumes its source answer"):
        validate_activity_purpose(activity, kit)


def test_text_question_accepts_natural_context_for_a_one_word_answer() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "comprehension"
    )
    unit["rendering_surface"] = "Регулярне читання розвиває мозок."
    unit["distinctness"]["question_topic"] = {
        "token_id": "s-1:t-1",
        "surface": "читання",
        "lemma": "читання",
    }
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Що розвиває регулярне читання?"],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    validate_activity_purpose(activity, kit)


@pytest.mark.parametrize(
    ("rendering_surface", "question", "topic_surface", "topic_lemma"),
    (
        (
            "При створенні гра була орієнтована на молодіжну аудиторію.",
            "Яку аудиторію обрали для гри?",
            "гра",
            "гра",
        ),
        (
            "Вниз западалися боки гори у глибокі чорні ізвори.",
            "Як описано боки гори?",
            "боки",
            "бік",
        ),
    ),
)
def test_text_question_overlap_counts_each_ambiguous_surface_once(
    rendering_surface: str,
    question: str,
    topic_surface: str,
    topic_lemma: str,
    monkeypatch,
) -> None:
    """VESUM ambiguity must not manufacture extra answer words."""
    original_matches = prompt_pack_v3._vesum_matches
    ambiguous = {
        "при": [
            {"lemma": "перти", "pos": "verb"},
            {"lemma": "при", "pos": "prep"},
        ],
        "створенні": [{"lemma": "створення", "pos": "noun"}],
        "гра": [{"lemma": "гра", "pos": "noun"}],
        "гру": [{"lemma": "гра", "pos": "noun"}],
        "гри": [{"lemma": "гра", "pos": "noun"}],
        "була": [{"lemma": "бути", "pos": "verb"}],
        "орієнтована": [{"lemma": "орієнтувати", "pos": "verb"}],
        "молодіжну": [{"lemma": "молодіжний", "pos": "adj"}],
        "аудиторію": [{"lemma": "аудиторія", "pos": "noun"}],
        "боки": [{"lemma": "бік", "pos": "noun"}],
        "гори": [
            {"lemma": "гора", "pos": "noun"},
            {"lemma": "горіти", "pos": "verb"},
        ],
        "западалися": [{"lemma": "западатися", "pos": "verb"}],
        "глибокі": [{"lemma": "глибокий", "pos": "adj"}],
        "чорні": [{"lemma": "чорний", "pos": "adj"}],
        "ізвори": [{"lemma": "ізвір", "pos": "noun"}],
    }

    def vesum_matches(word, db_path):
        normalized = word.casefold()
        if normalized in ambiguous:
            return ambiguous[normalized]
        return original_matches(word, db_path)

    monkeypatch.setattr(prompt_pack_v3, "_vesum_matches", vesum_matches)
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "comprehension"
    )
    unit["rendering_surface"] = rendering_surface
    unit["distinctness"]["question_topic"] = {
        "token_id": "s-1:t-1",
        "surface": topic_surface,
        "lemma": topic_lemma,
    }
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": [question],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    validate_activity_purpose(activity, kit)


def test_temporal_preposition_does_not_consume_a_source_answer_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary phrase ``за кілька хвилин`` leaves the event to recover."""
    analyses = {
        "за": [
            {"lemma": "за", "pos": "adv"},
            {"lemma": "за", "pos": "part"},
            {"lemma": "за", "pos": "prep"},
        ],
        "кілька": [{"lemma": "кілька", "pos": "noun"}],
        "хвилин": [{"lemma": "хвилина", "pos": "noun"}],
        "станеться": [{"lemma": "статися", "pos": "verb"}],
        "сонце": [{"lemma": "сонце", "pos": "noun"}],
        "розжене": [{"lemma": "розігнати", "pos": "verb"}],
        "туман": [{"lemma": "туман", "pos": "noun"}],
        "мандрівники": [{"lemma": "мандрівник", "pos": "noun"}],
        "замружаться": [{"lemma": "замружитися", "pos": "verb"}],
    }
    monkeypatch.setattr(
        prompt_pack_v3,
        "_vesum_matches",
        lambda word, _db_path: analyses.get(word.casefold(), []),
    )
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "comprehension"
    )
    unit["rendering_surface"] = "За кілька хвилин сонце розжене туман, і мандрівники замружаться."
    unit["distinctness"]["question_topic"] = {
        "token_id": "s-1:t-3",
        "surface": "хвилин",
        "lemma": "хвилина",
    }
    unit["distinctness"]["question_frame"] = {"allowed_prefixes": ["Що"]}
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Що станеться за кілька хвилин?"],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    validate_activity_purpose(activity, kit)


def test_anchored_application_accepts_natural_learner_experience_cue() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Українці читають книги про розвиток мозку щодня."
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["З вашого досвіду, як вам допомагають книги?"],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    validate_activity_purpose(activity, kit)


def test_anchored_application_accepts_an_explicit_source_noun_topic() -> None:
    """A short source noun topic leaves the source proposition for the answer."""
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Регулярне читання розвиває мозок."
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Як можна застосувати читання?"],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    validate_activity_purpose(activity, kit)


def test_application_block_requires_one_text_supported_interpretive_question() -> None:
    kit = _context("text-questions")["type_kits"][0]
    units = [
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    ][:2]
    assert len(units) == 2
    for unit in units:
        unit["rendering_surface"] = "Українці читають книги перед подорожжю."
        unit["distinctness"]["question_topic"] = {
            "surface": "книги",
            "lemma": "книга",
        }
    kit["certified_units"] = units
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": [
                "З вашого досвіду, як вам допомагають книги?",
                "Як можна застосувати ідею про книги у подорожі?",
            ],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    with pytest.raises(PromptPackV3Error, match="evidence-based interpretive"):
        validate_activity_purpose(activity, kit)

    activity["payload"]["items"][1] = "На вашу думку, яка деталь опису книги важлива?"
    validate_activity_purpose(activity, kit)


def test_application_question_accepts_a_grounded_reference_to_the_visible_description() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Регулярне читання розвиває мозок."
    unit["distinctness"]["question_topic"] = {
        "surface": "читання",
        "lemma": "читання",
    }
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["На вашу думку, як читання впливає на мозок у цьому описі?"],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    validate_activity_purpose(activity, kit)


def test_application_block_repair_receives_the_specific_evidence_code() -> None:
    kit = _context("text-questions")["type_kits"][0]
    units = [
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    ][:2]
    assert len(units) == 2
    for unit in units:
        unit["rendering_surface"] = "Українці читають книги перед подорожжю."
        unit["distinctness"]["question_topic"] = {
            "surface": "книги",
            "lemma": "книга",
        }
    kit["certified_units"] = units
    record = {
        "activity": {
            "payload": {
                "type": "text-questions",
                "instruction": "Дайте відповідь.",
                "items": [
                    "З вашого досвіду, як вам допомагають книги?",
                    "Як можна застосувати ідею про книги у подорожі?",
                ],
            },
            "answer_key": {"guidance": "Обґрунтуйте відповідь."},
        }
    }

    with pytest.raises(RuleNamedRejection) as rejected:
        validate_slot_deterministic_gates(
            record,
            kit,
            deterministic_gates=(validate_activity_purpose,),
        )

    assert rejected.value.suffix == "evidence_based_application_missing"


def test_application_question_must_not_reduce_to_yes_no() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Українці читають книги перед подорожжю."
    unit["distinctness"]["question_topic"] = {"surface": "книги", "lemma": "книга"}
    unit["distinctness"]["question_frame"]["allowed_prefixes"].append("Чи доводилося вам")
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Чи доводилося вам обирати книги для подорожі?"],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    with pytest.raises(PromptPackV3Error, match="yes-no"):
        validate_activity_purpose(activity, kit)


def test_anchored_application_rejects_recall_disguised_as_a_situation() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Оригінальну гру переклали українською мовою."
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["У якій реальній ситуації оригінальну гру переклали українською мовою?"],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    with pytest.raises(PromptPackV3Error, match="certified question category"):
        validate_activity_purpose(activity, kit)


def test_anchored_application_rejects_an_experience_cue_wrapper() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "anchored_application"
    )
    unit["rendering_surface"] = "Добра душа допомагає людям у скруті."
    unit["distinctness"]["question_topic"] = {"surface": "душа", "lemma": "душа"}
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["З вашого досвіду, як діє добра душа?"],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    with pytest.raises(PromptPackV3Error, match="does not center a learner application"):
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
                "З вашого досвіду, як ви відмовлялися від квартири "
                "через те, що вона красива, але дорога?"
            ],
        },
        "answer_key": {"guidance": "Обґрунтуйте відповідь."},
    }

    with pytest.raises(PromptPackV3Error, match="turns a contrast into one causal reason"):
        validate_activity_purpose(activity, kit)


@pytest.mark.parametrize(
    "question",
    (
        "До якого наслідку відбувається читання?",
        "Що відбувається одночасно з читанням?",
    ),
)
def test_text_questions_reject_removed_unsupported_relation_prompts(question: str) -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "explanation_inference"
    )
    unit["rendering_surface"] = "Регулярне читання розвиває мозок."
    unit["distinctness"]["question_intent"] = "explicit-causal"
    unit["distinctness"]["question_prefixes"] = [question.split(" читання", 1)[0]]
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": [question],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    with pytest.raises(PromptPackV3Error, match="question category"):
        validate_activity_purpose(activity, kit)


def test_degree_comprehension_question_must_keep_its_certified_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyses = {
        "старша": [{"lemma": "старий", "tags": "adj:f:nom:compc", "pos": "adj"}],
        "сестра": [{"lemma": "сестра", "tags": "noun:f:nom", "pos": "noun"}],
        "читає": [{"lemma": "читати", "tags": "verb:imperf:pres", "pos": "verb"}],
        "книжку": [{"lemma": "книжка", "tags": "noun:f:acc", "pos": "noun"}],
        "щовечора": [{"lemma": "щовечора", "tags": "adv", "pos": "adv"}],
        "повідомляє": [{"lemma": "повідомляти", "tags": "verb:imperf:pres", "pos": "verb"}],
        "уривок": [{"lemma": "уривок", "tags": "noun:m:nom", "pos": "noun"}],
    }
    monkeypatch.setattr(
        prompt_pack_v3,
        "_vesum_matches",
        lambda word, _db_path: analyses.get(word.casefold(), []),
    )
    monkeypatch.setattr(
        prompt_pack_v3,
        "_catalog_positive_lemma",
        lambda word: "старий" if word.casefold() == "старша" else None,
    )
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "comprehension"
    )
    unit["rendering_surface"] = "Старша сестра читає книжку щовечора."
    unit["distinctness"]["focus_alignment"] = "anchor-comprehension"
    unit["distinctness"]["question_topic"] = {"surface": "книжку", "lemma": "книжка"}
    unit["distinctness"]["question_frame"] = {"allowed_prefixes": ["Яку"]}
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Яку книжку обрали?"],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    with pytest.raises(PromptPackV3Error, match="omits its certified comparison"):
        validate_activity_purpose(activity, kit)

    unit["distinctness"].pop("focus_alignment")
    validate_activity_purpose(activity, kit)


def test_frequency_comprehension_question_must_keep_its_certified_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyses = {
        "як": [{"lemma": "як", "pos": "adv", "tags": "adv"}],
        "тепер": [{"lemma": "тепер", "pos": "adv", "tags": "adv"}],
        "уже": [{"lemma": "уже", "pos": "adv", "tags": "adv"}],
        "хати": [{"lemma": "хата", "pos": "noun", "tags": "noun:p:v_naz"}],
        "попадалися": [{"lemma": "попадатися", "pos": "verb", "tags": "verb:imperf:past:p"}],
        "рідше": [{"lemma": "рідше", "pos": "adv", "tags": "adv:compc"}],
        "часто": [{"lemma": "часто", "pos": "adv", "tags": "adv"}],
    }
    monkeypatch.setattr(
        prompt_pack_v3,
        "_vesum_matches",
        lambda word, _db_path: analyses.get(word.casefold(), []),
    )
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "comprehension"
    )
    unit["rendering_surface"] = "Тепер уже хати попадалися рідше."
    unit["distinctness"]["question_topic"] = {"surface": "хати", "lemma": "хата"}
    unit["distinctness"]["question_frame"] = {"allowed_prefixes": ["Як"]}
    kit["certified_units"] = [unit]
    activity = {
        "payload": {
            "type": "text-questions",
            "instruction": "Дайте відповідь.",
            "items": ["Як попадалися хати?"],
        },
        "answer_key": {"guidance": "Відповідайте за текстом."},
    }

    with pytest.raises(PromptPackV3Error, match="omits its certified comparison"):
        validate_activity_purpose(activity, kit)

    activity["payload"]["items"] = ["Як часто попадалися хати?"]
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
    marker = "утворіть потрібні форми від прикметників «малий», «світлий», «теплий», «близький»"
    unit = kit["certified_units"][0]
    unit["allowed_forms"] = [marker, "від 80 до 110 слів"]
    unit["rendering_surface"] = anchor_inventory_v3.DEGREE_WRITING_SCENARIO
    unit["distinctness"]["attribute_warrants"] = dict(anchor_inventory_v3.DEGREE_WRITING_WARRANTS)
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
        ("Що ми дізнаємося про читання?", "generic_metadiscourse"),
        ("Куди веде подорож?", "source_lemma_overlap_missing"),
        (
            "Що регулярне читання щодня розвиває у мозку дорослої людини вдома?",
            "answer_restatement",
        ),
        ("Що розвиває читання цих книжок?", "unresolved_reference"),
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


@pytest.mark.parametrize(
    ("category", "question", "suffix"),
    (
        ("explanation_inference", "Навіщо потрібно тільки берегтися?", "generic_modal_relation"),
        (
            "anchored_application",
            "Чи доводилося вам вчинити щось подібне?",
            "vague_application_object",
        ),
        (
            "anchored_application",
            "Чи доводилося вам знати про події лише з оповідань?",
            "stative_experience",
        ),
        (
            "explanation_inference",
            "Від чого щулишся на кормі?",
            "source_person_import",
        ),
    ),
)
def test_observed_tetiana_question_defects_receive_item_local_repairs(
    category: str, question: str, suffix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if suffix == "source_person_import":
        original_matches = prompt_pack_v3._vesum_matches
        monkeypatch.setattr(
            prompt_pack_v3,
            "_vesum_matches",
            lambda word, db_path: (
                [{"lemma": "щулитися", "pos": "verb", "tags": "verb:rev:imperf:pres:s:2"}]
                if word.casefold() == "щулишся"
                else original_matches(word, db_path)
            ),
        )
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == category
    )
    if category == "explanation_inference":
        intent = "purpose-clause.v1" if question.startswith("Навіщо") else "licensed-vid-cause.v1"
        prefix = "Навіщо" if question.startswith("Навіщо") else "Від чого"
        unit["distinctness"]["question_intent"] = intent
        unit["distinctness"]["question_frame"] = {
            "allowed_prefixes": [prefix],
            "category": category,
            "intent": intent,
        }
    elif question.startswith("Чи доводилося вам"):
        unit["distinctness"]["question_frame"]["allowed_prefixes"].append("Чи доводилося вам")
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


def test_missing_certified_question_topic_receives_an_item_local_repair_code() -> None:
    kit = _context("text-questions")["type_kits"][0]
    unit = next(
        unit
        for unit in kit["certified_units"]
        if unit["distinctness"]["question_category"] == "comprehension"
    )
    unit["rendering_surface"] = "Регулярне читання розвиває мозок."
    unit["distinctness"]["question_topic"] = {
        "surface": "читання",
        "lemma": "читання",
    }
    kit["certified_units"] = [unit]
    record = {
        "activity": {
            "payload": {
                "type": "text-questions",
                "instruction": "Дайте відповідь.",
                "items": ["Що розвиває мозок?"],
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
    assert rejected.value.suffix == "certified_topic_missing:item=0"


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


def test_pack_v315_no_options_zero_mandate_and_enforces_varied_placement() -> None:
    """Pack v3.15 contains no options[0] mandate and enforces varied answer placement."""
    from pathlib import Path

    template_path = Path(__file__).parent.parent / "prompts" / "gemma-phase-pack.v3.15.md"
    content = template_path.read_text(encoding="utf-8")
    assert "first option (`options[0]`)" not in content
    assert "options[0]" not in content
    assert "vary the correct-option position" in content

    context = _context("cloze")
    kit = context["type_kits"][0]
    units = kit["certified_units"][:3]
    blanks = []
    key_blanks = []
    for index, unit in enumerate(units, start=1):
        answer = unit["allowed_forms"][0]
        options = list(unit["distinctness"]["choice_bank"])
        options.remove(answer)
        options.insert(0, answer)
        blanks.append({"id": index, "answer": answer, "options": options})
        key_blanks.append({"id": index, "answer": answer})
    fixed_placement_activity = {
        "payload": {
            "type": "cloze",
            "instruction": "Заповніть пропуски.",
            "text": "У мене є ___, ___ та ___.",
            "blanks": blanks,
        },
        "answer_key": {"blanks": key_blanks},
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
