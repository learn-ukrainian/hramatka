"""Offline contracts for the feature-gated engineered prompt pack."""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.engine import fixtures, pipeline, prompt_pack, retrieval
from hramatka.engine.fixtures import _bundle_with_matchup_vocabulary
from hramatka.engine.generate import GenerationUnparseable, generate_prompt_pack


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

    with pytest.raises(GenerationUnparseable):
        generate_prompt_pack(context, generator=lambda _prompt: json.dumps(bad, ensure_ascii=False))


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


def test_unsupported_focus_is_an_honest_ukrainian_notice():
    shared = _shared(focus="умовний спосіб майбутнього часу")
    focus = shared["lesson_plan"]["focus"]
    assert focus["status"] == "unsupported"
    assert focus["notice_uk"].startswith("Опора не містить достатньо перевіреного матеріалу")
