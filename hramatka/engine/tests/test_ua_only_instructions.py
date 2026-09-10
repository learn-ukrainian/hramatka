"""#54 item 1 end-to-end: schema vocabulary must not survive to a teacher.

`test_schema_tokens.py` unit-tests the detector. This file proves the fix is
load-bearing rather than advisory — the gate actually removes content — and that
the generation prompt stops inviting the leak in the first place.
"""

from __future__ import annotations

from hramatka.engine import fixtures, pipeline, registry, schema
from hramatka.engine.gates import schema_tokens
from hramatka.engine.prompts import load_extractive_template


def _anchor() -> str:
    return fixtures.load_anchor()


def _true_false(items: list[dict]) -> dict:
    return {
        "type": "true-false",
        "instruction": "Позначте, чи правильні твердження за текстом.",
        "items": items,
    }


_CLEAN_ITEM = {
    "statement": "Третина українців за рік не прочитує жодної книжки.",
    "correct": True,
    "explanation": "Прямо сказано в тексті.",
    "evidence": "Третина українців за рік не прочитує жодної книжки",
}


def test_leaked_schema_token_in_a_statement_drops_that_item():
    """The per-item salvage path removes the leak; clean siblings still ship."""
    leaked = {
        "statement": "Під час читання активізуються 17 ділянок мозку. (True)",
        "correct": True,
        "explanation": "Число переказане з правильним керуванням.",
        "evidence": "активізуються одразу 17 ділянок головного мозку",
    }

    ir = pipeline.gate_activity(
        _true_false([_CLEAN_ITEM, leaked]), _anchor(), {}, atlas_lookup={}
    )

    assert [f["locator"] for f in ir.flagged] == ["items[1]"]
    assert [item["statement"] for item in ir.activity["items"]] == [_CLEAN_ITEM["statement"]]
    assert any(
        check.gate == "schema_tokens" and check.status == "fail" and check.locator == "items[1]"
        for check in ir.gate_result.checks
    )


def test_leak_in_an_explanation_is_caught_too():
    """`explanation` is teacher-visible prose the instruction bank never touches."""
    leaked = dict(_CLEAN_ITEM, explanation="Це твердження є true за текстом.")

    ir = pipeline.gate_activity(_true_false([leaked]), _anchor(), {}, atlas_lookup={})

    assert any(
        check.gate == "schema_tokens" and check.status == "fail"
        for check in ir.gate_result.checks
    )


def test_clean_ukrainian_items_raise_no_schema_token_check():
    ir = pipeline.gate_activity(_true_false([_CLEAN_ITEM]), _anchor(), {}, atlas_lookup={})

    assert not any(check.gate == "schema_tokens" for check in ir.gate_result.checks)


def test_cloze_gap_markers_do_not_trip_the_gate():
    """Regression: `{gap}`/`{gapN}` are our own placeholders, not model leakage."""
    cloze = next(a for a in fixtures.GOOD_ACTIVITIES if a["type"] == "cloze")

    ir = pipeline.gate_activity(cloze, _anchor(), {}, atlas_lookup={})

    assert not any(check.gate == "schema_tokens" for check in ir.gate_result.checks)


def test_no_pilot_activity_type_is_left_without_a_teacher_visible_check():
    """Sibling sweep: every type either gates its prose or has none to gate.

    mark-the-words is the sole exemption and is exempt by construction — its
    `text` is a verbatim anchor span, `target_words` is VESUM-derived from that
    span, and `criteria` is a machine contract, so it carries no model-authored
    prose beyond the bank-owned instruction.
    """
    gated = {"true-false", "quiz", "cloze", "match-up", "error-correction", "fill-in",
             "text-questions", "short-writing"}
    assert gated | {"mark-the-words"} == set(registry.ACTIVITY_REGISTRY)


# --- the prompt must stop inviting the leak -------------------------------


def _prompt() -> str:
    return registry.build_extractive_v1_prompt(
        _anchor(), "B1", ["true-false", "match-up"], "", counts={"true-false": 1, "match-up": 1}
    )


def test_prompt_forbids_schema_vocabulary_in_teacher_visible_text():
    prompt = _prompt()

    assert "МАШИННІ ключі" in prompt
    assert "Не пиши «правильними (True) чи неправильними (False)»" in prompt


def test_prompt_prefers_pravylnyi_over_pravdyvyi_for_true_false():
    """#M-13 / #54: «правдиві» was the wording both engines then Anglicised."""
    prompt = _prompt()

    assert "частина правильні, частина неправильні" in prompt
    # The only «правдиві» left is the rule telling the model NOT to use it.
    assert prompt.count("правдив") == 1
    assert "(не «правдиві»/«хибні»)" in prompt


def test_prompt_gives_a_concrete_ua_instruction_instead_of_a_placeholder():
    """The mechanism: «instruction»:«…» next to «correct»:true invited narration.

    quiz already carried a concrete instruction in the exemplar and never
    leaked; true-false/cloze/match-up carried «…» and did.
    """
    template = load_extractive_template()

    for instruction in (
        "Позначте, чи правильні твердження за текстом.",
        "Заповніть пропуск словом із тексту.",
        "З'єднайте слово з опори з його значенням.",
    ):
        assert f'"instruction":"{instruction}"' in template


def test_no_exemplar_instruction_in_the_prompt_leaks_schema_vocabulary():
    for line in load_extractive_template().splitlines():
        if '"instruction":"' not in line:
            continue
        instruction = line.split('"instruction":"', 1)[1].split('"', 1)[0]
        assert schema_tokens.find_schema_tokens(instruction, "") == [], instruction


def test_prompt_requires_lemma_form_match_up_left_sides():
    """#54 item 2, stated with the bake-off's own inflected examples."""
    prompt = _prompt()

    assert "СЛОВНИКОВІЙ (початковій) формі" in prompt
    assert "«привітний», не «привітною»" in prompt
    assert "«розкачати», не «розкачайте»" in prompt


def test_prompt_states_the_min_pairs_floor_so_the_model_self_censors():
    """#54 item 3 — cheaper to not generate a 2-pair board than to drop one."""
    assert "менше ніж 3" in _prompt()


def test_projection_still_repairs_a_leaked_instruction_end_to_end():
    """The bank, not this gate, owns `instruction` — pinned against regression."""
    ir = pipeline.gate_activity(
        {
            "type": "true-false",
            "instruction": "Визначте, чи є твердження правдивими (True), чи хибними (False).",
            "items": [_CLEAN_ITEM],
        },
        _anchor(),
        {},
        atlas_lookup={},
    )

    assert ir.gate_result.status != schema.DISPOSITION_REJECTED
    assert ir.activity["instruction"] == "Позначте, чи правильні твердження за текстом."
    assert schema_tokens.find_schema_tokens(ir.activity["instruction"], "") == []
