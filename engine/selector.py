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

from . import content_density, schema
from .registry import ACTIVITY_REGISTRY

SELECTOR_POLICY_VERSION = "grounding-mode-v1.selector.v7"


@dataclass(frozen=True)
class SelectorPolicy:
    version: str = SELECTOR_POLICY_VERSION
    density_target: int = 3
    max_puzzle_types: int = 1
    forbid_adjacent_repeated_type: bool = True
    forbid_duplicate_evidence_answer: bool = True
    require_content_density: bool = True
    require_productive: bool = False
    prioritize_response_units: bool = False

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
    sentence_reuse: content_density.SentenceReuseState = field(
        default_factory=content_density.SentenceReuseState
    )
    derived_lemmas: set[str] = field(default_factory=set)
    derived_stems: set[str] = field(default_factory=set)
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


def _focus_score(
    candidate: schema.HramatkaActivity,
    *,
    focus_context: Mapping[str, Any] | None,
    anchor: dict | None,
) -> int:
    """Prefer verified focus evidence without weakening any selection guard."""
    if not focus_context:
        return 0
    terms = [str(term).casefold() for term in focus_context.get("terms", []) if len(str(term)) > 2]
    text = _activity_identity(candidate).casefold()
    # Inflection makes exact phrase equality too brittle for Ukrainian.  A
    # conservative five-character stem still makes the signal deterministic,
    # and sentence IDs are the stronger source-grounded component below.
    term_matches = sum(term[:5] in text for term in terms if len(term) >= 5)
    if content_density._is_derived_mode_candidate(candidate):
        # Derived selection must not rank via sentence IDs or quote metrics.
        return term_matches
    focus_ids = {str(sentence_id) for sentence_id in focus_context.get("sentence_ids", [])}
    cited_ids = content_density.evidence_sentence_ids(candidate, anchor)
    return term_matches + 3 * len(focus_ids & cited_ids)


def _select(
    candidates: Sequence[schema.HramatkaActivity],
    *,
    count_plan: Mapping[str, int],
    density_target: int,
    phase: int | None,
    policy: SelectorPolicy,
    state: _SelectionState,
    anchor: dict | None = None,
    focus_context: Mapping[str, Any] | None = None,
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
        ranked: list[tuple[tuple[Any, ...], int, schema.HramatkaActivity]] = []
        for original_index, candidate in remaining:
            activity_type = candidate.activity["type"]
            entry = ACTIVITY_REGISTRY[activity_type]
            identity = _activity_identity(candidate)
            if identity in state.activity_identities:
                continue
            if state.selected_types.get(activity_type, 0) >= count_plan.get(activity_type, 0):
                continue
            if policy.forbid_adjacent_repeated_type and state.last_type == activity_type:
                continue
            if (
                entry.is_puzzle
                and activity_type not in state.puzzle_types
                and len(state.puzzle_types) >= policy.max_puzzle_types
            ):
                continue
            pairs = entry.evidence_answer_pairs(candidate)
            if policy.require_content_density and not content_density.composition_eligible(
                candidate,
                anchor=anchor,
                phase=phase,
                state=state.sentence_reuse,
                forbid_duplicate_evidence_answer=policy.forbid_duplicate_evidence_answer,
                evidence_answers=state.evidence_answers,
                evidence_answer_pairs=pairs,
            ):
                continue
            derived_mode = content_density._is_derived_mode_candidate(candidate)
            candidate_stems = (
                content_density.derived_stems(candidate) if derived_mode else frozenset()
            )
            if derived_mode and candidate_stems & state.derived_stems:
                # A same-stem rewrite is not a novel derived exercise.
                continue
            core_lemmas = (
                content_density.derived_core_lemmas(candidate) if derived_mode else frozenset()
            )
            novelty = len(core_lemmas - state.derived_lemmas)
            if derived_mode and state.derived_lemmas and novelty == 0:
                # Derived slots need a new core lemma after the first one;
                # repeated paradigms are blocked rather than merely deprioritized.
                continue
            lemma_budget = len(core_lemmas)
            new_coverage = len(_coverage(candidate) - state.covered)
            variety = int(activity_type not in state.selected_types)
            # Response-unit ranking is a density optimization, not permission
            # to collapse the lesson into a few high-yield families.  Protect
            # the canonical four-family floor during the first pass; once it
            # is reached, response units resume as the stronger preference.
            variety_floor_boost = int(
                policy.prioritize_response_units
                and len(state.selected_types)
                < content_density.MIN_TEACHER_READY_ACTIVITY_TYPES
                and activity_type not in state.selected_types
            )
            productive_boost = int(
                policy.require_productive
                and activity_type in content_density.PRODUCTIVE_TYPES
                and (
                    (phase == 3 and not any(
                        item.activity.get("type") in content_density.PRODUCTIVE_TYPES
                        for item in selected
                    ))
                    or not (state.selected_types.keys() & content_density.PRODUCTIVE_TYPES)
                )
            )
            response_unit_boost = (
                content_density.response_units(candidate.activity)
                if policy.prioritize_response_units
                else 0
            )
            focus_boost = _focus_score(
                candidate,
                focus_context=focus_context,
                anchor=anchor,
            )
            tie_break = candidate.candidate_id or f"{original_index:06d}"
            # Keep one fixed-width, all-comparable key.  The discriminator
            # makes derived win an otherwise equal cross-mode comparison;
            # neutral padding preserves quoting-only ordering byte-for-byte.
            score = (
                (
                    -focus_boost,
                    -productive_boost,
                    -variety_floor_boost,
                    -response_unit_boost,
                    0,
                    0,
                    -novelty,
                    -lemma_budget,
                    -variety,
                    tie_break,
                )
                if derived_mode
                else (
                    -focus_boost,
                    -productive_boost,
                    -variety_floor_boost,
                    -response_unit_boost,
                    1,
                    -new_coverage,
                    0,
                    0,
                    -variety,
                    tie_break,
                )
            )
            ranked.append((score, original_index, candidate))

        if not ranked:
            break
        _score, original_index, winner = min(ranked)
        selected.append(winner)
        activity_type = winner.activity["type"]
        state.selected_types[activity_type] = state.selected_types.get(activity_type, 0) + 1
        derived_mode = content_density._is_derived_mode_candidate(winner)
        if derived_mode:
            state.derived_lemmas |= set(content_density.derived_core_lemmas(winner))
            state.derived_stems |= set(content_density.derived_stems(winner))
        else:
            state.covered |= _coverage(winner)
            state.evidence_answers |= set(
                ACTIVITY_REGISTRY[activity_type].evidence_answer_pairs(winner)
            )
        state.activity_identities.add(_activity_identity(winner))
        if not derived_mode:
            primary = content_density.primary_sentence_id(winner, anchor)
            content_density.register_sentence_use(
                sentence_ids=frozenset({primary}) if primary else frozenset(),
                primary=primary,
                phase=phase,
                operation=content_density.COGNITIVE_OPERATION.get(activity_type, activity_type),
                state=state.sentence_reuse,
            )
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
    anchor: dict | None = None,
    focus_context: Mapping[str, Any] | None = None,
) -> list[schema.HramatkaActivity]:
    """Greedily select the strongest ready candidates under Wave-0 rules."""
    return _select(
        candidates,
        count_plan=count_plan,
        density_target=policy.density_target,
        phase=phase,
        policy=policy,
        state=_SelectionState(),
        anchor=anchor,
        focus_context=focus_context,
    )


def select_composed_lesson(
    candidates_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]],
    *,
    slots_by_phase: Mapping[int, int],
    count_plan: Mapping[str, int],
    policy: SelectorPolicy = DEFAULT_POLICY,
    anchor: dict | None = None,
    focus_context: Mapping[str, Any] | None = None,
) -> dict[int, list[schema.HramatkaActivity]]:
    """Select a TTT lesson under one global composition policy."""
    state = _SelectionState()
    selected_by_phase: dict[int, list[schema.HramatkaActivity]] = {}
    phase_order = sorted(
        slots_by_phase.items(),
        key=lambda row: (row[0] != 3, row[0]) if policy.require_productive else (False, row[0]),
    )
    for phase, slots in phase_order:
        state.sentence_reuse.last_primary = None
        state.last_type = None
        selected_by_phase[phase] = _select(
            candidates_by_phase.get(phase, ()),
            count_plan=count_plan,
            density_target=slots,
            phase=phase,
            policy=policy,
            state=state,
            anchor=anchor,
            focus_context=focus_context,
        )
    return {phase: selected_by_phase[phase] for phase in sorted(selected_by_phase)}
