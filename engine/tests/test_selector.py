"""Deterministic policy tests for the Wave-0 lesson selector."""

from __future__ import annotations

from dataclasses import replace

from hramatka.contracts import PILOT_ACTIVITY_TYPES
from hramatka.engine import registry, schema, selector


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
            "text": "one {gap} and two {gap2} and three {gap3}.",
            "blanks": [
                {"id": 1, "answer": answer, "options": [answer, "x", "y", "z"]},
                {"id": 2, "answer": "y", "options": ["y", "z", "a", "b"]},
                {"id": 3, "answer": "z", "options": ["z", "a", "b", "c"]},
            ],
        }
        locator = "text"
    else:
        pairs = [
            {"left": f"{candidate_id}-a", "right": answer},
            {"left": f"{candidate_id}-b", "right": "b"},
            {"left": f"{candidate_id}-c", "right": "c"},
            {"left": f"{candidate_id}-d", "right": "d"},
        ]
        activity = {
            "type": activity_type,
            "instruction": "i",
            "pairs": pairs,
        }
        locator = "pairs[0]"
        evidence = [
            schema.Evidence(
                quote=f"evidence-{start + offset}",
                locator=f"pairs[{index}]",
                char_start=start + offset,
                char_end=start + offset + 1,
            )
            for index, offset in enumerate((0, 10, 20, 30))
        ]
        return schema.HramatkaActivity(
            activity=activity,
            evidence=evidence,
            candidate_id=candidate_id,
        )
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
        "match",
        "cloze",
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


def test_composed_selector_can_select_all_nine_registered_types_without_a_hidden_cap(monkeypatch):
    """Ten B1 slots leave room for all nine types when evidence is distinct."""
    for activity_type, entry in registry.ACTIVITY_REGISTRY.items():
        monkeypatch.setitem(
            registry.ACTIVITY_REGISTRY,
            activity_type,
            replace(
                entry,
                evidence_answer_pairs=lambda candidate: [
                    (candidate.candidate_id or "", candidate.candidate_id or "")
                ],
            ),
        )

    def candidate(activity_type: str, phase: int, index: int) -> schema.HramatkaActivity:
        return schema.HramatkaActivity(
            activity={"type": activity_type, "fixture": index},
            evidence=[
                schema.Evidence(
                    quote=f"evidence-{phase}-{index}",
                    locator="fixture",
                    char_start=index,
                    char_end=index + 1,
                )
            ],
            candidate_id=f"{phase}-{activity_type}",
        )

    phase_types = {
        1: ("match-up", "true-false", "quiz"),
        2: ("short-writing", "cloze", "mark-the-words", "fill-in", "error-correction"),
        3: ("text-questions", "true-false"),
    }
    candidates_by_phase = {
        phase: [candidate(activity_type, phase, index) for index, activity_type in enumerate(types)]
        for phase, types in phase_types.items()
    }
    selected = selector.select_composed_lesson(
        candidates_by_phase,
        slots_by_phase={1: 3, 2: 5, 3: 2},
        count_plan={
            **{activity_type: 1 for activity_type in PILOT_ACTIVITY_TYPES},
            "true-false": 2,
        },
        policy=selector.SelectorPolicy(density_target=10, require_content_density=False),
    )

    assert {
        activity.activity["type"] for activities in selected.values() for activity in activities
    } == set(PILOT_ACTIVITY_TYPES)
