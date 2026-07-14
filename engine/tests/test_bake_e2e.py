"""End-to-end bake: fixture anchor + FAKE generator -> schema-valid lu.lesson.v1.

Proves the full private path with NO network and NO public checkout:
  synthetic anchor -> engine pipeline (mock generator) -> gate chain ->
  projected lu.activity.v1 items -> EngineLessonBaker composes a lu.lesson.v1
  block template -> durable materialize -> validated against the VENDORED,
  digest-verified lu.lesson.v1 schema.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter

import pytest

from hramatka.api.baking import engine_adapter
from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.baking.port import BakeError
from hramatka.engine import content_density, fixtures, pipeline, schema
from hramatka.engine.fixtures import _bundle_with_matchup_vocabulary
from hramatka.sizing_policy import B1, phase_plan


def _anchor_snapshot() -> dict:
    return pipeline.snapshot_anchor(fixtures.load_anchor())


def _payload_meets_density(payload: dict, anchor: dict | None = None) -> bool:
    return content_density.meets_content_density(
        schema.HramatkaActivity(activity=payload),
        anchor,
    )


def _assert_composed_blocks_are_rich(blocks: list[dict], *, anchor: dict | None = None) -> None:
    assert blocks
    for block in blocks:
        assert _payload_meets_density(block["activity"]["payload"], anchor)


def test_e2e_bake_raises_when_candidate_bank_misses_lesson_floor(tmp_path):
    anchor = fixtures.load_anchor()
    baker = EngineLessonBaker(generator=fixtures.mock_generator, cache_dir=tmp_path / "cache")

    with pytest.raises(BakeError, match="minimum activity density"):
        baker.bake(anchor, duration=45, focus=None)


def test_e2e_bake_raises_on_sub_floor_production_shaped_anchor(tmp_path):
    """Regression for pilot lesson 0d7f8035: ~680-char anchors must not ship 3 blocks."""
    base = fixtures.load_anchor()
    anchor = (
        f"{base} "
        "Дослідники наголошують, що регулярне читання формує увагу, памʼять і емпатію. "
        "Учні, які читають щотижня, легше будують аргументи в дискусіях і письмі. "
        "Вчителі часто просять короткий уривок доповнити двома прикладами з власного досвіду."
    )
    snapshot = pipeline.snapshot_anchor(anchor)
    assert 640 <= snapshot["char_len"] <= 720
    assert not content_density.source_lacks_lesson_evidence(snapshot)

    pilot_types = {"short-writing", "match-up", "quiz"}
    type_counters: Counter[str] = Counter()

    def generator(prompt: str) -> str:
        activities = [
            activity
            for activity in fixtures.activities_for_prompt(prompt, type_counters)
            if activity["type"] in pilot_types
        ]
        return json.dumps({"activities": activities}, ensure_ascii=False)

    baker = EngineLessonBaker(
        generator=generator,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(BakeError, match="minimum activity density"):
        baker.bake(anchor, duration=45, focus=None)


def test_e2e_baker_fills_eight_blocks_with_whole_lesson_variety(tmp_path):
    requested_counts: list[dict[str, int]] = []
    generated_counts: Counter[str] = Counter()
    type_counters: Counter[str] = Counter()
    records_lock = threading.Lock()

    def generator(prompt: str) -> str:
        counts = {
            activity_type: int(count)
            for activity_type, count in re.findall(r"^- ([a-z-]+): (\d+)$", prompt, re.MULTILINE)
        }
        with records_lock:
            requested_counts.append(counts)
            activities = fixtures.activities_for_prompt(prompt, type_counters)
            generated_counts.update(activity["type"] for activity in activities)
        return json.dumps({"activities": activities}, ensure_ascii=False)

    baker = EngineLessonBaker(
        generator=generator,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    baked = baker.bake(fixtures.load_anchor(), duration=45, focus=None)

    expected_counts = engine_adapter._candidate_count_plan(phase_plan(B1, 45))
    # Each phase starts with exactly its derived request. A phase can make its
    # existing one safety regeneration request when a candidate is gated out.
    assert all(counts in requested_counts for counts in expected_counts.values())
    expected_generated = sum((Counter(counts) for counts in expected_counts.values()), Counter())
    assert generated_counts >= expected_generated
    blocks = baked["blocks"]
    _assert_composed_blocks_are_rich(blocks, anchor=_anchor_snapshot())
    assert 4 <= len(blocks) <= 8
    assert len({block["id"] for block in blocks}) == len(blocks)
    block_types = {block["type"] for block in blocks}
    assert "quiz" in block_types
    # Non-anchor match-up synonyms stay in the review tray (Sol audit #3/#4).
    assert any(
        entry.get("type") == "match-up"
        and "review-required" in entry.get("reason", "")
        for entry in baked["rejected"]
    )
    if len(blocks) == 8:
        assert [block["phase"] for block in blocks] == [1, 1, 1, 2, 2, 2, 2, 3]


def test_e2e_baker_runs_three_independent_phases_concurrently(tmp_path):
    plans = engine_adapter._candidate_count_plan(phase_plan(B1, 45))
    started = threading.Event()
    active = 0
    max_active = 0
    lock = threading.Lock()

    type_counters: Counter[str] = Counter()

    def generator(prompt: str) -> str:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 3:
                started.set()
        try:
            assert started.wait(timeout=0.5), "phase generation was serialized"
            time.sleep(0.08)
            with lock:
                activities = fixtures.activities_for_prompt(prompt, type_counters)
            return json.dumps({"activities": activities}, ensure_ascii=False)
        finally:
            with lock:
                active -= 1

    baker = EngineLessonBaker(
        generator=generator,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    baked = baker.bake(fixtures.load_anchor(), duration=45, focus=None)

    assert max_active == len(plans) == 3
    assert 4 <= len(baked["blocks"]) <= 8
    _assert_composed_blocks_are_rich(baked["blocks"], anchor=_anchor_snapshot())


def test_e2e_baker_round_robins_one_primary_per_bake(tmp_path):
    assigned: list[str] = []
    handled: list[str] = []
    lock = threading.Lock()

    class StubLoadBalancer:
        def for_bake(self):
            with lock:
                provider = ("google-ais", "openrouter")[len(assigned) % 2]
                assigned.append(provider)
            type_counters: Counter[str] = Counter()

            def generator(prompt: str) -> str:
                activities = fixtures.activities_for_prompt(prompt, type_counters)
                with lock:
                    handled.append(provider)
                return json.dumps({"activities": activities}, ensure_ascii=False)

            return generator

    baker = EngineLessonBaker(
        generator=StubLoadBalancer(),
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    for _ in range(4):
        baked = baker.bake(fixtures.load_anchor(), duration=45, focus=None)
        assert len(baked["blocks"]) >= 6
        _assert_composed_blocks_are_rich(baked["blocks"], anchor=_anchor_snapshot())

    assert assigned == ["google-ais", "openrouter", "google-ais", "openrouter"]
    assert set(handled) == {"google-ais", "openrouter"}
    # Extra regeneration rounds when review-only types (e.g. match-up) miss READY quota.
    assert Counter(handled) == Counter({"google-ais": 14, "openrouter": 14})


def test_e2e_baker_surfaces_shortfall_when_constrained_types_are_unavailable(tmp_path):
    """Optional phase-specific variety must never consume the 45-minute fill floor."""
    constrained = {"match-up", "mark-the-words"}

    type_counters: Counter[str] = Counter()

    def generator(prompt: str) -> str:
        activities = [
            activity
            for activity in fixtures.activities_for_prompt(prompt, type_counters)
            if activity["type"] not in constrained
        ]
        return json.dumps({"activities": activities}, ensure_ascii=False)

    baker = EngineLessonBaker(
        generator=generator,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    baked = baker.bake(fixtures.load_anchor(), duration=45, focus=None)
    assert len(baked["blocks"]) < 8
    assert any(entry["reason"].startswith("shortfall:") for entry in baked["rejected"])


def test_e2e_bake_raises_bakeerror_when_generator_unavailable(tmp_path):
    # A dead generator degrades to a safe, teacher-visible BakeError (no crash,
    # no anchor/engine text in the message).
    def dead(_prompt):
        from hramatka.engine.transport import GeneratorUnavailable

        raise GeneratorUnavailable("simulated")

    baker = EngineLessonBaker(generator=dead, cache_dir=tmp_path / "cache")
    with pytest.raises(BakeError) as exc:
        baker.bake(fixtures.load_anchor(), duration=45, focus=None)
    assert "generator is unavailable" in str(exc.value)


def _true_false_4good_1bad() -> dict:
    # 4 anchor-grounded items + 1 hallucinated (evidence absent) -> the bad item
    # is salvaged out, the 4 good siblings still ship.
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
            {"statement": "У тексті йдеться про ранкову пробіжку.", "correct": True,
             "evidence": "щоденна ранкова пробіжка корисна для серця"},
        ],
    }


def test_e2e_salvage_visibility_generated_equals_shipped_plus_flagged_plus_rejected(tmp_path):
    # Sol defect 3: partial bake results surface as shipped blocks + rejected[]
    # entries with gate-failed:<gate> reasons — never all-or-nothing, never
    # silent. One activity trips a single item (salvage), one fails wholly.
    from hramatka.api.baking.engine_adapter import bake_accounting, rejected_entries
    from hramatka.engine import pipeline

    cloze = next(a for a in fixtures.GOOD_ACTIVITIES if a["type"] == "cloze")

    def gen(_p):
        import json as _json

        return _json.dumps(
            {"activities": [cloze, _true_false_4good_1bad(), fixtures.HALLUCINATED_ACTIVITY]}
        )

    anchor = fixtures.load_anchor()
    result = pipeline.run(
        anchor, generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )

    # accounting is a strict partition of the generated activities
    acct = bake_accounting(result)
    assert acct == {"generated": 3, "shipped": 1, "flagged": 1, "rejected": 1}
    assert acct["generated"] == acct["shipped"] + acct["flagged"] + acct["rejected"]

    # the salvaged sibling still ships (4 of 5 items), not all-or-nothing
    partial = next(ir for ir in result.activities if ir.flagged)
    assert len(partial.activity["items"]) == 4

    # rejected[] discloses BOTH the dropped activity and the salvaged item, each
    # with a machine-readable gate-failed:<gate> reason
    rejected = rejected_entries(result.activities)
    assert len(rejected) == 2
    assert all(entry["reason"].startswith("gate-failed:") for entry in rejected)
    assert all(entry["type"] == "gate-failed" for entry in rejected)
    reasons = sorted(entry["reason"] for entry in rejected)
    assert reasons == ["gate-failed:evidence_span", "gate-failed:evidence_span"]

    # The dense duration adapter must not repeat the lone clean cloze into
    # eight slots. It returns a ready-only partial lesson with an explicit
    # shortfall instead of promoting the warning candidate to a visible block.
    baker = EngineLessonBaker(generator=gen, cache_dir=tmp_path / "cache-bake")
    with pytest.raises(BakeError, match="minimum activity density"):
        baker.bake(anchor, duration=45, focus=None)


def test_e2e_bake_wraps_data_bundle_errors_as_bakeerror(tmp_path, monkeypatch):
    # review-p46 nit 4: a misconfigured/drifted data bundle degrades to the same
    # safe, teacher-visible BakeError as any other failure — never a raw trace,
    # never a leaked path.
    from hramatka.engine import data, pipeline

    def drift(*_a, **_k):
        raise data.DataDriftError("vesum.db: sha256 mismatch at /srv/lu-data/secret/vesum.db")

    monkeypatch.setattr(pipeline, "run", drift)
    baker = EngineLessonBaker(generator=fixtures.mock_generator, cache_dir=tmp_path / "cache")
    with pytest.raises(BakeError) as exc:
        baker.bake(fixtures.load_anchor(), duration=45, focus=None)
    message = str(exc.value)
    assert "data bundle" in message
    assert "/srv/lu-data" not in message  # the raw path must not leak to teachers
