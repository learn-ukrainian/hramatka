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


def test_full_pipeline_partitions_ready_and_review_candidates(
    tmp_path, active_matchup_vocabulary_bundle
):
    # The vocabulary bundle keeps this a test of the lesson: the base offline
    # bundle lacks «книга»/«непевність», which would salvage the match-up to a
    # 2-pair board and trip the #54 floor for a fixture reason.
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
    assert res.ready == []
    assert {ir.activity["type"] for ir in res.review_required} == {
        "true-false",
        "cloze",
        "match-up",
    }
    assert res.rejected == []
    # Honesty gates block auto-projection until the teacher acknowledges warnings.
    assert res.lesson_b1 == []


def test_persisted_lesson_b1_validates_against_schema(tmp_path):
    pipeline.run(
        _anchor(),
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    b1 = json.loads((tmp_path / "out" / "lesson.b1.json").read_text(encoding="utf-8"))
    assert len(b1) == 0


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
        c.status == "warn" and "fraction-variable-government" in c.detail for c in numeral_checks
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
    assert len(res.lesson_b1) == 0
    hallucinated = res.activities[-1]
    assert not hallucinated.gate_result.passed
    assert any(
        c.gate == "evidence_span" and c.status == "fail" for c in hallucinated.gate_result.checks
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
            {
                "statement": "Третина українців за рік не прочитує книжки.",
                "correct": True,
                "evidence": "Третина українців за рік не прочитує жодної книжки",
            },
            {
                "statement": "Дві третини щодня вмикають телевізор.",
                "correct": True,
                "evidence": "дві третини щодня знаходять час увімкнути телевізор",
            },
            {
                "statement": "Активізуються 17 ділянок мозку.",
                "correct": True,
                "evidence": "активізуються одразу 17 ділянок головного мозку",
            },
            {
                "statement": "Читання є одним з найскладніших завдань.",
                "correct": True,
                "evidence": "читання є одним з найскладніших завдань для мозку",
            },
            # HALLUCINATED — evidence quote absent from the anchor.
            {
                "statement": "У тексті йдеться про ранкову пробіжку.",
                "correct": True,
                "evidence": "щоденна ранкова пробіжка корисна для серця",
            },
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


def _match_up(pairs: list[dict]) -> dict:
    return {
        "type": "match-up",
        "instruction": "З'єднай слово з опори з його значенням.",
        "pairs": pairs,
    }


# Pairs that survive gating offline, plus one that fails BOTH sides (absent
# evidence + fabricated gloss) and is therefore salvaged out.
_GOOD_PAIR_A = {
    "left": "насолода",
    "right": "велике задоволення",
    "evidence": "насолоду від неспішного читання книжок",
}
_GOOD_PAIR_B = {
    "left": "телевізор",
    "right": "пристрій для перегляду передач",
    "evidence": "увімкнути телевізор",
}
_GOOD_PAIR_C = {
    "left": "ділянка",
    "right": "пристрій",
    "evidence": "17 ділянок головного мозку",
}
_FAILING_PAIR = {
    "left": "телевізор",
    "right": "фейкословоxx",
    "evidence": "немає такої цитати в опорі взагалі",
}


def test_match_up_dropped_when_below_min_pairs_floor(tmp_path):
    """#54 item 3: salvage must never deliver a 2-pair board.

    The bake-off shipped exactly this (rent×deepseek, 2 pairs) — with two pairs
    a learner gets the second for free once the first is placed.
    """
    mu = _match_up([_GOOD_PAIR_A, _GOOD_PAIR_B, _FAILING_PAIR])

    def gen(_p):
        return json.dumps({"activities": [mu]})

    res = pipeline.run(
        _anchor(), generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    ir = res.activities[0]
    assert ir.gate_result.status == schema.DISPOSITION_REJECTED
    partition = [c for c in ir.gate_result.checks if c.gate == "partition" and c.status == "fail"]
    assert partition and "requires >= 3" in partition[0].detail
    assert res.lesson_b1 == []
    assert res.review_required == []  # not merely downgraded — dropped


def test_match_up_with_three_surviving_pairs_is_reviewable_never_auto_included(tmp_path):
    """At the floor a salvaged board survives, but salvage still blocks accept."""
    mu = _match_up([_GOOD_PAIR_A, _GOOD_PAIR_B, _GOOD_PAIR_C, _FAILING_PAIR])

    def gen(_p):
        return json.dumps({"activities": [mu]})

    res = pipeline.run(
        _anchor(), generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    ir = res.activities[0]
    assert ir.gate_result.status == schema.DISPOSITION_REVIEW
    assert len(ir.activity["pairs"]) == 3
    assert [f["locator"] for f in ir.flagged] == ["pairs[3]"]
    assert res.ready == []  # salvaged -> teacher-confirm, never auto-included
    assert res.lesson_b1 == []


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
def _ready_quiz() -> dict:
    return fixtures._READY_CANDIDATES["quiz"](0)


def _ready_mark_the_words() -> dict:
    return fixtures._READY_CANDIDATES["mark-the-words"](0)


def _brain_region_quiz_item(*, options: list[str]) -> dict:
    return {
        "question": "Скільки ділянок мозку активізується під час читання?",
        "options": options,
        "correct": 0,
        "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
    }


def _first_activity_ir(result):
    return result.activities[0] if result.activities else result.rejected[0]


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
    quiz["items"].append(
        _brain_region_quiz_item(options=["ділянок", "мозку", "книжки"])
    )

    result = pipeline.run(
        _anchor(),
        types=["quiz"],
        generator=lambda _prompt: json.dumps({"activities": [quiz]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    rejected = _first_activity_ir(result)
    assert rejected.gate_result.status == schema.DISPOSITION_REVIEW
    assert any(
        check.gate == "quiz_ambiguous_key" and check.status == "fail"
        for check in rejected.gate_result.checks
    )


def test_quiz_accepts_substring_distractors(tmp_path):
    quiz = _ready_quiz()
    # "моз" is a proper substring of "мозку" but not a token in the evidence.
    # It should not count as supported, so the quiz passes.
    quiz["items"] = [
        _brain_region_quiz_item(options=["ділянок", "моз", "книжки"]),
        *quiz["items"][1:],
    ]

    result = pipeline.run(
        _anchor(),
        types=["quiz"],
        generator=lambda _prompt: json.dumps({"activities": [quiz]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert [activity["type"] for activity in result.lesson_b1] == ["quiz"]


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
    return fixtures._READY_CANDIDATES["error-correction"](0)


def _ready_fill_in() -> dict:
    return fixtures._READY_CANDIDATES["fill-in"](0)


def _ready_text_questions() -> dict:
    return fixtures._READY_CANDIDATES["text-questions"](0)


def _ready_short_writing() -> dict:
    return fixtures._READY_CANDIDATES["short-writing"](0)


def test_wave1b_types_reach_ready_in_an_injected_bake(tmp_path):
    activities = [
        fixtures._READY_CANDIDATES["error-correction"](2),
        fixtures._READY_CANDIDATES["fill-in"](1),
        _ready_text_questions(),
        fixtures._READY_CANDIDATES["short-writing"](1),
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
    assert sorted(activity["type"] for activity in result.lesson_b1) == sorted(
        ["error-correction", "fill-in", "short-writing"]
    )
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
        for check in _first_activity_ir(result).gate_result.checks
    )


def test_error_correction_with_a_non_vesum_error_option_reaches_ready(tmp_path):
    activity = _ready_error_correction()
    item = activity["items"][0]
    item["sentence"] = "Під час читання активізуються одразу 17 ділянокк головного мозку."
    item["options"] = ["ділянокк", "ділянок", "книжки"]
    item["error"] = "ділянокк"
    item["correction"] = "ділянок"
    item["evidence"] = "Під час читання активізуються одразу 17 ділянок головного мозку."

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
        for check in _first_activity_ir(result).gate_result.checks
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
    activity["items"][0]["answer"] = "завдань"
    activity["items"][0]["sentence"] = (
        "На думку вчених, читання є одним з найскладніших ____ для мозку."
    )
    activity["items"][0]["evidence"] = _FILL_EVIDENCE

    result = pipeline.run(
        _anchor(),
        types=["fill-in"],
        generator=lambda _prompt: json.dumps({"activities": [activity]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert any(
        check.gate == "fill_in_pos" and check.status == "fail"
        for check in _first_activity_ir(result).gate_result.checks
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


def test_parse_failures_persist_truncated_raw_output_in_phase_directory(tmp_path):
    rejected_json = '{"unexpected": true}'
    raw_outputs = iter([rejected_json, "x" * (64 * 1024 + 1), "still not JSON"])

    res = pipeline.run(
        _anchor(),
        generator=lambda _p: next(raw_outputs),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        use_cache=False,
    )

    assert res.generation_error is not None
    assert "GenerationUnparseable" in res.generation_error
    first_raw = tmp_path / "out" / "generation-raw-attempt1.txt"
    assert first_raw.read_text(encoding="utf-8") == rejected_json
    assert len((tmp_path / "out" / "generation-raw-attempt2.txt").read_bytes()) == 64 * 1024
    assert (
        (tmp_path / "out" / "generation-raw-attempt3.txt").read_text(encoding="utf-8")
        == "still not JSON"
    )


def test_no_cache_run_never_writes_the_cache_dir(tmp_path, monkeypatch):
    # Regression (pilot host 2026-07-13, PR #88 review): with use_cache=False
    # (the API baker), _run still mkdir'ed the cache path — on a deploy host
    # that is hramatka/engine/.cache inside the immutable release checkout
    # (EACCES before generation on a clean deploy). Point the default cache at
    # an unwritable location: the run must neither create it nor die.
    from hramatka.engine import paths as engine_paths

    ro_parent = tmp_path / "ro-parent"
    ro_parent.mkdir()
    blocked_cache = ro_parent / "cache"
    parent_mode = ro_parent.stat().st_mode
    ro_parent.chmod(0o555)
    monkeypatch.setattr(engine_paths, "CACHE_DIR", blocked_cache)
    monkeypatch.setattr(pipeline.paths, "CACHE_DIR", blocked_cache)
    try:
        res = pipeline.run(
            _anchor(),
            generator=fixtures.mock_generator,
            use_cache=False,
            out_dir=tmp_path / "out",
        )
    finally:
        ro_parent.chmod(parent_mode)
    assert res.generation_error is None
    assert not blocked_cache.exists()


def test_per_phase_deadline_wall_clock_bound(tmp_path, monkeypatch):
    import json
    import time

    # We want a slow generator that returns only a single cloze candidate, creating a deficit.
    # We will simulate a slow network call by sleeping 0.08 seconds per call.
    calls = {"count": 0}
    def slow_generator(_p):
        calls["count"] += 1
        time.sleep(0.08)
        return json.dumps({"activities": [fixtures.GOOD_ACTIVITIES[1]]}, ensure_ascii=False)

    # Scen 1: default path (fast/unset deadline) is unchanged.
    # It runs 2 regeneration attempts because we have a deficit.
    monkeypatch.delenv("HRAMATKA_PHASE_DEADLINE_SECONDS", raising=False)
    out_dir_default = tmp_path / "default"
    res_default = pipeline.run(
        _anchor(),
        generator=slow_generator,
        out_dir=out_dir_default,
        cache_dir=tmp_path / "cache_default",
        use_cache=False,
        count_plan={"cloze": 2, "match-up": 1},
        max_regeneration_attempts=2,
    )
    # The default path runs all 2 regeneration attempts (total 3 calls: 1 initial + 2 regen)
    assert res_default.regeneration_attempts == 2
    assert calls["count"] == 3

    # Scen 2: Hardened env parsing validation.
    # If the deadline env var is invalid (non-numeric, NaN, inf, or empty spaces), it should
    # gracefully fallback to the default (240.0s) and NOT hit the deadline, continuing to attempt 2.
    for invalid_val in ["invalid_deadline", "", "   ", "NaN", "inf", "-inf"]:
        monkeypatch.setenv("HRAMATKA_PHASE_DEADLINE_SECONDS", invalid_val)
        calls["count"] = 0
        out_dir_invalid = tmp_path / f"invalid_{invalid_val.replace(' ', '_')}"
        res_invalid = pipeline.run(
            _anchor(),
            generator=slow_generator,
            out_dir=out_dir_invalid,
            cache_dir=tmp_path / f"cache_invalid_{invalid_val.replace(' ', '_')}",
            use_cache=False,
            count_plan={"cloze": 2, "match-up": 1},
            max_regeneration_attempts=2,
        )
        assert res_invalid.regeneration_attempts == 2
        assert calls["count"] == 3

    # Scen 3: Deadline exceedance (soft deadline limits execution).
    # Setting deadline to 0.05 seconds. Since a single call takes 0.08s,
    # elapsed time (0.08s) will exceed the deadline (0.05s) after the first attempt.
    # So the regeneration loop must check the deadline, break, suppress regenerations,
    # and record the "phase_deadline_hit" event.
    monkeypatch.setenv("HRAMATKA_PHASE_DEADLINE_SECONDS", "0.05")
    calls["count"] = 0
    out_dir_deadline = tmp_path / "deadline"
    
    t_start = time.perf_counter()
    res_deadline = pipeline.run(
        _anchor(),
        generator=slow_generator,
        out_dir=out_dir_deadline,
        cache_dir=tmp_path / "cache_deadline",
        use_cache=False,
        count_plan={"cloze": 2, "match-up": 1},
        max_regeneration_attempts=2,
    )
    t_elapsed = time.perf_counter() - t_start

    # (a) Wall-clock bound: generous ceiling guards against a hung/extra provider call only;
    # deterministic suppression proof is calls["count"] == 1 below.
    assert t_elapsed < 2.0
    assert res_deadline.regeneration_attempts == 0
    assert calls["count"] == 1

    # The cloze candidate ships for review, not as an auto-selected clean item.
    assert len(res_deadline.ready) == 0
    assert len(res_deadline.review_required) == 1
    assert res_deadline.review_required[0].activity["type"] == "cloze"
    assert len(res_deadline.lesson_b1) == 0

    # (c) Event telemetry payload is correct:
    trace_path = out_dir_deadline / "trace.json"
    assert trace_path.exists()
    trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
    events = [ev for ev in trace_data if ev.get("event") == "phase_deadline_hit"]
    assert len(events) == 1
    event = events[0]
    assert event["phase"] == "unknown"
    assert isinstance(event["elapsed_s"], float)
    assert event["elapsed_s"] >= 0.08
    assert event["candidates_shipped"] == 0
    assert event["phase_deadline_s"] == 0.05
    assert event["regeneration_attempts_suppressed"] == 2


def test_cloze_blank_removal_drops_entire_activity(tmp_path):
    # Create a cloze activity with 2 blanks
    cloze = {
        "type": "cloze",
        "instruction": "Заповніть пропуски.",
        "text": "На думку вчених, читання є одним з найскладніших {gap} для {gap}.",
        "blanks": [
            {
                "id": 1,
                "answer": "завдань",
                "options": ["завдань", "вправ", "задач", "питань"],
            },
            {
                "id": 2,
                "answer": "мозку",
                "options": ["мозку", "серця"],
            }
        ],
        "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку"
    }

    # Run 1: both blanks are valid (answers are present in evidence)
    def gen_survivor(_p):
        return json.dumps({"activities": [cloze]})

    res_survivor = pipeline.run(
        _anchor(),
        generator=gen_survivor,
        out_dir=tmp_path / "out_survivor",
        cache_dir=tmp_path / "cache_survivor",
        use_cache=False
    )

    assert len(res_survivor.review_required) == 1
    assert res_survivor.review_required[0].gate_result.status == schema.GATE_REVIEW
    assert len(res_survivor.review_required[0].activity["blanks"]) == 2

    # Run 2: one blank fails gating (answer 'письмо' is not present in evidence)
    cloze_bad = json.loads(json.dumps(cloze))
    cloze_bad["blanks"][1]["answer"] = "письмо"  # 'письмо' not in evidence!

    def gen_bad(_p):
        return json.dumps({"activities": [cloze_bad]})

    res_bad = pipeline.run(
        _anchor(),
        generator=gen_bad,
        out_dir=tmp_path / "out_bad",
        cache_dir=tmp_path / "cache_bad",
        use_cache=False
    )

    # Cloze activity should be dropped to GATE_FAILED because it lost a blank
    assert len(res_bad.ready) == 0
    assert len(res_bad.activities) == 1
    assert not res_bad.activities[0].gate_result.passed
    assert res_bad.activities[0].gate_result.status == schema.GATE_FAILED
