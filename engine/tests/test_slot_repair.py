"""Contracts for the bounded post-selection repair planner."""

from __future__ import annotations

from hramatka.engine import repair, schema


def _pack() -> dict:
    return {
        "lesson_plan": {"focus": {"status": "supported"}},
        "slots": [
            {"slot_id": "P1-A1", "type": "quiz"},
            {"slot_id": "P1-A2", "type": "true-false"},
            {"slot_id": "P2-A1", "type": "fill-in"},
            {"slot_id": "P3-A1", "type": "short-writing"},
        ],
    }


def test_planner_batches_exact_slots_per_phase_and_respects_attempt_budget():
    planner = repair.RepairPlanner(_pack(), started_at=100.0)
    selected = {1: [], 2: [], 3: []}
    requests = planner.plan(
        round=1, selected_by_phase=selected, slots_by_phase={1: 2, 2: 1, 3: 1}, now=101
    )
    assert [(request.phase, [slot.slot_id for slot in request.slots]) for request in requests] == [
        (1, ["P1-A1", "P1-A2"]),
        (2, ["P2-A1"]),
        (3, ["P3-A1"]),
    ]
    for request in requests:
        planner.scheduled(request)
    again = planner.plan(
        round=2, selected_by_phase=selected, slots_by_phase={1: 2, 2: 1, 3: 1}, now=102
    )
    assert len(again) == 3
    for request in again:
        planner.scheduled(request)
    assert planner.calls == repair.MAX_CALLS
    assert planner.plan(
        round=3, selected_by_phase=selected, slots_by_phase={1: 2, 2: 1, 3: 1}, now=102
    ) == []


def test_planner_stops_at_wall_and_hard_deadline_guard():
    selected = {1: []}
    assert repair.RepairPlanner(_pack(), started_at=0).plan(
        round=1, selected_by_phase=selected, slots_by_phase={1: 1}, now=repair.MAX_WALL_SECONDS
    ) == []
    assert repair.RepairPlanner(_pack(), started_at=0, hard_deadline=700).plan(
        round=1, selected_by_phase=selected, slots_by_phase={1: 1}, now=220
    ) == []


def test_matchup_merge_preserves_two_and_dedupes_answer_forms():
    merged = repair.merge_matchup_pairs(
        [{"left": "a", "right": "Значення"}, {"left": "b", "right": "Інше"}],
        [
            {"left": "c", "right": " значення  "},
            {"left": "d", "right": "Третє"},
            {"left": "e", "right": "Четверте"},
        ],
    )
    assert [pair["right"] for pair in merged] == ["Значення", "Інше", "Третє", "Четверте"]


def test_bounded_gate_failures_emit_scoped_observed_expected_without_prose_or_bodies():
    raw_candidate = {
        "type": "true-false",
        "instruction": "raw activity body must never enter repair feedback",
        "items": [
            {
                "statement": "неправильне твердження",
                "correct": False,
                "evidence": "full evidence body must never enter repair feedback",
            }
        ],
    }
    gate_result = schema.GateResult()
    gate_result.add(
        "evidence_span",
        "fail",
        "validator detail prose must never enter repair feedback",
        locator="items[0]",
    )
    activity = schema.HramatkaActivity(
        activity={"type": "true-false"}, raw_candidate=raw_candidate, gate_result=gate_result
    )

    failures = repair.bounded_gate_failures([activity])

    assert failures == {
        "true-false": [
            {
                "gate": "evidence_span",
                "field": "items[0]",
                "observed": "неправильне твердження",
                "expected": "evidence_span: pass",
            }
        ]
    }
    rendered = repr(failures)
    assert "validator detail prose" not in rendered
    assert "raw activity body" not in rendered
    assert "full evidence body" not in rendered
    assert repr(raw_candidate) not in rendered
