"""Honesty flags for non-anchor-derived options (Sol audit findings #3, #4).

Audit: «invented distractors / non-anchor vocabulary pass as grounded while the
honesty/warning flags stay silent» — external_options must warn, never mark:ok.
"""

from __future__ import annotations

from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.engine import fixtures, pipeline, schema
from hramatka.engine.gates import vesum as vesum_gate


def _non_pass_checks(ir) -> list[tuple[str, str, str]]:
    return [(c.gate, c.status, c.detail) for c in ir.gate_result.checks if c.status != "pass"]


def test_quiz_invented_distractors_require_review_not_clean():
    """Audit lesson0: MC distractors like «холодного молока» must not ship mark:ok."""
    quiz = fixtures._READY_CANDIDATES["quiz"](0)
    quiz["items"][0]["options"] = ["третина", "вправ", "задач"]

    ir = pipeline.gate_activity(quiz, fixtures.load_anchor(), {}, atlas_lookup={})

    assert ir.gate_result.status == schema.GATE_REVIEW, _non_pass_checks(ir)
    external = [c for c in ir.gate_result.checks if c.gate == "external_options"]
    assert external
    assert all(check.status == "warn" for check in external)


def test_cloze_word_bank_distractors_require_review():
    """Audit lesson0: cloze bank «столом/ковдрою/папером» must flag external_options."""
    cloze = next(activity for activity in fixtures.GOOD_ACTIVITIES if activity["type"] == "cloze")

    ir = pipeline.gate_activity(cloze, fixtures.load_anchor(), {}, atlas_lookup={})

    assert ir.gate_result.status == schema.GATE_REVIEW, _non_pass_checks(ir)
    assert any(
        check.gate == "external_options" and check.status == "warn"
        for check in ir.gate_result.checks
    )


def test_match_up_non_anchor_left_word_warns_when_evidence_is_anchor_literal():
    """Audit lesson3 (Борщ): left-words «власний/родина» not verbatim in anchor."""
    match_up = {
        "type": "match-up",
        "instruction": "З'єднай слово з опори з його значенням.",
        "pairs": [
            {
                "left": "власний",
                "right": "мозку",
                "evidence": "активізуються одразу 17 ділянок головного мозку",
            },
            {
                "left": "ділянок",
                "right": "головного",
                "evidence": "17 ділянок головного мозку",
            },
        ],
    }

    ir = pipeline.gate_activity(match_up, fixtures.load_anchor(), {}, atlas_lookup={})

    assert ir.gate_result.status == schema.GATE_REVIEW, _non_pass_checks(ir)
    left_checks = [c for c in ir.gate_result.checks if c.gate == "matchup_left"]
    assert any(check.status == "warn" for check in left_checks)


def test_match_up_non_anchor_synonym_right_requires_review():
    match_up = {
        "type": "match-up",
        "instruction": "З'єднай слово з опори з його значенням.",
        "pairs": [
            {
                "left": "ділянка",
                "right": "пристрій",
                "evidence": "17 ділянок головного мозку",
            },
            {
                "left": "головний",
                "right": "велике задоволення",
                "evidence": "ділянок головного мозку",
            },
        ],
    }

    ir = pipeline.gate_activity(match_up, fixtures.load_anchor(), {}, atlas_lookup={})

    assert ir.gate_result.status == schema.GATE_REVIEW, _non_pass_checks(ir)
    assert any(
        check.gate == "external_options" and check.status == "warn"
        for check in ir.gate_result.checks
    )


def test_engine_adapter_block_surfaces_external_options_on_review():
    """Wire check: review disposition must set mark:warn and provenance.external_options."""
    cloze = next(activity for activity in fixtures.GOOD_ACTIVITIES if activity["type"] == "cloze")
    ir = pipeline.gate_activity(cloze, fixtures.load_anchor(), {}, atlas_lookup={})
    assert ir.gate_result.status == schema.GATE_REVIEW, _non_pass_checks(ir)

    baker = object.__new__(EngineLessonBaker)
    block = baker._block(ir, slot=0, phase=1)

    assert block["mark"] == "warn"
    assert block["provenance"]["external_options"] is True


def test_vesum_external_tokens_detects_non_anchor_forms():
    anchor = "Замісіть мʼяке тісто й залиште його під рушником."
    external = vesum_gate.external_tokens("столом ковдрою", anchor)
    assert "столом" in external
    assert "ковдрою" in external
    assert "рушником" not in external
