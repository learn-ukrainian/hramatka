"""Acceptance tests for issue #159 — content density and instruction bank."""

from __future__ import annotations

import re

from hramatka.engine import content_density, instruction_bank, schema, selector

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
