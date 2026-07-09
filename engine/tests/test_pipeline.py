"""Tests for pipeline.py — end-to-end with a MOCK generator (NO real Gemma).

Covers: full run over anchor #1, evidence-span drop of a hallucinated item,
cloze pre-gap span rule, numeral positive-probe passes the moat in-pipeline,
projection+schema validity of persisted lesson.b1.json, and fingerprint
idempotency (generator called once).
"""

from __future__ import annotations

import json

from engine import fixtures, pipeline, schema
from engine.generate import GeneratorUnavailable


def _anchor():
    return fixtures.load_anchor()


def test_full_pipeline_all_items_pass(tmp_path):
    res = pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    assert res.generation_error is None
    types = [ir.activity["type"] for ir in res.activities]
    assert types == ["true-false", "cloze", "match-up"]
    assert all(ir.gate_result.passed for ir in res.activities)
    assert len(res.lesson_b1) == 3


def test_persisted_lesson_b1_validates_against_schema(tmp_path):
    pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    b1 = json.loads((tmp_path / "out" / "lesson.b1.json").read_text(encoding="utf-8"))
    assert len(b1) == 3
    for item in b1:
        schema.validate_b1(item)  # every persisted item is schema-valid
        # evidence must never leak onto the b1 item
        assert "evidence" not in json.dumps(item)


def test_ir_file_carries_evidence_and_gates(tmp_path):
    pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    ir = json.loads((tmp_path / "out" / "lesson.ir.json").read_text(encoding="utf-8"))
    assert "activities" in ir and ir["fingerprint"]
    tf = ir["activities"][0]
    # evidence offsets were recomputed by the span gate (non-null on located quotes)
    assert any(e["char_start"] is not None for e in tf["evidence"])


def test_numeral_positive_probe_passes_moat_in_pipeline(tmp_path):
    res = pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    tf = res.activities[0]  # true-false, contains the "17 ділянок мозку" probe
    numeral_checks = [c for c in tf.gate_result.checks if c.gate == "numeral"]
    # the probe fired the numeral gate and did NOT fail it
    assert all(c.status != "fail" for c in numeral_checks)
    assert tf.gate_result.passed


def test_spelled_out_fraction_statement_survives_as_warn(tmp_path):
    # Regression for the real-Gemma false-fail: a true-false statement that
    # restates «2,5 рази» as «два з половиною рази» (correct Ukrainian) must
    # NOT be dropped — the numeral gate warns (variable government), and warn
    # does not fail the activity.
    statement_item = {
        "type": "true-false",
        "instruction": "Познач, чи правильні твердження за текстом.",
        "items": [
            {
                "statement": "Читання знижує ризик хвороби у два з половиною рази.",
                "correct": True,
                "explanation": "Число переказане словами (2,5 → два з половиною).",
                "evidence": "знижує в 2,5 рази ризик розвитку хвороби Альцгеймера",
            }
        ],
    }

    def gen(_p):
        return json.dumps({"activities": [statement_item]})

    res = pipeline.run(
        _anchor(), generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    tf = res.activities[0]
    numeral_checks = [c for c in tf.gate_result.checks if c.gate == "numeral"]
    assert any(
        c.status == "warn" and "fraction-variable-government" in c.detail
        for c in numeral_checks
    )
    assert all(c.status != "fail" for c in numeral_checks)
    assert tf.gate_result.passed  # warn keeps the item; it reaches lesson_b1
    assert len(res.lesson_b1) == 1


def test_hallucinated_item_dropped_by_evidence_span(tmp_path):
    res = pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator_with_hallucination,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    assert len(res.activities) == 4
    assert len(res.lesson_b1) == 3  # hallucinated one dropped (its only item failed)
    hallucinated = res.activities[-1]
    assert not hallucinated.gate_result.passed
    assert any(
        c.gate == "evidence_span" and c.status == "fail"
        for c in hallucinated.gate_result.checks
    )


# ---------------------------------------------------------------------------
# Per-item granularity (approved): a per-statement/per-pair FAIL drops only
# that item (carried in ir.flagged), not the whole activity. WARN items ship.
# ---------------------------------------------------------------------------
def _true_false_4good_1bad() -> dict:
    return {
        "type": "true-false",
        "instruction": "Познач правильні твердження за текстом.",
        "items": [
            {"statement": "Третина українців за рік не прочитує книжки.", "correct": True,
             "evidence": "Третина українців за рік не прочитує жодної книжки"},
            {"statement": "Дві третини щодня вмикають телевізор.", "correct": True,
             "evidence": "дві третини щодня знаходять час увімкнути телевізор"},
            {"statement": "Активізуються 17 ділянок мозку.", "correct": True,
             "evidence": "активізуються одразу 17 ділянок головного мозку"},
            {"statement": "Читання є одним з найскладніших завдань.", "correct": True,
             "evidence": "читання є одним з найскладніших завдань для мозку"},
            # HALLUCINATED — evidence quote absent from the anchor.
            {"statement": "У тексті йдеться про ранкову пробіжку.", "correct": True,
             "evidence": "щоденна ранкова пробіжка корисна для серця"},
        ],
    }


def test_partial_true_false_keeps_good_items_flags_bad(tmp_path):
    def gen(_p):
        return json.dumps({"activities": [_true_false_4good_1bad()]})

    res = pipeline.run(
        _anchor(), generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    tf = res.activities[0]
    # the activity SHIPS with only the 4 good items; the 1 bad is flagged
    assert tf.gate_result.passed
    assert len(tf.activity["items"]) == 4
    assert len(tf.flagged) == 1
    assert tf.flagged[0]["locator"] == "items[4]"
    assert tf.flagged[0]["reasons"][0]["gate"] == "evidence_span"
    # lesson_b1 carries the 4-item activity (not deleted, not shrunk to 0)
    assert len(res.lesson_b1) == 1
    assert len(res.lesson_b1[0]["items"]) == 4
    # persisted IR carries the flagged item for the review sheet
    ir_json = json.loads((tmp_path / "out" / "lesson.ir.json").read_text(encoding="utf-8"))
    assert ir_json["activities"][0]["flagged"][0]["locator"] == "items[4]"


def test_match_up_dropped_when_below_two_pairs(tmp_path):
    # one good pair + one that fails BOTH sides (absent evidence + fabricated
    # gloss) -> only 1 survives -> below match-up minItems=2 -> whole activity dropped.
    mu = {
        "type": "match-up",
        "instruction": "З'єднай слово з опори з його значенням.",
        "pairs": [
            {"left": "насолода", "right": "велике задоволення",
             "evidence": "насолоду від неспішного читання книжок"},
            {"left": "телевізор", "right": "фейкословоxx",
             "evidence": "немає такої цитати в опорі взагалі"},
        ],
    }

    def gen(_p):
        return json.dumps({"activities": [mu]})

    res = pipeline.run(
        _anchor(), generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    ir = res.activities[0]
    assert not ir.gate_result.passed
    assert any(c.gate == "partition" and c.status == "fail" for c in ir.gate_result.checks)
    assert res.lesson_b1 == []  # cannot ship a 1-pair match-up


def test_all_warn_activity_ships_every_item(tmp_path):
    # anchor01-style: the true-false has FALSE-statement warns + a numeral warn
    # but NO fails -> every item ships, nothing flagged.
    res = pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    tf = res.activities[0]
    assert tf.gate_result.passed
    assert tf.flagged == []
    assert len(tf.activity["items"]) == len(fixtures.GOOD_ACTIVITIES[0]["items"])
    # at least one warn is present (proving warns do NOT drop items)
    assert any(c.status == "warn" for c in tf.gate_result.checks)


def test_cloze_answer_must_be_in_source_and_options(tmp_path):
    # break the cloze: answer not present in the evidence sentence
    bad = json.loads(json.dumps(fixtures.GOOD_ACTIVITIES))  # deep copy
    bad_cloze = next(a for a in bad if a["type"] == "cloze")
    bad_cloze["blanks"][0]["answer"] = "неможливо"  # valid word, but not in evidence/options
    bad_cloze["blanks"][0]["options"] = ["неможливо", "вправ", "задач"]

    def gen(_p):
        return json.dumps({"activities": [bad_cloze]})

    res = pipeline.run(
        _anchor(), generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    cloze_ir = res.activities[0]
    assert not cloze_ir.gate_result.passed
    assert any(c.gate == "cloze_answer" and c.status == "fail" for c in cloze_ir.gate_result.checks)


def test_fingerprint_idempotency_generator_called_once(tmp_path):
    calls = {"n": 0}

    def counting(p):
        calls["n"] += 1
        return fixtures.mock_generator(p)

    r1 = pipeline.run(
        _anchor(), generator=counting, out_dir=tmp_path / "o", cache_dir=tmp_path / "c"
    )
    r2 = pipeline.run(
        _anchor(), generator=counting, out_dir=tmp_path / "o", cache_dir=tmp_path / "c"
    )
    assert r1.fingerprint == r2.fingerprint
    assert calls["n"] == 1  # second run served from cache


def test_generator_unavailable_does_not_crash_pipeline(tmp_path):
    def unavailable(_p):
        raise GeneratorUnavailable("simulated routing guard")

    res = pipeline.run(
        _anchor(),
        generator=unavailable,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        use_cache=False,
    )
    assert res.generation_error is not None
    assert "GeneratorUnavailable" in res.generation_error
    assert res.lesson_b1 == []
    # artifacts still written (empty lesson) — pipeline did not crash
    assert (tmp_path / "out" / "lesson.b1.json").exists()
