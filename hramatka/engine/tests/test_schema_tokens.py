"""Bake-off 2026-07-10 (#54 item 1): schema vocabulary in teacher-visible text.

Both engines narrated the response schema into the learner-facing instruction —
«правдивими (True), чи хибними (False)». The VESUM token gate cannot see this:
its tokenizer is Cyrillic-only, so Latin is invisible rather than invalid.
"""

from __future__ import annotations

import pytest

from hramatka.engine import instruction_bank, schema
from hramatka.engine.gates import schema_tokens

# Verbatim delivered instructions from hramatka/bakeoff/2026-07-10/*/lesson.json.
_BAKEOFF_LEAKS = [
    "Визначте, чи є твердження правдивими (True), чи хибними (False), спираючись на текст.",
    "Позначте, чи є твердження правильними (True) чи неправильними (False) на основі тексту.",
    "Прочитайте текст і визначте, чи є твердження правильними (true) чи хибними (false).",
    "Визначте, чи є твердження правильним (True) або хибним (False) відповідно до тексту.",
]


@pytest.mark.parametrize("instruction", _BAKEOFF_LEAKS)
def test_every_bakeoff_instruction_leak_is_detected(instruction):
    assert schema_tokens.find_schema_tokens(instruction, "") == ["false", "true"]


def test_clean_ukrainian_instruction_has_no_leak():
    clean = "Позначте, чи правильні твердження за текстом."
    assert schema_tokens.find_schema_tokens(clean, "") == []


def test_cloze_gap_marker_is_not_leakage():
    """`{gap}` is our own required placeholder, not the model narrating schema."""
    text = "Власниця, пані Оксана, виявилася {gap} жінкою."
    assert schema_tokens.find_schema_tokens(text, "") == []
    assert schema_tokens.find_schema_tokens("Ми {gap2} одразу. {{1}}", "") == []


def test_latin_present_in_the_anchor_is_a_quotation_not_a_leak():
    """A schema word in the teacher's own source text is quoted, never blamed."""
    anchor = "Стаття мала заголовок «True Detective» українською."
    assert schema_tokens.find_schema_tokens("Серіал «True Detective» цікавий.", anchor) == []
    # …but the model still cannot introduce a *different* schema word.
    assert schema_tokens.find_schema_tokens("Позначте false твердження.", anchor) == ["false"]


def test_check_strings_fails_with_an_actionable_ukrainian_remedy():
    verdict = schema_tokens.check_strings(["Твердження правдиве (True)."], "")
    assert verdict["status"] == "fail"
    assert "«true»" in verdict["detail"]
    assert "правильно" in verdict["detail"]


def test_check_strings_ignores_non_string_and_empty_payloads():
    assert schema_tokens.check_strings([None, "", "   ", 7], "")["status"] == "pass"


def test_mark_the_words_criterion_is_safe_if_ever_submitted():
    """`criteria` values are Latin by design and must not read as leakage."""
    for criterion in ("pos=verb", "pos=noun;case=gen", "pos=adj;case=loc"):
        assert schema_tokens.find_schema_tokens(criterion, "") == []


def test_type_value_tokenises_which_is_why_callers_never_submit_it():
    """The `type` value «true-false» IS literally true+false.

    It is a machine discriminator, never rendered as task prose, so no gate
    caller passes it. This pins the reason rather than pretending otherwise.
    """
    assert schema_tokens.find_schema_tokens("true-false", "") == ["false", "true"]


def test_instruction_bank_repairs_the_leaked_instruction_at_projection():
    """Why this gate does not judge `instruction`: the bank already fixes it.

    Failing the activity would drop good items over a defect that
    deterministically self-heals, so the bank is the repair and this test pins
    that contract (regression guard for the #54 fix boundary).
    """
    ir = schema.HramatkaActivity(
        activity={
            "type": "true-false",
            "instruction": _BAKEOFF_LEAKS[0],
            "items": [{"statement": "Квартира має ліфт.", "correct": True}],
        }
    )
    projected = schema.project_to_b1(ir)
    assert projected["instruction"] == "Позначте, чи правильні твердження за текстом."
    assert schema_tokens.find_schema_tokens(projected["instruction"], "") == []


def test_no_canonical_bank_instruction_leaks_schema_vocabulary():
    for instruction in instruction_bank.CANONICAL_INSTRUCTIONS.values():
        assert schema_tokens.find_schema_tokens(instruction, "") == []
