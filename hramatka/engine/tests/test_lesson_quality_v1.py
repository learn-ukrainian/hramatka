from __future__ import annotations

from copy import deepcopy

import pytest

from hramatka.engine.lesson_capacity_v3 import AllocatedSlot, LessonAllocation
from hramatka.engine.lesson_profile_45_v1 import lesson_profile_45
from hramatka.engine.lesson_quality_v1 import (
    LessonQualityError,
    estimated_active_work_minutes,
    validate_teacher_lesson_quality_45,
)
from hramatka.engine.unit_plan_v3 import (
    CertifiedTargetToken,
    CertifiedUnit,
    Citation,
    ExpectedKeyRule,
    ResourceClaim,
    UnitAnchor,
    certify_unit_plan,
)

_COUNTS = {
    "match-up": 8,
    "quiz": 8,
    "fill-in": 8,
    "error-correction": 8,
    "mark-the-words": 10,
    "cloze": 22,
}

_MARK_WORDS = (
    "швидко",
    "тихо",
    "уважно",
    "разом",
    "вчасно",
    "раптом",
    "потім",
    "сьогодні",
    "завтра",
    "вдома",
)


def _distinctness(activity_type: str, index: int) -> dict[str, object]:
    if activity_type == "match-up":
        return {"pair": {"left": f"ліве {index}", "right": f"праве {index}"}}
    if activity_type == "cloze":
        return {"gap": {"sentence_id": f"s-{index}", "token_id": f"t-{index}"}}
    if activity_type == "mark-the-words":
        return {"target": {"sentence_id": f"s-{index}", "token_id": f"t-{index}"}}
    return {"stem": f"завдання {activity_type} {index}"}


def _plan(slot_id: str, phase: int, activity_type: str, count: int):
    units = tuple(
        CertifiedUnit(
            unit_id=f"{slot_id}:{index}",
            resource_claims=(ResourceClaim("sentence", f"{activity_type}:s-{index}"),),
            anchor=UnitAnchor("evidence", f"source:{activity_type}:s-{index}"),
            allowed_forms=(f"відповідь-{index}",),
            expected_key_or_rule=ExpectedKeyRule(
                "rule" if activity_type == "error-correction" else "key",
                f"відповідь-{index}",
                certified_error_count=1 if activity_type == "error-correction" else 0,
            ),
            citation_plan=(Citation("source", f"sentences[{index}]"),),
            distinctness=_distinctness(activity_type, index),
        )
        for index in range(1, count + 1)
    )
    return certify_unit_plan(
        slot_id=slot_id,
        phase=phase,
        activity_type=activity_type,
        units=units,
        certified_target_tokens=(
            tuple(
                CertifiedTargetToken(
                    sentence_id=f"s-{index}",
                    token_id=f"t-{index}",
                    start_offset=(index - 1) * 10,
                    end_offset=(index - 1) * 10 + len(f"слово-{index}"),
                    surface=f"слово-{index}",
                )
                for index in range(1, count + 1)
            )
            if activity_type == "mark-the-words"
            else ()
        ),
    )


def _allocation(counts: dict[str, int] | None = None) -> LessonAllocation:
    active_counts = counts or _COUNTS
    slots = tuple(
        AllocatedSlot(
            slot_id=slot.slot_id,
            phase=slot.phase,
            requested_type=slot.requested_type,
            scheduled_type=slot.requested_type,
            plan=_plan(
                slot.slot_id,
                slot.phase,
                slot.requested_type,
                active_counts[slot.requested_type],
            ),
        )
        for slot in lesson_profile_45().slots
    )
    return LessonAllocation(("teacher-source",), slots)


def _cloze_payload(count: int = 22) -> dict[str, object]:
    sentences = []
    blanks = []
    for index in range(1, count + 1):
        # Sixteen Ukrainian words per sentence gives a natural 352-word
        # passage at the accepted 22-sentence density.
        sentences.append(
            f"Учні уважно читають {{{index}}} речення та разом помічають важливі "
            "деталі події у зв'язному цікавому тексті сьогодні."
        )
        blanks.append(
            {
                "id": index,
                "answer": f"нове-{index}",
                "options": [f"нове-{index}", f"інше-{index}", f"третє-{index}"],
            }
        )
    return {
        "type": "cloze",
        "instruction": "Відновіть оповідання.",
        "text": " ".join(sentences),
        "blanks": blanks,
    }


def _rendered() -> list[dict[str, object]]:
    activities: dict[str, dict[str, object]] = {
        "match-up": {
            "type": "match-up",
            "pairs": [
                {"left": f"слово {index}", "right": f"значення {index}"} for index in range(1, 9)
            ],
        },
        "quiz": {
            "type": "quiz",
            "items": [
                {
                    "question": f"Яку подію пояснює речення номер {index}?",
                    "options": ["перша", "друга", "третя"],
                    "correct": index % 3,
                }
                for index in range(1, 9)
            ],
        },
        "fill-in": {
            "type": "fill-in",
            "items": [
                {
                    "sentence": f"Доповніть окреме речення номер {index}: ___.",
                    "answer": "слово",
                    "options": ["слово", "форма", "вираз"],
                }
                for index in range(1, 9)
            ],
        },
        "error-correction": {
            "type": "error-correction",
            "items": [f"Виправте іншу помилку в реченні номер {index}." for index in range(1, 9)],
        },
        "mark-the-words": {
            "type": "mark-the-words",
            "text": " ".join(_MARK_WORDS),
            "target_words": list(_MARK_WORDS),
        },
        "cloze": _cloze_payload(),
    }
    return [
        {
            "slot_id": slot.slot_id,
            "type": slot.requested_type,
            "activity": {"payload": activities[slot.requested_type]},
        }
        for slot in lesson_profile_45().slots
    ]


def test_accepted_shape_is_varied_dense_and_human_paced() -> None:
    allocation = _allocation()

    validate_teacher_lesson_quality_45(allocation, _rendered())

    assert estimated_active_work_minutes(allocation) == pytest.approx(41.0)


def test_quiz_cannot_collapse_into_another_gap_drill() -> None:
    rendered = _rendered()
    quiz = rendered[1]["activity"]["payload"]  # type: ignore[index]
    quiz["items"][0]["question"] = "Що тут ___?"  # type: ignore[index]

    with pytest.raises(LessonQualityError, match="Quiz repeats"):
        validate_teacher_lesson_quality_45(_allocation(), rendered)


def test_complete_lesson_rejects_learner_work_repeated_across_activities() -> None:
    rendered = deepcopy(_rendered())
    quiz = rendered[1]["activity"]["payload"]  # type: ignore[index]
    correction = rendered[3]["activity"]["payload"]  # type: ignore[index]
    correction["items"][0] = quiz["items"][0]["question"]  # type: ignore[index]

    with pytest.raises(LessonQualityError, match="across activities"):
        validate_teacher_lesson_quality_45(_allocation(), rendered)


def test_final_story_requires_exactly_one_gap_in_every_sentence() -> None:
    rendered = deepcopy(_rendered())
    cloze = rendered[-1]["activity"]["payload"]  # type: ignore[index]
    cloze["text"] = str(cloze["text"]).replace("{2}", "звичайне", 1)  # type: ignore[index]

    with pytest.raises(LessonQualityError, match="lost certified gaps"):
        validate_teacher_lesson_quality_45(_allocation(), rendered)


def test_word_marking_counts_repeated_lemma_positions_as_real_interactions() -> None:
    rendered = deepcopy(_rendered())
    mark = rendered[4]["activity"]["payload"]  # type: ignore[index]
    mark["text"] = " ".join("слово" for _index in range(10))  # type: ignore[index]
    mark["target_words"] = ["слово"] * 10  # type: ignore[index]

    validate_teacher_lesson_quality_45(_allocation(), rendered)


def test_word_marking_rejects_targets_missing_from_the_passage() -> None:
    rendered = deepcopy(_rendered())
    mark = rendered[4]["activity"]["payload"]  # type: ignore[index]
    mark["text"] = "слово слово слово"  # type: ignore[index]
    mark["target_words"] = ["слово"] * 10  # type: ignore[index]

    with pytest.raises(LessonQualityError, match="target positions"):
        validate_teacher_lesson_quality_45(_allocation(), rendered)


def test_old_floor_sized_phase_is_rejected_as_under_paced() -> None:
    counts = {**_COUNTS, "match-up": 6, "quiz": 5}

    with pytest.raises(LessonQualityError, match="phase 1"):
        validate_teacher_lesson_quality_45(_allocation(counts), _rendered())
