"""Tests for schema.py — IR, evidence stripping, projection, b1 validation.

UA forms are anchor-verbatim / VESUM-verified (#M-4). No network.
"""

from __future__ import annotations

import pytest

from hramatka.engine import schema as S


def test_parse_raw_activity_strips_evidence_true_false():
    raw = {
        "type": "true-false",
        "instruction": "І",
        "items": [
            {"statement": "A", "correct": True, "evidence": "цитата A"},
            {"statement": "B", "correct": False, "evidence": "цитата B"},
        ],
    }
    clean, evidence, kit_anchors = S.parse_raw_activity(raw)
    # evidence removed from the projected item
    assert all("evidence" not in it for it in clean["items"])
    assert [(e.quote, e.locator) for e in evidence] == [
        ("цитата A", "items[0]"),
        ("цитата B", "items[1]"),
    ]
    assert kit_anchors == []


def test_parse_raw_activity_strips_top_level_cloze_evidence():
    raw = {
        "type": "cloze",
        "instruction": "І",
        "text": "... {gap} ...",
        "blanks": [{"id": 1, "answer": "мозку", "options": ["мозку", "серця"]}],
        "evidence": "речення-опора",
    }
    clean, evidence, kit_anchors = S.parse_raw_activity(raw)
    assert "evidence" not in clean
    assert [(e.quote, e.locator) for e in evidence] == [("речення-опора", "text")]
    assert kit_anchors == []


def test_project_to_b1_defensively_strips_and_validates():
    ir = S.HramatkaActivity(
        activity={
            "type": "match-up",
            "instruction": "І",
            "pairs": [
                {"left": "насолода", "right": "задоволення", "evidence": "leftover"},
                {"left": "хобі", "right": "захоплення"},
            ],
        }
    )
    projected = S.project_to_b1(ir)
    assert all("evidence" not in p for p in projected["pairs"])
    S.validate_b1(projected)  # must not raise


def test_validate_b1_rejects_projection_drift_correct_to_istrue():
    # correct -> isTrue is the classic renderer-prop drift; b1 schema is
    # additionalProperties:false + requires `correct`, so this must fail.
    drift = {
        "type": "true-false",
        "instruction": "І",
        "items": [{"statement": "A", "isTrue": True}],
    }
    with pytest.raises(S.B1ValidationError):
        S.validate_b1(drift)


def test_validate_b1_rejects_leftover_evidence_key():
    bad = {
        "type": "true-false",
        "instruction": "І",
        "items": [{"statement": "A", "correct": True, "evidence": "x"}],
    }
    with pytest.raises(S.B1ValidationError):
        S.validate_b1(bad)


def test_validate_b1_rejects_unknown_type():
    with pytest.raises(S.B1ValidationError):
        S.validate_b1({"type": "totally-not-a-type", "instruction": "І"})


def test_gate_result_status_tracks_worst_check():
    # Tri-state (Sol defect 1): clean -> review_required -> failed, monotonic,
    # with `passed` preserved as "ships" (clean OR review_required).
    gr = S.GateResult()
    assert gr.status == S.GATE_CLEAN and gr.passed is True
    gr.add("evidence_span", "pass", "ok")
    assert gr.status == S.GATE_CLEAN and gr.passed is True
    gr.add("evidence_span", "warn", "meh")
    assert gr.status == S.GATE_REVIEW and gr.passed is True  # warn -> ships, must review
    gr.add("evidence_span", "fail", "bad")
    assert gr.status == S.GATE_FAILED and gr.passed is False


def test_gate_result_as_dict_carries_tristate_status():
    gr = S.GateResult()
    gr.add("evidence_span", "warn", "teacher-confirm")
    d = gr.as_dict()
    assert d["status"] == S.GATE_REVIEW
    assert d["passed"] is True
