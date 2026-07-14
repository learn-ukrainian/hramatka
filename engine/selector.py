"""Deterministic ready-candidate selector for a B1 lesson.

The selector is intentionally independent of generation and gates: it receives
only candidates already marked ``ready`` and makes its trade-offs visible in a
versioned policy.  Future waves can add types without changing the safety
boundary between review/rejected material and the automatically assembled
lesson.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from . import schema
from .registry import ACTIVITY_REGISTRY

SELECTOR_POLICY_VERSION = "wave0.selector.v1"


@dataclass(frozen=True)
class SelectorPolicy:
    version: str = SELECTOR_POLICY_VERSION
    density_target: int = 3
    max_puzzle_types: int = 1
    forbid_adjacent_repeated_type: bool = True
    forbid_duplicate_evidence_answer: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_POLICY = SelectorPolicy()


@dataclass
class _SelectionState:
    """Mutable selection facts shared by all slots in one assembled lesson."""

    selected_types: dict[str, int] = field(default_factory=dict)
    covered: set[tuple[int, int]] = field(default_factory=set)
    evidence_answers: set[tuple[str, str]] = field(default_factory=set)
    puzzle_types: set[str] = field(default_factory=set)
    activity_identities: set[str] = field(default_factory=set)
    last_type: str | None = None


def _coverage(ir: schema.HramatkaActivity) -> set[tuple[int, int]]:
    return {
        (e.char_start, e.char_end)
        for e in ir.evidence
        if e.char_start is not None and e.char_end is not None
    }


def _activity_identity(ir: schema.HramatkaActivity) -> str:
    """Stable whole-lesson duplicate guard for semantically equal payloads."""
    return json.dumps(ir.activity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _select(
    candidates: Sequence[schema.HramatkaActivity],
    *,
    count_plan: Mapping[str, int],
    density_target: int,
    phase: int | None,
    policy: SelectorPolicy,
    state: _SelectionState,
) -> list[schema.HramatkaActivity]:
    """Select one slot group while retaining any supplied whole-lesson state."""
    phase_candidates = [
        candidate
        for candidate in candidates
        if candidate.gate_result.status == schema.DISPOSITION_READY
        and candidate.activity.get("type") in ACTIVITY_REGISTRY
        and (phase is None or phase in ACTIVITY_REGISTRY[candidate.activity["type"]].ttt_phases)
    ]
    remaining = list(enumerate(phase_candidates))
    selected: list[schema.HramatkaActivity] = []

    while remaining and len(selected) < density_target:
        ranked: list[tuple[tuple[int, int, str], int, schema.HramatkaActivity]] = []
        for original_index, candidate in remaining:
            activity_type = candidate.activity["type"]
            entry = ACTIVITY_REGISTRY[activity_type]
            identity = _activity_identity(candidate)
            if identity in state.activity_identities:
                continue
            if state.selected_types.get(activity_type, 0) >= count_plan.get(activity_type, 0):
                continue
            if (
                policy.forbid_adjacent_repeated_type
                and state.last_type == activity_type
            ):
                continue
            if (
                entry.is_puzzle
                and activity_type not in state.puzzle_types
                and len(state.puzzle_types) >= policy.max_puzzle_types
            ):
                continue
            pairs = entry.evidence_answer_pairs(candidate)
            pair_set = set(pairs)
            if policy.forbid_duplicate_evidence_answer and (
                len(pair_set) != len(pairs) or pair_set & state.evidence_answers
            ):
                continue
            new_coverage = len(_coverage(candidate) - state.covered)
            variety = int(activity_type not in state.selected_types)
            tie_break = candidate.candidate_id or f"{original_index:06d}"
            ranked.append(((-new_coverage, -variety, tie_break), original_index, candidate))

        if not ranked:
            break
        _score, original_index, winner = min(ranked)
        selected.append(winner)
        activity_type = winner.activity["type"]
        state.selected_types[activity_type] = state.selected_types.get(activity_type, 0) + 1
        state.covered |= _coverage(winner)
        state.evidence_answers |= set(
            ACTIVITY_REGISTRY[activity_type].evidence_answer_pairs(winner)
        )
        state.activity_identities.add(_activity_identity(winner))
        state.last_type = activity_type
        if ACTIVITY_REGISTRY[activity_type].is_puzzle:
            state.puzzle_types.add(activity_type)
        remaining = [row for row in remaining if row[0] != original_index]
    return selected


def select_lesson(
    candidates: list[schema.HramatkaActivity],
    *,
    count_plan: dict[str, int],
    phase: int | None = None,
    policy: SelectorPolicy = DEFAULT_POLICY,
) -> list[schema.HramatkaActivity]:
    """Greedily select the strongest ready candidates under Wave-0 rules.

    Ranking favours new anchor coverage, then type variety, then stable source
    order.  The deterministic tie-break is candidate id (falling back to input
    order), which keeps fixture and cache comparisons reproducible.
    """
    return _select(
        candidates,
        count_plan=count_plan,
        density_target=policy.density_target,
        phase=phase,
        policy=policy,
        state=_SelectionState(),
    )


def select_composed_lesson(
    candidates_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]],
    *,
    slots_by_phase: Mapping[int, int],
    count_plan: Mapping[str, int],
    policy: SelectorPolicy = DEFAULT_POLICY,
) -> dict[int, list[schema.HramatkaActivity]]:
    """Select a TTT lesson under one global composition policy.

    Pipeline runs remain phase-local for independent generation, gates, and
    artifacts.  The assembler uses this helper afterwards: it fills each phase
    quota in pedagogical order while retaining one selection state for the
    whole lesson.  Diversity, duplicate-evidence, puzzle, and adjacency rules
    therefore cannot reset at a phase boundary.
    """
    state = _SelectionState()
    selected_by_phase: dict[int, list[schema.HramatkaActivity]] = {}
    for phase, slots in sorted(slots_by_phase.items()):
        selected_by_phase[phase] = _select(
            candidates_by_phase.get(phase, ()),
            count_plan=count_plan,
            density_target=slots,
            phase=phase,
            policy=policy,
            state=state,
        )
    return selected_by_phase
