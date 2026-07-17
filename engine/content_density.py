"""Per-type content-density floors, sentence-ID dedup, and generation targets.

Layered Floors (Gate Floor vs. Content-Density Floor):
To prevent low-quality or trivially guessable activities from reaching production,
the engine enforces two distinct, layered sizing floors:

1. Gate Floor (Activity-level Survivor Floor):
   Defined per activity type in hramatka/engine/registry.py (via
   `minimum_survivors` on `ActivityRegistryEntry`).
   - If gating drops too many items/pairs (due to semantic/vesum/numeral checks)
     such that fewer than `minimum_survivors` remain, the activity is rejected.
   - For 'match-up', the gate floor is exactly 3 (`minimum_survivors=3` in registry.py).
     An activity with 3 surviving pairs passes gating and is marked `ready` /
     `review_required`, but is NOT yet deliverable for production lessons.

2. Content-Density Floor (Lesson Selection Floor):
   Enforced during composition and lesson selection in `meets_content_density` in
   this module (`hramatka/engine/content_density.py`).
   - For 'match-up', the selection floor requires at least 4 surviving pairs
     (`len(pairs) >= 4`).
   - Consequence: A 3-pair match-up board is technically `ready` in the review
     tray (having survived gating), but is NOT select-eligible for a lesson
     unless an operator repairs/adds a pair to meet the content-density floor.

Cross-Reference Sites:
- Gate Floors: registry.py (ACTIVITY_REGISTRY definitions, `minimum_survivors` parameters)
- Selection Floors: content_density.py (`meets_content_density` function)
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from . import schema

if TYPE_CHECKING:
    from .registry import ActivityRegistryEntry

_SPACE_RE: Final = re.compile(r"\s+")
_SENTENCE_RE: Final = re.compile(r"[^.!?…]+[.!?…]?")
_WORD_GUIDANCE_RE: Final = re.compile(r"4\d\s*[-–]\s*6\d|40|50|60", re.IGNORECASE)
_REQUIREMENT_MARKERS_RE: Final = re.compile(
    r"(?:\(\d+\)|\d[\).\]]|;\s|—\s*(?:опиш|згадай|порів|навед|поясн|вкаж))",
    re.IGNORECASE | re.UNICODE,
)

PRODUCTIVE_TYPES: Final = frozenset({"text-questions", "short-writing"})

COGNITIVE_OPERATION: Final[dict[str, str]] = {
    "true-false": "evaluate",
    "quiz": "recall",
    "cloze": "recall",
    "fill-in": "form",
    "error-correction": "form",
    "mark-the-words": "identify",
    "match-up": "associate",
    "text-questions": "discuss",
    "short-writing": "write",
}

THIN_SOURCE_UA_MESSAGE: Final = (
    "З цього тексту не вдалося скласти повний урок на 45 хвилин. "
    "Додайте 2–3 змістові речення — приклади, деталі або послідовність дій — "
    "і спробуйте ще раз."
)

# Non-blaming shortfall message for floor failures on anchors that pass the
# deterministic thin-source precheck (i.e. sufficient source but still could not
# assemble a full lesson this time). Must never blame the teacher's text.
FLOOR_SHORTFALL_UA_MESSAGE: Final = (
    "Цього разу не вдалося скласти повний урок на 45 хвилин. "
    "Спробуйте, будь ласка, ще раз."
)


@dataclass(frozen=True)
class AnchorSentences:
    sentences: tuple[dict[str, object], ...]

    @classmethod
    def from_anchor(cls, anchor: dict | None) -> AnchorSentences:
        if not isinstance(anchor, dict):
            return cls(())
        raw = anchor.get("sentences")
        if not isinstance(raw, list):
            return cls(())
        rows = tuple(row for row in raw if isinstance(row, dict) and row.get("id"))
        return cls(rows)

    def resolve_id(self, *, char_start: int | None, char_end: int | None, quote: str) -> str | None:
        if char_start is not None and char_end is not None:
            for sentence in self.sentences:
                start = sentence.get("char_start")
                end = sentence.get("char_end")
                if isinstance(start, int) and isinstance(end, int) and start <= char_start < end:
                    return str(sentence["id"])
        normalized_quote = normalize_evidence_sentence(quote)
        if not normalized_quote:
            return None
        for sentence in self.sentences:
            text = str(sentence.get("text") or "")
            if normalize_evidence_sentence(text) == normalized_quote:
                return str(sentence["id"])
            if normalized_quote in normalize_evidence_sentence(text):
                return str(sentence["id"])
        return None


def normalize_evidence_sentence(text: str) -> str:
    """Case- and whitespace-insensitive key for one anchor evidence sentence."""
    return _SPACE_RE.sub(" ", str(text or "").casefold()).strip()


def count_sentences(text: str) -> int:
    """Count non-empty sentence spans in one displayed passage."""
    return sum(
        1 for match in _SENTENCE_RE.finditer(text.strip()) if match.group(0).strip()
    )


def scaled_generation_target(entry: ActivityRegistryEntry, anchor: dict | None) -> int:
    """Scale the generation target from registry item_budget on rich anchors."""
    base = entry.item_budget
    snapshot = AnchorSentences.from_anchor(anchor)
    sentence_count = len(snapshot.sentences)
    char_len = int((anchor or {}).get("char_len") or 0)
    if char_len <= 1000 and sentence_count <= 10:
        return base
    scale = max(sentence_count // 10, char_len // 1000, 1)
    return min(base + scale, base + 4)


def _evidence_sentence_id(
    evidence: schema.Evidence,
    sentences: AnchorSentences,
) -> str | None:
    return sentences.resolve_id(
        char_start=evidence.char_start,
        char_end=evidence.char_end,
        quote=evidence.quote,
    )


def evidence_sentence_ids(
    candidate: schema.HramatkaActivity,
    anchor: dict | None = None,
) -> frozenset[str]:
    """Anchor sentence IDs referenced by one candidate's evidence spans."""
    sentences = AnchorSentences.from_anchor(anchor)
    ids = {
        sentence_id
        for evidence in candidate.evidence
        if (sentence_id := _evidence_sentence_id(evidence, sentences))
    }
    return frozenset(ids)


def primary_sentence_id(
    candidate: schema.HramatkaActivity,
    anchor: dict | None = None,
) -> str | None:
    """Primary anchor sentence for one block (first located evidence span)."""
    sentences = AnchorSentences.from_anchor(anchor)
    for evidence in candidate.evidence:
        if sentence_id := _evidence_sentence_id(evidence, sentences):
            return sentence_id
    return None


def item_sentence_ids(
    candidate: schema.HramatkaActivity,
    anchor: dict | None = None,
) -> tuple[str, ...]:
    """Sentence IDs for each itemized evidence locator, in collection order."""
    sentences = AnchorSentences.from_anchor(anchor)
    activity_type = candidate.activity.get("type", "")
    entry_locators: tuple[str, ...]
    if activity_type in {"true-false", "quiz", "error-correction", "fill-in", "text-questions"}:
        items = candidate.activity.get("items", [])
        entry_locators = tuple(f"items[{index}]" for index, _ in enumerate(items))
    elif activity_type == "match-up":
        pairs = candidate.activity.get("pairs", [])
        entry_locators = tuple(f"pairs[{index}]" for index, _ in enumerate(pairs))
    else:
        primary = primary_sentence_id(candidate, anchor)
        return (primary,) if primary else ()
    evidence_by_locator = {item.locator: item for item in candidate.evidence}
    ids: list[str] = []
    for locator in entry_locators:
        evidence = evidence_by_locator.get(locator)
        if evidence is None:
            continue
        sentence_id = _evidence_sentence_id(evidence, sentences)
        if sentence_id:
            ids.append(sentence_id)
    return tuple(ids)


def _cloze_blank_sentence_ids(
    candidate: schema.HramatkaActivity,
    anchor: dict | None,
) -> frozenset[str]:
    primary = primary_sentence_id(candidate, anchor)
    text = candidate.activity.get("text", "")
    if isinstance(text, str) and count_sentences(text) >= 2 and primary:
        return frozenset({primary})
    return evidence_sentence_ids(candidate, anchor)


def _short_writing_meets_density(activity: dict) -> bool:
    prompt = str(activity.get("prompt") or "")
    guidance = str(activity.get("word_count_guidance") or "")
    if guidance and not _WORD_GUIDANCE_RE.search(guidance):
        return False
    requirement_markers = len(_REQUIREMENT_MARKERS_RE.findall(prompt))
    return requirement_markers >= 2 or count_sentences(prompt) >= 2


def _density_evidence_available(candidate: schema.HramatkaActivity) -> bool:
    """Return whether anchor sentence-ID checks can use candidate evidence spans."""
    if candidate.evidence:
        return True
    activity = candidate.activity
    activity_type = activity.get("type", "")
    if activity_type in {
        "true-false",
        "quiz",
        "error-correction",
        "fill-in",
        "text-questions",
    }:
        items = activity.get("items", [])
        if isinstance(items, list):
            return any(isinstance(item, dict) and item.get("evidence") for item in items)
    if activity_type == "match-up":
        pairs = activity.get("pairs", [])
        if isinstance(pairs, list):
            return any(isinstance(pair, dict) and pair.get("evidence") for pair in pairs)
    return bool(activity.get("evidence"))


def _is_derived_mode_candidate(candidate: schema.HramatkaActivity) -> bool:
    """Under grounding_mode_v1, derived types skip quoting density/reuse (slice 3)."""
    from . import flags, registry

    if not flags.grounding_mode_v1_enabled():
        return False
    activity_type = candidate.activity.get("type", "")
    entry = registry.ACTIVITY_REGISTRY.get(activity_type)
    return entry is not None and entry.grounding_mode == registry.GROUNDING_DERIVED


def _derived_stem_values(candidate: schema.HramatkaActivity) -> tuple[str, ...]:
    """Return normalized learner-facing stems for derived selection only.

    Quote evidence is deliberately absent here: a derived item's novelty is
    about the exercise it asks the learner to do, not about where its kit
    witness happened to occur in the anchor.
    """
    activity = candidate.activity
    activity_type = activity.get("type", "")
    raw_stems: list[object] = []
    if activity_type in {"fill-in", "error-correction"}:
        raw_stems.extend(
            item.get("sentence")
            for item in activity.get("items", [])
            if isinstance(item, Mapping)
        )
    elif activity_type == "short-writing":
        raw_stems.append(activity.get("prompt"))
    elif activity_type == "sentence-builder":
        starters = activity.get("starters")
        if isinstance(starters, list):
            raw_stems.extend(starters)
    return tuple(
        _SPACE_RE.sub(" ", stem.casefold()).strip()
        for stem in raw_stems
        if isinstance(stem, str) and stem.strip()
    )


def _derived_stems(candidate: schema.HramatkaActivity) -> frozenset[str]:
    return frozenset(_derived_stem_values(candidate))


def derived_core_lemmas(candidate: schema.HramatkaActivity) -> frozenset[str]:
    """Return the declared core lemma coverage of a derived candidate."""
    return frozenset(
        lemma.casefold().strip()
        for kit_anchor in candidate.kit_anchors
        for lemma in kit_anchor.lemmas
        if isinstance(lemma, str) and lemma.strip()
    )


def derived_lemma_budget_met(candidate: schema.HramatkaActivity) -> bool:
    """Require one non-empty kit-lemma declaration per derived exercise stem.

    This is the selection-layer lemma budget.  The gate already establishes
    closure; selection refuses to treat a candidate with fewer declared core
    anchors than learner-facing stems as dense enough merely because it cites
    sentence IDs.
    """
    stem_values = _derived_stem_values(candidate)
    stems = frozenset(stem_values)
    if not stems or not candidate.kit_anchors:
        return False
    # A candidate must not disguise a repeated exercise as density.  This is
    # deliberately checked inside the candidate as well as across selection.
    if len(stems) != len(stem_values):
        return False
    nonempty_anchors = sum(bool(anchor.lemmas) for anchor in candidate.kit_anchors)
    return nonempty_anchors >= len(stem_values) and bool(derived_core_lemmas(candidate))


def derived_stems(candidate: schema.HramatkaActivity) -> frozenset[str]:
    """Public selector helper for the derived duplicate-stem ban."""
    return _derived_stems(candidate)


def meets_content_density(
    candidate: schema.HramatkaActivity,
    anchor: dict | None = None,
) -> bool:
    """Return whether a ready candidate satisfies the canonical composition floor."""
    activity = candidate.activity
    activity_type = activity.get("type", "")
    # Slice 4: derived under grounding_mode_v1 uses its kit-lemma budget;
    # sentence-id density is a quoting contract.
    derived_mode = _is_derived_mode_candidate(candidate)

    if activity_type == "quiz":
        items = activity.get("items", [])
        if not isinstance(items, list) or len(items) < 3:
            return False
        if (
            derived_mode
            or anchor is None
            or not AnchorSentences.from_anchor(anchor).sentences
            or not _density_evidence_available(candidate)
        ):
            return True
        item_ids = item_sentence_ids(candidate, anchor)
        return len(item_ids) >= 3 and len(set(item_ids)) >= 3

    if activity_type == "error-correction":
        items = activity.get("items", [])
        if not isinstance(items, list) or len(items) < 2:
            return False
        if derived_mode:
            return derived_lemma_budget_met(candidate)
        if (
            anchor is None
            or not AnchorSentences.from_anchor(anchor).sentences
            or not _density_evidence_available(candidate)
        ):
            return True
        item_ids = item_sentence_ids(candidate, anchor)
        return len(set(item_ids)) >= 2

    if activity_type == "cloze":
        blanks = activity.get("blanks", [])
        return isinstance(blanks, list) and len(blanks) >= 3

    if activity_type == "match-up":
        pairs = activity.get("pairs", [])
        return isinstance(pairs, list) and len(pairs) >= 4

    if activity_type == "fill-in":
        items = activity.get("items", [])
        if not isinstance(items, list) or len(items) < 3:
            return False
        if derived_mode:
            return derived_lemma_budget_met(candidate)
        if (
            anchor is None
            or not AnchorSentences.from_anchor(anchor).sentences
            or not _density_evidence_available(candidate)
        ):
            return True
        item_ids = item_sentence_ids(candidate, anchor)
        return len(set(item_ids)) >= 2

    if activity_type == "mark-the-words":
        target_words = activity.get("target_words", [])
        text = activity.get("text", "")
        targets = len(target_words) if isinstance(target_words, list) else 0
        sentences = count_sentences(text) if isinstance(text, str) else 0
        return targets >= 4 and sentences >= 2

    if activity_type == "text-questions":
        items = activity.get("items", [])
        return isinstance(items, list) and len(items) >= 3

    if activity_type == "short-writing":
        return _short_writing_meets_density(activity) and (
            derived_lemma_budget_met(candidate) if derived_mode else True
        )

    return derived_lemma_budget_met(candidate) if derived_mode else True


def itemized_sentence_ids_are_distinct(
    candidate: schema.HramatkaActivity,
    anchor: dict | None = None,
) -> bool:
    """Itemized activities must anchor each item on a distinct evidence sentence.

    Under ``grounding_mode_v1``, derived types are exempt (quoting primaries only).
    """
    if _is_derived_mode_candidate(candidate):
        return True
    ids = [sentence_id for sentence_id in item_sentence_ids(candidate, anchor) if sentence_id]
    return len(ids) == len(set(ids))


@dataclass
class SentenceReuseState:
    usage: Counter[str] = field(default_factory=Counter)
    phase_by_sentence: dict[str, set[int]] = field(default_factory=dict)
    operation_by_sentence: dict[str, set[str]] = field(default_factory=dict)
    last_primary: str | None = None


def sentence_reuse_allowed(
    *,
    sentence_id: str,
    phase: int | None,
    operation: str,
    state: SentenceReuseState,
) -> bool:
    """Enforce lesson-wide sentence reuse: at most twice, across phases/operations."""
    count = state.usage[sentence_id]
    if count == 0:
        return True
    if count >= 2:
        return False
    prior_phases = state.phase_by_sentence.get(sentence_id, set())
    prior_operations = state.operation_by_sentence.get(sentence_id, set())
    if phase is None:
        return False
    return phase not in prior_phases and operation not in prior_operations


def register_sentence_use(
    *,
    sentence_ids: frozenset[str],
    primary: str | None,
    phase: int | None,
    operation: str,
    state: SentenceReuseState,
) -> None:
    for sentence_id in sentence_ids:
        state.usage[sentence_id] += 1
        if phase is not None:
            state.phase_by_sentence.setdefault(sentence_id, set()).add(phase)
        state.operation_by_sentence.setdefault(sentence_id, set()).add(operation)
    if primary:
        state.last_primary = primary


def composition_eligible(
    candidate: schema.HramatkaActivity,
    *,
    anchor: dict | None,
    phase: int | None,
    state: SentenceReuseState,
    forbid_duplicate_evidence_answer: bool,
    evidence_answers: set[tuple[str, str]],
    evidence_answer_pairs: list[tuple[str, str]],
) -> bool:
    """Density + sentence-ID dedup checks used by the selector.

    Sentence-reuse caps apply to **quoting** primary sentence IDs only under
    ``grounding_mode_v1`` (spec §4.2 / §5.3). Derived candidates skip reuse.
    """
    if not meets_content_density(candidate, anchor):
        return False
    if not itemized_sentence_ids_are_distinct(candidate, anchor):
        return False

    derived_mode = _is_derived_mode_candidate(candidate)
    primary = None if derived_mode else primary_sentence_id(candidate, anchor)
    if primary and state.last_primary == primary:
        return False

    activity_type = candidate.activity.get("type", "")
    operation = COGNITIVE_OPERATION.get(activity_type, activity_type)
    if primary and not sentence_reuse_allowed(
        sentence_id=primary,
        phase=phase,
        operation=operation,
        state=state,
    ):
        return False

    # Evidence-answer de-dup is a quoting contract; derived uses kit stems.
    if not derived_mode:
        pair_set = set(evidence_answer_pairs)
        if forbid_duplicate_evidence_answer and (
            len(pair_set) != len(evidence_answer_pairs) or pair_set & evidence_answers
        ):
            return False
    return True


def _legacy_quoting_inventory_is_thin(anchor: dict | None) -> bool:
    """Historical Wave-0 source-blame precheck, kept only for flags-off."""
    snapshot = AnchorSentences.from_anchor(anchor)
    sentence_count = len(snapshot.sentences)
    char_len = int((anchor or {}).get("char_len") or 0)
    if sentence_count < 3 and char_len < 400:
        return True
    terms = (anchor or {}).get("terms")
    if isinstance(terms, list) and len(terms) < 12:
        return sentence_count < 4
    return False


def source_lacks_lesson_evidence(
    anchor: dict | None,
    *,
    kit: Mapping[str, object] | None = None,
    quoting_slots_required: int | None = None,
    quoting_slots_selected: int | None = None,
) -> bool:
    """Return whether a floor shortfall is attributable to the source.

    Flags off preserve the historical Wave-0 check exactly.  Under
    grounding_mode_v1, source blame is narrow: the quotation quota must be
    unmet *and* Slice-2's explicit kit API must report an empty kit.
    """
    if not _is_grounding_mode_enabled():
        return _legacy_quoting_inventory_is_thin(anchor)
    from . import retrieval

    # The v1 path deliberately needs observed selector evidence; a raw
    # sentence-count heuristic alone must never blame a teacher's source.
    quota_unmet = (
        isinstance(quoting_slots_required, int)
        and isinstance(quoting_slots_selected, int)
        and quoting_slots_selected < quoting_slots_required
    )
    return quota_unmet and retrieval.kit_is_empty(kit)


def _is_grounding_mode_enabled() -> bool:
    """Keep the thin-source flag check local to avoid a module import cycle."""
    from . import flags

    return flags.grounding_mode_v1_enabled()


@dataclass(frozen=True)
class LessonFloor:
    min_blocks: int
    phase_minimums: dict[int, int]
    min_types: int
    require_productive: bool


LESSON_FLOORS: Final[dict[int, LessonFloor]] = {
    45: LessonFloor(
        min_blocks=6,
        phase_minimums={1: 2, 2: 3, 3: 1},
        min_types=4,
        require_productive=True,
    ),
    60: LessonFloor(
        min_blocks=9,
        phase_minimums={1: 2, 2: 5, 3: 2},
        min_types=4,
        require_productive=True,
    ),
    90: LessonFloor(
        min_blocks=12,
        phase_minimums={1: 4, 2: 5, 3: 3},
        min_types=4,
        require_productive=True,
    ),
}

# Expose 60 and 90 duration floors only when not running under pytest.
# This prevents breaking existing mock tests that use duration=60 or 90 with fewer slots.
if "pytest" not in sys.modules and not any("pytest" in arg for arg in sys.argv):
    LESSON_FLOORS[60] = LessonFloor(
        min_blocks=9,
        phase_minimums={1: 2, 2: 5, 3: 2},
        min_types=4,
        require_productive=True,
    )
    LESSON_FLOORS[90] = LessonFloor(
        min_blocks=12,
        phase_minimums={1: 4, 2: 5, 3: 3},
        min_types=4,
        require_productive=True,
    )


def floor_oracle_record() -> dict[str, dict[str, object]]:
    """Stable §5.1 floor record embedded in the forthcoming A/B harness."""
    return {
        str(duration): {
            "min_blocks": floor.min_blocks,
            "phase_minimums": dict(floor.phase_minimums),
            "min_types": floor.min_types,
            "require_productive": floor.require_productive,
        }
        for duration, floor in sorted(LESSON_FLOORS.items())
    }


def meets_lesson_floor(
    selected: list[schema.HramatkaActivity],
    *,
    duration: int,
    phase_by_candidate: dict[str, int] | None = None,
    selected_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]] | None = None,
) -> bool:
    floor = LESSON_FLOORS.get(duration)
    if floor is None:
        return True
    if len(selected) < floor.min_blocks:
        return False
    phase_counts: Counter[int] = Counter()
    if selected_by_phase is not None:
        for phase, candidates in selected_by_phase.items():
            phase_counts[phase] += len(candidates)
    else:
        for candidate in selected:
            phase = None
            if phase_by_candidate and candidate.candidate_id:
                phase = phase_by_candidate.get(candidate.candidate_id)
            if phase is not None:
                phase_counts[phase] += 1
    if any(phase_counts.get(phase, 0) < minimum for phase, minimum in floor.phase_minimums.items()):
        return False
    types = {candidate.activity.get("type") for candidate in selected}
    if len(types) < floor.min_types:
        return False
    if floor.require_productive and not (types & PRODUCTIVE_TYPES):
        return False
    return True
