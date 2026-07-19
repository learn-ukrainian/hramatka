"""Acceptance tests for issue #159 — content density and instruction bank."""

from __future__ import annotations

import re
from collections import Counter

import pytest

from hramatka.api.baking import engine_adapter
from hramatka.engine import (
    content_density,
    fixtures,
    instruction_bank,
    pipeline,
    prompt_pack,
    repair,
    retrieval,
    schema,
    selector,
)
from hramatka.sizing_policy import B1, phase_plan

VARENIKI_SENTENCE = "Замісіть мʼяке тісто й залиште його на пів години під рушником."

_TI_IMPERATIVE_RE = re.compile(
    r"\b(?:Обери|Познач|Заповни|Виправ|Напиши|З['ʼ]єднай|Вибери|Прочитай|Дай)\b",
    re.UNICODE,
)


def _candidate(
    candidate_id: str,
    activity: dict,
    *,
    evidence_quote: str,
    locator: str = "text",
    char_start: int = 0,
) -> schema.HramatkaActivity:
    return schema.HramatkaActivity(
        activity=activity,
        evidence=[
            schema.Evidence(
                quote=evidence_quote,
                locator=locator,
                char_start=char_start,
                char_end=char_start + len(evidence_quote),
            )
        ],
        candidate_id=candidate_id,
    )


def _fixture_candidate(activity_type: str, index: int) -> schema.HramatkaActivity:
    """Build a density-valid candidate with selection-distinct evidence."""
    activity = fixtures._READY_CANDIDATES[activity_type](index % 4)  # noqa: SLF001
    activity["title"] = f"fixture-{activity_type}-{index}"
    units = content_density.response_units(activity)
    collection = "pairs" if activity_type == "match-up" else "items"
    locators = (
        ["text"]
        if activity_type in {"cloze", "mark-the-words", "short-writing"}
        else [f"{collection}[{item}]" for item in range(units)]
    )
    evidence = [
        schema.Evidence(
            quote=f"унікальна опора {index}-{item}",
            locator=locator,
            char_start=index * 100 + item * 10,
            char_end=index * 100 + item * 10 + 8,
        )
        for item, locator in enumerate(locators)
    ]
    return schema.HramatkaActivity(
        activity=activity,
        evidence=evidence,
        candidate_id=f"{activity_type}-{index}",
        gate_result=schema.GateResult(status=schema.GATE_CLEAN, checks=[]),
    )


def _production_selection(duration: int) -> dict[int, list[schema.HramatkaActivity]]:
    plan = phase_plan(B1, duration)
    count_plans = engine_adapter._prompt_pack_candidate_count_plan(plan)  # noqa: SLF001
    pools: dict[int, list[schema.HramatkaActivity]] = {}
    candidate_index = 0
    for phase, counts in count_plans.items():
        pools[phase] = []
        for activity_type, count in counts.items():
            for _ in range(count):
                pools[phase].append(_fixture_candidate(activity_type, candidate_index))
                candidate_index += 1
    total_count_plan = sum((Counter(counts) for counts in count_plans.values()), Counter())
    return selector.select_composed_lesson(
        pools,
        slots_by_phase=Counter(plan),
        count_plan=total_count_plan,
        policy=selector.SelectorPolicy(
            density_target=len(plan),
            require_productive=True,
            prioritize_response_units=True,
        ),
    )


def test_production_45_60_90_plans_satisfy_the_teacher_ready_receipt() -> None:
    for duration in (45, 60, 90):
        contract = content_density.teacher_ready_density(duration)
        expected_plan = [
            phase
            for phase, count in contract.phase_blocks.items()
            for _ in range(count)
        ]
        # Production sizing and delivery density are separate modules.  Prove
        # their live, unpatched B1 contracts are identical for every duration.
        assert phase_plan(B1, duration) == expected_plan
        assert Counter(phase_plan(B1, duration)) == contract.phase_blocks
        selected_by_phase = _production_selection(duration)
        receipt = content_density.evaluate_teacher_ready_density(
            selected_by_phase, duration=duration
        )
        assert receipt.ready, (duration, receipt.errors)
        assert receipt.phase_counts == dict(
            contract.phase_blocks
        )
        # This is the production prompt-pack bank, not a hand-picked lesson.
        # Response-unit prioritization must retain the four-family first-pass
        # floor while still reaching the duration's response target.
        assert len(receipt.type_units) >= contract.min_types
        assert receipt.response_units >= contract.minimum_response_units


def test_prompt_pack_cloze_uses_the_canonical_registry_target() -> None:
    snapshot = pipeline.snapshot_anchor(fixtures.load_anchor())
    grounding = retrieval.build_grounding_pack(snapshot["body_uk"])
    shared = prompt_pack.build_shared_input(
        snapshot=snapshot,
        grounding=grounding,
        duration_minutes=45,
        focus=None,
        phase_count_plans={
            1: {"true-false": 1},
            2: {"cloze": 1},
            3: {"short-writing": 1},
        },
        visible_slots_by_phase={1: 3, 2: 4, 3: 1},
    )
    cloze_kit = prompt_pack.phase_context(shared, phase=2)["type_kits"][0]

    assert len(cloze_kit["cloze"]["blanks"]) == content_density.registry_item_targets()[
        "cloze"
    ]


def test_legacy_flat_floor_check_fails_closed_without_phase_assignment() -> None:
    selected_by_phase = _production_selection(45)
    selected = [
        candidate
        for phase in sorted(selected_by_phase)
        for candidate in selected_by_phase[phase]
    ]

    assert content_density.meets_lesson_floor(selected, duration=45) is False
    assert content_density.meets_lesson_floor(
        selected,
        duration=45,
        phase_by_candidate={},
    ) is False


def test_repair_planner_rejects_unsupported_duration_pack_and_phase_shape() -> None:
    pack = {
        "teacher_ready_density": {"duration": 45},
        "lesson_plan": {"duration_minutes": 45, "focus": {"status": "supported"}},
        "slots": [{"slot_id": "P1-A1", "type": "quiz"}],
    }
    with pytest.raises(ValueError, match="Unsupported teacher-ready duration"):
        repair.RepairPlanner(pack, duration=30)
    with pytest.raises(ValueError, match="does not match the immutable prompt pack"):
        repair.RepairPlanner(pack, duration=60)

    planner = repair.RepairPlanner(pack, duration=45, started_at=0)
    assert planner.plan(
        round=1,
        selected_by_phase={1: [], 2: [], 3: []},
        slots_by_phase={1: 2, 2: 5, 3: 1},
        now=1,
    ) == []


def test_minimum_activity_types_is_an_enforced_delivery_error() -> None:
    selected_by_phase = {
        1: [_fixture_candidate("true-false", index) for index in range(3)],
        2: [_fixture_candidate("true-false", index) for index in range(3, 7)],
        3: [_fixture_candidate("short-writing", 7)],
    }
    receipt = content_density.evaluate_teacher_ready_density(selected_by_phase, duration=45)
    assert "activity_types_2_minimum_4" in receipt.errors
    assert receipt.ready is False


def test_repair_planner_targets_block_density_response_variety_and_phase_three() -> None:
    plan = phase_plan(B1, 45)
    count_plans = engine_adapter._prompt_pack_candidate_count_plan(plan)  # noqa: SLF001
    slots = [
        {"slot_id": f"P{phase}-A{position}", "type": activity_type}
        for phase, counts in count_plans.items()
        for position, activity_type in enumerate(
            (
                activity_type
                for activity_type, count in counts.items()
                for _ in range(count)
            ),
            start=1,
        )
    ]
    pack = {"lesson_plan": {"focus": {"status": "supported"}}, "slots": slots}
    slots_by_phase = Counter(plan)

    sparse = {
        1: [_fixture_candidate("short-writing", index) for index in range(3)],
        2: [_fixture_candidate("short-writing", index) for index in range(3, 7)],
        3: [_fixture_candidate("short-writing", 7)],
    }
    sparse_requests = repair.RepairPlanner(pack, duration=45, started_at=0).plan(
        round=1, selected_by_phase=sparse, slots_by_phase=slots_by_phase, now=1
    )
    assert sparse_requests
    assert any(
        error.startswith("response_units_")
        for request in sparse_requests
        for error in request.density_errors
    )
    assert any(
        slot.activity_type != "short-writing"
        for request in sparse_requests
        for slot in request.slots
    )

    valid = _production_selection(45)
    invalid_cloze = _fixture_candidate("cloze", 99)
    invalid_cloze.activity["blanks"] = invalid_cloze.activity["blanks"][:1]
    valid[2][-1] = invalid_cloze
    block_requests = repair.RepairPlanner(pack, duration=45, started_at=0).plan(
        round=1, selected_by_phase=valid, slots_by_phase=slots_by_phase, now=1
    )
    assert [(request.phase, request.slots[0].activity_type) for request in block_requests] == [
        (2, "cloze")
    ]

    no_transfer = _production_selection(45)
    no_transfer[3] = [_fixture_candidate("quiz", 100)]
    transfer_requests = repair.RepairPlanner(pack, duration=45, started_at=0).plan(
        round=1, selected_by_phase=no_transfer, slots_by_phase=slots_by_phase, now=1
    )
    assert [(request.phase, request.slots[0].activity_type) for request in transfer_requests] == [
        (3, "short-writing")
    ]


def test_response_repair_preserves_sole_productive_slot_and_closes_real_shortfall() -> None:
    plan = phase_plan(B1, 45)
    count_plans = engine_adapter._prompt_pack_candidate_count_plan(plan)  # noqa: SLF001
    slots_by_phase = Counter(plan)
    slots = [
        {"slot_id": f"P{phase}-A{position}", "type": activity_type}
        for phase, counts in count_plans.items()
        for position, activity_type in enumerate(
            (
                activity_type
                for activity_type, count in counts.items()
                for _ in range(count)
            ),
            start=1,
        )
    ]
    pack = {"lesson_plan": {"focus": {"status": "supported"}}, "slots": slots}
    count_plan = sum((Counter(counts) for counts in count_plans.values()), Counter())

    def candidate_with_units(activity_type: str, index: int, units: int):
        candidate = _fixture_candidate(activity_type, index)
        collection = {
            "true-false": "items",
            "quiz": "items",
            "cloze": "blanks",
            "fill-in": "items",
            "mark-the-words": "target_words",
        }.get(activity_type)
        if collection is not None:
            candidate.activity[collection] = candidate.activity[collection][:units]
        return candidate

    initial_pools = {
        1: [
            candidate_with_units("true-false", 10, 5),
            candidate_with_units("quiz", 11, 3),
            candidate_with_units("cloze", 12, 3),
        ],
        2: [
            candidate_with_units("quiz", 20, 3),
            candidate_with_units("cloze", 21, 3),
            candidate_with_units("fill-in", 22, 3),
            candidate_with_units("mark-the-words", 23, 4),
        ],
        3: [
            candidate_with_units("short-writing", 30, 1),
            candidate_with_units("quiz", 31, 4),
        ],
    }
    policy = selector.SelectorPolicy(
        density_target=len(plan),
        require_productive=True,
        prioritize_response_units=True,
    )
    before_selection = selector.select_composed_lesson(
        initial_pools,
        slots_by_phase=slots_by_phase,
        count_plan=count_plan,
        policy=policy,
    )
    before = content_density.evaluate_teacher_ready_density(before_selection, duration=45)
    assert before.ready is False
    assert before.response_units == 25
    assert before.errors == ("response_units_25_minimum_28",)

    requests = repair.RepairPlanner(pack, duration=45, started_at=0).plan(
        round=1,
        selected_by_phase=before_selection,
        slots_by_phase=slots_by_phase,
        now=1,
    )
    requested = {
        slot.slot_id: slot
        for request in requests
        for slot in request.slots
    }
    assert "P3-A2" not in requested
    assert set(requested) == {"P1-A2", "P2-A1", "P2-A3"}

    amended_pools = {phase: list(candidates) for phase, candidates in initial_pools.items()}
    for replacement_index, slot in enumerate(requested.values(), start=100):
        amended_pools[slot.phase].append(
            _fixture_candidate(slot.activity_type, replacement_index)
        )
    after_selection = selector.select_composed_lesson(
        amended_pools,
        slots_by_phase=slots_by_phase,
        count_plan=count_plan,
        policy=policy,
    )
    after = content_density.evaluate_teacher_ready_density(after_selection, duration=45)

    assert [candidate.activity["type"] for candidate in after_selection[3]] == [
        "short-writing"
    ]
    assert after.response_units >= 28
    assert after.ready is True


def test_quiz_requires_three_items_at_composition():
    skeletal = _candidate(
        "quiz-1",
        {
            "type": "quiz",
            "instruction": "Обери правильну відповідь.",
            "items": [
                {
                    "question": "Q?",
                    "options": ["a", "b", "c"],
                    "correct": 0,
                    "evidence": "one quote",
                }
            ],
        },
        evidence_quote="one quote",
        locator="items[0]",
    )
    rich_items = [
        {
            "question": f"Q{i}?",
            "options": ["a", "b", "c"],
            "correct": 0,
            "evidence": f"quote {i}",
        }
        for i in range(3)
    ]
    rich = schema.HramatkaActivity(
        activity={
            "type": "quiz",
            "instruction": "Обери правильну відповідь.",
            "items": rich_items,
        },
        evidence=[
            schema.Evidence(
                quote=f"quote {i}",
                locator=f"items[{i}]",
                char_start=i * 10,
                char_end=i * 10 + 8,
            )
            for i in range(3)
        ],
        candidate_id="quiz-3",
    )
    assert content_density.meets_content_density(skeletal) is False
    assert content_density.meets_content_density(rich) is True

    selected = selector.select_lesson(
        [skeletal, rich],
        count_plan={"quiz": 1},
        policy=selector.SelectorPolicy(density_target=1),
    )
    assert [candidate.candidate_id for candidate in selected] == ["quiz-3"]


def test_error_correction_requires_two_items_at_composition():
    skeletal = _candidate(
        "ec-1",
        {
            "type": "error-correction",
            "instruction": "Виправ помилку.",
            "items": [
                {
                    "sentence": "S.",
                    "error": "a",
                    "correction": "b",
                    "options": ["a", "b", "c"],
                    "explanation": "e",
                    "evidence": "E1",
                }
            ],
        },
        evidence_quote="E1",
        locator="items[0]",
    )
    rich = schema.HramatkaActivity(
        activity={
            "type": "error-correction",
            "instruction": "Виправ помилку.",
            "items": [
                {
                    "sentence": "S1.",
                    "error": "a",
                    "correction": "b",
                    "options": ["a", "b", "c"],
                    "explanation": "e",
                    "evidence": "E1",
                },
                {
                    "sentence": "S2.",
                    "error": "c",
                    "correction": "d",
                    "options": ["c", "d", "e"],
                    "explanation": "e",
                    "evidence": "E2",
                },
            ],
        },
        evidence=[
            schema.Evidence(
                quote="E1",
                locator="items[0]",
                char_start=10,
                char_end=12,
            ),
            schema.Evidence(
                quote="E2",
                locator="items[1]",
                char_start=20,
                char_end=22,
            ),
        ],
        candidate_id="ec-2",
    )
    assert content_density.meets_content_density(skeletal) is False
    assert content_density.meets_content_density(rich) is True


def test_cloze_requires_three_blanks_at_composition():
    one_blank = _candidate(
        "cloze-1",
        {
            "type": "cloze",
            "instruction": "Заповни пропуск.",
            "text": "one {gap} here.",
            "blanks": [{"id": 1, "answer": "gap", "options": ["gap", "x", "y", "z"]}],
        },
        evidence_quote="one gap here.",
    )
    three_blanks = _candidate(
        "cloze-3",
        {
            "type": "cloze",
            "instruction": "Заповни пропуск.",
            "text": "one {gap} and two {gap2} and three {gap3}.",
            "blanks": [
                {"id": 1, "answer": "gap", "options": ["gap", "x", "y", "z"]},
                {"id": 2, "answer": "gap2", "options": ["gap2", "x", "y", "z"]},
                {"id": 3, "answer": "gap3", "options": ["gap3", "x", "y", "z"]},
            ],
        },
        evidence_quote="one gap and two gap2 and three gap3.",
        char_start=20,
    )
    assert content_density.meets_content_density(one_blank) is False
    assert content_density.meets_content_density(three_blanks) is True


def test_mark_the_words_requires_four_targets_and_two_sentences():
    sparse = _candidate(
        "mtw-sparse",
        {
            "type": "mark-the-words",
            "instruction": "Познач усі дієслова.",
            "text": "One verb only.",
            "target_words": ["verb"],
            "criteria": "pos=verb",
        },
        evidence_quote="One verb only.",
    )
    four_targets = _candidate(
        "mtw-four",
        {
            "type": "mark-the-words",
            "instruction": "Познач усі іменники.",
            "text": "Alpha beta gamma delta.",
            "target_words": ["Alpha", "beta", "gamma", "delta"],
            "criteria": "pos=noun",
        },
        evidence_quote="Alpha beta gamma delta.",
        char_start=30,
    )
    two_sentences = _candidate(
        "mtw-two-sentences",
        {
            "type": "mark-the-words",
            "instruction": "Познач усі дієслова.",
            "text": "First sentence. Second sentence.",
            "target_words": ["First"],
            "criteria": "pos=adj",
        },
        evidence_quote="First sentence. Second sentence.",
        char_start=60,
    )
    rich = _candidate(
        "mtw-rich",
        {
            "type": "mark-the-words",
            "instruction": "Познач усі дієслова.",
            "text": "First runs. Second jumps.",
            "target_words": ["runs", "jumps", "walks", "falls"],
            "criteria": "pos=verb",
        },
        evidence_quote="First runs. Second jumps.",
        char_start=90,
    )
    assert content_density.meets_content_density(sparse) is False
    assert content_density.meets_content_density(four_targets) is False
    assert content_density.meets_content_density(two_sentences) is False
    assert content_density.meets_content_density(rich) is True



def test_composition_rejects_duplicate_vareniki_evidence_sentence():
    """Red-proof on the live-bake duplicate: cloze + fill-in on the same sentence."""
    shared = VARENIKI_SENTENCE
    anchor = {
        "body_uk": shared,
        "sentences": [
            {
                "id": "sentence-1",
                "text": shared,
                "char_start": 0,
                "char_end": len(shared),
            },
            {
                "id": "sentence-2",
                "text": "коли спливуть, зачекайте ще дві-три хвилини й виймайте.",
                "char_start": 100,
                "char_end": 156,
            },
            {
                "id": "sentence-3",
                "text": "Подавайте вареники гарячими — зі сметаною або зі шкварками.",
                "char_start": 160,
                "char_end": 220,
            },
        ],
    }
    cloze = _candidate(
        "cloze-rushnyk",
        {
            "type": "cloze",
            "instruction": "Заповни пропуск.",
            "text": "Замісіть мʼяке тісто й залиште його на пів години під {gap}.",
            "blanks": [
                {
                    "id": 1,
                    "answer": "рушником",
                    "options": ["рушником", "столом", "дверима", "вікном"],
                },
                {"id": 2, "answer": "години", "options": ["години", "хвилини", "дні", "тижня"]},
                {
                    "id": 3,
                    "answer": "тісто",
                    "options": ["тісто", "борошно", "начинку", "вареники"],
                },
            ],
        },
        evidence_quote=shared,
        char_start=0,
    )
    fill_in = _candidate(
        "fillin-godyny",
        {
            "type": "fill-in",
            "instruction": "Обери форму.",
            "items": [
                {
                    "sentence": "Замісіть мʼяке тісто й залиште його на пів ____ під рушником.",
                    "answer": "години",
                    "options": ["години", "хвилини", "дні", "тижня"],
                    "evidence": shared,
                },
                {
                    "sentence": (
                        "Варіть вареники в підсоленій воді: коли спливуть, зачекайте ще ____."
                    ),
                    "answer": "хвилини",
                    "options": ["хвилини", "години", "дні", "тижня"],
                    "evidence": "коли спливуть, зачекайте ще дві-три хвилини й виймайте.",
                },
            ],
        },
        evidence_quote=shared,
        locator="items[0]",
        char_start=0,
    )
    other = schema.HramatkaActivity(
        activity={
            "type": "fill-in",
            "instruction": "Обери форму.",
            "items": [
                {
                    "sentence": (
                        "Варіть вареники в підсоленій воді: коли спливуть, зачекайте ще ____."
                    ),
                    "answer": "хвилини",
                    "options": ["хвилини", "години", "дні", "тижня"],
                    "evidence": "коли спливуть, зачекайте ще дві-три хвилини й виймайте.",
                },
                {
                    "sentence": "Подавайте вареники гарячими — зі ____.",
                    "answer": "сметаною",
                    "options": ["сметаною", "водою", "олією", "молоком"],
                    "evidence": "Подавайте вареники гарячими — зі сметаною або зі шкварками.",
                },
                {
                    "sentence": "Замісіть мʼяке тісто й залиште його на пів ____ під рушником.",
                    "answer": "години",
                    "options": ["години", "хвилини", "дні", "тижня"],
                    "evidence": shared,
                },
            ],
        },
        evidence=[
            schema.Evidence(
                quote="коли спливуть, зачекайте ще дві-три хвилини й виймайте.",
                locator="items[0]",
                char_start=100,
                char_end=156,
            ),
            schema.Evidence(
                quote="Подавайте вареники гарячими — зі сметаною або зі шкварками.",
                locator="items[1]",
                char_start=160,
                char_end=220,
            ),
            schema.Evidence(quote=shared, locator="items[2]", char_start=0, char_end=len(shared)),
        ],
        candidate_id="fillin-other",
    )

    selected = selector.select_lesson(
        [cloze, fill_in, other],
        count_plan={"cloze": 1, "fill-in": 2},
        policy=selector.SelectorPolicy(density_target=2),
        anchor=anchor,
    )
    selected_ids = [candidate.candidate_id for candidate in selected]
    assert "cloze-rushnyk" in selected_ids
    assert "fillin-godyny" not in selected_ids
    assert len(selected) == 2
    used_sentence_ids = set()
    for candidate in selected:
        primary = content_density.primary_sentence_id(candidate, anchor)
        if primary:
            used_sentence_ids.add(primary)
    assert "sentence-1" in used_sentence_ids
    assert len(used_sentence_ids) == 2


def test_instruction_bank_is_vi_form_and_error_correction_states_deliberate_mistake():
    bank_strings = list(instruction_bank.CANONICAL_INSTRUCTIONS.values())
    bank_strings.extend(instruction_bank._MARK_WORDS_BY_CRITERIA.values())  # noqa: SLF001
    for text in bank_strings:
        assert _TI_IMPERATIVE_RE.search(text) is None, text

    error_instruction = instruction_bank.CANONICAL_INSTRUCTIONS["error-correction"]
    assert "навмисн" in error_instruction.casefold()

    projected = schema.project_to_b1(
        schema.HramatkaActivity(
            activity={
                "type": "quiz",
                "instruction": "Обери правильну відповідь.",
                "items": [
                    {
                        "question": "Q?",
                        "options": ["a", "b", "c"],
                        "correct": 0,
                    }
                ],
            }
        )
    )
    assert projected["instruction"] == "Оберіть правильну відповідь за текстом."
    assert _TI_IMPERATIVE_RE.search(projected["instruction"]) is None

    projected_ec = schema.project_to_b1(
        schema.HramatkaActivity(
            activity={
                "type": "error-correction",
                "instruction": "Виправ помилку.",
                "items": [
                    {
                        "sentence": "S.",
                        "error": "a",
                        "correction": "b",
                        "options": ["a", "b", "c"],
                        "explanation": "e",
                    }
                ],
            }
        )
    )
    canonical_ec = instruction_bank.CANONICAL_INSTRUCTIONS["error-correction"]
    assert projected_ec["instruction"] == canonical_ec


def test_phase_three_text_questions_accept_locator_backed_parsed_evidence():
    """The public activity projection omits evidence; the parsed IR must not."""
    candidate = schema.HramatkaActivity(
        activity={
            "type": "text-questions",
            "items": [
                {"question": "Що відбувається в тексті?"},
                {"question": "Чому це важливо?"},
                {"question": "Як ви застосуєте це у власному досвіді?"},
            ],
        },
        evidence=[
            schema.Evidence(quote="Перше речення.", locator="items[0]"),
            schema.Evidence(quote="Друге речення.", locator="items[1]"),
            schema.Evidence(quote="Третє речення.", locator="items[2]"),
        ],
    )

    assert content_density._phase_three_transfer_errors([candidate]) == []  # noqa: SLF001


def test_teacher_ready_density_rejects_one_sparse_block_despite_type_aggregate():
    def true_false(candidate_id: str, count: int) -> schema.HramatkaActivity:
        return schema.HramatkaActivity(
            activity={
                "type": "true-false",
                "items": [
                    {"statement": f"Твердження {item}", "correct": True}
                    for item in range(count)
                ],
            },
            candidate_id=candidate_id,
        )

    writing = schema.HramatkaActivity(
        activity={
            "type": "short-writing",
            "prompt": "(1) Опишіть думку. (2) Поясніть власний приклад.",
            "word_count_guidance": "40–60 слів",
        },
        candidate_id="writing",
    )
    selected = {
        1: [true_false("dense-1", 5), true_false("sparse", 1), true_false("dense-2", 5)],
        2: [
            true_false("dense-3", 5),
            true_false("dense-4", 5),
            true_false("dense-5", 5),
            true_false("dense-6", 5),
        ],
        3: [writing],
    }

    receipt = content_density.evaluate_teacher_ready_density(selected, duration=45)

    assert receipt.response_units >= 28
    assert "block_2_true-false_below_delivered_floor" in receipt.errors
