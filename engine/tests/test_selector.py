"""Deterministic policy tests for the Wave-0 lesson selector."""

from __future__ import annotations

from hramatka.engine import schema, selector


def _candidate(
    candidate_id: str,
    activity_type: str,
    *,
    start: int,
    answer: object,
) -> schema.HramatkaActivity:
    if activity_type == "true-false":
        activity = {
            "type": activity_type,
            "instruction": "i",
            "items": [{"statement": candidate_id, "correct": answer}],
        }
        locator = "items[0]"
    elif activity_type == "cloze":
        activity = {
            "type": activity_type,
            "instruction": "i",
            "text": "{gap}",
            "blanks": [{"id": 1, "answer": answer, "options": [answer, "x"]}],
        }
        locator = "text"
    else:
        activity = {
            "type": activity_type,
            "instruction": "i",
            "pairs": [{"left": candidate_id, "right": answer}],
        }
        locator = "pairs[0]"
    return schema.HramatkaActivity(
        activity=activity,
        evidence=[
            schema.Evidence(
                quote=f"evidence-{start}", locator=locator, char_start=start, char_end=start + 1
            )
        ],
        candidate_id=candidate_id,
    )


def test_selector_honours_phase_coverage_variety_puzzle_and_duplicate_constraints():
    true_false = _candidate("tf", "true-false", start=0, answer=True)
    cloze = _candidate("cloze", "cloze", start=10, answer="answer")
    match_up = _candidate("match", "match-up", start=20, answer="meaning")
    duplicate_match = _candidate("z-duplicate", "match-up", start=20, answer="meaning")
    second_match = _candidate("z-second", "match-up", start=30, answer="other")
    review = _candidate("review", "true-false", start=40, answer=False)
    review.gate_result.status = schema.DISPOSITION_REVIEW
    rejected = _candidate("rejected", "cloze", start=50, answer="no")
    rejected.gate_result.status = schema.DISPOSITION_REJECTED

    selected = selector.select_lesson(
        [true_false, cloze, match_up, duplicate_match, second_match, review, rejected],
        count_plan={"true-false": 2, "cloze": 2, "match-up": 2},
        policy=selector.SelectorPolicy(density_target=4),
    )

    assert [candidate.candidate_id for candidate in selected] == [
        "cloze",
        "match",
        "tf",
        "z-second",
    ]
    assert all(candidate.gate_result.status == schema.DISPOSITION_READY for candidate in selected)
    puzzle_types = {
        candidate.activity["type"]
        for candidate in selected
        if candidate.activity["type"] == "match-up"
    }
    assert puzzle_types == {"match-up"}
    assert all(
        previous.activity["type"] != current.activity["type"]
        for previous, current in zip(selected, selected[1:], strict=False)
    )

    phase_three = selector.select_lesson(
        [true_false, cloze, match_up],
        count_plan={"true-false": 1, "cloze": 1, "match-up": 1},
        phase=3,
    )
    assert [candidate.activity["type"] for candidate in phase_three] == ["cloze", "true-false"]
