"""Tests for pipeline.py — end-to-end with a MOCK generator (NO real Gemma).

Covers: full run over anchor #1, evidence-span drop of a hallucinated item,
cloze pre-gap span rule, numeral positive-probe passes the moat in-pipeline,
projection+schema validity of persisted lesson.b1.json, and fingerprint
idempotency (generator called once).
"""

from __future__ import annotations

import json

from hramatka.engine import fixtures, pipeline, registry, schema
from hramatka.engine.generate import GeneratorUnavailable


def _anchor():
    return fixtures.load_anchor()


def test_full_pipeline_partitions_ready_and_review_candidates(tmp_path):
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
    assert [ir.activity["type"] for ir in res.ready] == ["cloze"]
    assert [ir.activity["type"] for ir in res.review_required] == ["true-false", "match-up"]
    assert res.rejected == []
    # Review-required candidates are visible in IR but never auto-projected.
    assert [activity["type"] for activity in res.lesson_b1] == ["cloze"]


def test_persisted_lesson_b1_validates_against_schema(tmp_path):
    pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    b1 = json.loads((tmp_path / "out" / "lesson.b1.json").read_text(encoding="utf-8"))
    assert len(b1) == 1
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
    assert tf.gate_result.status == schema.DISPOSITION_REVIEW
    assert tf in res.review_required
    assert res.lesson_b1 == []


def test_hallucinated_item_dropped_by_evidence_span(tmp_path):
    res = pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator_with_hallucination,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    assert len(res.activities) == 4
    assert len(res.lesson_b1) == 1  # only the clean cloze is auto-selected
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
    assert [e.locator for e in tf.evidence] == ["items[0]", "items[1]", "items[2]", "items[3]"]
    # Salvage is review-required, not silently auto-shipped.
    assert tf.gate_result.status == schema.DISPOSITION_REVIEW
    assert res.lesson_b1 == []
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


# ---------------------------------------------------------------------------
# Wave 1A extractive types — fully deterministic gate coverage.
# ---------------------------------------------------------------------------
_MARK_TEXT = "Під час читання активізуються одразу 17 ділянок головного мозку."


def _ready_quiz() -> dict:
    return {
        "type": "quiz",
        "instruction": "Обери правильну відповідь за текстом.",
        "items": [
            {
                "question": "Що активізується під час читання?",
                "options": ["ділянок", "книжки", "телевізор"],
                "correct": 0,
                "evidence": "активізуються одразу 17 ділянок головного мозку",
            }
        ],
    }


def _ready_mark_the_words() -> dict:
    return {
        "type": "mark-the-words",
        "instruction": "Познач усі дієслова.",
        "text": _MARK_TEXT,
        "target_words": ["активізуються"],
        "criteria": "pos=verb",
        "evidence": _MARK_TEXT,
    }


def test_quiz_and_mark_the_words_reach_ready_and_mix_in_a_lesson(tmp_path):
    activities = [_ready_quiz(), _ready_mark_the_words()]
    result = pipeline.run(
        _anchor(),
        types=["quiz", "mark-the-words"],
        count_plan={"quiz": 1, "mark-the-words": 1},
        generator=lambda _prompt: json.dumps({"activities": activities}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert [(ir.activity["type"], ir.gate_result.status) for ir in result.activities] == [
        ("quiz", schema.DISPOSITION_READY),
        ("mark-the-words", schema.DISPOSITION_READY),
    ]
    assert [activity["type"] for activity in result.lesson_b1] == ["quiz", "mark-the-words"]


def test_quiz_rejects_an_ambiguous_key_with_two_evidence_supported_options(tmp_path):
    quiz = _ready_quiz()
    quiz["items"][0]["options"] = ["ділянок", "мозку", "книжки"]

    result = pipeline.run(
        _anchor(),
        types=["quiz"],
        generator=lambda _prompt: json.dumps({"activities": [quiz]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    rejected = result.rejected[0]
    assert rejected.gate_result.status == schema.DISPOSITION_REJECTED
    assert any(
        check.gate == "quiz_ambiguous_key" and check.status == "fail"
        for check in rejected.gate_result.checks
    )


def test_mark_the_words_rejects_an_incomplete_target_set(tmp_path):
    mark = _ready_mark_the_words()
    mark["instruction"] = "Познач усі іменники."
    mark["criteria"] = "pos=noun"
    mark["target_words"] = ["ділянок"]

    result = pipeline.run(
        _anchor(),
        types=["mark-the-words"],
        generator=lambda _prompt: json.dumps({"activities": [mark]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    rejected = result.rejected[0]
    assert rejected.gate_result.status == schema.DISPOSITION_REJECTED
    assert any(
        check.gate == "mark_words_completeness" and check.status == "fail"
        for check in rejected.gate_result.checks
    )


def test_mark_the_words_rejects_a_non_vesum_criterion(tmp_path):
    mark = _ready_mark_the_words()
    mark["criteria"] = "усі важливі слова"

    result = pipeline.run(
        _anchor(),
        types=["mark-the-words"],
        generator=lambda _prompt: json.dumps({"activities": [mark]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert any(
        check.gate == "mark_words_criterion" and check.status == "fail"
        for check in result.rejected[0].gate_result.checks
    )


# ---------------------------------------------------------------------------
# Wave 1B feature-rich types — deterministic injected bake coverage.
# ---------------------------------------------------------------------------
_NUMERAL_EVIDENCE = "Під час читання активізуються одразу 17 ділянок головного мозку."
_FILL_EVIDENCE = "На думку вчених, читання є одним з найскладніших завдань для мозку."


def _ready_error_correction() -> dict:
    return {
        "type": "error-correction",
        "instruction": "Виправ помилку.",
        "items": [
            {
                "sentence": "Під час читання активізуються одразу 17 ділянки головного мозку.",
                "error": "ділянки",
                "correction": "ділянок",
                "options": ["ділянки", "ділянок", "книжки"],
                "explanation": "Після 17 потрібна форма родового множини.",
                "evidence": _NUMERAL_EVIDENCE,
            }
        ],
    }


def _ready_fill_in() -> dict:
    return {
        "type": "fill-in",
        "instruction": "Обери правильну форму.",
        "items": [
            {
                "sentence": "На думку вчених, читання є одним з найскладніших ____ для мозку.",
                "answer": "завдань",
                "options": ["завдань", "вправ", "задач", "питань"],
                "explanation": "Вибери форму з речення опори.",
                "evidence": _FILL_EVIDENCE,
            }
        ],
    }


def _ready_text_questions() -> dict:
    return {
        "type": "text-questions",
        "instruction": "Обговоріть запитання за текстом.",
        "source_ref": "Текст-опора",
        "items": [
            {
                "question": "Що активізується під час читання?",
                "model_answer": "Під час читання активізуються 17 ділянок головного мозку.",
                "evidence": _NUMERAL_EVIDENCE,
            },
            {
                "question": "Що знижує ризик розвитку хвороби Альцгеймера?",
                "model_answer": "Регулярне читання.",
                "evidence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера."
                ),
            },
        ],
        "teacher_guidance": "Приймайте змістовні відповіді учнів.",
    }


def _ready_short_writing() -> dict:
    return {
        "type": "short-writing",
        "instruction": "Напиши короткий текст.",
        "prompt": "Напиши три речення про читання своїми словами.",
        "source_ref": "Текст-опора",
        "word_count_guidance": "3 речення (30–40 слів)",
        "model_answer": "Читання корисне для мозку.",
        "rubric_hint": "Є три речення і зв'язок з опорою.",
        "teacher_guidance": "Оцінюйте зміст і зв'язність.",
        "evidence": _FILL_EVIDENCE,
    }


def test_wave1b_types_reach_ready_in_an_injected_bake(tmp_path):
    activities = [
        _ready_error_correction(),
        _ready_fill_in(),
        _ready_text_questions(),
        _ready_short_writing(),
    ]
    types = [activity["type"] for activity in activities]
    result = pipeline.run(
        _anchor(),
        types=types,
        count_plan={activity_type: 1 for activity_type in types},
        generator=lambda _prompt: json.dumps({"activities": activities}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert [(ir.activity["type"], ir.gate_result.status) for ir in result.activities] == [
        ("error-correction", schema.DISPOSITION_READY),
        ("fill-in", schema.DISPOSITION_READY),
        ("text-questions", schema.DISPOSITION_READY),
        ("short-writing", schema.DISPOSITION_READY),
    ]
    assert sorted(activity["type"] for activity in result.lesson_b1) == sorted(types)
    assert all(schema.validate_b1(activity) is None for activity in result.lesson_b1)
    assert registry.ACTIVITY_REGISTRY["text-questions"].assessment_mode == "teacher_assessed"
    assert registry.ACTIVITY_REGISTRY["short-writing"].assessment_mode == "teacher_assessed"


def test_error_correction_rejects_a_non_restoring_correction(tmp_path):
    activity = _ready_error_correction()
    item = activity["items"][0]
    item["correction"] = "мозку"
    item["options"] = ["ділянки", "мозку", "книжки"]

    result = pipeline.run(
        _anchor(),
        types=["error-correction"],
        generator=lambda _prompt: json.dumps({"activities": [activity]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert any(
        check.gate == "error_correction_source" and check.status == "fail"
        for check in result.rejected[0].gate_result.checks
    )


def test_error_correction_with_a_non_vesum_error_option_reaches_ready(tmp_path):
    activity = _ready_error_correction()
    item = activity["items"][0]
    item["sentence"] = "Під час читання активізуються одразу 17 ділянокк головного мозку."
    item["error"] = "ділянокк"
    item["options"] = ["ділянокк", "ділянок", "книжки"]

    result = pipeline.run(
        _anchor(),
        types=["error-correction"],
        generator=lambda _prompt: json.dumps({"activities": [activity]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert [ir.gate_result.status for ir in result.activities] == [schema.DISPOSITION_READY]
    assert [activity["type"] for activity in result.lesson_b1] == ["error-correction"]
    assert not any(
        check.gate == "error_correction_vesum" and check.status == "fail"
        for check in result.activities[0].gate_result.checks
    )


def test_error_correction_rejects_a_non_vesum_distractor(tmp_path):
    activity = _ready_error_correction()
    activity["items"][0]["options"] = ["ділянки", "ділянок", "книжкк"]

    result = pipeline.run(
        _anchor(),
        types=["error-correction"],
        generator=lambda _prompt: json.dumps({"activities": [activity]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert any(
        check.gate == "error_correction_vesum" and check.status == "fail"
        for check in result.rejected[0].gate_result.checks
    )


def test_error_correction_rejects_a_valid_form_without_a_proven_target_rule(tmp_path):
    activity = _ready_error_correction()
    activity["items"] = [
        {
            "sentence": "На думку вчених, читання є одним з найскладніших завдань для речення.",
            "error": "речення",
            "correction": "мозку",
            "options": ["речення", "мозку", "книжки"],
            "explanation": "Перевірте словоформу.",
            "evidence": _FILL_EVIDENCE,
        }
    ]

    result = pipeline.run(
        _anchor(),
        types=["error-correction"],
        generator=lambda _prompt: json.dumps({"activities": [activity]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert any(
        check.gate == "error_correction_error" and check.status == "fail"
        for check in result.rejected[0].gate_result.checks
    )


def test_fill_in_rejects_a_distractor_of_the_wrong_vesum_class(tmp_path):
    activity = _ready_fill_in()
    activity["items"][0]["options"] = ["завдань", "вправ", "задач", "знижує"]

    result = pipeline.run(
        _anchor(),
        types=["fill-in"],
        generator=lambda _prompt: json.dumps({"activities": [activity]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert any(
        check.gate == "fill_in_pos" and check.status == "fail"
        for check in result.rejected[0].gate_result.checks
    )


def test_text_questions_reject_a_non_vesum_task_stem(tmp_path):
    activity = _ready_text_questions()
    activity["items"][0]["question"] = "Фейкословоxx читання?"

    result = pipeline.run(
        _anchor(),
        types=["text-questions"],
        generator=lambda _prompt: json.dumps({"activities": [activity]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert any(
        check.gate == "open_task_vesum" and check.status == "fail"
        for check in result.rejected[0].gate_result.checks
    )


def test_short_writing_rejects_a_non_vesum_task_stem(tmp_path):
    activity = _ready_short_writing()
    activity["prompt"] = "Фейкословоxx читання."

    result = pipeline.run(
        _anchor(),
        types=["short-writing"],
        generator=lambda _prompt: json.dumps({"activities": [activity]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert any(
        check.gate == "open_task_vesum" and check.status == "fail"
        for check in result.rejected[0].gate_result.checks
    )


def test_fingerprint_includes_gate_impl_digest(monkeypatch):
    # review-p46 nit 2: gate LOGIC is part of bake identity, so a gate-code
    # change reshuffles the fingerprint even with identical declared inputs.
    kw = dict(
        anchor_hash="h",
        level="B1",
        pedagogy="ttt",
        phase="1",
        types=["true-false"],
        grounding_text="g",
        prompt_template="p",
    )
    inputs = pipeline.fingerprint_inputs(**kw)
    assert isinstance(inputs["gate_impl_digest"], str) and len(inputs["gate_impl_digest"]) == 64

    base = pipeline.make_fingerprint(**kw)
    monkeypatch.setattr(pipeline, "_gate_impl_digest", lambda: "0" * 64)
    assert pipeline.make_fingerprint(**kw) != base


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
