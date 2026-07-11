"""Deterministic ready-candidate selector for a B1 lesson.

The selector is intentionally independent of generation and gates: it receives
only candidates already marked ``ready`` and makes its trade-offs visible in a
versioned policy.  Future waves can add types without changing the safety
boundary between review/rejected material and the automatically assembled
lesson.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from . import schema
from .registry import ACTIVITY_REGISTRY

SELECTOR_POLICY_VERSION = "wave0.selector.v1"
_SPACE_RE = re.compile(r"\s+")


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


def _normalise(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "").casefold()).strip()


def _evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    activity = ir.activity
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    activity_type = activity.get("type")
    pairs: list[tuple[str, str]] = []
    if activity_type == "true-false":
        for index, item in enumerate(activity.get("items", [])):
            pairs.append(
                (
                    _normalise(evidence_by_locator.get(f"items[{index}]")),
                    _normalise(item.get("correct")),
                )
            )
    elif activity_type == "cloze":
        answer = "|".join(_normalise(blank.get("answer")) for blank in activity.get("blanks", []))
        pairs.append((_normalise(evidence_by_locator.get("text")), answer))
    elif activity_type == "match-up":
        for index, pair in enumerate(activity.get("pairs", [])):
            pairs.append(
                (
                    _normalise(evidence_by_locator.get(f"pairs[{index}]")),
                    _normalise(pair.get("right")),
                )
            )
    return pairs


def _coverage(ir: schema.HramatkaActivity) -> set[tuple[int, int]]:
    return {
        (e.char_start, e.char_end)
        for e in ir.evidence
        if e.char_start is not None and e.char_end is not None
    }


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
    phase_candidates = [
        candidate
        for candidate in candidates
        if candidate.gate_result.status == schema.DISPOSITION_READY
        and candidate.activity.get("type") in ACTIVITY_REGISTRY
        and (phase is None or phase in ACTIVITY_REGISTRY[candidate.activity["type"]].ttt_phases)
    ]
    remaining = list(enumerate(phase_candidates))
    selected: list[schema.HramatkaActivity] = []
    selected_types: dict[str, int] = {}
    covered: set[tuple[int, int]] = set()
    evidence_answers: set[tuple[str, str]] = set()
    puzzle_types: set[str] = set()

    while remaining and len(selected) < policy.density_target:
        ranked: list[tuple[tuple[int, int, str], int, schema.HramatkaActivity]] = []
        for original_index, candidate in remaining:
            activity_type = candidate.activity["type"]
            entry = ACTIVITY_REGISTRY[activity_type]
            if selected_types.get(activity_type, 0) >= count_plan.get(activity_type, 0):
                continue
            if (
                policy.forbid_adjacent_repeated_type
                and selected
                and selected[-1].activity.get("type") == activity_type
            ):
                continue
            if (
                entry.is_puzzle
                and activity_type not in puzzle_types
                and len(puzzle_types) >= policy.max_puzzle_types
            ):
                continue
            pairs = _evidence_answer_pairs(candidate)
            pair_set = set(pairs)
            if policy.forbid_duplicate_evidence_answer and (
                len(pair_set) != len(pairs) or pair_set & evidence_answers
            ):
                continue
            new_coverage = len(_coverage(candidate) - covered)
            variety = int(activity_type not in selected_types)
            tie_break = candidate.candidate_id or f"{original_index:06d}"
            ranked.append(((-new_coverage, -variety, tie_break), original_index, candidate))

        if not ranked:
            break
        _score, original_index, winner = min(ranked)
        selected.append(winner)
        activity_type = winner.activity["type"]
        selected_types[activity_type] = selected_types.get(activity_type, 0) + 1
        covered |= _coverage(winner)
        evidence_answers |= set(_evidence_answer_pairs(winner))
        if ACTIVITY_REGISTRY[activity_type].is_puzzle:
            puzzle_types.add(activity_type)
        remaining = [row for row in remaining if row[0] != original_index]
    return selected
