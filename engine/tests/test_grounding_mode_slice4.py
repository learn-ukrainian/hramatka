"""Grounding-mode v1 — Slice 4 selector, density, source, and floor acceptance."""

from __future__ import annotations

from pathlib import Path

from hramatka.engine import content_density, fixtures, pipeline, schema, selector


def _anchor() -> dict:
    sentence = "Читання розвиває увагу."
    return {
        "body_uk": sentence,
        "char_len": len(sentence),
        "terms": ["читання", "розвиває", "увагу"],
        "sentences": [
            {"id": "sentence-1", "text": sentence, "char_start": 0, "char_end": len(sentence)}
        ],
    }


def _derived_fill_in(candidate_id: str, stem: str, lemma: str) -> schema.HramatkaActivity:
    items = [
        {
            "sentence": f"{stem} ({index})",
            "answer": lemma,
            "options": [lemma, "варіант", "слово", "форма"],
        }
        for index in range(3)
    ]
    return schema.HramatkaActivity(
        activity={"type": "fill-in", "instruction": "Оберіть форму.", "items": items},
        # Deliberately share one quote sentence: it must not affect derived selection.
        evidence=[
            schema.Evidence(
                quote="Читання розвиває увагу.",
                locator=f"items[{index}]",
                char_start=0,
                char_end=24,
            )
            for index in range(3)
        ],
        kit_anchors=[
            schema.KitAnchors(
                lemmas=[lemma], witness_span="Читання розвиває увагу", locator=f"items[{index}]"
            )
            for index in range(3)
        ],
        candidate_id=candidate_id,
    )


def _quote_candidate(candidate_id: str, activity_type: str) -> schema.HramatkaActivity:
    evidence = schema.Evidence(
        quote="Читання розвиває увагу.", locator="text", char_start=0, char_end=24
    )
    if activity_type == "true-false":
        activity = {
            "type": activity_type,
            "instruction": "Позначте.",
            "items": [
                {"statement": f"{candidate_id}-{index}", "correct": True}
                for index in range(4)
            ],
        }
        evidence.locator = "items[0]"
    elif activity_type == "cloze":
        activity = {
            "type": activity_type,
            "instruction": "Заповніть.",
            "text": "Читання {a} увагу {b} щодня {c}.",
            "blanks": [
                {
                    "id": "a",
                    "answer": "розвиває",
                    "options": ["розвиває", "читає", "пише", "вчить"],
                },
                {"id": "b", "answer": "увагу", "options": ["увагу", "книгу", "школу", "мову"]},
                {"id": "c", "answer": "щодня", "options": ["щодня", "інколи", "завтра", "вчора"]},
            ],
        }
    elif activity_type == "short-writing":
        activity = {
            "type": activity_type,
            "prompt": "(1) Опишіть читання. (2) Поясніть приклад.",
            "word_count_guidance": "40–60 слів",
        }
    else:
        activity = {
            "type": activity_type,
            "instruction": "Позначте слова.",
            "text": "Читання розвиває увагу. Учні читають щодня.",
            "target_words": ["Читання", "розвиває", "Учні", "читають"],
            "criteria": "дієслова",
        }
    return schema.HramatkaActivity(
        activity=activity,
        evidence=[evidence],
        candidate_id=candidate_id,
    )


def _teacher_ready_by_phase(duration: int) -> dict[int, list[schema.HramatkaActivity]]:
    contract = content_density.teacher_ready_density(duration)
    next_index = 0
    activity_types = ("true-false", "quiz", "cloze", "mark-the-words", "fill-in")

    def closed_response() -> schema.HramatkaActivity:
        nonlocal next_index
        activity_type = activity_types[next_index % len(activity_types)]
        candidate = schema.HramatkaActivity(
            activity=fixtures._READY_CANDIDATES[activity_type](next_index),  # noqa: SLF001
            candidate_id=f"{activity_type}-{next_index}",
        )
        next_index += 1
        return candidate

    selected = {
        1: [closed_response() for _ in range(contract.phase_blocks[1])],
        2: [closed_response() for _ in range(contract.phase_blocks[2])],
        3: [closed_response() for _ in range(contract.phase_blocks[3] - 1)],
    }
    selected[3].append(
        schema.HramatkaActivity(
            activity=fixtures._READY_CANDIDATES["short-writing"](next_index),  # noqa: SLF001
            candidate_id="final-transfer",
        )
    )
    return selected


def _fp_kwargs(**overrides):
    values = {
        "anchor_hash": "slice4-anchor",
        "level": "B1",
        "pedagogy": "ttt",
        "phase": "1",
        "types": ["true-false", "fill-in"],
        "grounding_text": "grounding",
        "prompt_template": "prompt",
    }
    values.update(overrides)
    return values


def test_derived_selection_uses_lemma_novelty_not_shared_sentence_ids(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    anchor = _anchor()
    first = _derived_fill_in("derived-1", "Нове завдання", "читання")
    second = _derived_fill_in("derived-2", "Інше завдання", "увага")

    assert content_density.meets_content_density(first, anchor)
    assert content_density.itemized_sentence_ids_are_distinct(first, anchor)
    selected = selector.select_lesson(
        [first, second, _derived_fill_in("derived-3", "Третє завдання", "читання")],
        count_plan={"fill-in": 3},
        anchor=anchor,
        policy=selector.SelectorPolicy(density_target=3, forbid_adjacent_repeated_type=False),
    )
    assert [candidate.candidate_id for candidate in selected] == ["derived-1", "derived-2"]

    duplicate_stems = _derived_fill_in("duplicate-stems", "Повторене завдання", "читання")
    for item in duplicate_stems.activity["items"]:
        item["sentence"] = "Повторене завдання"
    assert content_density.meets_content_density(duplicate_stems, anchor) is False

    undersized = _derived_fill_in("undersized", "Коротке завдання", "читання")
    undersized.activity["items"] = undersized.activity["items"][:1]
    undersized.kit_anchors = undersized.kit_anchors[:1]
    assert content_density.meets_content_density(undersized, anchor) is False

    monkeypatch.delenv("HRAMATKA_GROUNDING_MODE_V1", raising=False)
    assert content_density.meets_content_density(first, anchor) is False


def test_quoting_reuse_cap_still_rejects_third_shared_primary(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    anchor = _anchor()
    selected = selector.select_composed_lesson(
        {
            1: [_quote_candidate("quote-1", "mark-the-words")],
            2: [_quote_candidate("quote-2", "cloze")],
            3: [_quote_candidate("quote-3", "short-writing")],
        },
        slots_by_phase={1: 1, 2: 1, 3: 1},
        count_plan={"mark-the-words": 1, "cloze": 1, "short-writing": 1},
        anchor=anchor,
    )
    assert [candidate.candidate_id for candidate in selected[1]] == ["quote-1"]
    assert [candidate.candidate_id for candidate in selected[2]] == ["quote-2"]
    assert selected[3] == []


def test_mixed_mode_phase_selection_uses_a_fixed_width_key(monkeypatch):
    """Derived and quoting candidates can share a flagged TTT phase safely."""
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    anchor = _anchor()
    derived = _derived_fill_in("derived-1", "Нове завдання", "читання")
    quoting = _quote_candidate("quote-1", "mark-the-words")

    selected = selector.select_lesson(
        [derived, quoting],
        count_plan={"fill-in": 1, "mark-the-words": 1},
        anchor=anchor,
        phase=1,
        policy=selector.SelectorPolicy(density_target=2),
    )

    assert [candidate.candidate_id for candidate in selected] == ["derived-1", "quote-1"]


def test_sentence_builder_derived_density_reads_starters_list():
    candidate = schema.HramatkaActivity(
        activity={"type": "sentence-builder", "starters": ["Почніть так.", "Потім додайте."]},
        kit_anchors=[
            schema.KitAnchors(
                lemmas=["почати"], witness_span="Почніть так", locator="starters[0]"
            ),
            schema.KitAnchors(
                lemmas=["додати"], witness_span="Потім додайте", locator="starters[1]"
            ),
        ],
    )

    assert content_density.derived_lemma_budget_met(candidate)


def test_selector_policy_version_is_in_fingerprint_identity():
    assert selector.SELECTOR_POLICY_VERSION == "grounding-mode-v1.selector.v7"
    assert (
        pipeline.fingerprint_inputs(**_fp_kwargs())["selector_policy"]["version"]
        == selector.SELECTOR_POLICY_VERSION
    )
    current = pipeline.make_fingerprint(**_fp_kwargs())
    prior = pipeline.make_fingerprint(
        **_fp_kwargs(
            selector_policy={
                **selector.DEFAULT_POLICY.as_dict(),
                "version": "wave0.selector.v3",
            }
        )
    )
    assert current != prior


def test_thin_source_blame_requires_empty_kit_only_in_grounding_mode(monkeypatch):
    thin = _anchor()
    populated_kit = {"status": "available"}
    empty_kit = {"status": "empty"}

    monkeypatch.delenv("HRAMATKA_GROUNDING_MODE_V1", raising=False)
    assert content_density.source_lacks_lesson_evidence(thin, kit=populated_kit) is True

    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    assert content_density.source_lacks_lesson_evidence(
        thin,
        kit=populated_kit,
        quoting_slots_required=2,
        quoting_slots_selected=1,
    ) is False
    assert content_density.source_lacks_lesson_evidence(
        thin,
        kit=empty_kit,
        quoting_slots_required=2,
        quoting_slots_selected=1,
    ) is True
    assert content_density.source_lacks_lesson_evidence(
        thin,
        kit=empty_kit,
        quoting_slots_required=2,
        quoting_slots_selected=2,
    ) is False
    assert content_density.source_lacks_lesson_evidence(thin, kit=empty_kit) is False


def test_teacher_ready_8_10_12_contract_rejects_underfilled_lessons(monkeypatch):
    forty_five = _teacher_ready_by_phase(45)
    assert content_density.meets_lesson_floor(
        [candidate for phase in forty_five.values() for candidate in phase],
        duration=45,
        selected_by_phase=forty_five,
    )
    for grounding_mode in (None, "1"):
        if grounding_mode is None:
            monkeypatch.delenv("HRAMATKA_GROUNDING_MODE_V1", raising=False)
        else:
            monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", grounding_mode)
        sixty = _teacher_ready_by_phase(60)
        sixty[3].pop()
        assert not content_density.meets_lesson_floor(
            [candidate for phase in sixty.values() for candidate in phase],
            duration=60,
            selected_by_phase=sixty,
        )
        ninety = _teacher_ready_by_phase(90)
        ninety[3].pop()
        assert not content_density.meets_lesson_floor(
            [candidate for phase in ninety.values() for candidate in phase],
            duration=90,
            selected_by_phase=ninety,
        )


def test_floor_oracle_is_frozen_in_the_measurement_harness_record():
    assert content_density.floor_oracle_record() == {
        "teacher_ready_density": content_density.teacher_ready_density_record(),
        "compatibility_floors": {
            "45": {
            "min_blocks": 8,
            "phase_minimums": {1: 3, 2: 4, 3: 1},
            "min_types": 4,
            "require_productive": True,
            "minimum_response_units": 28,
            "version": "TeacherReadyDensity.v2",
            "digest": content_density.teacher_ready_density_digest(),
            },
            "60": {
            "min_blocks": 10,
            "phase_minimums": {1: 3, 2: 5, 3: 2},
            "min_types": 4,
            "require_productive": True,
            "minimum_response_units": 35,
            "version": "TeacherReadyDensity.v2",
            "digest": content_density.teacher_ready_density_digest(),
            },
            "90": {
            "min_blocks": 12,
            "phase_minimums": {1: 4, 2: 5, 3: 3},
            "min_types": 4,
            "require_productive": True,
            "minimum_response_units": 42,
            "version": "TeacherReadyDensity.v2",
            "digest": content_density.teacher_ready_density_digest(),
            },
        },
    }
    harness = Path("hramatka/grounding-mode-v1-ab-harness.md")
    assert harness.is_file()
    assert "floor_oracle_v1" in harness.read_text(encoding="utf-8")
