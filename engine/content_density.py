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
   - Consequence: A 3-pair match-up board is technically gate-passing in the
     review tray. It is never auto-selected, but its actual three response
     units now contribute to teacher-ready floor accounting until the teacher
     explicitly acknowledges the tray item.

Cross-Reference Sites:
- Gate Floors: registry.py (ACTIVITY_REGISTRY definitions, `minimum_survivors` parameters)
- Selection Floors: content_density.py (`meets_content_density` function)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from . import schema
from .sentence_segmentation_v1 import sentence_spans

if TYPE_CHECKING:
    from .registry import ActivityRegistryEntry

_SPACE_RE: Final = re.compile(r"\s+")
_WORD_GUIDANCE_RE: Final = re.compile(r"4\d\s*[-–]\s*6\d|40|50|60", re.IGNORECASE)
_REQUIREMENT_MARKERS_RE: Final = re.compile(
    r"(?:\(\d+\)|\d[\).\]]|;\s|—\s*(?:опиш|згадай|порів|навед|поясн|вкаж))",
    re.IGNORECASE | re.UNICODE,
)
_WORD_RE: Final = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’-]+", re.UNICODE)

PRODUCTIVE_TYPES: Final = frozenset({"text-questions", "short-writing"})
MIN_TEACHER_READY_ACTIVITY_TYPES: Final = 4
TEACHER_READY_DENSITY_VERSION: Final = "TeacherReadyDensity.v2"

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
    "З цього тексту не вдалося скласти повний урок. "
    "Додайте 2–3 змістові речення — приклади, деталі або послідовність дій — "
    "і спробуйте ще раз."
)

# Non-blaming shortfall message for floor failures on anchors that pass the
# deterministic thin-source precheck (i.e. sufficient source but still could not
# assemble a full lesson this time). Must never blame the teacher's text.
FLOOR_SHORTFALL_UA_MESSAGE: Final = (
    "Цього разу не вдалося скласти повний урок. Спробуйте, будь ласка, ще раз."
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
    return len(sentence_spans(text))


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
            item.get("sentence") for item in activity.get("items", []) if isinstance(item, Mapping)
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

    if activity_type == "true-false":
        items = activity.get("items", [])
        return isinstance(items, list) and len(items) >= delivered_item_floors()["true-false"]

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
        return isinstance(blanks, list) and len(blanks) >= delivered_item_floors()["cloze"]

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
class TeacherReadyDensity:
    """One teacher-delivery contract, independent of test/runtime environment."""

    duration: int
    phase_blocks: Mapping[int, int]
    minimum_response_units: int

    @property
    def min_blocks(self) -> int:
        """Compatibility view derived from the exact phase shape."""
        return sum(self.phase_blocks.values())

    @property
    def phase_minimums(self) -> Mapping[int, int]:
        """Compatibility view of the per-phase teacher-ready minimums."""
        return self.phase_blocks

    @property
    def min_types(self) -> int:
        """Return the enforced minimum number of distinct activity families."""
        return MIN_TEACHER_READY_ACTIVITY_TYPES

    @property
    def require_productive(self) -> bool:
        return True


TEACHER_READY_DENSITIES: Final[dict[int, TeacherReadyDensity]] = {
    45: TeacherReadyDensity(45, {1: 3, 2: 4, 3: 1}, 28),
    60: TeacherReadyDensity(60, {1: 3, 2: 5, 3: 2}, 35),
    90: TeacherReadyDensity(90, {1: 4, 2: 5, 3: 3}, 42),
}

# A thin compatibility name only.  It is derived from the single teacher-ready
# authority above and must never become a second delivery policy.
LESSON_FLOORS: Final[dict[int, TeacherReadyDensity]] = TEACHER_READY_DENSITIES
LessonFloor = TeacherReadyDensity


def teacher_ready_density(duration: int) -> TeacherReadyDensity:
    """Return the exact contract for a supported teacher lesson duration."""
    try:
        return TEACHER_READY_DENSITIES[duration]
    except KeyError as exc:
        raise ValueError(f"Unsupported teacher-ready duration: {duration!r}") from exc


def registry_item_targets() -> dict[str, int]:
    """Read exact generation targets from the registry after its construction.

    ``registry`` imports this module, so this deliberately local import keeps
    the authoritative per-type budget in one place without an import cycle.
    """
    from .registry import ACTIVITY_REGISTRY

    return {
        activity_type: entry.item_budget
        for activity_type, entry in sorted(ACTIVITY_REGISTRY.items())
    }


def delivered_item_floors() -> dict[str, int]:
    """Return the per-block lesson-delivery floors, separate from generation.

    The generator deliberately asks for the exact registry target (notably five
    true/false items).  A gated, selected block remains deliverable with four
    true/false items, but it may never borrow units from another block of the
    same type to conceal a sparse result.
    """
    return {
        "true-false": 4,
        "quiz": 3,
        "cloze": 3,
        "fill-in": 3,
        "match-up": 4,
        "mark-the-words": 4,
        "error-correction": 2,
        "text-questions": 3,
        "short-writing": 1,
    }


def teacher_ready_density_record() -> dict[str, object]:
    """Canonical public-safe input used for fingerprinting and telemetry."""
    return {
        "version": TEACHER_READY_DENSITY_VERSION,
        "floor_accounting": "ready_plus_review_tray",
        "durations": {
            str(duration): {
                "phase_blocks": dict(contract.phase_blocks),
                "minimum_response_units": contract.minimum_response_units,
                "minimum_activity_types": contract.min_types,
                "require_productive_transfer": contract.require_productive,
            }
            for duration, contract in sorted(TEACHER_READY_DENSITIES.items())
        },
        "generation_item_targets": registry_item_targets(),
        "delivered_item_floors": delivered_item_floors(),
    }


def teacher_ready_density_digest() -> str:
    """Stable digest of the complete delivery contract, never of lesson content."""
    encoded = json.dumps(
        teacher_ready_density_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def response_units(activity: Mapping[str, object]) -> int:
    """Return the learner-response units an activity contributes."""
    activity_type = activity.get("type")
    collection_by_type = {
        "true-false": "items",
        "quiz": "items",
        "error-correction": "items",
        "fill-in": "items",
        "cloze": "blanks",
        "match-up": "pairs",
        "mark-the-words": "target_words",
        "text-questions": "items",
    }
    collection = collection_by_type.get(activity_type)
    if collection is not None:
        value = activity.get(collection)
        return len(value) if isinstance(value, list) else 0
    return 1 if activity_type == "short-writing" else 0


# Kept private-name compatible for older internal callers while the selector
# and delivery evaluator share the explicit public calculation above.
_response_units = response_units


def _phase_three_transfer_errors(
    phase_three: Sequence[schema.HramatkaActivity],
) -> list[str]:
    """Require productive transfer somewhere in the final phase.

    A multi-block final phase can legitimately contain writing, discussion, and
    a closed-response consolidation task.  Productive transfer is therefore an
    aggregate phase property, rather than a restriction on every phase-three
    block.  Text-question grounding stays strict and reads the parsed evidence
    retained on the candidate, not the public activity projection.
    """
    productive = [
        candidate for candidate in phase_three if candidate.activity.get("type") in PRODUCTIVE_TYPES
    ]
    if not productive:
        return ["phase_3_requires_productive_transfer"]
    if any(candidate.activity.get("type") == "short-writing" for candidate in productive):
        return []
    text_questions = [
        candidate for candidate in productive if candidate.activity.get("type") == "text-questions"
    ]
    if not text_questions:
        return []
    candidate = text_questions[-1]
    items = candidate.activity.get("items")
    if not isinstance(items, list) or len(items) < 3:
        return ["phase_3_text_questions_require_three_moves"]
    questions = [
        str(item.get("question") or "").casefold() for item in items[:3] if isinstance(item, dict)
    ]
    evidence_by_locator = {
        evidence.locator: evidence.quote.strip()
        for evidence in candidate.evidence
        if isinstance(evidence.locator, str) and isinstance(evidence.quote, str)
    }
    if len(questions) != 3 or not all(
        evidence_by_locator.get(f"items[{index}]") for index in range(3)
    ):
        return ["phase_3_text_questions_require_anchored_moves"]
    comprehension = ("що", "хто", "де", "коли", "скільки", "який", "яка", "які", "назвіть")
    explanation = ("чому", "як", "поясніть", "виснов")
    application_tokens = frozenset({"ви", "ваш", "свій", "власний", "власному", "досвід"})
    application_bigrams = {("наведіть", "приклад"), ("застосуєте", "це")}
    if not any(marker in questions[0] for marker in comprehension):
        return ["phase_3_text_questions_require_comprehension"]
    if not any(marker in questions[1] for marker in explanation):
        return ["phase_3_text_questions_require_explanation"]
    question_tokens = tuple(token.casefold() for token in _WORD_RE.findall(questions[2]))
    if not (
        application_tokens.intersection(question_tokens)
        or any(
            question_tokens[index : index + 2] in application_bigrams
            for index in range(len(question_tokens) - 1)
        )
    ):
        return ["phase_3_text_questions_require_anchored_application"]
    return []


@dataclass(frozen=True)
class TeacherReadyDensityReceipt:
    """Safe deterministic result for delivery, telemetry, and qualification."""

    duration: int
    phase_counts: Mapping[int, int]
    ready_phase_counts: Mapping[int, int]
    tray_phase_counts: Mapping[int, int]
    delivered_blocks: int
    ready_blocks: int
    tray_blocks: int
    floor_blocks: int
    response_units: int
    ready_response_units: int
    tray_response_units: int
    type_units: Mapping[str, int]
    errors: tuple[str, ...]
    version: str = TEACHER_READY_DENSITY_VERSION
    digest: str = ""

    @property
    def ready(self) -> bool:
        return not self.errors

    def telemetry_record(self) -> dict[str, object]:
        return {
            "version": self.version,
            "digest": self.digest,
            "duration": self.duration,
            "phase_counts": {
                str(phase): count for phase, count in sorted(self.phase_counts.items())
            },
            "ready_phase_counts": {
                str(phase): count for phase, count in sorted(self.ready_phase_counts.items())
            },
            "tray_phase_counts": {
                str(phase): count for phase, count in sorted(self.tray_phase_counts.items())
            },
            "delivered_blocks": self.delivered_blocks,
            "ready_blocks": self.ready_blocks,
            "tray_blocks": self.tray_blocks,
            "floor_blocks": self.floor_blocks,
            "response_units": self.response_units,
            "ready_response_units": self.ready_response_units,
            "tray_response_units": self.tray_response_units,
            "type_units": dict(sorted(self.type_units.items())),
            "disposition": "teacher_ready" if self.ready else "recoverable_draft",
            "error_codes": list(self.errors),
        }


def evaluate_teacher_ready_density(
    selected_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]],
    *,
    duration: int,
    tray_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]] | None = None,
) -> TeacherReadyDensityReceipt:
    """Evaluate the teacher-ready floor without promoting tray content.

    ``selected_by_phase`` contains auto-ready blocks. ``tray_by_phase`` contains
    gate-passing blocks a teacher can inspect and explicitly acknowledge. Both
    contribute to the floor; only the former are automatically included in the
    lesson document.
    """
    contract = teacher_ready_density(duration)
    tray_by_phase = tray_by_phase or {}
    ready_phase_counts = {
        phase: len(selected_by_phase.get(phase, ())) for phase in contract.phase_blocks
    }
    tray_phase_counts = {
        phase: len(tray_by_phase.get(phase, ())) for phase in contract.phase_blocks
    }
    phase_counts = {
        phase: ready_phase_counts[phase] + tray_phase_counts[phase]
        for phase in contract.phase_blocks
    }
    ready = [
        candidate
        for phase in sorted(contract.phase_blocks)
        for candidate in selected_by_phase.get(phase, ())
    ]
    tray = [
        candidate
        for phase in sorted(contract.phase_blocks)
        for candidate in tray_by_phase.get(phase, ())
    ]
    selected = [*ready, *tray]
    type_units: Counter[str] = Counter()
    ready_response_units = sum(response_units(candidate.activity) for candidate in ready)
    tray_response_units = sum(response_units(candidate.activity) for candidate in tray)
    errors: list[str] = []
    delivered_floors = delivered_item_floors()
    for index, candidate in enumerate(ready, start=1):
        activity_type = str(candidate.activity.get("type") or "")
        type_units[activity_type] += response_units(candidate.activity)
        if activity_type not in delivered_floors:
            errors.append(f"unknown_activity_type_{activity_type}")
        elif not meets_content_density(candidate):
            errors.append(f"block_{index}_{activity_type}_below_delivered_floor")
    for candidate in tray:
        activity_type = str(candidate.activity.get("type") or "")
        type_units[activity_type] += response_units(candidate.activity)
        if activity_type not in delivered_floors:
            errors.append(f"unknown_activity_type_{activity_type}")

    for phase, expected in contract.phase_blocks.items():
        if phase_counts[phase] < expected:
            errors.append(f"phase_{phase}_blocks_{phase_counts[phase]}_expected_{expected}")
    total_response_units = sum(type_units.values())
    if total_response_units < contract.minimum_response_units:
        errors.append(
            f"response_units_{total_response_units}_minimum_{contract.minimum_response_units}"
        )
    for activity_type, units in sorted(type_units.items()):
        target = delivered_floors.get(activity_type)
        if target is None:
            continue
        elif units < target:
            errors.append(f"{activity_type}_units_{units}_minimum_{target}")
    if len(type_units) < contract.min_types:
        errors.append(f"activity_types_{len(type_units)}_minimum_{contract.min_types}")
    errors.extend(
        _phase_three_transfer_errors([*selected_by_phase.get(3, ()), *tray_by_phase.get(3, ())])
    )
    return TeacherReadyDensityReceipt(
        duration=duration,
        phase_counts=phase_counts,
        ready_phase_counts=ready_phase_counts,
        tray_phase_counts=tray_phase_counts,
        delivered_blocks=len(ready),
        ready_blocks=len(ready),
        tray_blocks=len(tray),
        floor_blocks=len(selected),
        response_units=total_response_units,
        ready_response_units=ready_response_units,
        tray_response_units=tray_response_units,
        type_units=dict(type_units),
        errors=tuple(errors),
        digest=teacher_ready_density_digest(),
    )


def floor_oracle_record() -> dict[str, object]:
    """Frozen measurement oracle, including every teacher-ready policy input."""
    compatibility_floors = {
        str(duration): {
            "min_blocks": contract.min_blocks,
            "phase_minimums": dict(contract.phase_blocks),
            "min_types": contract.min_types,
            "require_productive": contract.require_productive,
            "minimum_response_units": contract.minimum_response_units,
            "version": TEACHER_READY_DENSITY_VERSION,
            "digest": teacher_ready_density_digest(),
        }
        for duration, contract in sorted(TEACHER_READY_DENSITIES.items())
    }
    return {
        "teacher_ready_density": teacher_ready_density_record(),
        "compatibility_floors": compatibility_floors,
    }


def meets_lesson_floor(
    selected: list[schema.HramatkaActivity],
    *,
    duration: int,
    phase_by_candidate: dict[str, int] | None = None,
    selected_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]] | None = None,
    tray_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]] | None = None,
) -> bool:
    """Fail-closed compatibility adapter around the canonical evaluator.

    A flat candidate list cannot prove the exact Test-Teach-Test delivery shape.
    Legacy callers that omit both phase representations therefore receive
    ``False`` rather than an ambiguous local-density success.
    """
    if selected_by_phase is None and phase_by_candidate is None:
        return False
    if selected_by_phase is None:
        grouped: dict[int, list[schema.HramatkaActivity]] = {1: [], 2: [], 3: []}
        for candidate in selected:
            phase = phase_by_candidate.get(candidate.candidate_id) if phase_by_candidate else None
            if phase in grouped:
                grouped[phase].append(candidate)
        selected_by_phase = grouped
    return evaluate_teacher_ready_density(
        selected_by_phase, duration=duration, tray_by_phase=tray_by_phase
    ).ready
