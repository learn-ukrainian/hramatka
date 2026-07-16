"""Wave-0 typed candidate-bank acceptance tests (all deterministic/offline)."""

from __future__ import annotations

import json

import pytest

from hramatka.contracts import PILOT_ACTIVITY_TYPES
from hramatka.engine import fixtures, pipeline, registry, schema
from hramatka.engine.generate import generate, generate_baseline_v1


def _project(raw: dict) -> dict:
    return schema.project_to_b1(schema.HramatkaActivity(activity=raw))


def test_registry_migrates_all_wave_1b_activity_types():
    assert tuple(registry.ACTIVITY_REGISTRY) == PILOT_ACTIVITY_TYPES
    wave_1b_examples = [
        {
            "type": "quiz",
            "instruction": "Обери правильну відповідь.",
            "items": [
                {
                    "question": "Що активізується?",
                    "options": ["ділянки", "книжки", "телевізор"],
                    "correct": 0,
                    "evidence": "активізуються одразу 17 ділянок головного мозку",
                }
            ],
        },
        {
            "type": "mark-the-words",
            "instruction": "Познач усі дієслова.",
            "text": "Під час читання активізуються одразу 17 ділянок головного мозку.",
            "target_words": ["активізуються"],
            "criteria": "pos=verb",
            "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
        },
        {
            "type": "error-correction",
            "instruction": "Виправ помилку.",
            "items": [
                {
                    "sentence": "Під час читання активізуються одразу 17 ділянки головного мозку.",
                    "error": "ділянки",
                    "correction": "ділянок",
                    "options": ["ділянки", "ділянок", "книжки"],
                    "explanation": "Після 17 потрібна форма родового множини.",
                    "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
                }
            ],
        },
        {
            "type": "fill-in",
            "instruction": "Обери правильну форму.",
            "items": [
                {
                    "sentence": "На думку вчених, читання є одним з найскладніших ____ для мозку.",
                    "answer": "завдань",
                    "options": ["завдань", "вправ", "задач", "питань"],
                    "evidence": (
                        "На думку вчених, читання є одним з найскладніших завдань для мозку."
                    ),
                }
            ],
        },
        {
            "type": "text-questions",
            "instruction": "Обговоріть запитання за текстом.",
            "source_ref": "Текст-опора",
            "items": [
                {
                    "question": "Що активізується під час читання?",
                    "model_answer": "17 ділянок головного мозку.",
                    "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
                },
                {
                    "question": "Що знижує ризик розвитку хвороби Альцгеймера?",
                    "evidence": (
                        "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера."
                    ),
                },
            ],
            "teacher_guidance": "Приймайте змістовні відповіді учнів.",
        },
        {
            "type": "short-writing",
            "instruction": "Напиши короткий текст.",
            "prompt": "Напиши три речення про читання своїми словами.",
            "source_ref": "Текст-опора",
            "word_count_guidance": "3 речення (30–40 слів)",
            "model_answer": "Читання корисне для мозку.",
            "rubric_hint": "Є три речення і зв'язок з опорою.",
            "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку.",
        },
    ]
    for raw in [*fixtures.GOOD_ACTIVITIES, *wave_1b_examples]:
        entry = registry.ACTIVITY_REGISTRY[raw["type"]]
        assert entry.prompt_version.startswith("extractive-v5:")
        assert entry.assessment_mode in {"auto_gradable", "teacher_assessed"}
        assert entry.gate_chain and entry.gate_version
        assert entry.minimum_survivors >= 1 and entry.item_budget >= 1
        assert entry.ttt_phases
        assert entry.raw_validator(raw) == []
        assert entry.evidence_locator(raw)
        assert entry.gate and entry.evidence_answer_pairs
        projected = entry.public_projector(schema.HramatkaActivity(activity=raw))
        schema.validate_b1(projected)

    assert registry.ACTIVITY_REGISTRY["text-questions"].assessment_mode == "teacher_assessed"
    assert registry.ACTIVITY_REGISTRY["short-writing"].assessment_mode == "teacher_assessed"


def test_default_extractive_types_are_registry_derived_and_non_puzzle():
    assert pipeline.EXTRACTIVE_TYPES == tuple(registry.ACTIVITY_REGISTRY)
    assert not registry.ACTIVITY_REGISTRY["quiz"].is_puzzle
    assert not registry.ACTIVITY_REGISTRY["mark-the-words"].is_puzzle


def test_extractive_v1_baseline_and_registry_generation_are_equivalent():
    anchor = fixtures.load_anchor()
    baseline = generate_baseline_v1(anchor, generator=fixtures.mock_generator)
    typed = generate(anchor, generator=fixtures.mock_generator)

    # The same one-shot prompt and parse path preserves every legacy candidate.
    assert typed == baseline
    assert [_project(raw) for raw in typed] == [_project(raw) for raw in baseline]


def test_baseline_pipeline_and_candidate_bank_preserve_gated_public_content(
    tmp_path, active_matchup_vocabulary_bundle
):
    # Vocabulary bundle: the base offline one lacks «книга»/«непевність», which
    # would salvage the match-up to a 2-pair board and trip the #54 floor.
    anchor = fixtures.load_anchor()
    baseline = pipeline.run_baseline_v1(
        anchor,
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "baseline",
        cache_dir=tmp_path / "baseline-cache",
        measurement_only=True,
    )
    candidate_bank = pipeline.run(
        anchor,
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "candidate-bank",
        cache_dir=tmp_path / "candidate-cache",
    )

    assert baseline.lesson_b1 == [
        candidate.activity
        for candidate in candidate_bank.activities
        if candidate.gate_result.status != schema.DISPOSITION_REJECTED
    ]
    assert [item["type"] for item in baseline.lesson_b1] == [
        "true-false",
        "cloze",
        "match-up",
    ]
    assert candidate_bank.lesson_b1 == []


def test_baseline_requires_explicit_measurement_only_guard():
    with pytest.raises(TypeError, match="measurement_only"):
        pipeline.run_baseline_v1(fixtures.load_anchor())
    with pytest.raises(ValueError, match="measurement_only=True"):
        pipeline.run_baseline_v1(fixtures.load_anchor(), measurement_only=False)  # type: ignore[arg-type]


def test_registration_contract_fake_type_needs_only_registry_entry(tmp_path, monkeypatch):
    """A temporary `quiz` entry reaches every generic stage without engine edits."""
    calls = {"raw_validator": 0, "gate": 0, "evidence_answer_pairs": 0}

    def raw_validator(raw: object) -> list[str]:
        calls["raw_validator"] += 1
        if not isinstance(raw, dict) or raw.get("type") != "quiz":
            return ["quiz candidate has the wrong type"]
        items = raw.get("items")
        if not isinstance(items, list) or not items:
            return ["quiz candidate needs items"]
        item = items[0]
        if not isinstance(item, dict) or not isinstance(item.get("evidence"), str):
            return ["quiz candidate needs item evidence"]
        if raw.get("contract_outcome") not in {"ready", "review", "rejected"}:
            return ["quiz candidate needs a contract outcome"]
        return []

    def gate(
        activity: dict,
        _evidence: list[schema.Evidence],
        _anchor: str,
        gate_result: schema.GateResult,
        _atlas_lookup: dict | None,
    ) -> None:
        calls["gate"] += 1
        status = {"ready": "pass", "review": "warn", "rejected": "fail"}[
            activity["contract_outcome"]
        ]
        gate_result.add("contract_probe", status, "registry-owned test gate", locator="items[0]")

    def evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
        calls["evidence_answer_pairs"] += 1
        return [(ir.evidence[0].quote, str(ir.activity["items"][0]["correct"]))]

    def project_quiz(ir: schema.HramatkaActivity) -> dict:
        activity = dict(ir.activity)
        activity.pop("contract_outcome")
        return schema.project_to_b1(schema.HramatkaActivity(activity=activity))

    fake_entry = registry.ActivityRegistryEntry(
        activity_type="quiz",
        prompt_builder=registry.build_extractive_v1_prompt,
        prompt_version="test.registry-contract.v1",
        raw_schema_version="test.quiz.raw.v1",
        raw_validator=raw_validator,
        evidence_locator=lambda raw: tuple(f"items[{index}]" for index in range(len(raw["items"]))),
        gate=gate,
        evidence_answer_pairs=evidence_answer_pairs,
        assessment_mode="auto_gradable",
        gate_chain="test.quiz.v1",
        gate_version="test.quiz.gates.v1",
        partition_key="items",
        minimum_survivors=3,
        ttt_phases=(1,),
        item_budget=1,
        is_puzzle=False,
        public_projector=project_quiz,
    )
    monkeypatch.setitem(registry.ACTIVITY_REGISTRY, "quiz", fake_entry)

    def candidate(outcome: str) -> dict:
        anchor_sentences = [
            "Київ є столицею України.",
            "Львів має старий центр.",
            "Одеса лежить біля моря.",
        ]
        return {
            "type": "quiz",
            "instruction": "Вибери правильну відповідь.",
            "contract_outcome": outcome,
            "items": [
                {
                    "question": f"Запитання {item}?",
                    "options": ["Київ", "Львів", "Одеса"],
                    "correct": 0,
                    "evidence": anchor_sentences[item],
                }
                for item in range(3)
            ],
        }

    result = pipeline.run(
        (
            "Київ є столицею України. "
            "Львів має старий центр. "
            "Одеса лежить біля моря."
        ),
        types=["quiz"],
        generator=lambda _prompt: json.dumps(
            {"activities": [candidate("ready"), candidate("review"), candidate("rejected")]},
            ensure_ascii=False,
        ),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert calls["raw_validator"] >= 3
    assert calls["gate"] == 3
    assert calls["evidence_answer_pairs"] >= 1
    assert [ir.gate_result.status for ir in result.activities] == [
        schema.DISPOSITION_READY,
        schema.DISPOSITION_REVIEW,
        schema.DISPOSITION_REJECTED,
    ]
    assert [activity["type"] for activity in result.lesson_b1] == ["quiz"]


def test_false_statement_is_review_required_and_never_auto_projected(tmp_path):
    result = pipeline.run(
        fixtures.load_anchor(),
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    true_false = next(ir for ir in result.activities if ir.activity["type"] == "true-false")

    assert true_false.gate_result.status == schema.DISPOSITION_REVIEW
    assert any(
        check.gate == "false_statement" and check.status == "warn"
        for check in true_false.gate_result.checks
    )
    assert true_false in result.review_required
    assert true_false.activity not in result.lesson_b1

    persisted = json.loads((tmp_path / "out" / "lesson.ir.json").read_text(encoding="utf-8"))
    assert true_false.candidate_id in persisted["dispositions"]["review_required"]
    assert true_false.candidate_id not in persisted["dispositions"]["selected"]


def test_rejected_candidate_retains_raw_content_and_gate_reason(tmp_path):
    def generator(_prompt: str) -> str:
        return json.dumps({"activities": [fixtures.HALLUCINATED_ACTIVITY]}, ensure_ascii=False)

    result = pipeline.run(
        fixtures.load_anchor(),
        generator=generator,
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    assert result.ready == [] and result.review_required == []
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.raw_candidate == fixtures.HALLUCINATED_ACTIVITY
    assert any(
        check.gate == "evidence_span" and check.status == "fail"
        for check in rejected.gate_result.checks
    )
    assert result.lesson_b1 == []


def test_raw_contract_rejection_is_retained_before_gates(tmp_path):
    invalid = json.loads(json.dumps(fixtures.GOOD_ACTIVITIES[0], ensure_ascii=False))
    invalid["items"][0].pop("evidence")

    result = pipeline.run(
        fixtures.load_anchor(),
        generator=lambda _prompt: json.dumps({"activities": [invalid]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    rejected = result.rejected[0]
    assert rejected.raw_candidate == invalid
    assert rejected.gate_result.checks[0].gate == "raw_contract"
    assert rejected.gate_result.status == schema.DISPOSITION_REJECTED


def test_malformed_cloze_is_rejected_before_lexical_grounding_can_crash(tmp_path):
    invalid = json.loads(json.dumps(fixtures.GOOD_ACTIVITIES[1], ensure_ascii=False))
    invalid["blanks"] = "not-a-list"

    result = pipeline.run(
        fixtures.load_anchor(),
        generator=lambda _prompt: json.dumps({"activities": [invalid]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    assert len(result.rejected) == 1
    assert result.rejected[0].gate_result.checks[0].gate == "raw_contract"


def test_unexpected_and_non_object_outputs_are_rejected_not_silently_dropped(tmp_path):
    unexpected = {"type": "quiz", "instruction": "Несподіваний тип"}
    valid = next(activity for activity in fixtures.GOOD_ACTIVITIES if activity["type"] == "cloze")

    result = pipeline.run(
        fixtures.load_anchor(),
        types=["cloze"],
        generator=lambda _prompt: json.dumps(
            {"activities": [42, unexpected, valid]}, ensure_ascii=False
        ),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )
    assert len(result.activities) == 3
    assert [candidate.raw_candidate for candidate in result.rejected] == [
        {"value": 42},
        unexpected,
    ]
    assert all(
        candidate.gate_result.checks[0].gate == "raw_contract"
        for candidate in result.rejected
    )


def test_targeted_regeneration_requests_ready_pool_deficits(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict[str, int]]] = []
    evidence = [
        "Третина українців за рік не прочитує жодної книжки",
        "На думку вчених, читання є одним з найскладніших завдань для мозку",
    ]

    def fake_generate(_anchor, _level, types, *, counts, **_kwargs):
        calls.append((types, counts))
        index = len(calls) - 1
        return [
            {
                "type": "true-false",
                "instruction": "Познач правильне твердження.",
                "items": [
                    {
                        "statement": evidence[index],
                        "correct": True,
                        "evidence": evidence[index],
                    }
                ],
            }
        ]

    monkeypatch.setattr(pipeline, "generate", fake_generate)

    result = pipeline.run(
        fixtures.load_anchor(),
        types=["true-false"],
        count_plan={"true-false": 2},
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        use_cache=False,
        max_regeneration_attempts=1,
    )
    assert calls == [
        (["true-false"], {"true-false": 2}),
        (["true-false"], {"true-false": 1}),
    ]
    assert result.regeneration_attempts == 1
    assert len(result.ready) == 2


def test_snapshot_annotation_and_fingerprint_record_wave0_inputs():
    snap = pipeline.snapshot_anchor(fixtures.load_anchor())
    assert snap["sentences"] and snap["spans"] and snap["terms"]
    for span in snap["spans"]:
        assert snap["body_uk"][span["char_start"] : span["char_end"]]

    inputs = pipeline.fingerprint_inputs(
        anchor_hash="anchor",
        level="B1",
        pedagogy="ttt",
        phase="2",
        types=["true-false"],
        grounding_text="grounding",
        prompt_template="prompt",
        count_plan={"true-false": 2},
    )
    assert inputs["requested_type_count_plan"] == {"true-false": 2}
    assert inputs["registry"]["entries"][0]["prompt_version"]
    assert inputs["registry"]["entries"][0]["gate_version"]
    assert inputs["selector_policy"]["version"]
    assert "max_regeneration_attempts" in inputs
    assert inputs["model"] and inputs["grounding_digest"] and inputs["data_bundle"]
