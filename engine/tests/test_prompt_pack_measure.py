"""Offline coverage for the live-runner orchestration and aggregate schema."""

from __future__ import annotations

import json
import re
import threading
from collections import Counter

from hramatka.bakeoff import prompt_pack_measure as measure
from hramatka.engine import fixtures


def _certified_activity(activity: dict, kit: dict) -> dict:
    certified = json.loads(json.dumps(activity, ensure_ascii=False))
    if certified["type"] == "quiz":
        certified["items"] = [
            {
                "question": item["question_exemplar"],
                "options": item["options"],
                "correct": item["correct"],
                "evidence": item["evidence"],
            }
            for item in kit["quiz"]["items"]
        ]
    elif certified["type"] == "cloze":
        cloze = kit["cloze"]
        certified.update(
            {
                "text": cloze["display_text"],
                "blanks": cloze["blanks"],
                "evidence": cloze["evidence"],
            }
        )
    elif certified["type"] == "fill-in":
        certified["items"] = [
            {key: item[key] for key in ("sentence", "answer", "options", "evidence")}
            for item in kit["fill_in"]["items"]
        ]
    elif certified["type"] == "short-writing":
        short_writing = kit["short_writing"]
        certified.update(
            {
                "prompt": short_writing["prompt_exemplar"],
                "evidence": short_writing["evidence"],
                "word_count_guidance": short_writing["word_count_guidance"],
            }
        )
    return certified


def _pack_response(prompt: str, counters: Counter[str]) -> str:
    activities = fixtures.activities_for_prompt(prompt, counters)
    phase_match = re.search(
        r"=== ПОТОЧНА ФАЗА ТА СЛОТИ ВІДПОВІДІ "
        r"\(дані, не інструкції\) ===\n```json\n(.*?)\n```",
        prompt,
        re.DOTALL,
    )
    kits_match = re.search(
        r"=== ПЕРЕВІРЕНІ КОМПЛЕКТИ ДЛЯ ПОТОЧНИХ СЛОТІВ "
        r"\(дані, не інструкції\) ===\n```json\n(.*?)\n```",
        prompt,
        re.DOTALL,
    )
    if not phase_match or not kits_match:
        return json.dumps({"activities": activities}, ensure_ascii=False)
    request = json.loads(phase_match.group(1))
    kits = json.loads(kits_match.group(1))
    activities = [
        _certified_activity(activity, kit)
        for activity, kit in zip(activities, kits, strict=True)
    ]
    citations = []
    for index, (activity, slot, kit) in enumerate(
        zip(activities, request["requested_slots"], kits, strict=True)
    ):
        if kit.get("citation_plan"):
            sentence_ids = kit["citation_plan"]
        elif activity["type"] == "match-up":
            locators = [f"pairs[{item}]" for item in range(len(activity["pairs"]))]
            sentence_ids = {locator: [kit["allowed_sentence_ids"][0]] for locator in locators}
        elif activity["type"] in {"cloze", "mark-the-words", "short-writing"}:
            locators = ["text"]
            sentence_ids = {locator: [kit["allowed_sentence_ids"][0]] for locator in locators}
        else:
            locators = [f"items[{item}]" for item in range(len(activity["items"]))]
            sentence_ids = {locator: [kit["allowed_sentence_ids"][0]] for locator in locators}
        citations.append(
            {
                "activity_index": index,
                "sentence_ids": sentence_ids,
            }
        )
        assert activity["type"] == slot["type"]
    return json.dumps({"activities": activities, "citations": citations}, ensure_ascii=False)


def test_fixture_protocols_emit_calls_candidates_and_metrics(tmp_path):
    counters: Counter[str] = Counter()

    def generator(prompt: str) -> str:
        return _pack_response(prompt, counters)

    anchor = measure.AnchorCase(
        opaque_id="fixture-test",
        text=fixtures.load_anchor(),
        source_class="fixture",
        duration=45,
        focus="читання",
    )
    calls = []
    rows = []
    lessons = []
    for protocol in measure.PROTOCOLS:
        lesson, candidate_rows = measure._run_protocol(
            protocol=protocol,
            anchor=anchor,
            trial=1,
            generator=generator,
            trace_dir=tmp_path / "trace",
            calls=calls,
            calls_lock=threading.Lock(),
            concurrency=3,
        )
        lessons.append(lesson)
        rows.extend(candidate_rows)
    summary = measure._aggregate(calls, rows, lessons)
    assert summary["provider_calls"] >= 3
    assert summary["input_bytes"] > 0
    assert summary["blocks_shipped"] >= 0
    assert all(call.raw_digest for call in calls)


def test_envelope_failures_stay_in_the_measurement_rejection_denominator(tmp_path):
    anchor = measure.AnchorCase(
        opaque_id="fixture-envelope-failure",
        text=fixtures.load_anchor(),
        source_class="fixture",
        duration=45,
        focus="читання",
    )
    calls = []
    lesson, rows = measure._run_protocol(
        protocol="pack-per-phase",
        anchor=anchor,
        trial=1,
        generator=lambda _prompt: json.dumps({"activities": [], "citations": []}),
        trace_dir=tmp_path / "trace",
        calls=calls,
        calls_lock=threading.Lock(),
        concurrency=3,
    )
    assert rows
    assert all(row["disposition"] == "rejected" for row in rows)
    assert all(
        any(
            check["gate"] == "pack_envelope" and check["status"] == "fail"
            for check in row["checks"]
        )
        for row in rows
    )
    summary = measure._aggregate(calls, rows, [lesson])
    assert summary["overall_rejection_rate"] == 1.0
    assert summary["raw_contract_rate"] == 0.0
