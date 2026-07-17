"""Offline contracts for the feature-gated engineered prompt pack."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from hramatka.api.baking import engine_adapter
from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.baking.port import BakeError, FloorUnmetError, ProviderUnavailable
from hramatka.engine import fixtures, pipeline, prompt_pack, providers, registry, retrieval
from hramatka.engine.content_density import FLOOR_SHORTFALL_UA_MESSAGE
from hramatka.engine.fixtures import _bundle_with_matchup_vocabulary
from hramatka.engine.generate import (
    GeneratorUnavailable,
    generate_prompt_pack,
)


def _shared(*, focus: str | None = "читання") -> dict:
    snapshot = pipeline.snapshot_anchor(fixtures.load_anchor())
    grounding = retrieval.build_grounding_pack(snapshot["body_uk"])
    return prompt_pack.build_shared_input(
        snapshot=snapshot,
        grounding=grounding,
        duration_minutes=45,
        focus=focus,
        phase_count_plans={
            1: {"true-false": 1, "quiz": 1},
            2: {"cloze": 1, "fill-in": 1},
            3: {"text-questions": 1, "short-writing": 1},
        },
        visible_slots_by_phase={1: 3, 2: 4, 3: 1},
    )


def _citations(activities: list[dict], context: dict) -> list[dict]:
    by_type = {
        slot["type"]: slot["allowed_sentence_ids"]
        for slot in context["phase_request"]["requested_slots"]
    }
    rows = []
    kits = context.get("type_kits", [])
    for index, activity in enumerate(activities):
        activity_type = activity["type"]
        kit = kits[index] if index < len(kits) else {}
        if kit.get("citation_plan"):
            sentence_ids = kit["citation_plan"]
        elif activity_type == "match-up":
            locators = [f"pairs[{item}]" for item in range(len(activity["pairs"]))]
            sentence_ids = {locator: [by_type[activity_type][0]] for locator in locators}
        elif activity_type in {"cloze", "mark-the-words", "short-writing"}:
            locators = ["text"]
            sentence_ids = {locator: [by_type[activity_type][0]] for locator in locators}
        else:
            locators = [f"items[{item}]" for item in range(len(activity["items"]))]
            sentence_ids = {locator: [by_type[activity_type][0]] for locator in locators}
        rows.append(
            {
                "activity_index": index,
                "sentence_ids": sentence_ids,
            }
        )
    return rows


def _certified_activity(activity: dict, kit: dict) -> dict:
    """Use a pack's deterministic fields while keeping fixture prompts harmless."""
    certified = json.loads(json.dumps(activity, ensure_ascii=False))
    activity_type = certified["type"]
    if activity_type == "quiz":
        certified["items"] = [
            {
                "question": item["question_exemplar"],
                "options": item["options"],
                "correct": item["correct"],
                "evidence": item["evidence"],
            }
            for item in kit["quiz"]["items"]
        ]
    elif activity_type == "cloze":
        certified.update(kit["cloze"])
        certified.pop("display_text")
        certified["text"] = kit["cloze"]["display_text"]
        certified.pop("sentence_ids")
    elif activity_type == "fill-in":
        certified["items"] = [
            {
                key: item[key]
                for key in ("sentence", "answer", "options", "evidence")
            }
            for item in kit["fill_in"]["items"]
        ]
    elif activity_type == "match-up":
        certified["pairs"] = [
            {key: pair[key] for key in ("left", "right", "evidence")}
            for pair in kit["pairs"]
        ]
    elif activity_type == "mark-the-words":
        mark = kit["mark"]
        certified.update(
            {
                "instruction": mark["instruction"],
                "text": mark["text"],
                "criteria": mark["criterion"],
                "target_words": mark["expected_target_words"],
                "evidence": mark["text"],
            }
        )
    elif activity_type == "short-writing":
        short_writing = kit["short_writing"]
        certified.update(
            {
                "prompt": short_writing["prompt_exemplar"],
                "evidence": short_writing["evidence"],
                "word_count_guidance": short_writing["word_count_guidance"],
            }
        )
    return certified


def test_pack_builds_immutable_sentence_ids_focus_and_provisional_quota():
    shared = _shared()
    assert shared["pack_version"] == prompt_pack.PROMPT_PACK_VERSION
    assert [row["id"] for row in shared["anchor_sentence_inventory"]][:2] == ["S01", "S02"]
    assert shared["lesson_plan"]["focus"]["status"] == "supported"
    assert prompt_pack.phase_candidate_quota(1) == 2
    assert prompt_pack.phase_candidate_quota(5) == 5
    assert prompt_pack.phase_candidate_quota(9) == 6
    assert shared["provenance"]["injection_sha256"]


def test_pack_response_envelope_validates_citations_and_removes_them():
    context = prompt_pack.phase_context(_shared(), phase=1)
    activities = [
        _certified_activity(fixtures._READY_CANDIDATES["true-false"](0), context["type_kits"][0]),
        _certified_activity(fixtures._READY_CANDIDATES["quiz"](0), context["type_kits"][1]),
    ]
    payload = {"activities": activities, "citations": _citations(activities, context)}
    validated = prompt_pack.validate_response_envelope(payload, context)
    assert validated == activities
    assert all("slot_id" not in activity for activity in validated)

    payload["activities"][0]["slot_id"] = "P1-A1"
    with pytest.raises(prompt_pack.PromptPackError, match="leaks pack-only"):
        prompt_pack.validate_response_envelope(payload, context)


def test_pack_retries_a_bad_citation_envelope_and_keeps_raw_activities_clean():
    context = prompt_pack.phase_context(_shared(), phase=1)
    activities = [
        _certified_activity(fixtures._READY_CANDIDATES["true-false"](0), context["type_kits"][0]),
        _certified_activity(fixtures._READY_CANDIDATES["quiz"](0), context["type_kits"][1]),
    ]
    bad = {"activities": activities, "citations": []}
    good = {"activities": activities, "citations": _citations(activities, context)}
    responses = iter([json.dumps(bad, ensure_ascii=False), json.dumps(good, ensure_ascii=False)])
    assert generate_prompt_pack(context, generator=lambda _prompt: next(responses)) == activities

    with pytest.raises(prompt_pack.PromptPackError):
        generate_prompt_pack(context, generator=lambda _prompt: json.dumps(bad, ensure_ascii=False))


def _synthetic_phase_one_envelope(context: dict) -> dict:
    """Return a valid envelope from committed synthetic test fixtures only."""
    activities = [
        _certified_activity(fixtures._READY_CANDIDATES["true-false"](0), context["type_kits"][0]),
        _certified_activity(fixtures._READY_CANDIDATES["quiz"](0), context["type_kits"][1]),
    ]
    return {"activities": activities, "citations": _citations(activities, context)}


def test_pack_repairs_split_envelope_after_thought_and_records_telemetry():
    context = prompt_pack.phase_context(_shared(), phase=1)
    envelope = _synthetic_phase_one_envelope(context)
    raw = "<thought>synthetic preamble</thought>\n" + json.dumps(
        {"activities": envelope["activities"]}, ensure_ascii=False
    ) + "," + json.dumps({"citations": envelope["citations"]}, ensure_ascii=False)
    telemetry = providers.TelemetryContext(phase=1)
    token = providers.telemetry_ctx.set(telemetry)
    try:
        assert generate_prompt_pack(
            context, generator=lambda _prompt: raw
        ) == envelope["activities"]
    finally:
        providers.telemetry_ctx.reset(token)

    assert telemetry.traces == [
        {"event": "envelope_repaired", "phase": 1, "object_count": 2}
    ]


def test_pack_does_not_merge_split_objects_with_overlapping_keys():
    context = prompt_pack.phase_context(_shared(), phase=1)
    envelope = _synthetic_phase_one_envelope(context)
    raw = json.dumps({"activities": envelope["activities"]}, ensure_ascii=False) + "," + json.dumps(
        {"activities": [], "citations": envelope["citations"]}, ensure_ascii=False
    )
    telemetry = providers.TelemetryContext(phase=1)
    token = providers.telemetry_ctx.set(telemetry)
    try:
        with pytest.raises(
            prompt_pack.PromptPackError,
            match="requires activities and citations arrays",
        ):
            generate_prompt_pack(context, generator=lambda _prompt: raw)
    finally:
        providers.telemetry_ctx.reset(token)

    assert telemetry.traces == []


def test_pack_single_object_envelope_remains_unrepaired():
    context = prompt_pack.phase_context(_shared(), phase=1)
    envelope = _synthetic_phase_one_envelope(context)
    telemetry = providers.TelemetryContext(phase=1)
    token = providers.telemetry_ctx.set(telemetry)
    try:
        assert generate_prompt_pack(
            context,
            generator=lambda _prompt: json.dumps(envelope, ensure_ascii=False),
        ) == envelope["activities"]
    finally:
        providers.telemetry_ctx.reset(token)

    assert telemetry.traces == []


def test_slot_density_contracts_are_preflighted_and_enforced_in_the_envelope():
    context = prompt_pack.phase_context(_shared(), phase=2)
    cloze_kit, fill_kit = context["type_kits"]
    assert prompt_pack._preflight_density_contract(cloze_kit) == []
    assert prompt_pack._preflight_density_contract(fill_kit) == []
    assert cloze_kit["density_contract"]["multi_sentence_display"] == 2
    assert len(cloze_kit["cloze"]["blanks"]) == 3
    assert all(len(item["options"]) == 3 for item in fill_kit["fill_in"]["items"])

    activities = [
        _certified_activity(fixtures._READY_CANDIDATES["cloze"](0), cloze_kit),
        _certified_activity(fixtures._READY_CANDIDATES["fill-in"](0), fill_kit),
    ]
    payload = {"activities": activities, "citations": _citations(activities, context)}
    assert prompt_pack.validate_response_envelope(payload, context) == activities

    payload["activities"][0]["text"] = payload["activities"][0]["text"].replace(
        "{gap2}", "друге"
    )
    with pytest.raises(prompt_pack.PromptPackError, match="density_contract cloze"):
        prompt_pack.validate_response_envelope(payload, context)


def test_mark_target_set_and_writing_requirements_are_exact_output_contracts():
    snapshot = pipeline.snapshot_anchor(fixtures.load_anchor())
    grounding = retrieval.build_grounding_pack(snapshot["body_uk"])
    mark_shared = prompt_pack.build_shared_input(
        snapshot=snapshot,
        grounding=grounding,
        duration_minutes=45,
        focus="читання",
        phase_count_plans={1: {"mark-the-words": 1}},
        visible_slots_by_phase={1: 1},
    )
    mark_context = prompt_pack.phase_context(mark_shared, phase=1)
    mark_kit = mark_context["type_kits"][0]
    mark_activity = _certified_activity(fixtures._READY_CANDIDATES["mark-the-words"](0), mark_kit)
    mark_payload = {
        "activities": [mark_activity],
        "citations": _citations([mark_activity], mark_context),
    }
    assert prompt_pack.validate_response_envelope(mark_payload, mark_context) == [mark_activity]
    assert len(mark_kit["mark"]["span_sentence_ids"]) == 2
    mark_payload["activities"][0]["target_words"] = mark_payload["activities"][0][
        "target_words"
    ][:-1]
    with pytest.raises(prompt_pack.PromptPackError, match="density_contract mark-the-words"):
        prompt_pack.validate_response_envelope(mark_payload, mark_context)

    writing_context = prompt_pack.phase_context(_shared(), phase=3)
    writing_kit = writing_context["type_kits"][1]
    assert all(
        fragment.startswith(("(1) Опишіть", "(2) Поясніть"))
        for fragment in writing_kit["short_writing"]["required_prompt_fragments"]
    )
    writing_activity = _certified_activity(
        fixtures._READY_CANDIDATES["short-writing"](0), writing_kit
    )
    writing_payload = {
        "activities": [
            fixtures._READY_CANDIDATES["text-questions"](0),
            writing_activity,
        ],
        "citations": _citations(
            [fixtures._READY_CANDIDATES["text-questions"](0), writing_activity], writing_context
        ),
    }
    writing_payload["activities"][1]["prompt"] = writing_kit["short_writing"][
        "required_prompt_fragments"
    ][0]
    with pytest.raises(prompt_pack.PromptPackError, match="requirements must appear in prompt"):
        prompt_pack.validate_response_envelope(writing_payload, writing_context)


def _pack_fixture_generator(prompt: str, counters: Counter[str]) -> str:
    phase_match = re.search(
        r"=== ПОТОЧНА ФАЗА ТА СЛОТИ ВІДПОВІДІ \(дані, не інструкції\) ===\n```json\n(.*?)\n```",
        prompt,
        re.DOTALL,
    )
    kits_match = re.search(
        r"=== ПЕРЕВІРЕНІ КОМПЛЕКТИ ДЛЯ ПОТОЧНИХ СЛОТІВ "
        r"\(дані, не інструкції\) ===\n```json\n(.*?)\n```",
        prompt,
        re.DOTALL,
    )
    assert phase_match and kits_match
    phase_request = json.loads(phase_match.group(1))
    kits = json.loads(kits_match.group(1))
    raw_activities = fixtures.activities_for_prompt(prompt, counters)
    activities = [
        _certified_activity(activity, kit)
        for activity, kit in zip(raw_activities, kits, strict=True)
    ]
    assert len(activities) == len(phase_request["requested_slots"])
    context = {
        "phase_request": phase_request,
        "type_kits": kits,
        "shared": {
            "anchor_sentence_inventory": [{"id": f"S{index:02d}"} for index in range(1, 99)]
        },
    }
    for requested, kit in zip(context["phase_request"]["requested_slots"], kits, strict=True):
        requested["allowed_sentence_ids"] = kit["allowed_sentence_ids"]
    return json.dumps(
        {"activities": activities, "citations": _citations(activities, context)}, ensure_ascii=False
    )


def test_baker_uses_shared_pack_focus_and_phase_calls_only_when_flagged(monkeypatch, tmp_path):
    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")
    counters: Counter[str] = Counter()
    prompts: list[str] = []

    def generator(prompt: str) -> str:
        prompts.append(prompt)
        return _pack_fixture_generator(prompt, counters)

    baker = EngineLessonBaker(
        generator=generator,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    baked = baker.bake(fixtures.load_anchor(), duration=45, focus="читання")
    assert prompts
    assert all("ПОВНИЙ ПЛАН УРОКУ" in prompt for prompt in prompts)
    assert all('"focus"' in prompt for prompt in prompts)
    assert all("response_format" not in prompt for prompt in prompts)
    assert baked["blocks"]


def test_all_flags_real_bake_uses_derived_fill_in_contract_and_provenance(monkeypatch, tmp_path):
    """Exercise adapter -> phase worker -> pipeline under the all-on treatment vector.

    The one-slot plan intentionally remains below the lesson floor.  That makes
    this a cheap real-path regression while retaining the phase IR, where both
    the raw-validator decision and provenance stamp are inspectable.
    """
    for env in (
        "HRAMATKA_PROMPT_PACK",
        "HRAMATKA_KIT_ENRICHMENT_V1",
        "HRAMATKA_WRITER_PROMPT_V2",
        "HRAMATKA_GROUNDING_MODE_V1",
        "HRAMATKA_TEACHER_REVIEW_TRAY_V1",
    ):
        monkeypatch.setenv(env, "1")
    engine_out = tmp_path / "engine-out"
    monkeypatch.setenv("HRAMATKA_ENGINE_OUT_DIR", str(engine_out))
    monkeypatch.setattr(engine_adapter, "phase_plan", lambda *_args: [2])
    monkeypatch.setattr(
        engine_adapter,
        "_prompt_pack_candidate_count_plan",
        lambda _plan: {2: {"fill-in": 1}},
    )
    prompts: list[str] = []

    def generator(prompt: str) -> str:
        prompts.append(prompt)
        phase_match = re.search(
            r"=== ПОТОЧНА ФАЗА ТА СЛОТИ ВІДПОВІДІ \(дані, не інструкції\) ===\n```json\n(.*?)\n```",
            prompt,
            re.DOTALL,
        )
        kits_match = re.search(
            r"=== ПЕРЕВІРЕНІ КОМПЛЕКТИ ДЛЯ ПОТОЧНИХ СЛОТІВ "
            r"\(дані, не інструкції\) ===\n```json\n(.*?)\n```",
            prompt,
            re.DOTALL,
        )
        assert phase_match and kits_match
        phase_request = json.loads(phase_match.group(1))
        fill_in_kit = json.loads(kits_match.group(1))[0]
        items = [
            {
                "sentence": f"У новій вправі доберіть форму ____ для слова «{item['answer']}».",
                **{
                    key: item[key]
                    for key in ("answer", "options", "kit_anchors")
                },
            }
            for item in fill_in_kit["fill_in"]["items"]
        ]
        activity = {
            "type": "fill-in",
            "instruction": "Заповніть пропуски правильними формами.",
            "items": items,
        }
        context = {
            "phase_request": phase_request,
            "type_kits": [fill_in_kit],
            "shared": {
                "anchor_sentence_inventory": [{"id": f"S{index:02d}"} for index in range(1, 99)]
            },
        }
        context["phase_request"]["requested_slots"][0]["allowed_sentence_ids"] = fill_in_kit[
            "allowed_sentence_ids"
        ]
        return json.dumps(
            {"activities": [activity], "citations": _citations([activity], context)},
            ensure_ascii=False,
        )

    baker = EngineLessonBaker(
        generator=generator,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(FloorUnmetError):
        baker.bake(fixtures.load_anchor(), duration=45, focus="читання")

    assert prompts
    assert "fill-in [derived]: 1" in prompts[0]
    assert "НЕ переносіть evidence" in prompts[0]
    assert "дослівно перенесіть sentence, answer, options і evidence" not in prompts[0]
    assert "kit_anchors" in prompts[0]
    ir_paths = list(engine_out.glob("*/phase-2/lesson.ir.json"))
    assert len(ir_paths) == 1
    ir = json.loads(ir_paths[0].read_text(encoding="utf-8"))
    fill_in = next(
        activity for activity in ir["activities"] if activity["activity"]["type"] == "fill-in"
    )
    assert fill_in["provenance"]["grounding_mode"] == "derived"
    assert fill_in["kit_anchors"]
    assert all("evidence" not in item for item in fill_in["activity"]["items"])
    assert all(check["gate"] != "raw_contract" for check in fill_in["gate_result"]["checks"])

    quote_restore = {
        "type": "fill-in",
        "instruction": "Заповніть пропуски правильними формами.",
        "items": [
            {
                "sentence": item["sentence"],
                "answer": item["answer"],
                "options": item["options"],
                "evidence": item["kit_anchors"]["witness_span"],
            }
            for item in fill_in["raw_candidate"]["items"]
        ],
    }
    errors = registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(quote_restore)
    assert any("kit_anchors" in error for error in errors)


def test_unsupported_focus_is_an_honest_ukrainian_notice():
    shared = _shared(focus="умовний спосіб майбутнього часу")
    focus = shared["lesson_plan"]["focus"]
    assert focus["status"] == "unsupported"
    assert focus["notice_uk"].startswith("Опора не містить достатньо перевіреного матеріалу")


# --- tests for #181 partial phase degrade (pack mode) ---
# Use pack scaffolding (_pack_fixture_generator, certified paths) and anchor verbatim.
# external_options gate remains live via real bundle + pipeline.


def _make_phase_failing_generator(bad_phase: int):
    """Return a generator that produces valid pack responses for good phases
    (using the file-local scaffolding) but always-bad envelopes for bad_phase
    (causing GenerationUnparseable after bounded JSON parsing retries).
    """
    counters: Counter[str] = Counter()

    def generator(prompt: str) -> str:
        phase_match = re.search(
            r"=== ПОТОЧНА ФАЗА ТА СЛОТИ ВІДПОВІДІ \(дані, не інструкції\) ===\n```json\n(.*?)\n```",
            prompt,
            re.DOTALL,
        )
        phase = 0
        if phase_match:
            try:
                phase_req = json.loads(phase_match.group(1))
                phase = int(phase_req.get("phase", 0))
            except Exception:
                phase = 0
        if phase == bad_phase:
            return "not JSON"
        return _pack_fixture_generator(prompt, counters)

    return generator


def test_pack_partial_phase_unparseable_60min_rejects_under_floor_with_degraded_telemetry(
    monkeypatch, tmp_path: Path
):
    """Slice 4: a partial 60-minute bake cannot ship below its new floor."""

    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")
    engine_out = tmp_path / "engine-out"
    monkeypatch.setenv("HRAMATKA_ENGINE_OUT_DIR", str(engine_out))

    gen = _make_phase_failing_generator(bad_phase=1)
    baker = EngineLessonBaker(
        generator=gen,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    anchor = fixtures.load_anchor()
    # dict form with anchor_id to trigger out dir job
    if isinstance(anchor, str):
        anchor = {"anchor_id": "hramatka-181-partial-ship", "body_uk": anchor}
    else:
        anchor = dict(anchor)
        anchor.setdefault("anchor_id", "hramatka-181-partial-ship")

    with pytest.raises(FloorUnmetError) as exc_info:
        baker.bake(anchor, duration=60, focus="читання")
    assert exc_info.value.blames_source is False
    assert "could not reach the minimum activity density." in str(exc_info.value)

    # telemetry durable via trace
    trace_paths = list(engine_out.glob("*/trace.json"))
    assert trace_paths, "expected trace.json for telemetry"
    traces = json.loads(trace_paths[0].read_text(encoding="utf-8"))
    degraded_events = [t for t in traces if t.get("event") == "phase_generation_degraded"]
    assert len(degraded_events) >= 1
    ev = degraded_events[0]
    assert ev["phase"] == 1
    assert ev["error_class"] == "GenerationUnparseable"


def test_pack_partial_phase_unparseable_below_floor_raises_floorunmet_nonblaming(
    monkeypatch, tmp_path: Path
):
    """Case 2: partial +45min below floor -> FloorUnmet(blames=False) + lesson_floor_unmet."""

    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")
    gen = _make_phase_failing_generator(bad_phase=1)
    baker = EngineLessonBaker(
        generator=gen,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    anchor = fixtures.load_anchor()
    with pytest.raises(FloorUnmetError) as ei:
        baker.bake(anchor, duration=45, focus="читання")
    err = ei.value
    assert err.blames_source is False
    assert "could not reach the minimum activity density." in str(err)
    # Simulate runner classification used for failure_code + exact non-blaming msg
    failure_code = "lesson_floor_unmet"
    failure_message = FLOOR_SHORTFALL_UA_MESSAGE if not err.blames_source else "thin"
    assert failure_code == "lesson_floor_unmet"
    assert failure_message == FLOOR_SHORTFALL_UA_MESSAGE
    # exact non-blaming (not thin source)
    assert "З цього тексту не вдалося скласти повний урок" not in failure_message


def test_slot_repair_recovers_post_selection_floor_with_exact_pack_slots(
    monkeypatch, tmp_path: Path
):
    """The repair pass is the same generator + gates, not an assembly escape hatch."""
    monkeypatch.setenv("HRAMATKA_SLOT_REPAIR", "1")
    monkeypatch.setenv("HRAMATKA_ENGINE_OUT_DIR", str(tmp_path / "engine-out"))
    counters: Counter[str] = Counter()
    repair_requests: list[dict] = []

    def generator(prompt: str) -> str:
        phase_match = re.search(
            r"=== ПОТОЧНА ФАЗА ТА СЛОТИ ВІДПОВІДІ \(дані, не інструкції\) ===\n```json\n(.*?)\n```",
            prompt,
            re.DOTALL,
        )
        assert phase_match
        request = json.loads(phase_match.group(1))
        if request["phase"] == 1 and request["mode"] == "initial":
            return "not json"  # both provider-owned envelope retries fail
        if request["mode"] == "repair":
            repair_requests.append(request)
        return _pack_fixture_generator(prompt, counters)

    baker = EngineLessonBaker(
        generator=generator,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    baked = baker.bake(fixtures.load_anchor(), duration=45, focus="читання")
    assert len(baked["blocks"]) >= 6
    assert repair_requests
    assert {slot["slot_id"] for slot in repair_requests[0]["requested_slots"]} == {
        "P1-A1",
        "P1-A2",
        "P1-A3",
    }
    traces = json.loads(next((tmp_path / "engine-out").glob("*/trace.json")).read_text())
    attempt = next(event for event in traces if event.get("event") == "slot_repair_attempt")
    assert attempt["provider_status"] == "ok"
    assert attempt["gate_outcomes"]["ready"] == len(repair_requests[0]["requested_slots"])


def test_pack_response_rejection_falls_back_to_legacy_full_bake(monkeypatch, tmp_path: Path):
    """A real pack response failure takes the adapter's legacy full-bake path."""
    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")
    pack_prompts: list[str] = []
    legacy_prompts: list[str] = []
    legacy_counts: Counter[str] = Counter()

    def generator(prompt: str) -> str:
        if "ПОВНИЙ ПЛАН УРОКУ" in prompt:
            pack_prompts.append(prompt)
            # This is parseable JSON but fails the real pack envelope contract.
            return json.dumps({"activities": [], "citations": []}, ensure_ascii=False)
        legacy_prompts.append(prompt)
        return json.dumps(
            {"activities": fixtures.activities_for_prompt(prompt, legacy_counts)},
            ensure_ascii=False,
        )

    baker = EngineLessonBaker(
        generator=generator,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    real_bake = baker.bake
    bake_flags: list[bool] = []

    def recording_bake(*args, **kwargs):
        bake_flags.append(kwargs.get("_allow_prompt_pack", True))
        return real_bake(*args, **kwargs)

    monkeypatch.setattr(baker, "bake", recording_bake)
    baked = baker.bake(fixtures.load_anchor(), duration=45, focus=None)

    assert bake_flags == [True, False]
    assert pack_prompts
    assert legacy_prompts
    assert baked["blocks"]


def test_pack_all_phases_generator_unavailable_raises_provider_unavailable(
    monkeypatch, tmp_path: Path
):
    """Case 4: all GeneratorUnavailable -> ProviderUnavailable preserved."""
    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")

    def always_unavailable(prompt: str) -> str:
        raise GeneratorUnavailable("simulated full outage for #181")

    baker = EngineLessonBaker(
        generator=always_unavailable,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(ProviderUnavailable):
        baker.bake(fixtures.load_anchor(), duration=45, focus=None)


def test_legacy_flag_off_error_path_aborts_whole_unchanged(monkeypatch, tmp_path: Path):
    """Legacy (flag off): error still aborts whole (prove unchanged behavior)."""

    monkeypatch.delenv("HRAMATKA_PROMPT_PACK", raising=False)
    # bad return triggers generation_error in legacy path too
    def bad_legacy(prompt: str) -> str:
        return "not valid json for legacy generate"

    baker = EngineLessonBaker(
        generator=bad_legacy,
        bundle=_bundle_with_matchup_vocabulary(tmp_path / "data"),
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(BakeError, match="generator response was not valid JSON"):
        baker.bake(fixtures.load_anchor(), duration=60, focus=None)


def test_slot_repair_flag_off_preserves_full_legacy_bake(monkeypatch, tmp_path: Path):
    """The default-off switch retains the parent bake output and regeneration policy."""
    monkeypatch.delenv("HRAMATKA_SLOT_REPAIR", raising=False)
    monkeypatch.delenv("HRAMATKA_PROMPT_PACK", raising=False)
    monkeypatch.setenv("HRAMATKA_ENGINE_OUT_DIR", str(tmp_path / "engine-out"))

    def scripted_baker(counter: Counter[str], name: str) -> EngineLessonBaker:
        def generator(prompt: str) -> str:
            return json.dumps(
                {"activities": fixtures.activities_for_prompt(prompt, counter)}, ensure_ascii=False
            )

        return EngineLessonBaker(
            generator=generator,
            bundle=_bundle_with_matchup_vocabulary(tmp_path / f"data-{name}"),
            cache_dir=tmp_path / f"cache-{name}",
        )

    parent = scripted_baker(Counter(), "parent").bake(
        fixtures.load_anchor(), duration=45, focus="читання", _allow_prompt_pack=False
    )
    captured: list[dict] = []
    real_run = pipeline.run

    def recording_run(*args, **kwargs):
        captured.append(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run", recording_run)
    baked = scripted_baker(Counter(), "flag-off").bake(
        fixtures.load_anchor(), duration=45, focus="читання"
    )

    assert baked == parent
    assert captured
    assert all(call["max_regeneration_attempts"] == 2 for call in captured)
    traces = json.loads(next((tmp_path / "engine-out").glob("*/trace.json")).read_text())
    assert not any(str(event.get("event", "")).startswith("slot_repair_") for event in traces)


# ---------------------------------------------------------------------------
# #54: the pack must not invite schema vocabulary, and it builds match-up
# boards itself — so IT owns the citation-form requirement, not the model.
# ---------------------------------------------------------------------------
def test_pack_certifies_match_up_pairs_on_the_lemma_not_the_sentence_form():
    """#54 item 2: the pack picks the left side, so it must pick the lemma.

    The anchor says «людей»; a board must teach «людина». The surface form is
    retained as `anchor_form` so the evidence link stays inspectable.
    """
    pairs = prompt_pack._match_pairs(  # noqa: SLF001
        [{"id": "S01", "text": "Багато людей втратили насолоду."}],
        {"людей": [{"lemma": "людина", "pos": "noun"}]},
        {"людина": {"synonyms": ["особа"]}},
    )

    assert [pair["left"] for pair in pairs] == ["людина"]
    assert pairs[0]["anchor_form"] == "людей"
    assert pairs[0]["right"] == "особа"


def test_pack_dedupes_two_inflections_of_one_lemma_into_a_single_pair():
    """«книжки» and «книжок» are one lemma — one board pair, not two."""
    pairs = prompt_pack._match_pairs(  # noqa: SLF001
        [
            {"id": "S01", "text": "Не прочитує жодної книжки."},
            {"id": "S02", "text": "Насолода від читання книжок."},
        ],
        {
            "книжки": [{"lemma": "книжка", "pos": "noun"}],
            "книжок": [{"lemma": "книжка", "pos": "noun"}],
        },
        {"книжка": {"synonyms": ["книга"]}},
    )

    assert [pair["left"] for pair in pairs] == ["книжка"]


def test_pack_shared_policy_forbids_schema_vocabulary_in_teacher_visible_text():
    policy = _shared()["shared_form_policy"]

    assert {"true", "false", "correct"} <= set(policy["forbidden_teacher_visible_vocabulary"])
    assert "ніколи true/false" in policy["true_false_wording"]


def test_pack_phase_prompt_states_the_ua_only_rule():
    shared = _shared()
    prompt = prompt_pack.render_phase_prompt(prompt_pack.phase_context(shared, phase=1))

    assert "машинні ключі" in prompt
    assert "«правильно»/«неправильно» (П/Н)" in prompt
