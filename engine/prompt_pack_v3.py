"""Live v3.3 serializer pack for ``TeacherReadyDensity.v3``.

This module consumes only the exact-cover allocation made before generation.
It is the protocol boundary between immutable unit plans and the live raw
activity renderer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, Final

from . import paths
from .anchor_inventory_v3 import DEGREE_CATALOG_VERSION, DEGREE_LADDERS, degree_role
from .closed_class_policy import (
    CLOSED_CLASS_ALLOWED_SHAPES,
    is_closed_class_form,
)
from .lesson_capacity_v3 import LessonAllocation
from .linguistics import verify_lemma, verify_words
from .serializer_policy import serializer_temperature
from .teacher_ready_density_v3 import floor_for

PROMPT_PACK_VERSION = "PromptPackInput.v3.4"
TEMPLATE_VERSION = "gemma-phase-pack.v3.15"
TEMPLATE_SHA256: Final[str] = "31691f4ab0c093fe34d5f122f472f050e629eb08426a45b7b15053e7b141145d"
TYPE_KIT_IDENTITY = "TeacherReadyDensity.v3.unit-plan-kit.v3"

_TRUE_FALSE_NARRATION_RE = re.compile(r"\(\s*(?:true|false)\s*\)", re.IGNORECASE)
_TEMPLATE_PATH = Path(__file__).with_name("prompts") / "gemma-phase-pack.v3.15.md"

# Literal strings that appear only in the compact synthetic schema shapes.  Their
# presence in a model response means the serializer copied the exemplar instead
# of generating teacher-ready content from the certified substrate.
EXEMPLAR_ONLY_STRINGS = frozenset(
    {
        "SYNTHETIC-QUIZ-STEM",
        "SYNTHETIC-CLOZE-TEXT",
        "SYNTHETIC-FILLIN-STEM",
        "SYNTHETIC-TRUEFALSE-STEM",
        "SYNTHETIC-MATCH-LEFT",
        "SYNTHETIC-MATCH-RIGHT",
        "SYNTHETIC-ERROR-SOURCE",
        "SYNTHETIC-ERROR-CORRECTION",
        "SYNTHETIC-OPEN-QUESTION",
        "SYNTHETIC-WRITING-PROMPT",
        "SYNTHETIC-WRITING-GUIDANCE",
        "SYNTHETIC-WRITING-TARGET",
        "SYNTHETIC-MARK-TEXT",
        "SYNTHETIC-DISTRACTOR",
        "Синтетична вказівка.",
    }
)
_EXEMPLAR_QUIZ_QUESTION_TEMPLATE = "Вкажіть правильну форму: {}"


class PromptPackV3Error(ValueError):
    """A deterministic v3.3 pack serialization or validation failure."""


class RepairableSerializationError(PromptPackV3Error):
    """A model-side binding failure that can be re-rendered from the fixed kit."""


class RepairableGapConstructionError(PromptPackV3Error):
    """A content-free gap-shape failure that can be re-rendered from the fixed kit."""


class RuleNamedRejection(PromptPackV3Error):
    """A deterministic model-output rejection with a stable, safe rule key."""

    def __init__(self, rule_key: str, *, suffix: str | None = None) -> None:
        self.rule_key = rule_key
        self.suffix = suffix
        super().__init__(rule_key if suffix is None else f"{rule_key}: {suffix}")


class RuleNamedRejectionGroup(PromptPackV3Error):
    """Several independently repairable item-local failures from one gate."""

    def __init__(self, rejections: Sequence[RuleNamedRejection]) -> None:
        self.rejections = tuple(rejections)
        if len(self.rejections) < 2:
            raise ValueError("A rejection group needs at least two item-local failures.")
        super().__init__("; ".join(str(rejection) for rejection in self.rejections))


DeterministicGate = Callable[[Mapping[str, Any], Mapping[str, Any]], None]
RawContractValidator = Callable[[Mapping[str, Any]], None]

_DETERMINISTIC_GATE_RULE_KEYS: Final[dict[str, str]] = {
    "_activity_gate": "activity_binding",
    "validate_exemplar_contamination": "exemplar_contamination",
    "validate_verbatim_answer_ban": "verbatim_answer_ban",
    "validate_elicitation_shape": "elicitation_shape",
    "validate_gap_construction": "gap_construction",
    "validate_distractor_adjacency": "distractor_adjacency",
    "validate_non_revealing_sequence": "non_revealing_sequence",
    "validate_activity_purpose": "activity_purpose",
    "validate_visible_writing_constraints": "short_writing_visible_constraints",
}
_ACTIVITY_PURPOSE_SAFE_PREFIXES: Final[dict[str, str]] = {
    "text question asks for a token label instead of meaning": "token_retrieval",
    "text question ignores its certified question category": "question_category_mismatch",
    "text question ignores its certified purpose intent": "question_intent_mismatch",
    "text question is detached from its certified topic": "certified_topic_missing",
    "text question is detached from its rendering surface": "source_lemma_overlap_missing",
    "text question omits its certified comparison": "degree_comparison_missing",
    "text question turns a contrast into one causal reason": "contrast_causality_malformed",
    "text question uses generic text-summary metadiscourse": "generic_metadiscourse",
    "text question uses a generic modal relation": "generic_modal_relation",
    "text question uses a vague application object": "vague_application_object",
    "text question pairs experience with a stative predicate": "stative_experience",
    "text question imports source second person": "source_person_import",
    "text question restates its expected answer": "answer_leak",
    "text question consumes its source answer": "answer_restatement",
    "text question uses unresolved source deixis": "unresolved_reference",
    "text question does not center a learner application": "application_not_learner_centered",
}


def _activity_purpose_safe_suffix(error: Exception) -> str | None:
    """Return an actionable content-free code and failing item index."""
    message = str(error)
    code = next(
        (
            safe_code
            for prefix, safe_code in _ACTIVITY_PURPOSE_SAFE_PREFIXES.items()
            if message.startswith(prefix)
        ),
        None,
    )
    if code is None:
        return None
    index = re.search(r"items\[(\d+)\]", message)
    return code if index is None else f"{code}:item={index.group(1)}"


def _activity_purpose_item_rejections(
    gate: DeterministicGate,
    activity: Mapping[str, Any],
    type_kit: Mapping[str, Any],
) -> tuple[RuleNamedRejection, ...]:
    """Collect every independent text-question failure without weakening the gate."""
    payload = activity.get("payload")
    units = type_kit.get("certified_units")
    if (
        not isinstance(payload, Mapping)
        or payload.get("type") != "text-questions"
        or not isinstance(payload.get("items"), list)
        or not isinstance(units, list)
        or len(payload["items"]) != len(units)
    ):
        return ()
    narrowed_activity = deepcopy(dict(activity))
    narrowed_kit = deepcopy(dict(type_kit))
    narrowed_payload = narrowed_activity.get("payload")
    narrowed_units = narrowed_kit.get("certified_units")
    if not isinstance(narrowed_payload, dict) or not isinstance(narrowed_units, list):
        return ()
    narrowed_items = narrowed_payload.get("items")
    if not isinstance(narrowed_items, list):
        return ()
    original_indexes = list(range(len(narrowed_items)))
    rejections: list[RuleNamedRejection] = []
    while narrowed_items:
        try:
            gate(narrowed_activity, narrowed_kit)
            break
        except PromptPackV3Error as error:
            suffix = _activity_purpose_safe_suffix(error)
            item_match = re.search(r":item=(\d+)$", suffix or "")
            if item_match is None:
                raise
            relative_index = int(item_match.group(1))
            if relative_index >= len(original_indexes):
                raise
            code = suffix[: item_match.start()]
            original_index = original_indexes.pop(relative_index)
            rejections.append(
                RuleNamedRejection("activity_purpose", suffix=f"{code}:item={original_index}")
            )
            del narrowed_items[relative_index]
            del narrowed_units[relative_index]
    return tuple(rejections)


_CLOZE_MARKER_RE = re.compile(r"\{[1-9]\d*\}")
_UKRAINIAN_WORD_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґʼ’'-]+")
_EXPLICIT_CAUSAL_SURFACE_RE = re.compile(
    r"\b(?:тому|бо|адже|оскільки|завдяки|через\s+те)\b",
    re.IGNORECASE,
)
_SOURCE_RELATION_INTENTS: Final[frozenset[str]] = frozenset(
    {
        "causal-clause.v1",
        "purpose-clause.v1",
        "temporal-clause.v1",
        "definition-content.v1",
        "licensed-vid-cause.v1",
    }
)
_FREQUENCY_SCALE_FORMS: Final[frozenset[str]] = frozenset(
    {"рідше", "частіше", "рідко", "часто"}
)
_FREQUENCY_COMPARATIVES: Final[frozenset[str]] = frozenset({"рідше", "частіше"})
_UNRESOLVED_QUESTION_DEIXIS_RE = re.compile(
    r"\b(?:цих|цьому|цього)\b",
    re.IGNORECASE,
)
_UNRESOLVED_TOPIC_PRONOUN_RE = re.compile(r"^(?:він|вона|вони|воно)\b", re.IGNORECASE)

_TYPE_PURPOSE_CONTRACTS: Final[dict[str, dict[str, object]]] = {
    "quiz": {
        "purpose": "test one grammatical or lexical choice in meaningful context",
        "required": "copy each certified_unit.gapped_rendering_surface byte for byte",
        "reject": "successively blanking one sentence or exposing another unit answer",
    },
    "cloze": {
        "purpose": "read a coherent passage and restore context-supported forms",
        "required": "copy marked_rendering_surface verbatim into payload.text",
        "reject": "repeated carrier sentences, adjacent marker runs, or a mostly blank passage",
    },
    "fill-in": {
        "purpose": "apply one form from a certified shared bank in a complete sentence",
        "required": (
            "copy each certified_unit.gapped_rendering_surface byte for byte and use the "
            "complete distinctness.choice_bank as options"
        ),
        "reject": "meta-linguistic carriers, one-lemma suffix clues, or repeated templates",
    },
    "true-false": {
        "purpose": "evaluate the meaning of a plausible complete statement",
        "required": "statement exactly equals certified_units[i].allowed_forms[0]",
        "reject": "word doubling, token swapping, paraphrase, or syntax corruption",
    },
    "match-up": {
        "purpose": "associate forms by the one certified relation declared by the board",
        "required": (
            "pair exactly equals its certified semantic pair, and the Ukrainian "
            "instruction explicitly names that relation"
        ),
        "reject": "vague wording, mixed relations, invented pairs, or same-root morphology",
    },
    "error-correction": {
        "purpose": "identify and repair one realistic form error",
        "required": "source exactly equals the certified one-error allowed surface",
        "reject": "free rewriting, repeated-token corruption, or multiple errors",
    },
    "text-questions": {
        "purpose": "check comprehension, inference, and anchored application",
        "required": (
            "one certified question_frame prefix, one natural question clause around the "
            "short source topic, and at least two source content lemmas left for the answer"
        ),
        "reject": (
            "generic what-the-text-says metadiscourse, a bare topic label, or asking only "
            "for a token, preposition, conjunction, or part of speech"
        ),
    },
    "mark-the-words": {
        "purpose": "notice every certified comparison-degree form in the certified source excerpts",
        "required": "exact merged rendering surfaces and exact certified target words",
        "reject": "isolated word lists, invented targets, or a grammar-label question",
    },
    "short-writing": {
        "purpose": "produce a source-grounded communicative response",
        "required": "the exact source-proposition marker and numeric range appear in the prompt",
        "reject": "hidden range/source, linguistic-jargon topic, or copied answer form",
    },
}

_CONTRASTIVE_NEGATIVES: Final[dict[str, str]] = {
    "quiz": "REJECT: eight questions reveal successive words of one source sentence.",
    "cloze": "REJECT: {1} {2} {3} is a consecutive blank run without local context.",
    "true-false": "REJECT: a false statement doubles or reorders words.",
    "match-up": "REJECT: left and right are same-root degree forms or neighboring source words.",
    "error-correction": "REJECT: all items mutate the same sentence or contain several errors.",
    "text-questions": "REJECT: questions merely ask which token or part of speech occurs.",
    "mark-the-words": "REJECT: targets are copied into a detached word list.",
    "short-writing": "REJECT: word range and source proposition appear only in answer guidance.",
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _has_bare_gap_marker(text: str) -> bool:
    """Return True if text contains a bare '___' gap marker (exactly 3 underscores, unbracketed)."""
    if not isinstance(text, str):
        return False
    return bool(re.search(r"(?<![_\(\[\{])___(?![_\)\]\}])", text))


def template_digest() -> str:
    """Return the stable digest of the literal v3.3 instruction template."""
    return hashlib.sha256(_template_source().encode("utf-8")).hexdigest()


def _marked_cloze_rendering_surface(units: Sequence[Mapping[str, Any]]) -> str:
    """Render the one exact marked passage from certified cloze spans."""
    surfaces = [unit.get("rendering_surface") for unit in units]
    if not surfaces or not all(isinstance(surface, str) and surface for surface in surfaces):
        raise PromptPackV3Error("Cloze units need certified rendering surfaces.")
    carrier_passage = " ".join(dict.fromkeys(surfaces))
    spans: list[tuple[int, int, int]] = []
    for index, unit in enumerate(units, start=1):
        allowed_forms = unit.get("allowed_forms")
        distinctness = unit.get("distinctness")
        gap = distinctness.get("gap") if isinstance(distinctness, Mapping) else None
        start = gap.get("start_offset") if isinstance(gap, Mapping) else None
        end = gap.get("end_offset") if isinstance(gap, Mapping) else None
        if (
            not isinstance(allowed_forms, Sequence)
            or isinstance(allowed_forms, (str, bytes))
            or not allowed_forms
            or not isinstance(allowed_forms[0], str)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or carrier_passage[start:end] != allowed_forms[0]
        ):
            raise PromptPackV3Error("Cloze gap span is detached from its certified answer.")
        spans.append((start, end, index))
    ordered_spans = sorted(spans)
    if any(
        left[1] > right[0] for left, right in zip(ordered_spans, ordered_spans[1:], strict=False)
    ):
        raise PromptPackV3Error("Cloze gap spans overlap.")
    marked = carrier_passage
    for start, end, index in reversed(ordered_spans):
        marked = marked[:start] + f"{{{index}}}" + marked[end:]
    return marked


def _gapped_rendering_surface(unit: Mapping[str, Any]) -> str:
    """Render one exact learner carrier from its certified source span."""
    surface = unit.get("rendering_surface")
    allowed_forms = unit.get("allowed_forms")
    distinctness = unit.get("distinctness")
    gap = distinctness.get("gap") if isinstance(distinctness, Mapping) else None
    start = gap.get("start_offset") if isinstance(gap, Mapping) else None
    end = gap.get("end_offset") if isinstance(gap, Mapping) else None
    if (
        not isinstance(surface, str)
        or not surface
        or not isinstance(allowed_forms, Sequence)
        or isinstance(allowed_forms, (str, bytes))
        or not allowed_forms
        or not isinstance(allowed_forms[0], str)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end <= start
        or surface[start:end] != allowed_forms[0]
    ):
        raise PromptPackV3Error("Closed activity gap span is detached from its certified answer.")
    return surface[:start] + "___" + surface[end:]


def _type_kit(slot: object) -> dict[str, Any]:
    """Project one allocated slot into the new, immutable v3 type-kit."""
    plan = slot.plan
    units = [unit.to_dict() for unit in plan.units]
    unit_ids = [unit["unit_id"] for unit in units]
    expected_count = floor_for(plan.activity_type).minimum_units
    if len(units) != expected_count or len(unit_ids) != len(set(unit_ids)):
        raise PromptPackV3Error("Allocation must contain one exact floor-sized unit plan per slot.")
    alignments = [
        unit.get("distinctness", {}).get("focus_alignment")
        for unit in units
        if isinstance(unit.get("distinctness"), Mapping)
        and isinstance(unit.get("distinctness", {}).get("focus_alignment"), str)
    ]
    focus_alignment = None
    if alignments and len(alignments) == len(units) and set(alignments) == {"degree-primary"}:
        focus_alignment = "degree-primary"
    elif alignments and len(alignments) == len(units) and len(set(alignments)) == 1:
        focus_alignment = alignments[0]
    elif len(alignments) >= 6 and set(alignments) == {"degree-reinforcement"}:
        focus_alignment = "degree-reinforcement"
    elif alignments == ["degree-writing"]:
        focus_alignment = "degree-writing"
    kit = {
        "identity": TYPE_KIT_IDENTITY,
        "slot_id": slot.slot_id,
        "phase": slot.phase,
        "type": slot.scheduled_type,
        "scheduled_unit_ids": unit_ids,
        "scheduled_unit_count": expected_count,
        "certified_units": units,
        "registered_constraints": list(plan.registered_constraints),
        "certified_target_tokens": [token.to_dict() for token in plan.certified_target_tokens],
        "focus_alignment": focus_alignment,
        "degree_catalog_version": DEGREE_CATALOG_VERSION,
        "degree_seed_pairs": [
            sorted(pair) for pair in sorted(_DEGREE_SEED_PAIRS, key=lambda p: sorted(p))
        ]
        + [[row.positive, row.comparative] for row in DEGREE_LADDERS.values()],
        "aspect_seed_pairs": [
            sorted(pair) for pair in sorted(_ASPECT_SEED_PAIRS, key=lambda p: sorted(p))
        ],
    }
    if plan.activity_type == "cloze":
        kit["marked_rendering_surface"] = _marked_cloze_rendering_surface(units)
    elif plan.activity_type in {"quiz", "fill-in"}:
        for unit in units:
            unit["gapped_rendering_surface"] = _gapped_rendering_surface(unit)
    elif plan.activity_type == "text-questions":
        for unit in units:
            distinctness = unit.get("distinctness")
            category = (
                distinctness.get("question_category") if isinstance(distinctness, Mapping) else None
            )
            intent = (
                distinctness.get("question_intent") if isinstance(distinctness, Mapping) else None
            )
            frame = (
                distinctness.get("question_frame") if isinstance(distinctness, Mapping) else None
            )
            prefixes = frame.get("allowed_prefixes") if isinstance(frame, Mapping) else None
            if (
                not isinstance(prefixes, list)
                or not prefixes
                or not all(isinstance(prefix, str) and prefix.strip() for prefix in prefixes)
                or frame.get("category") != category
                or frame.get("intent") != intent
            ):
                raise PromptPackV3Error("Text-question unit lacks a certified question frame.")
            if intent == "explicit-causal" and not _EXPLICIT_CAUSAL_SURFACE_RE.search(
                str(unit.get("rendering_surface", ""))
            ):
                raise PromptPackV3Error(
                    "Text-question explanation unit lacks an explicit causal source warrant."
                )
            answer_span = distinctness.get("answer_span")
            question_topic = distinctness.get("question_topic")
            if answer_span is not None or question_topic is not None:
                surface = unit.get("rendering_surface")
                forms = unit.get("allowed_forms")
                start = (
                    answer_span.get("start_offset") if isinstance(answer_span, Mapping) else None
                )
                end = answer_span.get("end_offset") if isinstance(answer_span, Mapping) else None
                text = answer_span.get("text") if isinstance(answer_span, Mapping) else None
                allowed_intents = {
                    "comprehension": {"fact-recovery"},
                    "explanation_inference": set(_SOURCE_RELATION_INTENTS),
                    "anchored_application": {"anchored-application.v1"},
                }.get(category, set())
                if (
                    not isinstance(surface, str)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or start < 0
                    or end <= start
                    or surface[start:end] != text
                    or forms != [text]
                    or intent not in allowed_intents
                    or not isinstance(question_topic, Mapping)
                    or not isinstance(question_topic.get("token_id"), str)
                    or not isinstance(question_topic.get("lemma"), str)
                ):
                    raise PromptPackV3Error(
                        "Text-question source proposition proof is detached from its unit."
                    )
    return kit


def build_phase_context(
    allocation: LessonAllocation, *, phase: int, focus: str | None = None
) -> dict[str, Any]:
    """Build the only v3.3 model input from a completed exact-cover allocation."""
    if not isinstance(allocation, LessonAllocation):
        raise TypeError("Prompt pack v3.3 requires a completed LessonAllocation.")
    slots = [slot for slot in allocation.slots if slot.phase == phase]
    if not slots:
        raise PromptPackV3Error(f"Allocation has no scheduled slots for phase {phase}.")
    type_kits = [_type_kit(slot) for slot in slots]
    normalized_focus = focus.casefold() if isinstance(focus, str) else ""
    is_degree_lesson = len(allocation.slots) == 10 and (
        "компаратив" in normalized_focus
        or "суперлатив" in normalized_focus
        or ("ступен" in normalized_focus and "порівнян" in normalized_focus)
    )
    if is_degree_lesson:
        for kit in type_kits:
            expected_role = degree_role(str(kit["slot_id"]), str(kit["type"]))
            if expected_role is None:
                raise PromptPackV3Error("Degree lesson contains an unregistered slot role.")
            if expected_role == "anchor-comprehension":
                continue
            actual_role = kit.get("focus_alignment")
            if expected_role == "degree-writing":
                if actual_role != "degree-writing":
                    raise PromptPackV3Error("Degree writing slot is detached from its role.")
            elif actual_role != expected_role:
                raise PromptPackV3Error("Degree closed slot is detached from its role.")
    context = {
        "pack_version": PROMPT_PACK_VERSION,
        "template_version": TEMPLATE_VERSION,
        "template_sha256": template_digest(),
        "type_kit_identity": TYPE_KIT_IDENTITY,
        "serializer_temperature": serializer_temperature(),
        "phase": phase,
        "lesson_focus": focus.strip()[:200] if isinstance(focus, str) and focus.strip() else None,
        "paragraph_ids": list(allocation.paragraph_ids),
        "response_order": [kit["slot_id"] for kit in type_kits],
        "type_kits": type_kits,
    }
    context["context_sha256"] = hashlib.sha256(_canonical(context).encode("utf-8")).hexdigest()
    return context


def _plan_degree_class(unit: object) -> str | None:
    distinctness = getattr(unit, "distinctness", None)
    value = distinctness.get("degree_class") if isinstance(distinctness, Mapping) else None
    return value if value in {"positive", "comparative", "superlative"} else None


def _plan_source_lemma(unit: object) -> str | None:
    distinctness = getattr(unit, "distinctness", None)
    value = distinctness.get("source_lemma") if isinstance(distinctness, Mapping) else None
    return value.casefold() if isinstance(value, str) and value.strip() else None


def _carrier_skeleton(surface: str, answer: str) -> str:
    blanked = surface.replace(answer, "___", 1).casefold()
    preserved = {
        "за",
        "ніж",
        "серед",
        "усіх",
        "ще",
        "так",
        "само",
        "як",
        "удвічі",
        "дедалі",
        "щораз",
    }
    return " ".join(
        token if token == "___" or token in preserved else "WORD"
        for token in re.findall(r"___|[А-Яа-яІіЇїЄєҐґʼ’'-]+", blanked)
    )


def _answer_carrier_sentence(surface: str, answer: str) -> str:
    """Return one normalized answer-bearing sentence from a passage carrier."""
    return " ".join(
        next(
            (
                match.group(0).strip()
                for match in re.finditer(r"[^.!?…]+[.!?…]?", surface)
                if match.group(0).count(answer) == 1
            ),
            surface,
        )
        .casefold()
        .split()
    )


def _blanked_carrier(surface: str, answer: str) -> str:
    """Return the exact answer-bearing carrier sentence with one visible gap."""
    return _answer_carrier_sentence(surface, answer).replace(answer.casefold(), "___", 1)


def _agreement_frames(form: str) -> set[tuple[str, str, str]]:
    frames: set[tuple[str, str, str]] = set()
    for match in _vesum_matches(form, paths.vesum_db()):
        tags = str(match.get("tags", "")).split(":")
        gender = next((tag for tag in tags if tag in {"m", "f", "n", "p"}), "")
        case = next((tag for tag in tags if tag.startswith("v_")), "")
        number = "p" if "p" in tags else "s"
        if gender and case:
            frames.add((gender, case, number))
    return frames


def _surface_has_degree_cue(surface: str, degree_class: str) -> bool:
    normalized = surface.casefold()
    if degree_class == "positive":
        return bool(
            re.search(
                r"\b(?:так\s+само|так(?:ий|а|е|і)\s+сам(?:ий|а|е|і))\b.+\bяк\b",
                normalized,
            )
        )
    if degree_class == "comparative":
        return bool(
            re.search(r"\b(?:ніж|удвічі|дедалі|щораз|ще)\b", normalized)
            or re.search(r"\bза\b", normalized)
            or re.search(r"\bщо\b.+\bто\b", normalized)
            or re.search(r"\bчим\b.+\bтим\b", normalized)
        )
    return bool(re.search(r"\b(?:серед|з\s+усіх)\b", normalized))


def validate_degree_lesson_plan(allocation: LessonAllocation) -> None:
    """Fail closed on the advisor-approved degree lesson before provider spend."""
    expected = tuple(
        (slot_id, activity_type)
        for slot_id, activity_type, _role in (
            ("P1-A1", "text-questions", "anchor-comprehension"),
            ("P1-A2", "quiz", "degree-recognition"),
            ("P1-A3", "cloze", "degree-cloze"),
            ("P2-A1", "fill-in", "degree-formation"),
            ("P2-A2", "match-up", "degree-positive-comparative"),
            ("P2-A3", "error-correction", "degree-error-correction"),
            ("P2-A4", "quiz", "degree-comparison-syntax"),
            ("P2-A5", "fill-in", "degree-context"),
            ("P3-A1", "match-up", "degree-comparative-superlative"),
            ("P3-A2", "short-writing", "degree-writing"),
        )
    )
    observed = tuple((slot.slot_id, slot.scheduled_type) for slot in allocation.slots)
    if observed != expected:
        return

    role_by_slot = {
        slot.slot_id: degree_role(slot.slot_id, slot.scheduled_type) for slot in allocation.slots
    }
    carriers: list[tuple[str, str, str]] = []
    keyed_lemmas: Counter[str] = Counter()
    phase_one_surfaces: dict[str, set[str]] = {}
    family_counts: Counter[str] = Counter()
    block_sequences: dict[str, tuple[str, ...]] = {}
    scored_carrier_roles = {
        "degree-recognition",
        "degree-cloze",
        "degree-formation",
        "degree-error-correction",
        "degree-comparison-syntax",
        "degree-context",
    }

    for slot in allocation.slots:
        role = role_by_slot[slot.slot_id]
        if role is None or len(slot.plan.units) != floor_for(slot.scheduled_type).minimum_units:
            raise PromptPackV3Error("Degree lesson plan is missing a complete registered role.")
        degree_classes = [_plan_degree_class(unit) for unit in slot.plan.units]
        sequence: list[str] = []
        if role in {
            "degree-recognition",
            "degree-cloze",
            "degree-formation",
            "degree-comparison-syntax",
            "degree-context",
        }:
            if any(item is None for item in degree_classes):
                raise PromptPackV3Error("Degree lesson plan omits a certified degree class.")
            counts = Counter(degree_classes)
            if max(counts.values(), default=0) > 6:
                raise PromptPackV3Error("Degree lesson plan has a monotone answer category.")
            if role in {"degree-recognition", "degree-cloze", "degree-comparison-syntax"}:
                if counts["positive"] < 2 or counts["comparative"] + counts["superlative"] < 4:
                    raise PromptPackV3Error(
                        "Degree contrast plan lacks a mixed answer distribution."
                    )
            if role == "degree-context" and counts["superlative"] < 3:
                raise PromptPackV3Error("Guided degree production lacks superlative constructions.")

        for unit, degree_class in zip(slot.plan.units, degree_classes, strict=True):
            surface = unit.rendering_surface
            distinctness = unit.distinctness
            answer = (
                unit.expected_key_or_rule.value
                if slot.scheduled_type == "error-correction"
                else unit.allowed_forms[0]
                if unit.allowed_forms
                else ""
            )
            if slot.scheduled_type in {"quiz", "cloze", "fill-in", "error-correction"}:
                if not isinstance(surface, str) or not surface or not answer:
                    raise PromptPackV3Error("Degree lesson plan contains an unbound carrier.")
                carriers.append((slot.slot_id, surface, answer))
                if re.search(
                    r"\b(?:ступінь|прикметник|форма|вищий\s+ступінь|найвищий\s+ступінь)\b",
                    surface.casefold(),
                ):
                    raise PromptPackV3Error(
                        "Degree lesson plan contains a meta-linguistic carrier."
                    )
            if role in scored_carrier_roles:
                frame_family = distinctness.get("frame_family")
                warrant = distinctness.get("semantic_warrant")
                if (
                    not isinstance(frame_family, str)
                    or not frame_family.strip()
                    or not isinstance(warrant, str)
                    or not warrant.strip()
                ):
                    raise PromptPackV3Error(
                        "Degree scored carrier lacks its certified family or semantic warrant."
                    )
                family_counts[frame_family] += 1
                sequence.append(frame_family)
                choice_bank = distinctness.get("choice_bank")
                exclusions = distinctness.get("exclusion_warrants")
                if isinstance(choice_bank, Sequence) and not isinstance(choice_bank, (str, bytes)):
                    if (
                        not isinstance(exclusions, Mapping)
                        or set(exclusions) != set(choice_bank) - {answer}
                        or not all(
                            isinstance(value, str) and value.strip()
                            for value in exclusions.values()
                        )
                    ):
                        raise PromptPackV3Error(
                            "Degree scored carrier lacks per-option exclusion warrants."
                        )
            elif role in {"degree-positive-comparative", "degree-comparative-superlative"}:
                pair = distinctness.get("pair")
                relation = pair.get("relation") if isinstance(pair, Mapping) else None
                if not isinstance(relation, str) or not relation.strip():
                    raise PromptPackV3Error("Degree match board lacks its relation family.")
                sequence.append(relation)
            if isinstance(degree_class, str) and not _surface_has_degree_cue(
                surface or "", degree_class
            ):
                raise PromptPackV3Error(
                    "Degree lesson plan contains an unlicensed answer degree at "
                    f"{slot.slot_id}:{unit.unit_id}."
                )
            lemma = _plan_source_lemma(unit)
            if lemma is not None and slot.scheduled_type != "match-up":
                keyed_lemmas[lemma] += 1

        if sequence:
            if role in scored_carrier_roles and max(Counter(sequence).values(), default=0) > 2:
                raise PromptPackV3Error("One degree block overuses a carrier-frame family.")
            block_sequences[slot.slot_id] = tuple(sequence)

        if slot.phase == 1:
            phase_one_surfaces[slot.slot_id] = {
                " ".join(unit.rendering_surface.casefold().split())
                for unit in slot.plan.units
                if isinstance(unit.rendering_surface, str)
            }

    if max(keyed_lemmas.values(), default=0) > 3:
        raise PromptPackV3Error("Degree lesson plan reuses one keyed adjective in too many blocks.")
    if max(family_counts.values(), default=0) > 4:
        raise PromptPackV3Error("Degree lesson plan overuses one carrier-frame family.")

    scored_sequences = [
        sequence
        for slot_id, sequence in block_sequences.items()
        if role_by_slot[slot_id] in scored_carrier_roles
    ]
    if len(scored_sequences) != len(set(scored_sequences)):
        raise PromptPackV3Error("Degree scored blocks repeat an ordered family sequence.")
    for activity_type in {slot.scheduled_type for slot in allocation.slots}:
        same_type = [
            block_sequences[slot.slot_id]
            for slot in allocation.slots
            if slot.scheduled_type == activity_type and slot.slot_id in block_sequences
        ]
        for index, left in enumerate(same_type):
            for right in same_type[index + 1 :]:
                if (
                    len(left) == len(right)
                    and sum(a != b for a, b in zip(left, right, strict=True)) < 3
                ):
                    raise PromptPackV3Error(
                        "Repeated activity types do not differ across three family positions."
                    )

    exact_carriers = [
        _answer_carrier_sentence(surface, answer) for _slot, surface, answer in carriers
    ]
    if len(exact_carriers) != len(set(exact_carriers)):
        raise PromptPackV3Error("Degree lesson plan repeats an exact carrier sentence.")
    normalized_carriers = [_blanked_carrier(surface, answer) for _slot, surface, answer in carriers]
    if len(normalized_carriers) != len(set(normalized_carriers)):
        raise PromptPackV3Error("Degree lesson plan repeats a blanked carrier sentence.")
    skeletons_by_slot: dict[str, Counter[str]] = {}
    lesson_skeletons: Counter[str] = Counter()
    for slot_id, surface, answer in carriers:
        skeleton = _carrier_skeleton(surface, answer)
        skeletons_by_slot.setdefault(slot_id, Counter())[skeleton] += 1
        lesson_skeletons[skeleton] += 1
    if (
        any(max(counts.values(), default=0) > 2 for counts in skeletons_by_slot.values())
        or max(lesson_skeletons.values(), default=0) > 4
    ):
        raise PromptPackV3Error("Degree lesson plan repeats one carrier template too often.")

    phase_one_sets = list(phase_one_surfaces.values())
    if any(
        left & right
        for index, left in enumerate(phase_one_sets)
        for right in phase_one_sets[index + 1 :]
    ):
        raise PromptPackV3Error("Degree lesson plan leaks one Phase-1 carrier across blocks.")

    text_slot = next(slot for slot in allocation.slots if slot.slot_id == "P1-A1")
    degree_question_sources = sum(
        any(
            _degree_rank(str(match.get("tags", ""))) in {1, 2}
            for word in _UKRAINIAN_WORD_RE.findall(unit.rendering_surface or "")
            for match in _vesum_matches(word, paths.vesum_db())
        )
        for unit in text_slot.plan.units
    )
    if degree_question_sources < 3:
        raise PromptPackV3Error("Degree lesson plan lacks three comparison-focused questions.")
    expected_question_intents = (
        "fact-recovery",
        "fact-recovery",
        "fact-recovery",
        "explicit-causal",
        "explicit-causal",
        "explicit-causal",
        "realistic-transfer",
        "realistic-transfer",
    )
    causal_re = re.compile(r"\b(?:тому|бо|адже|оскільки|щоб|завдяки|через\s+те)\b|[:—]")
    for expected_intent, unit in zip(expected_question_intents, text_slot.plan.units, strict=True):
        actual_intent = unit.distinctness.get("question_intent")
        if actual_intent != expected_intent:
            raise PromptPackV3Error("Text-question unit lacks its certified purpose intent.")
        if expected_intent == "explicit-causal" and not causal_re.search(
            (unit.rendering_surface or "").casefold()
        ):
            raise PromptPackV3Error("Explanation question lacks an explicit source relation.")

    formation = next(slot for slot in allocation.slots if slot.slot_id == "P2-A1")
    formation_classes = Counter(
        unit.distinctness.get("morphology_class") for unit in formation.plan.units
    )
    if formation_classes["alternation"] < 3 or formation_classes["suppletive"] < 2:
        raise PromptPackV3Error("Degree formation plan lacks morphological-class coverage.")
    for unit in formation.plan.units:
        lemma = _plan_source_lemma(unit)
        if not isinstance(lemma, str) or not re.search(
            rf"\(\s*{re.escape(lemma)}\s*\)\s*$",
            unit.rendering_surface or "",
            re.IGNORECASE,
        ):
            raise PromptPackV3Error(
                "Degree formation carrier omits its visible dictionary-form transformation cue."
            )

    contextual = next(slot for slot in allocation.slots if slot.slot_id == "P2-A5")
    contextual_cues = re.compile(
        r"\b(?:після|чому|тому|для|коли|серед|з\s+усіх|більшість)\b",
        re.IGNORECASE,
    )
    context_cue_count = sum(
        bool(contextual_cues.search(unit.rendering_surface or "")) for unit in contextual.plan.units
    )
    has_transformation_suffix = any(
        re.search(r"\([^)]*\)\s*$", unit.rendering_surface or "") for unit in contextual.plan.units
    )
    if context_cue_count < 6 or has_transformation_suffix:
        raise PromptPackV3Error(
            "Degree context block does not differ from guided transformation practice."
        )

    for slot_id in ("P1-A2", "P1-A3", "P2-A4"):
        slot = next(candidate for candidate in allocation.slots if candidate.slot_id == slot_id)
        for unit in slot.plan.units:
            raw_bank = unit.distinctness.get("choice_bank")
            if (
                not isinstance(raw_bank, Sequence)
                or isinstance(raw_bank, (str, bytes))
                or len(raw_bank) != 3
                or len(set(raw_bank)) != 3
                or unit.allowed_forms[0] not in raw_bank
            ):
                raise PromptPackV3Error("Degree contrast bank is incomplete or inconsistent.")
            answer_frames = _agreement_frames(unit.allowed_forms[0])
            if not answer_frames or any(
                not (answer_frames & _agreement_frames(option)) for option in raw_bank
            ):
                raise PromptPackV3Error("Degree contrast bank mixes agreement frames.")
            positive_lemmas = {
                lemma
                for option in raw_bank
                if (lemma := _catalog_positive_lemma(option)) is not None
            }
            degree_classes = {
                rank
                for option in raw_bank
                for match in _vesum_matches(option, paths.vesum_db())
                if (rank := _degree_rank(str(match.get("tags", "")))) is not None
            }
            if len(positive_lemmas) != 1 or len(degree_classes) < 2:
                raise PromptPackV3Error(
                    "Degree contrast bank lacks a same-lemma degree contrast at "
                    f"{slot_id}:{unit.unit_id}."
                )

    for slot_id in ("P2-A1", "P2-A5"):
        slot = next(candidate for candidate in allocation.slots if candidate.slot_id == slot_id)
        answers = [unit.allowed_forms[0] for unit in slot.plan.units]
        if len(answers) != len(set(answers)):
            raise PromptPackV3Error("Degree shared bank repeats an answer key.")
        expected_bank = set(answers)
        observed_banks: list[frozenset[str]] = []
        for unit in slot.plan.units:
            raw_bank = unit.distinctness.get("choice_bank")
            if (
                not isinstance(raw_bank, Sequence)
                or isinstance(raw_bank, (str, bytes))
                or len(raw_bank) != 6
                or len(set(raw_bank)) != 6
                or unit.allowed_forms[0] not in raw_bank
                or not set(raw_bank) <= expected_bank
            ):
                raise PromptPackV3Error("Degree shared bank is incomplete or inconsistent.")
            observed_banks.append(frozenset(raw_bank))
            answer_frames = _agreement_frames(unit.allowed_forms[0])
            option_frames = [_agreement_frames(option) for option in raw_bank]
            survivor_degrees = {
                rank
                for option in raw_bank
                for match in _vesum_matches(option, paths.vesum_db())
                if (rank := _degree_rank(str(match.get("tags", "")))) is not None
            }
            exclusions = unit.distinctness.get("exclusion_warrants")
            if not answer_frames or any(
                not frames or not (answer_frames & frames) for frames in option_frames
            ):
                raise PromptPackV3Error(
                    f"Degree shared bank mixes agreement frames at {slot_id}:{unit.unit_id}."
                )
            if len(survivor_degrees) < 2:
                raise PromptPackV3Error(
                    f"Degree shared bank lacks mixed degree classes at {slot_id}:{unit.unit_id}."
                )
            if (
                not isinstance(exclusions, Mapping)
                or set(exclusions) != set(raw_bank) - {unit.allowed_forms[0]}
                or not all(
                    isinstance(value, str) and value.strip() for value in exclusions.values()
                )
            ):
                raise PromptPackV3Error(
                    f"Degree shared bank lacks option exclusions at {slot_id}:{unit.unit_id}."
                )
        if (
            len(set(observed_banks)) != len(observed_banks)
            or set().union(*observed_banks) != expected_bank
        ):
            raise PromptPackV3Error("Degree shared banks do not rotate across every answer key.")

    for slot_id in ("P2-A2", "P3-A1"):
        slot = next(candidate for candidate in allocation.slots if candidate.slot_id == slot_id)
        for unit in slot.plan.units:
            pair = unit.distinctness.get("pair")
            if not isinstance(pair, Mapping):
                raise PromptPackV3Error("Degree match board omits its certified relation.")
            left_lemmas = {
                lemma
                for word in _UKRAINIAN_WORD_RE.findall(str(pair.get("left", "")))
                if (lemma := _catalog_positive_lemma(word)) is not None
            }
            right_lemmas = {
                lemma
                for word in _UKRAINIAN_WORD_RE.findall(str(pair.get("right", "")))
                if (lemma := _catalog_positive_lemma(word)) is not None
            }
            if left_lemmas & right_lemmas:
                raise PromptPackV3Error("Degree match board contains a same-root pair.")

    paraphrase_slot = next(slot for slot in allocation.slots if slot.slot_id == "P2-A2")
    irregular_lemmas = {"добрий", "поганий", "великий", "малий"}
    irregular_pairs = 0
    consequence_re = re.compile(
        r"\b(?:доведеться|потрапляє|дорога|легше|важче|опалювати|складніше|більше|гірше)\b",
        re.IGNORECASE,
    )
    for unit in paraphrase_slot.plan.units:
        pair = unit.distinctness["pair"]
        left = str(pair["left"])
        right = str(pair["right"])
        words = _UKRAINIAN_WORD_RE.findall(left)
        if {
            lemma for word in words if (lemma := _catalog_positive_lemma(word)) is not None
        } & irregular_lemmas:
            irregular_pairs += 1
        if (
            len(_UKRAINIAN_WORD_RE.findall(left)) < 4
            or len(_UKRAINIAN_WORD_RE.findall(right)) < 4
            or not consequence_re.search(right)
            or not any(_catalog_positive_lemma(word) is not None for word in words)
        ):
            raise PromptPackV3Error(
                "Degree paraphrase board contains a bare form or non-equivalent lookup pair."
            )
    if irregular_pairs < 2:
        raise PromptPackV3Error("Degree paraphrase board lacks suppletive comparison coverage.")

    situation_slot = next(slot for slot in allocation.slots if slot.slot_id == "P3-A1")
    winner_positions: Counter[str] = Counter()
    priority_re = re.compile(
        r"\b(?:шукає|потрібн\w*|важлив\w*|може|важко|не\s+хоче)\b",
        re.IGNORECASE,
    )
    for unit in situation_slot.plan.units:
        situation = str(unit.distinctness["pair"]["left"])
        conclusion = str(unit.distinctness["pair"]["right"]).casefold()
        if (
            len(_UKRAINIAN_WORD_RE.findall(situation)) < 10
            or not priority_re.search(situation)
            or "бо" not in conclusion
        ):
            raise PromptPackV3Error(
                "Degree recommendation board omits a learner priority or explicit reason."
            )
        winner = next(
            (
                position
                for position, pattern in {
                    "first": r"\bперш(?:ий|а|е)\b",
                    "second": r"\bдруг(?:ий|а|е)\b",
                    "third": r"\bтрет(?:ій|я|є)\b",
                }.items()
                if re.search(pattern, conclusion)
            ),
            None,
        )
        if winner is not None:
            winner_positions[winner] += 1
    if len(winner_positions) < 3 or max(winner_positions.values(), default=0) > 4:
        raise PromptPackV3Error("Degree situation board has a monotone winning position.")

    class_families: dict[str, set[str]] = {"comparative": set(), "superlative": set()}
    for slot in allocation.slots:
        for unit in slot.plan.units:
            degree_class = _plan_degree_class(unit)
            frame_family = unit.distinctness.get("frame_family")
            if degree_class in class_families and isinstance(frame_family, str):
                class_families[degree_class].add(frame_family)
    if len(class_families["comparative"]) < 3 or len(class_families["superlative"]) < 2:
        raise PromptPackV3Error("Degree catalog lacks distinct comparative/superlative families.")

    correction = next(slot for slot in allocation.slots if slot.slot_id == "P2-A3")
    if (
        sum(
            unit.distinctness.get("question_intent") == "degree-specific"
            for unit in correction.plan.units
        )
        < 5
    ):
        raise PromptPackV3Error("Degree correction block overuses elementary agreement errors.")

    writing = next(slot for slot in allocation.slots if slot.slot_id == "P3-A2")
    writing_unit = writing.plan.units[0]
    specs = writing_unit.distinctness.get("constraint_specs")
    lemma_specs = [
        spec
        for spec in specs or ()
        if isinstance(spec, Mapping) and spec.get("kind") == "contains_lemma_set"
    ]
    lemmas = (
        lemma_specs[0].get("params", {}).get("lemmas", ())
        if len(lemma_specs) == 1 and isinstance(lemma_specs[0].get("params"), Mapping)
        else ()
    )
    warrants = writing_unit.distinctness.get("attribute_warrants")
    scenario_lemmas = {
        _catalog_positive_lemma(word) or str(match["lemma"]).casefold()
        for word in _UKRAINIAN_WORD_RE.findall(writing_unit.rendering_surface or "")
        for match in _vesum_matches(word, paths.vesum_db())
        if isinstance(match.get("lemma"), str)
    }
    if (
        not isinstance(lemmas, (list, tuple))
        or len(lemmas) < 3
        or not set(lemmas) <= scenario_lemmas
        or not isinstance(warrants, Mapping)
        or set(warrants) != set(lemmas)
        or not all(isinstance(value, str) and value.strip() for value in warrants.values())
        or any(_catalog_positive_lemma(str(lemma)) not in {None, str(lemma)} for lemma in lemmas)
    ):
        raise PromptPackV3Error("Degree writing constraints are not lemma-bound to the scenario.")
    leaked_degree_lemmas = {
        _catalog_positive_lemma(word)
        for word in _UKRAINIAN_WORD_RE.findall(writing_unit.rendering_surface or "")
        for match in _vesum_matches(word, paths.vesum_db())
        if _degree_rank(str(match.get("tags", ""))) in {1, 2}
    }
    if set(lemmas) & leaked_degree_lemmas:
        raise PromptPackV3Error("Degree writing scenario leaks a target degree form.")


def one_slot_context(context: Mapping[str, Any], *, slot_id: str) -> dict[str, Any]:
    """Return an integrity-bound one-kit context for a repair or replacement."""
    _validate_context_integrity(context)
    type_kits = context.get("type_kits")
    if not isinstance(type_kits, list):
        raise PromptPackV3Error("A repair context needs type-kits.")
    matches = [
        kit for kit in type_kits if isinstance(kit, Mapping) and kit.get("slot_id") == slot_id
    ]
    if len(matches) != 1:
        raise PromptPackV3Error("A repair context must select exactly one scheduled slot.")
    narrowed = {
        str(key): value
        for key, value in context.items()
        if key not in {"context_sha256", "response_order", "type_kits"}
    }
    narrowed["response_order"] = [slot_id]
    narrowed["type_kits"] = [dict(matches[0])]
    narrowed["context_sha256"] = hashlib.sha256(_canonical(narrowed).encode("utf-8")).hexdigest()
    return narrowed


def compact_schema_exemplars(type_kits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return one one-item schema shape per requested type, plus its exact count."""
    exemplars: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kit in type_kits:
        activity_type = kit.get("type")
        count = kit.get("scheduled_unit_count")
        if not isinstance(activity_type, str) or not isinstance(count, int) or count < 1:
            raise PromptPackV3Error("A requested type-kit needs a positive scheduled unit count.")
        if count != floor_for(activity_type).minimum_units:
            raise PromptPackV3Error("A compact exemplar must retain the locked v3 type floor.")
        if activity_type in seen:
            continue
        seen.add(activity_type)
        exemplars.append(
            {
                "type": activity_type,
                "required_unit_count": count,
                "one_item_slot_shape": {
                    "slot_id": f"<synthetic-{activity_type}-slot>",
                    "type": activity_type,
                    "activity": _synthetic_activity_example(activity_type, 1),
                    "serialized_units": [{"unit_id": f"<{activity_type}-unit-1>"}],
                },
            }
        )
    return exemplars


def _synthetic_options(form: str, index: int) -> tuple[list[str], int]:
    correct = (index - 1) % 3
    options = [f"SYNTHETIC-DISTRACTOR-A-{index}", f"SYNTHETIC-DISTRACTOR-B-{index}"]
    options.insert(correct, form)
    return options, correct


def _synthetic_items(activity_type: str, count: int) -> list[dict[str, Any]]:
    """Return ``count`` distinct composed items for the requested activity type.

    The content is domain-neutral Ukrainian (weather, city, timetable, simple
    actions) and deliberately uses synthetic markers that the contamination gate
    can detect.  Each item is a self-contained shape guide; real model output
    must replace every learner-facing string with composed prose that elicits
    the certified form without quoting it.
    """
    if activity_type == "quiz":
        items = []
        for index in range(1, count + 1):
            options, correct = _synthetic_options(f"форм{index}", index)
            items.append(
                {
                    "question": f"SYNTHETIC-QUIZ-STEM {index}: якою буде форма?",
                    "options": options,
                    "correct": correct,
                }
            )
        return items
    if activity_type == "cloze":
        items = []
        for index in range(1, count + 1):
            answer = f"форм{index}"
            options, _correct = _synthetic_options(answer, index)
            items.append({"id": index, "answer": answer, "options": options})
        return items
    if activity_type == "fill-in":
        items = []
        for index in range(1, count + 1):
            answer = f"форм{index}"
            options, _correct = _synthetic_options(answer, index)
            items.append(
                {
                    "sentence": f"SYNTHETIC-FILLIN-STEM {index}: речення потребує слова.",
                    "answer": answer,
                    "options": options,
                }
            )
        return items
    if activity_type == "true-false":
        return [
            {
                "statement": f"SYNTHETIC-TRUEFALSE-STEM {index}: твердження.",
                "correct": index % 2 == 1,
            }
            for index in range(1, count + 1)
        ]
    if activity_type == "match-up":
        return [
            {
                "left": f"SYNTHETIC-MATCH-LEFT {index}",
                "right": f"SYNTHETIC-MATCH-RIGHT {index}",
            }
            for index in range(1, count + 1)
        ]
    if activity_type == "error-correction":
        return [
            {
                "source": (
                    f"SYNTHETIC-ERROR-SOURCE {index}: "
                    "речення містить помилкову форму SYNTHETIC-DISTRACTOR."
                ),
                "correction": f"SYNTHETIC-ERROR-CORRECTION {index}",
            }
            for index in range(1, count + 1)
        ]
    if activity_type == "text-questions":
        return [
            {"question": f"SYNTHETIC-OPEN-QUESTION {index}: питання?"}
            for index in range(1, count + 1)
        ]
    if activity_type == "short-writing":
        return [
            {
                "prompt": (
                    "SYNTHETIC-WRITING-PROMPT: опишіть ситуацію, не називаючи цільову форму."
                ),
                "guidance": (
                    "SYNTHETIC-WRITING-GUIDANCE: текст має обов'язково містити цільову "
                    "форму SYNTHETIC-WRITING-TARGET."
                ),
            }
        ]
    if activity_type == "mark-the-words":
        return [
            {
                "text": "SYNTHETIC-MARK-TEXT: позначте правильні слова.",
                "target_words": [f"форм{index}" for index in range(1, count + 1)],
            }
        ]
    raise PromptPackV3Error(f"A compact exemplar has unsupported type {activity_type!r}.")


def _synthetic_activity_example(activity_type: str, count: int) -> dict[str, Any]:
    """Return a compact activity shape whose learner values must be composed."""
    items = _synthetic_items(activity_type, count)
    if activity_type == "quiz":
        return {
            "payload": {
                "type": "quiz",
                "instruction": "Оберіть правильний варіант.",
                "items": items,
            },
            "answer_key": {
                "items": [
                    {"index": index, "correct": item["correct"]} for index, item in enumerate(items)
                ]
            },
        }
    if activity_type == "cloze":
        return {
            "payload": {
                "type": "cloze",
                "instruction": "Заповніть пропуски.",
                "text": "SYNTHETIC-CLOZE-TEXT: текст із пропусками.",
                "blanks": items,
            },
            "answer_key": {
                "blanks": [{"id": item["id"], "answer": item["answer"]} for item in items]
            },
        }
    if activity_type == "fill-in":
        return {
            "payload": {
                "type": "fill-in",
                "instruction": "Вставте слово.",
                "items": items,
            },
            "answer_key": {"items": [item["answer"] for item in items]},
        }
    if activity_type == "true-false":
        return {
            "payload": {
                "type": "true-false",
                "instruction": "Визначте правильність твердження.",
                "items": items,
            },
            "answer_key": {
                "items": [
                    {"index": index, "correct": items[index]["correct"]} for index in range(count)
                ]
            },
        }
    if activity_type == "match-up":
        return {
            "payload": {
                "type": "match-up",
                "instruction": "Знайдіть пару.",
                "pairs": items,
            },
            "answer_key": {
                "pairs": [{"left_index": index, "right_index": index} for index in range(count)]
            },
        }
    if activity_type == "error-correction":
        return {
            "payload": {
                "type": "error-correction",
                "instruction": "Виправте помилку.",
                "items": [item["source"] for item in items],
            },
            "answer_key": {"items": [item["correction"] for item in items]},
        }
    if activity_type == "text-questions":
        # Text questions are the only open-ended list activity in this pack.
        # A concrete synthetic question or guidance string is unusually easy
        # for a serializer to copy verbatim, even though the template forbids
        # exemplar reuse.  Keep the JSON shape but leave its learner-facing
        # values blank so the model has no competing question content.
        return {
            "payload": {
                "type": "text-questions",
                "instruction": "",
                "items": ["" for _item in items],
            },
            "answer_key": {"guidance": ""},
        }
    if activity_type == "short-writing":
        item = items[0]
        return {
            "payload": {
                "type": "short-writing",
                "prompt": item["prompt"],
            },
            "answer_key": {"guidance": item["guidance"]},
        }
    if activity_type == "mark-the-words":
        mark_item = items[0]
        return {
            "payload": {
                "type": "mark-the-words",
                "instruction": "Позначте слова.",
                "text": mark_item["text"],
                "target_words": mark_item["target_words"],
            },
            "answer_key": {"target_words": mark_item["target_words"]},
        }
    raise PromptPackV3Error(f"A compact exemplar has unsupported type {activity_type!r}.")


def _primary_forms(type_kit: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        unit["allowed_forms"][0]
        for unit in type_kit.get("certified_units", ())
        if isinstance(unit, Mapping)
        and isinstance(unit.get("allowed_forms"), list)
        and unit["allowed_forms"]
    )


def _certified_answer_forms(type_kit: Mapping[str, Any]) -> set[str]:
    """Return every certified answer form from the immutable kit."""
    forms: set[str] = set()
    for unit in type_kit.get("certified_units", ()):
        if not isinstance(unit, Mapping):
            continue
        allowed = unit.get("allowed_forms")
        if isinstance(allowed, list) and allowed:
            forms.add(allowed[0])
    return forms


def _is_trivial_template(text: str, forms: set[str]) -> bool:
    """True when ``text`` is the exemplar template or a bare answer form."""
    if text in forms:
        return True
    for form in forms:
        if text == _EXEMPLAR_QUIZ_QUESTION_TEMPLATE.format(form):
            return True
    return False


def _word_boundary_pattern(form: str) -> re.Pattern[str]:
    """Return a pattern that matches ``form`` as whole words/phrase.

    Multi-word forms are matched by ordered whole-word tokens; single-word
    forms by one whole-word token.  Ukrainian letters and apostrophes count
    as word characters.
    """
    word = r"[А-ЯҐЄІЇа-яґєіїʼ'’]+"
    words = re.findall(word, form)
    if not words:
        return re.compile(re.escape(form))
    parts = [rf"{re.escape(w)}" for w in words]
    sep = r"[^А-ЯҐЄІЇа-яґєіїʼ'’]*"
    body = sep.join(parts)
    return re.compile(
        rf"(?<![А-ЯҐЄІЇа-яґєіїʼ'’]){body}(?![А-ЯҐЄІЇа-яґєіїʼ'’])",
        re.IGNORECASE,
    )


def _contains_form(text: str, form: str) -> bool:
    return bool(_word_boundary_pattern(form).search(text))


def _is_certified_error_correction_context(
    item: str,
    correction: str,
    unit: object,
) -> bool:
    """Return whether ``item`` is the certified one-token error surface.

    A correction can occur naturally elsewhere in its source sentence.  That
    is safe only when the immutable unit proves that the learner item is the
    exact derived surface and that restoring ``correction`` at the one changed
    token reconstructs the certified source surface.
    """
    if not isinstance(unit, Mapping):
        return False
    allowed_forms = unit.get("allowed_forms")
    rendering_surface = unit.get("rendering_surface")
    if (
        not isinstance(allowed_forms, list)
        or len(allowed_forms) < 2
        or allowed_forms[0] != item
        or allowed_forms[1] != correction
        or not isinstance(rendering_surface, str)
    ):
        return False

    item_words = re.findall(r"[А-ЯҐЄІЇа-яґєіїʼ'’]+", item)
    source_words = re.findall(r"[А-ЯҐЄІЇа-яґєіїʼ'’]+", rendering_surface)
    if len(item_words) != len(source_words):
        return False
    differences = [
        index
        for index, (item_word, source_word) in enumerate(zip(item_words, source_words, strict=True))
        if item_word != source_word
    ]
    return (
        len(differences) == 1 and source_words[differences[0]].casefold() == correction.casefold()
    )


def validate_verbatim_answer_ban(activity: Mapping[str, Any], kit: Mapping[str, Any]) -> None:
    """Reject learner-facing prose that contains, names, or quotes a correct answer.

    The certified substrate supplies the correct form; the model must elicit it
    through composed Ukrainian prose.  Any verbatim occurrence of a correct
    answer in a prose field is a leak.  Displayed option lists and match-up
    pairs are not prose fields, so they are excluded from this check.

    The ban is scoped per item: an item's own answer must not appear in its own
    question or sentence, but a mention of another item's answer is allowed.
    Shared prose (instruction, cloze text) is checked against every answer.
    Cloze blanks are gaps by definition, so the cloze text keeps a whole-slot
    ban for all blank answers.
    """
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return
    activity_type = payload.get("type")
    answer_key = activity.get("answer_key")
    certified_forms = _certified_answer_forms(kit)

    # Each entry is (prose_field, answer_forms_that_must_not_occur_in_it).
    checks: list[tuple[str, list[str]]] = []
    all_answers: list[str] = []

    def _add_field(text: str, forms: list[str]) -> None:
        if isinstance(text, str):
            checks.append((text, forms))

    db_path = paths.vesum_db()

    if activity_type == "quiz":
        instruction = payload.get("instruction", "")
        item_checks: list[tuple[str, list[str]]] = []
        key_items = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
        for index, item in enumerate(payload.get("items", ())):
            if not isinstance(item, Mapping):
                continue
            question = item.get("question")
            options = item.get("options", ())
            correct = item.get("correct")
            key_correct = correct
            if isinstance(key_items, Sequence) and index < len(key_items):
                key_item = key_items[index]
                if isinstance(key_item, Mapping) and isinstance(key_item.get("correct"), int):
                    key_correct = key_item["correct"]
            item_answers: list[str] = []
            if (
                isinstance(options, Sequence)
                and not isinstance(options, (bytes, bytearray, str))
                and isinstance(key_correct, int)
                and 0 <= key_correct < len(options)
            ):
                answer = options[key_correct]
                if isinstance(answer, str):
                    item_answers.append(answer)
                    all_answers.append(answer)
                if certified_forms:
                    certified_match = [f for f in certified_forms if f in options]
                    if certified_match:
                        item_answers.append(certified_match[0])
                        all_answers.append(certified_match[0])
            # Closed-class function words in quiz questions must be elicited via
            # an explicit gap marker '___' rather than verbatim stem prose.
            for form in item_answers:
                if is_closed_class_form(form, db_path):
                    if not _has_bare_gap_marker(question):
                        raise PromptPackV3Error(
                            f"closed-class quiz stem missing gap marker '___': {question!r}"
                        )
            item_checks.append((question, item_answers))
        checks.extend((text, forms) for text, forms in item_checks if isinstance(text, str))
        _add_field(instruction, all_answers)
    elif activity_type == "cloze":
        instruction = payload.get("instruction", "")
        text = payload.get("text")
        for blank in payload.get("blanks", ()):
            if isinstance(blank, Mapping):
                answer = blank.get("answer")
                if isinstance(answer, str):
                    all_answers.append(answer)
        _add_field(instruction, all_answers)
        # The exact cloze text is independently reconstructed from certified
        # source sentences and ordered markers by the binding gate. Repeated
        # lexical forms elsewhere in that source passage are legitimate
        # context, not an invented answer leak.
        # A marker-free payload is not a valid reconstructed passage, however,
        # so retain the whole-slot answer-leak rejection for that malformed
        # shape before the gap-construction gate reports its missing markers.
        if isinstance(text, str) and not _CLOZE_MARKER_RE.search(text):
            _add_field(text, all_answers)
    elif activity_type == "fill-in":
        instruction = payload.get("instruction", "")
        item_checks: list[tuple[str, list[str]]] = []
        for item in payload.get("items", ()):
            if isinstance(item, Mapping):
                sentence = item.get("sentence")
                answer = item.get("answer")
                item_answers: list[str] = []
                if isinstance(answer, str):
                    item_answers.append(answer)
                    all_answers.append(answer)
                item_checks.append((sentence, item_answers))
        checks.extend((text, forms) for text, forms in item_checks if isinstance(text, str))
        _add_field(instruction, all_answers)
    elif activity_type == "error-correction":
        instruction = payload.get("instruction", "")
        corrections = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
        units = kit.get("certified_units", ())
        if isinstance(corrections, Sequence) and not isinstance(
            corrections, (bytes, bytearray, str)
        ):
            all_answers.extend(item for item in corrections if isinstance(item, str))
            for index, (item, correction) in enumerate(
                zip(payload.get("items", ()), corrections, strict=False)
            ):
                if not isinstance(item, str) or not isinstance(correction, str):
                    continue
                unit = units[index] if isinstance(units, Sequence) and index < len(units) else None
                if not _is_certified_error_correction_context(item, correction, unit):
                    _add_field(item, [correction])
        # The adapter also binds every learner item byte-for-byte to its unit;
        # this independent check limits the contextual exception to a proven
        # one-token mutation.  Instructions may never disclose corrections.
        _add_field(instruction, all_answers)
    elif activity_type == "short-writing":
        # These are learner-visible task constraints, not hidden answer forms.
        return

    if not any(forms for _, forms in checks):
        return

    for text, forms in checks:
        if activity_type != "error-correction" and _is_trivial_template(text, certified_forms):
            raise PromptPackV3Error(
                f"learner-facing text is a bare answer form or template: {text!r}"
            )
        for form in forms:
            if is_closed_class_form(form, db_path) and activity_type in CLOSED_CLASS_ALLOWED_SHAPES:
                continue
            if _contains_form(text, form):
                raise PromptPackV3Error(
                    f"learner-facing text contains answer form {form!r}: {text!r}"
                )


def _token_count(text: str) -> int:
    return len(text.split())


def validate_elicitation_shape(activity: Mapping[str, Any], kit: Mapping[str, Any]) -> None:
    """Require quiz/cloze/fill-in prompts to be composed sentences, not bare forms."""
    certified_forms = _certified_answer_forms(kit)
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return
    activity_type = payload.get("type")

    def _check(text: str | None, label: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise PromptPackV3Error(f"{label} is empty or missing")
        if _token_count(text) < 2:
            raise PromptPackV3Error(f"{label} must be a composed sentence: {text!r}")
        if activity_type != "error-correction" and (
            text in certified_forms or _is_trivial_template(text, certified_forms)
        ):
            raise PromptPackV3Error(f"{label} is a bare answer form or template: {text!r}")

    if activity_type == "quiz":
        for index, item in enumerate(payload.get("items", ())):
            if isinstance(item, Mapping):
                _check(item.get("question"), f"quiz items[{index}].question")
    elif activity_type == "cloze":
        text = payload.get("text")
        _check(text, "cloze text")
    elif activity_type == "fill-in":
        for index, item in enumerate(payload.get("items", ())):
            if isinstance(item, Mapping):
                _check(item.get("sentence"), f"fill-in items[{index}].sentence")
    elif activity_type == "error-correction":
        for index, item in enumerate(payload.get("items", ())):
            if isinstance(item, str):
                _check(item, f"error-correction items[{index}]")
    elif activity_type == "short-writing":
        _check(payload.get("prompt"), "short-writing prompt")


def validate_gap_construction(activity: Mapping[str, Any], _kit: Mapping[str, Any]) -> None:
    """Reject degenerate cloze and fill-in shapes using content-free rule names.

    These checks apply only to the learner-facing gap layout.  They deliberately
    do not inspect, retain, or expose anchor content or answer forms in their
    failures, so the bounded repair prompt can name the violated construction
    rule without leaking protected lesson material.
    """
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return
    activity_type = payload.get("type")
    if activity_type == "cloze":
        text = payload.get("text")
        blanks = payload.get("blanks")
        if not isinstance(text, str) or not isinstance(blanks, list):
            return
        marker_count = len(_CLOZE_MARKER_RE.findall(text))
        if marker_count != len(blanks):
            raise RepairableGapConstructionError("cloze_markers_match_blanks")
        marker_ids = [int(value[1:-1]) for value in _CLOZE_MARKER_RE.findall(text)]
        if marker_ids != list(range(1, marker_count + 1)):
            raise RepairableGapConstructionError("cloze_markers_ordered")
        segments = _CLOZE_MARKER_RE.split(text)
        visible_by_segment = [len(_UKRAINIAN_WORD_RE.findall(segment)) for segment in segments]
        if marker_count and (
            visible_by_segment[0] < 2
            or visible_by_segment[-1] < 2
            or any(count < 3 for count in visible_by_segment[1:-1])
        ):
            raise RepairableGapConstructionError("cloze_local_context")
        visible_words = len(_UKRAINIAN_WORD_RE.findall(_CLOZE_MARKER_RE.sub("", text)))
        if marker_count and visible_words <= marker_count:
            raise RepairableGapConstructionError("cloze_context_visible_majority")
        return
    if activity_type != "fill-in":
        return
    items = payload.get("items")
    if not isinstance(items, list):
        return
    if any(
        not isinstance(item, Mapping)
        or not isinstance(item.get("sentence"), str)
        or item["sentence"].count("___") != 1
        for item in items
    ):
        raise RepairableGapConstructionError("fill_in_single_gap_marker")
    carrier_sentences = {
        " ".join(item["sentence"].replace("___", item["answer"]).split()).casefold()
        for item in items
        if isinstance(item, Mapping)
        and isinstance(item.get("sentence"), str)
        and isinstance(item.get("answer"), str)
    }
    if len(items) >= 2 and len(carrier_sentences) < 2:
        raise RepairableGapConstructionError("fill_in_distinct_carrier_sentences")


def validate_non_revealing_sequence(activity: Mapping[str, Any], kit: Mapping[str, Any]) -> None:
    """Reject repeated learner-facing stems in multi-item activities."""
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return
    activity_type = payload.get("type")
    stems: list[str] = []
    if activity_type == "quiz":
        items = payload.get("items")
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("question"), str):
                continue
            stems.append(item["question"])
    elif activity_type == "fill-in":
        items = payload.get("items")
        if isinstance(items, list):
            stems.extend(
                item["sentence"]
                for item in items
                if isinstance(item, Mapping) and isinstance(item.get("sentence"), str)
            )
    elif activity_type == "true-false":
        raw_items = payload.get("items")
        if isinstance(raw_items, list):
            stems.extend(
                item["statement"]
                for item in raw_items
                if isinstance(item, Mapping) and isinstance(item.get("statement"), str)
            )
    elif activity_type in {"error-correction", "text-questions"}:
        raw_items = payload.get("items")
        if isinstance(raw_items, list):
            stems.extend(item for item in raw_items if isinstance(item, str))
    normalized = [" ".join(stem.casefold().split()) for stem in stems if stem.strip()]
    if normalized and len(normalized) != len(set(normalized)):
        raise PromptPackV3Error("multi-item activity repeats a learner-facing stem")
    if (
        isinstance(kit.get("focus_alignment"), str)
        and str(kit["focus_alignment"]).startswith("degree-")
        and stems
        and max(Counter(_carrier_skeleton(stem, "___") for stem in stems).values()) > 2
    ):
        raise PromptPackV3Error("multi-item activity repeats a learner-facing template")


_TOKEN_RETRIEVAL_QUESTION_RE = re.compile(
    r"\b(?:яке|який|яка|які)\s+(?:слово|прийменник|сполучник|займенник|частина\s+мови)\b",
    re.IGNORECASE,
)
_GENERIC_TEXT_SUMMARY_QUESTION_RE = re.compile(
    r"^(?:що\s+(?:в\s+тексті\s+сказано|ми\s+дізнаємося)|"
    r"яку\s+інформацію\s+подає\s+текст)\b",
    re.IGNORECASE,
)
_GENERIC_MODAL_RELATION_RE = re.compile(
    r"^(?:навіщо|чому)\s+(?:потрібно|треба|слід|варто)\b",
    re.IGNORECASE,
)
_VAGUE_APPLICATION_OBJECT_RE = re.compile(
    r"\bщось\s+(?:подібн\w*|схож\w*|таке)\b",
    re.IGNORECASE,
)
_STATIVE_EXPERIENCE_RE = re.compile(
    r"\bдоводилося\s+(?:вам|тобі)\b[^?!.]{0,80}\bзнати\b",
    re.IGNORECASE,
)
_SECOND_PERSON_SURFACES: Final[frozenset[str]] = frozenset(
    {"ти", "тебе", "тобі", "тобою", "ви", "вас", "вам", "вами"}
)


def _uses_second_person(text: str, db_path: Path) -> bool:
    for word in _UKRAINIAN_WORD_RE.findall(text):
        if word.casefold() in _SECOND_PERSON_SURFACES:
            return True
        matches = _vesum_matches(word, db_path)
        if matches and all(
            match.get("pos") == "verb"
            and re.search(r":(?:s|p):2(?:$|:)", str(match.get("tags", ""))) is not None
            for match in matches
        ):
            return True
    return False


_QUESTION_CONTENT_POS: Final[frozenset[str]] = frozenset({"noun", "verb", "adj", "adv"})
_QUESTION_CLOSED_SURFACE_OVERRIDES: Final[frozenset[str]] = frozenset({"при", "під"})


def _question_content_lemma_groups(text: str, db_path: Path) -> tuple[frozenset[str], ...]:
    """Return at most one semantic-overlap vote per visible word.

    VESUM intentionally exposes every analysis. Counting each analysis as a
    separate word made ``при`` consume the lemma ``перти`` and made ``гори``
    consume both ``гора`` and ``горіти``. Group content analyses by visible
    surface. Only the two observed high-confidence prepositions override an
    ambiguous content parse; words such as ``коло`` and ``край`` retain their
    possible content reading and still receive at most one vote.
    """
    groups: list[frozenset[str]] = []
    for word in _UKRAINIAN_WORD_RE.findall(text):
        if word.casefold() in _QUESTION_CLOSED_SURFACE_OVERRIDES:
            continue
        matches = _vesum_matches(word, db_path)
        lemmas = frozenset(
            lemma.casefold()
            for match in matches
            if match.get("pos") in _QUESTION_CONTENT_POS
            and isinstance((lemma := match.get("lemma")), str)
            and lemma.strip()
        )
        if lemmas:
            groups.append(lemmas)
    return tuple(groups)


def validate_activity_purpose(activity: Mapping[str, Any], kit: Mapping[str, Any]) -> None:
    """Require source-grounded, category-appropriate text questions."""
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return
    if payload.get("type") == "match-up":
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise PromptPackV3Error("match-up instruction does not declare its relation")
        relations = {
            pair.get("relation")
            for unit in kit.get("certified_units", ())
            if isinstance(unit, Mapping)
            and isinstance((distinctness := unit.get("distinctness")), Mapping)
            and isinstance((pair := distinctness.get("pair")), Mapping)
            and isinstance(pair.get("relation"), str)
        }
        normalized = instruction.casefold()
        if relations == {"vesum_degree_positive_comparative.v1"}:
            if not (
                re.search(r"початков|звичайн|прикметник", normalized)
                and re.search(r"вищ|порівняль", normalized)
            ):
                raise PromptPackV3Error("match-up instruction hides positive-comparative relation")
        elif relations == {"vesum_degree_comparative_superlative.v1"}:
            if not ("вищ" in normalized and "найвищ" in normalized):
                raise PromptPackV3Error(
                    "match-up instruction hides comparative-superlative relation"
                )
        elif relations == {"degree-comparison-paraphrase.v1"}:
            if not (
                re.search(r"перефраз|те\s+саме|рівнознач|відповідн", normalized)
                and re.search(r"порівнян|твердж|реченн", normalized)
            ):
                raise PromptPackV3Error("match-up instruction hides comparison-paraphrase relation")
        elif relations == {"degree-priority-recommendation.v2"}:
            if not (
                re.search(r"потреб|пріоритет|опис|ситуац", normalized)
                and re.search(r"виснов|рекомендац|варіант", normalized)
            ):
                raise PromptPackV3Error(
                    "match-up instruction hides priority-recommendation relation"
                )
        elif relations == {"atlas_antonym.v1"}:
            if not re.search(r"антонім|протилеж", normalized):
                raise PromptPackV3Error("match-up instruction hides antonym relation")
        elif relations == {"atlas_synonym.v1"}:
            if not re.search(r"синонім|близьк.*значенн", normalized):
                raise PromptPackV3Error("match-up instruction hides synonym relation")
        else:
            raise PromptPackV3Error("match-up board mixes or omits certified relations")
        return
    if payload.get("type") != "text-questions":
        return
    items = payload.get("items")
    if not isinstance(items, list):
        return
    units = kit.get("certified_units")
    if not isinstance(units, list) or len(items) != len(units):
        return
    category_patterns = {
        "comprehension": re.compile(
            r"\b(?:що|хто|де|коли|куди|звідки|як|скільки|чи|"
            r"який|яка|які|яке|якого|яку|яким|якими)\b",
            re.IGNORECASE,
        ),
        "explanation_inference": re.compile(
            r"\b(?:чому|навіщо|коли|доки)\b|як\s+довго|з\s+якої\s+причини|"
            r"з\s+якою\s+метою|до\s+якого\s+моменту|"
            r"від\s+чого|через\s+що|"
            r"що\s+(?:це\s+)?(?:пояснює|показує|свідчить)|"
            r"який\s+висновок|як\s+(?:ви|можна)\s+(?:пояснити|поясните|зрозуміти)|"
            r"у\s+чому\s+полягає",
            re.IGNORECASE,
        ),
        "anchored_application": re.compile(
            r"\b(?:як|де)\b.*\b(?:застосувати|використати|скористатися)\b|"
            r"\b(?:у|в)\s+якій\b.*\bситуації\b|"
            r"\b(?:власному\s+досвіді|з\s+(?:вашого|твого)\s+досвіду|"
            r"подібній\s+ситуації|повсякденному\s+житті)\b|"
            r"\bчи\s+доводилося\s+(?:вам|тобі)\b",
            re.IGNORECASE,
        ),
    }
    db_path = paths.vesum_db()
    for index, (item, unit) in enumerate(zip(items, units, strict=True)):
        if not isinstance(item, str):
            continue
        if len(_UKRAINIAN_WORD_RE.findall(item)) < 3 or _TOKEN_RETRIEVAL_QUESTION_RE.search(item):
            raise PromptPackV3Error(
                f"text question asks for a token label instead of meaning at items[{index}]"
            )
        if not isinstance(unit, Mapping):
            continue
        rendering_surface = unit.get("rendering_surface")
        distinctness = unit.get("distinctness")
        category = (
            distinctness.get("question_category") if isinstance(distinctness, Mapping) else None
        )
        intent = distinctness.get("question_intent") if isinstance(distinctness, Mapping) else None
        frame = distinctness.get("question_frame") if isinstance(distinctness, Mapping) else None
        prefixes = frame.get("allowed_prefixes") if isinstance(frame, Mapping) else None
        normalized_item = item.strip().casefold()
        matched_prefix = next(
            (
                prefix.strip()
                for prefix in (prefixes if isinstance(prefixes, list) else ())
                if isinstance(prefix, str)
                and normalized_item.startswith(prefix.strip().casefold())
                and (
                    len(normalized_item) == len(prefix.strip())
                    or not normalized_item[len(prefix.strip())].isalnum()
                )
            ),
            None,
        )
        if not isinstance(prefixes, list) or not prefixes or matched_prefix is None:
            raise PromptPackV3Error(
                f"text question ignores its certified question category at items[{index}]"
            )
        if category == "comprehension" and _GENERIC_TEXT_SUMMARY_QUESTION_RE.search(item):
            raise PromptPackV3Error(
                f"text question uses generic text-summary metadiscourse at items[{index}]"
            )
        if category == "explanation_inference" and _GENERIC_MODAL_RELATION_RE.search(item):
            raise PromptPackV3Error(
                f"text question uses a generic modal relation at items[{index}]"
            )
        if category == "anchored_application" and _VAGUE_APPLICATION_OBJECT_RE.search(item):
            raise PromptPackV3Error(
                f"text question uses a vague application object at items[{index}]"
            )
        if category == "anchored_application" and _STATIVE_EXPERIENCE_RE.search(item):
            raise PromptPackV3Error(
                f"text question pairs experience with a stative predicate at items[{index}]"
            )
        if category != "anchored_application" and _uses_second_person(item, db_path):
            raise PromptPackV3Error(f"text question imports source second person at items[{index}]")
        question_topic = item.strip()[len(matched_prefix) :].strip(" \t\n:—–-?!.«»")
        if (
            category == "anchored_application"
            and matched_prefix.casefold() == "з вашого досвіду"
            and re.match(
                r"^(?:як|де|коли)\s+"
                r"(?!(?:ви|вам|вас|ваш\w*|можна|варто|слід|краще|найкраще|"
                r"зазвичай|часто)\b)",
                question_topic.lstrip(", "),
                re.IGNORECASE,
            )
        ):
            raise PromptPackV3Error(
                f"text question does not center a learner application at items[{index}]"
            )
        pattern = category_patterns.get(category)
        if pattern is None or not pattern.search(item):
            raise PromptPackV3Error(
                f"text question ignores its certified question category at items[{index}]"
            )
        allowed_intents = {
            "comprehension": {"fact-recovery"},
            "explanation_inference": {"explicit-causal", *_SOURCE_RELATION_INTENTS},
            "anchored_application": {"realistic-transfer", "anchored-application.v1"},
        }.get(category, set())
        if intent is not None and intent not in allowed_intents:
            raise PromptPackV3Error(
                f"text question ignores its certified purpose intent at items[{index}]"
            )
        if intent in {"realistic-transfer", "anchored-application.v1"} and re.search(
            r"через\s+те,?\s+що[^?!.]{0,180}\bале\b",
            item,
            re.IGNORECASE,
        ):
            raise PromptPackV3Error(
                f"text question turns a contrast into one causal reason at items[{index}]"
            )
        topic_limit = 9 if category == "anchored_application" else 4
        if len(_UKRAINIAN_WORD_RE.findall(question_topic)) > topic_limit:
            raise PromptPackV3Error(f"text question restates its expected answer at items[{index}]")
        if _UNRESOLVED_QUESTION_DEIXIS_RE.search(
            question_topic
        ) or _UNRESOLVED_TOPIC_PRONOUN_RE.search(question_topic):
            raise PromptPackV3Error(
                f"text question uses unresolved source deixis at items[{index}]"
            )
        source_groups = _question_content_lemma_groups(rendering_surface or "", db_path)
        question_groups = _question_content_lemma_groups(item, db_path)
        topic_groups = _question_content_lemma_groups(question_topic, db_path)
        source_lemmas = set().union(*source_groups) if source_groups else set()
        question_lemmas = set().union(*question_groups) if question_groups else set()
        certified_topic = (
            distinctness.get("question_topic") if isinstance(distinctness, Mapping) else None
        )
        certified_topic_lemma = (
            certified_topic.get("lemma") if isinstance(certified_topic, Mapping) else None
        )
        if (
            isinstance(certified_topic_lemma, str)
            and certified_topic_lemma.casefold() not in question_lemmas
        ):
            raise PromptPackV3Error(
                f"text question is detached from its certified topic at items[{index}]"
            )
        # One naturally reused content lemma plus a locked cognitive category
        # grounds the question without forcing awkward two-word parroting of
        # the source sentence.  Distinct-stem and token-retrieval gates still
        # reject generic or degenerate question sets.
        overlap_words = sum(bool(group & source_lemmas) for group in topic_groups)
        if not source_groups or overlap_words < 1:
            raise PromptPackV3Error(
                f"text question is detached from its rendering surface at items[{index}]"
            )
        if overlap_words > 2 or len(source_lemmas - question_lemmas) < 2:
            raise PromptPackV3Error(f"text question consumes its source answer at items[{index}]")
        source_frequency_comparative = any(
            word.casefold() in _FREQUENCY_COMPARATIVES
            and any(
                parse.get("pos") == "adv" and "compc" in str(parse.get("tags", ""))
                for parse in _vesum_matches(word, db_path)
            )
            for word in _UKRAINIAN_WORD_RE.findall(rendering_surface or "")
        )
        if source_frequency_comparative and not any(
            word.casefold() in _FREQUENCY_SCALE_FORMS
            for word in _UKRAINIAN_WORD_RE.findall(item)
        ):
            raise PromptPackV3Error(
                f"text question omits its certified comparison at items[{index}]"
            )
        source_degree_lemmas = {
            lemma
            for word in _UKRAINIAN_WORD_RE.findall(rendering_surface or "")
            if (lemma := _catalog_positive_lemma(word)) is not None
            and any(
                _degree_rank(str(match.get("tags", ""))) in {1, 2}
                for match in _vesum_matches(word, db_path)
            )
        }
        question_degree_lemmas = {
            lemma
            for word in _UKRAINIAN_WORD_RE.findall(item)
            if (lemma := _catalog_positive_lemma(word)) is not None
        }
        focus_alignment = (
            distinctness.get("focus_alignment") if isinstance(distinctness, Mapping) else None
        )
        if (
            focus_alignment == "anchor-comprehension"
            and source_degree_lemmas
            and not source_degree_lemmas & question_degree_lemmas
        ):
            raise PromptPackV3Error(
                f"text question omits its certified comparison at items[{index}]"
            )


def validate_visible_writing_constraints(
    activity: Mapping[str, Any], kit: Mapping[str, Any]
) -> None:
    """Require every certified productive-task marker in the learner prompt."""
    payload = activity.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "short-writing":
        return
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptPackV3Error("short-writing prompt is missing")
    markers = tuple(
        marker
        for unit in kit.get("certified_units", ())
        if isinstance(unit, Mapping) and isinstance(unit.get("allowed_forms"), list)
        for marker in unit["allowed_forms"]
        if isinstance(marker, str) and marker
    )
    for marker in markers:
        if not _contains_form(prompt, marker):
            raise PromptPackV3Error("short-writing prompt omits a certified constraint")
    if kit.get("focus_alignment") != "degree-writing":
        normalized = prompt.casefold()
        if re.search(
            r"\b(?:лем(?:а|и|у|ою|і)|лексем\w*|морфолог\w*|інфінітив\w*|"
            r"відмін(?:ок|ка|ку|ком|ки)|частин\w*\s+мови)\b",
            normalized,
        ):
            raise PromptPackV3Error("short-writing prompt exposes linguistic jargon")
        if not re.search(
            r"\b(?:поясн\w*|порівн\w*|обґрунт\w*|опиш\w*|розкаж\w*|"
            r"уяв\w*|оцін\w*|вислов\w*|напиш\w*)\b",
            normalized,
        ):
            raise PromptPackV3Error(
                "short-writing prompt does not ask for communicative source use"
            )
        return
    if kit.get("focus_alignment") == "degree-writing":
        normalized = prompt.casefold()
        if "ступен" in normalized and "порівнян" in normalized:
            raise PromptPackV3Error("short-writing prompt exposes a grammar label as its theme")
        if not re.search(r"порівн|зістав|обґрунт|поясн|вибер|кращ", normalized):
            raise PromptPackV3Error(
                "short-writing prompt does not ask for a communicative comparison or choice"
            )
        if not re.search(r"(?:мінімум|щонайменше)\s+3\b", normalized) or not re.search(
            r"вищ|найвищ", normalized
        ):
            raise PromptPackV3Error(
                "short-writing prompt omits the required degree-adjective count"
            )
        if re.search(r"\b(?:використайте|ужийте)\s+(?:усі\s+)?слова\b", normalized):
            raise PromptPackV3Error(
                "short-writing prompt requires literal base words instead of derived degree forms"
            )
        units = kit.get("certified_units")
        unit = units[0] if isinstance(units, list) and len(units) == 1 else None
        surface = unit.get("rendering_surface") if isinstance(unit, Mapping) else None
        distinctness = unit.get("distinctness") if isinstance(unit, Mapping) else None
        warrants = (
            distinctness.get("attribute_warrants") if isinstance(distinctness, Mapping) else None
        )
        required_lemmas = set(warrants) if isinstance(warrants, Mapping) else set()
        if (
            not isinstance(surface, str)
            or surface not in prompt
            or not required_lemmas
            or len(
                {
                    _catalog_positive_lemma(word)
                    for word in _UKRAINIAN_WORD_RE.findall(surface)
                    if _catalog_positive_lemma(word) is not None
                }
            )
            < 3
        ):
            raise PromptPackV3Error("short-writing prompt omits its certified factual scenario")
        leaked = {
            _catalog_positive_lemma(word)
            for word in _UKRAINIAN_WORD_RE.findall(prompt)
            for match in _vesum_matches(word, paths.vesum_db())
            if _degree_rank(str(match.get("tags", ""))) in {1, 2}
        }
        if required_lemmas & leaked:
            raise PromptPackV3Error("short-writing prompt leaks a target degree form")


# Closed-class parts of speech.  Words belonging to these classes have no
# inflectional paradigm to share with a distractor, so adjacency is checked
# by POS class instead of by lemma.
_CLOSED_CLASS_POS = frozenset({"prep", "part", "conj"})


def _vesum_matches(form: str, db_path: Path) -> list[dict]:
    """Return every VESUM analysis for ``form`` across common capitalisations."""
    variants = {form, form.lower()}
    if form:
        variants.add(form[:1].upper() + form[1:])
    results = verify_words(sorted(variants), db_path=db_path)
    matches: list[dict] = []
    for variant in variants:
        matches.extend(results.get(variant, []))
    return matches


def _lemma_set(form: str, db_path: Path) -> set[str]:
    """Return the lowercased lemma set for ``form`` according to VESUM.

    Checks the exact spelling, lowercase, and first-letter-uppercase variants
    so capitalized sentence-initial forms resolve to their lemma.
    """
    return {
        match["lemma"].lower()
        for match in _vesum_matches(form, db_path)
        if isinstance(match.get("lemma"), str)
    }


def _pos_set(form: str, db_path: Path) -> set[str]:
    """Return the set of VESUM POS tags for ``form``."""
    return {pos for pos in (match.get("pos") for match in _vesum_matches(form, db_path)) if pos}


def _personal_pronoun_frames(form: str, db_path: Path) -> set[tuple[str, str]]:
    """Return (case, number) frames for personal-pronoun analyses.

    Person and gender are the meaningful distractor contrast (for example
    ``Ми / Ви / Вони`` in one plural nominative subject slot), not a reason to
    reject an otherwise morphosyntactically adjacent option.
    """
    frames: set[tuple[str, str]] = set()
    for match in _vesum_matches(form, db_path):
        tags = str(match.get("tags", "")).split(":")
        if "pron" not in tags or "pers" not in tags:
            continue
        person_index = tags.index("pers") + 1
        person = tags[person_index] if person_index < len(tags) else ""
        case = next((tag for tag in tags if tag.startswith("v_")), "")
        number = "pl" if "p" in tags else "sg"
        if person and case:
            frames.add((case, number))
    return frames


def _is_personal_pronoun_adjacent(answer: str, option: str, db_path: Path) -> bool:
    """Allow a real pronoun alternative in the same case/number frame."""
    answer_frames = _personal_pronoun_frames(answer, db_path)
    option_frames = _personal_pronoun_frames(option, db_path)
    return bool(answer_frames and answer_frames & option_frames)


def _is_single_form_lemma(lemma: str, db_path: Path) -> bool:
    """True when ``lemma`` has exactly one distinct word form in VESUM."""
    forms = verify_lemma(lemma, db_path=db_path)
    if not forms:
        return False
    return len({form["word_form"] for form in forms}) == 1


def _uninflectable_allowed_pos(answer: str, db_path: Path) -> set[str] | None:
    """Return the allowed distractor POS set if ``answer`` is uninflectable.

    An answer is uninflectable when any of its VESUM analyses is a closed-class
    POS (preposition, particle, conjunction) or when one of its lemmas has a
    one-form paradigm.  In the closed-class case the allowed POS set is narrowed
    to those closed classes, so a preposition-target item does not accept an
    interjection distractor just because the surface form is homonymous.
    """
    matches = _vesum_matches(answer, db_path)
    answer_pos = {pos for pos in (match.get("pos") for match in matches) if pos}
    closed_pos = answer_pos & _CLOSED_CLASS_POS
    if closed_pos:
        return closed_pos
    lemmas = {match.get("lemma") for match in matches if isinstance(match.get("lemma"), str)}
    if any(_is_single_form_lemma(lemma, db_path) for lemma in lemmas):
        return answer_pos
    return None


_DEGREE_SEED_PAIRS: Final[frozenset[frozenset[str]]] = frozenset(
    {
        frozenset({"добрий", "кращий"}),
        frozenset({"високий", "вищий"}),
        frozenset({"низький", "нижчий"}),
        frozenset({"легкий", "легший"}),
        frozenset({"великий", "більший"}),
        frozenset({"малий", "менший"}),
        frozenset({"поганий", "гірший"}),
        frozenset({"глибокий", "глибший"}),
        frozenset({"довгий", "довший"}),
        frozenset({"широкий", "ширший"}),
        frozenset({"вузький", "вужчий"}),
        frozenset({"дорогий", "дорожчий"}),
        frozenset({"близький", "ближчий"}),
        frozenset({"далекий", "дальший"}),
        frozenset({"дешевий", "дешевший"}),
        frozenset({"дивний", "дивніший"}),
        frozenset({"світлий", "світліший"}),
        frozenset({"теплий", "тепліший"}),
        frozenset({"важливий", "важливіший"}),
        frozenset({"простий", "простіший"}),
        frozenset({"тихий", "тихіший"}),
        frozenset({"холодний", "холодніший"}),
    }
)


def _degree_rank(tags_str: str) -> int | None:
    if "compb" in tags_str:
        return 0
    if "compc" in tags_str:
        return 1
    if "comps" in tags_str:
        return 2
    return None


def _catalog_positive_lemma(form: str) -> str | None:
    normalized = form.casefold()
    normalized_candidates = {normalized}
    normalized_candidates.update(
        str(match.get("lemma", "")).casefold()
        for match in _vesum_matches(normalized, paths.vesum_db())
        if str(match.get("tags", "")).startswith("adj:")
    )
    return next(
        (
            ladder.positive
            for ladder in DEGREE_LADDERS.values()
            if normalized_candidates
            & {
                ladder.positive.casefold(),
                ladder.comparative.casefold(),
                ladder.superlative.casefold(),
            }
        ),
        None,
    )


def _strip_degree(word: str) -> set[str]:
    w = word.lower()
    bases = {w}
    if w.startswith("най"):
        w = w[3:]
        bases.add(w)
    if w.endswith("іший"):
        bases.add(w[:-4] + "ий")
    elif w.endswith("ший"):
        bases.add(w[:-3] + "ий")
    return bases


def _is_degree_adjacent(
    answer: str, option: str, db_path: Path, kit: Mapping[str, Any] | None = None
) -> bool:
    """Return True if option and answer form a valid degree-adjacent pair."""
    ans_matches = _vesum_matches(answer, db_path)
    opt_matches = _vesum_matches(option, db_path)
    if not ans_matches or not opt_matches:
        return False

    ans_degrees = {_degree_rank(m.get("tags", "")) for m in ans_matches}
    opt_degrees = {_degree_rank(m.get("tags", "")) for m in opt_matches}
    ans_degrees.discard(None)
    opt_degrees.discard(None)

    has_degree_diff = any(d1 != d2 for d1 in ans_degrees for d2 in opt_degrees)
    if not has_degree_diff:
        return False

    ans_frames = {
        tuple(
            t
            for t in m.get("tags", "").split(":")
            if t in {"m", "f", "n", "p", "nom", "gen", "dat", "acc", "instr", "loc", "voc"}
        )
        for m in ans_matches
    }
    opt_frames = {
        tuple(
            t
            for t in m.get("tags", "").split(":")
            if t in {"m", "f", "n", "p", "nom", "gen", "dat", "acc", "instr", "loc", "voc"}
        )
        for m in opt_matches
    }
    if ans_frames and opt_frames and not (ans_frames & opt_frames):
        return False

    ans_lemmas = {m.get("lemma", "").lower() for m in ans_matches if m.get("lemma")}
    opt_lemmas = {m.get("lemma", "").lower() for m in opt_matches if m.get("lemma")}

    ans_bases = set().union(*[_strip_degree(lem) for lem in ans_lemmas])
    opt_bases = set().union(*[_strip_degree(lem) for lem in opt_lemmas])

    if ans_bases & opt_bases:
        return True

    degree_pairs = (
        frozenset(frozenset(p) for p in kit.get("degree_seed_pairs", ()))
        if kit and isinstance(kit, Mapping) and "degree_seed_pairs" in kit
        else _DEGREE_SEED_PAIRS
    )
    for pair in degree_pairs:
        if (ans_bases & pair) and (opt_bases & pair):
            return True

    return False


_IMPERFECTIVE_FORCING_CUES: Final[frozenset[str]] = frozenset(
    {
        "щодня",
        "завжди",
        "регулярно",
        "кожного дня",
        "часто",
        "зазвичай",
        "постійно",
        "тривалий час",
        "годинами",
        "кожного ранку",
        "щопонеділка",
    }
)

_PERFECTIVE_FORCING_CUES: Final[frozenset[str]] = frozenset(
    {"раптом", "зненацька", "за одну хвилину", "раптово", "вмить", "одного разу"}
)

_ASPECT_SEED_PAIRS: Final[frozenset[frozenset[str]]] = frozenset(
    {
        frozenset({"прочитувати", "прочитати"}),
        frozenset({"читати", "прочитати"}),
        frozenset({"писати", "написати"}),
        frozenset({"робити", "зробити"}),
        frozenset({"бачити", "побачити"}),
        frozenset({"чути", "почути"}),
        frozenset({"говорити", "сказати"}),
        frozenset({"брати", "взяти"}),
        frozenset({"допомагати", "допомогти"}),
    }
)


def _is_aspect_adjacent(
    answer: str,
    option: str,
    stem_text: str,
    db_path: Path,
    kit: Mapping[str, Any] | None = None,
) -> bool:
    """Return True if option and answer are opposite aspect forms with a forcing cue in stem."""
    if not stem_text:
        return False

    stem_lower = stem_text.lower()
    words = re.findall(r"[А-ЯҐЄІЇа-яґєіїʼ'’]+", stem_lower)
    if "не" in words or "ні" in words:
        return False

    ans_matches = _vesum_matches(answer, db_path)
    opt_matches = _vesum_matches(option, db_path)
    if not ans_matches or not opt_matches:
        return False

    ans_aspects = {
        "imperf"
        if "imperf" in m.get("tags", "")
        else ("perf" if "perf" in m.get("tags", "") else None)
        for m in ans_matches
    }
    opt_aspects = {
        "imperf"
        if "imperf" in m.get("tags", "")
        else ("perf" if "perf" in m.get("tags", "") else None)
        for m in opt_matches
    }
    ans_aspects.discard(None)
    opt_aspects.discard(None)

    is_opposite = ("imperf" in ans_aspects and "perf" in opt_aspects) or (
        "perf" in ans_aspects and "imperf" in opt_aspects
    )
    if not is_opposite:
        return False

    ans_frames = {
        tuple(
            t
            for t in m.get("tags", "").split(":")
            if t in {"s", "p", "1", "2", "3", "m", "f", "n", "past", "inf"}
        )
        for m in ans_matches
    }
    opt_frames = {
        tuple(
            t
            for t in m.get("tags", "").split(":")
            if t in {"s", "p", "1", "2", "3", "m", "f", "n", "past", "inf"}
        )
        for m in opt_matches
    }
    if ans_frames and opt_frames and not (ans_frames & opt_frames):
        return False

    ans_lemmas = {m.get("lemma", "").lower() for m in ans_matches if m.get("lemma")}
    opt_lemmas = {m.get("lemma", "").lower() for m in opt_matches if m.get("lemma")}

    related = bool(ans_lemmas & opt_lemmas)
    if not related:
        aspect_pairs = (
            frozenset(frozenset(p) for p in kit.get("aspect_seed_pairs", ()))
            if kit and isinstance(kit, Mapping) and "aspect_seed_pairs" in kit
            else _ASPECT_SEED_PAIRS
        )
        for pair in aspect_pairs:
            if (ans_lemmas & pair) and (opt_lemmas & pair):
                related = True
                break

    if not related:
        return False

    has_forcing_cue = any(
        cue in stem_lower for cue in _IMPERFECTIVE_FORCING_CUES | _PERFECTIVE_FORCING_CUES
    )
    return has_forcing_cue


def validate_distractor_adjacency(activity: Mapping[str, Any], kit: Mapping[str, Any]) -> None:
    """Require every distractor to be a real VESUM form adjacent to the answer.

    For INFLECTABLE answer forms the distractor must share at least one
    lowercased VESUM lemma with the certified answer form, be a personal
    pronoun in the same person/case/number frame, or be degree-adjacent or
    aspect-adjacent with a forcing cue in the stem. For UNINFLECTABLE answers --
    closed-class POS (prep/part/conj) or a one-form paradigm -- the distractor
    must instead be a real VESUM word of the same POS class and distinct from
    the answer.

    MCQ option lists must contain at least 3 options.
    The answer form must sit at the index declared by the answer key.
    """
    db_path = paths.vesum_db()
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return
    activity_type = payload.get("type")
    answer_key = activity.get("answer_key")

    def _validate_options(
        answer: str,
        options: Sequence[Any],
        correct_index: int,
        label: str,
        stem_text: str = "",
        unit_index: int | None = None,
    ) -> None:
        if not isinstance(options, Sequence) or isinstance(options, (bytes, bytearray, str)):
            raise PromptPackV3Error(f"{label} options must be a list")
        if len(options) < 3:
            raise PromptPackV3Error(f"{label} options count must be at least 3")
        if len(options) != len(set(options)):
            raise PromptPackV3Error(f"{label} options contain duplicate entries")
        if answer not in options:
            raise PromptPackV3Error(f"{label} answer {answer!r} is not in options")
        if options.count(answer) > 1:
            raise PromptPackV3Error(f"{label} options contain duplicate answer {answer!r}")
        if correct_index < 0 or correct_index >= len(options):
            raise PromptPackV3Error(f"{label} correct index {correct_index} is out of range")
        if options[correct_index] != answer:
            raise PromptPackV3Error(f"{label} answer form is not at the declared correct index")
        answer_lemmas = _lemma_set(answer, db_path)
        if not answer_lemmas:
            raise PromptPackV3Error(f"{label} answer form {answer!r} is not in VESUM")
        certified_units = kit.get("certified_units", ())
        unit_distinctness = None
        if (
            isinstance(unit_index, int)
            and isinstance(certified_units, Sequence)
            and unit_index < len(certified_units)
            and isinstance(certified_units[unit_index], Mapping)
        ):
            candidate_distinctness = certified_units[unit_index].get("distinctness")
            if isinstance(candidate_distinctness, Mapping):
                unit_distinctness = candidate_distinctness
        unit_alignment = (
            unit_distinctness.get("focus_alignment")
            if isinstance(unit_distinctness, Mapping)
            else None
        )
        choice_bank = (
            unit_distinctness.get("choice_bank") if isinstance(unit_distinctness, Mapping) else None
        )
        frame_family = (
            unit_distinctness.get("frame_family")
            if isinstance(unit_distinctness, Mapping)
            else None
        )
        shared_degree_bank = (
            unit_alignment in {"degree-formation", "degree-context"}
            and isinstance(choice_bank, list)
            and len(choice_bank) >= 6
        )
        closed_degree_bank = (
            isinstance(unit_alignment, str)
            and unit_alignment.startswith("degree-")
            and isinstance(choice_bank, list)
        )
        if closed_degree_bank and (
            len(options) != len(choice_bank) or set(options) != set(choice_bank)
        ):
            raise PromptPackV3Error(f"{label} does not preserve its exact certified choice bank")
        if frame_family == "cross-gap-lexical.v1":
            if not isinstance(choice_bank, list) or set(options) != set(choice_bank):
                raise PromptPackV3Error(
                    f"{label} does not preserve its exact cross-gap lexical bank"
                )
            all_units = kit.get("certified_units", ())
            other_gap_answers = {
                forms[0]
                for other_index, other_unit in enumerate(all_units)
                if other_index != unit_index
                and isinstance(other_unit, Mapping)
                and isinstance((forms := other_unit.get("allowed_forms")), list)
                and forms
                and isinstance(forms[0], str)
            }
            distractors = [option for option in options if option != answer]
            if not distractors or not set(distractors) <= other_gap_answers:
                raise PromptPackV3Error(
                    f"{label} contains a distractor that is not another certified gap answer"
                )
            if any(answer_lemmas & _lemma_set(option, db_path) for option in distractors):
                raise PromptPackV3Error(f"{label} cross-gap bank repeats the answer lemma")
            answer_pos = _pos_set(answer, db_path)
            if not any(answer_pos & _pos_set(option, db_path) for option in distractors):
                raise PromptPackV3Error(
                    f"{label} cross-gap bank lacks a same-POS lexical distractor"
                )
            return
        if shared_degree_bank:
            degree_classes: set[int] = set()
            positive_lemmas: set[str] = set()
            for option in options:
                if not isinstance(option, str):
                    raise PromptPackV3Error(f"{label} option must be a string")
                matches = _vesum_matches(option, db_path)
                ranks = {
                    rank
                    for match in matches
                    if (rank := _degree_rank(str(match.get("tags", "")))) is not None
                }
                positive = _catalog_positive_lemma(option)
                if not ranks or positive is None:
                    raise PromptPackV3Error(
                        f"{label} shared-bank option {option!r} is not a catalogued degree form"
                    )
                degree_classes.update(ranks)
                if positive in positive_lemmas:
                    raise PromptPackV3Error(
                        f"{label} shared bank repeats one adjective degree paradigm"
                    )
                positive_lemmas.add(positive)
            if len(degree_classes) < 2:
                raise PromptPackV3Error(f"{label} shared bank has only one degree category")
            return
        if closed_degree_bank:
            degree_classes: set[int] = set()
            positive_lemmas: set[str] = set()
            for option in options:
                if not isinstance(option, str):
                    raise PromptPackV3Error(f"{label} option must be a string")
                matches = _vesum_matches(option, db_path)
                ranks = {
                    rank
                    for match in matches
                    if (rank := _degree_rank(str(match.get("tags", "")))) is not None
                }
                positive = _catalog_positive_lemma(option)
                if not ranks or positive is None:
                    raise PromptPackV3Error(
                        f"{label} option {option!r} is not a catalogued degree form"
                    )
                degree_classes.update(ranks)
                positive_lemmas.add(positive)
            if len(positive_lemmas) != 1 or len(degree_classes) < 2:
                raise PromptPackV3Error(f"{label} same-lemma bank lacks a real degree contrast")
            return
        allowed_pos = _uninflectable_allowed_pos(answer, db_path)
        degree_adjacent_count = 0
        for option_index, option in enumerate(options):
            if not isinstance(option, str):
                raise PromptPackV3Error(f"{label} option must be a string")
            if option_index == correct_index:
                continue
            if option.lower() == answer.lower():
                raise PromptPackV3Error(f"{label} distractor {option!r} equals answer {answer!r}")
            option_lemmas = _lemma_set(option, db_path)
            if not option_lemmas:
                raise PromptPackV3Error(f"{label} distractor {option!r} is not in VESUM")
            if allowed_pos is not None:
                option_pos = _pos_set(option, db_path)
                if not (option_pos & allowed_pos):
                    raise PromptPackV3Error(
                        f"{label} distractor {option!r} is not of the same POS class "
                        f"as answer {answer!r}"
                    )
            elif not (answer_lemmas & option_lemmas):
                if _is_personal_pronoun_adjacent(answer, option, db_path):
                    continue
                if _is_degree_adjacent(answer, option, db_path, kit):
                    degree_adjacent_count += 1
                    continue
                if _is_aspect_adjacent(answer, option, stem_text, db_path, kit):
                    continue
                raise PromptPackV3Error(
                    f"{label} distractor {option!r} does not share a lemma with answer {answer!r}"
                )
        kit_alignment = kit.get("focus_alignment")
        if (isinstance(kit_alignment, str) and kit_alignment.startswith("degree-")) or (
            isinstance(unit_alignment, str) and unit_alignment.startswith("degree-")
        ):
            if not any(
                _degree_rank(str(match.get("tags", ""))) is not None
                for match in _vesum_matches(answer, db_path)
            ):
                raise PromptPackV3Error(f"{label} answer is detached from the degree focus")
            if degree_adjacent_count == 0:
                raise PromptPackV3Error(f"{label} needs at least one degree-contrast distractor")

    if activity_type == "quiz":
        key_items = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
        correct_indices: list[int] = []
        for index, item in enumerate(payload.get("items", ())):
            if not isinstance(item, Mapping):
                continue
            options = item.get("options")
            correct = item.get("correct")
            if (
                not isinstance(options, Sequence)
                or isinstance(options, (bytes, bytearray, str))
                or not isinstance(correct, int)
                or correct < 0
                or correct >= len(options)
            ):
                raise PromptPackV3Error(f"quiz items[{index}] lacks valid options or correct index")
            answer = options[correct]
            if not isinstance(answer, str):
                raise PromptPackV3Error(f"quiz items[{index}] answer is not a string")
            declared_correct = correct
            if isinstance(key_items, Sequence) and index < len(key_items):
                key_item = key_items[index]
                if isinstance(key_item, Mapping) and isinstance(key_item.get("correct"), int):
                    declared_correct = key_item["correct"]
            _validate_options(
                answer,
                options,
                declared_correct,
                f"quiz items[{index}]",
                item.get("question", ""),
                index,
            )
            correct_indices.append(declared_correct)
        if len(correct_indices) >= 3 and len(set(correct_indices)) == 1:
            raise PromptPackV3Error(
                "quiz activity options must vary answer placement across items "
                f"(all {len(correct_indices)} items placed answer at index {correct_indices[0]})"
            )
    elif activity_type == "cloze":
        key_blanks = answer_key.get("blanks", []) if isinstance(answer_key, Mapping) else []
        text = payload.get("text", "")
        correct_indices = []
        for index, blank in enumerate(payload.get("blanks", ())):
            if not isinstance(blank, Mapping):
                continue
            answer = blank.get("answer")
            if not isinstance(answer, str):
                raise PromptPackV3Error(f"cloze blanks[{index}] lacks answer")
            options = blank.get("options")
            if not isinstance(options, Sequence) or isinstance(options, (bytes, bytearray, str)):
                raise PromptPackV3Error(f"cloze blanks[{index}] options must be a list")
            if answer not in options:
                raise PromptPackV3Error(
                    f"cloze blanks[{index}] answer {answer!r} is not in options"
                )
            if options.count(answer) > 1:
                raise PromptPackV3Error(
                    f"cloze blanks[{index}] options contain duplicate answer {answer!r}"
                )
            correct_idx = options.index(answer)
            _validate_options(
                answer,
                options,
                correct_idx,
                f"cloze blanks[{index}]",
                text if isinstance(text, str) else "",
                index,
            )
            correct_indices.append(correct_idx)
            if isinstance(key_blanks, Sequence) and index < len(key_blanks):
                key_blank = key_blanks[index]
                if isinstance(key_blank, Mapping) and key_blank.get("answer") != answer:
                    raise PromptPackV3Error(f"cloze blanks[{index}] answer key mismatch")
        if len(correct_indices) >= 3 and len(set(correct_indices)) == 1:
            raise PromptPackV3Error(
                "cloze activity options must vary answer placement across items "
                f"(all {len(correct_indices)} blanks placed answer at index {correct_indices[0]})"
            )
    elif activity_type == "fill-in":
        key_forms = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
        correct_indices = []
        for index, item in enumerate(payload.get("items", ())):
            if not isinstance(item, Mapping):
                continue
            answer = item.get("answer")
            if not isinstance(answer, str):
                raise PromptPackV3Error(f"fill-in items[{index}] lacks answer")
            if (
                isinstance(key_forms, Sequence)
                and index < len(key_forms)
                and key_forms[index] != answer
            ):
                raise PromptPackV3Error(f"fill-in items[{index}] answer key mismatch")
            options = item.get("options")
            if not isinstance(options, Sequence) or isinstance(options, (bytes, bytearray, str)):
                raise PromptPackV3Error(f"fill-in items[{index}] options must be a list")
            if answer not in options:
                raise PromptPackV3Error(
                    f"fill-in items[{index}] answer {answer!r} is not in options"
                )
            if options.count(answer) > 1:
                raise PromptPackV3Error(
                    f"fill-in items[{index}] options contain duplicate answer {answer!r}"
                )
            correct_idx = options.index(answer)
            _validate_options(
                answer,
                options,
                correct_idx,
                f"fill-in items[{index}]",
                item.get("sentence", ""),
                index,
            )
            correct_indices.append(correct_idx)
        if len(correct_indices) >= 3 and len(set(correct_indices)) == 1:
            raise PromptPackV3Error(
                "fill-in activity options must vary answer placement across items "
                f"(all {len(correct_indices)} items placed answer at index {correct_indices[0]})"
            )
    elif activity_type == "error-correction":
        key_items = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
        items = payload.get("items", ())
        if (
            not isinstance(items, Sequence)
            or isinstance(items, (bytes, bytearray, str))
            or not isinstance(key_items, Sequence)
            or isinstance(key_items, (bytes, bytearray, str))
            or len(items) != len(key_items)
        ):
            raise PromptPackV3Error("error-correction payload/answer_key count mismatch")
        for index, (item, answer) in enumerate(zip(items, key_items, strict=True)):
            if not isinstance(item, str) or not item.strip():
                raise PromptPackV3Error(f"error-correction items[{index}] is empty or not a string")
            if not isinstance(answer, str) or not answer.strip():
                raise PromptPackV3Error(
                    f"error-correction answer_key items[{index}] is empty or not a string"
                )
            words = re.findall(r"[А-ЯҐЄІЇа-яґєіїʼ'’]+", item)
            for j in range(len(words) - 1):
                if words[j].lower() == words[j + 1].lower():
                    raise PromptPackV3Error(
                        f"error-correction items[{index}] contains repeated token "
                        f"corruption: {words[j]!r}"
                    )
            answer_lemmas = _lemma_set(answer, db_path)
            if not answer_lemmas:
                raise PromptPackV3Error(
                    f"error-correction items[{index}] answer form {answer!r} is not in VESUM"
                )
            allowed_pos = _uninflectable_allowed_pos(answer, db_path)
            found_adjacent = False
            for w in words:
                if w.lower() == answer.lower():
                    continue
                w_lemmas = _lemma_set(w, db_path)
                if not w_lemmas:
                    continue
                if allowed_pos is not None:
                    w_pos = _pos_set(w, db_path)
                    if bool(w_pos & allowed_pos):
                        found_adjacent = True
                        break
                elif (
                    bool(answer_lemmas & w_lemmas)
                    or _is_degree_adjacent(answer, w, db_path, kit)
                    or _is_aspect_adjacent(answer, w, item, db_path, kit)
                ):
                    found_adjacent = True
                    break
            if not found_adjacent:
                raise PromptPackV3Error(
                    f"error-correction items[{index}] sentence does not contain an "
                    f"adjacent wrong form of answer {answer!r}"
                )


def validate_exemplar_contamination(
    activity: Mapping[str, Any], type_kit: Mapping[str, Any]
) -> None:
    """Reject learner-facing output that copies the synthetic exemplar.

    The compact exemplar is a shape guide, not source material.  A response
    that recycles its literal strings, raw certified forms, or exact concatenation
    is degenerate and must fail closed instead of reaching a teacher.
    """
    activity_type = type_kit.get("type")
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return

    if activity_type == "text-questions":
        primary = _primary_forms(type_kit)
        items = payload.get("items")
        if isinstance(items, list) and items == list(primary):
            raise PromptPackV3Error(
                f"{activity_type} items are raw certified forms instead of composed content"
            )
    elif activity_type == "short-writing":
        primary = _primary_forms(type_kit)
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt == " ".join(primary):
            raise PromptPackV3Error(
                "short-writing prompt is a concatenation of raw certified forms"
            )

    for text in _learner_facing_strings(activity):
        for fragment in EXEMPLAR_ONLY_STRINGS:
            if fragment in text:
                raise PromptPackV3Error(
                    f"learner-facing text contains synthetic exemplar fragment: {fragment!r}"
                )


def six_item_negative_exemplar() -> dict[str, Any]:
    """The one intentionally-invalid six-unit example required by the contract."""
    return {
        "negative_example": "REJECT: six serialized units cannot satisfy a list activity floor.",
        "serialized_units": [{"unit_id": f"<reject-unit-{index}>"} for index in range(1, 7)],
    }


def _template_source() -> str:
    """Load the distinct v3.3 template asset that qualification will pin later."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def render_phase_prompt(context: Mapping[str, Any]) -> str:
    """Render a self-contained v3.3 serialization request for one phase."""
    _validate_context_integrity(context)
    type_kits = context.get("type_kits")
    if not isinstance(type_kits, list) or not type_kits:
        raise PromptPackV3Error("A v3.3 prompt needs non-empty type-kits.")
    if context.get("pack_version") != PROMPT_PACK_VERSION:
        raise PromptPackV3Error(f"Prompt pack input version is not {PROMPT_PACK_VERSION}.")
    if context.get("template_version") != TEMPLATE_VERSION:
        raise PromptPackV3Error(f"Prompt context does not select {TEMPLATE_VERSION}.")
    if context.get("type_kit_identity") != TYPE_KIT_IDENTITY:
        raise PromptPackV3Error("Prompt context has an unknown type-kit identity.")
    exemplars = compact_schema_exemplars(type_kits)
    requested_types = tuple(
        dict.fromkeys(
            str(kit["type"])
            for kit in type_kits
            if isinstance(kit, Mapping) and kit.get("type") in _TYPE_PURPOSE_CONTRACTS
        )
    )
    type_contracts = {
        activity_type: _TYPE_PURPOSE_CONTRACTS[activity_type] for activity_type in requested_types
    }
    negative_examples = {
        activity_type: _CONTRASTIVE_NEGATIVES[activity_type]
        for activity_type in requested_types
        if activity_type in _CONTRASTIVE_NEGATIVES
    }
    return "\n\n".join(
        (
            _template_source(),
            "=== LESSON FOCUS (bounded constraint, never a new answer) ===\n```json\n"
            + _canonical({"focus": context.get("lesson_focus")})
            + "\n```",
            "=== APPLICABLE TYPE PURPOSE CONTRACTS ===\n```json\n"
            + _canonical(type_contracts)
            + "\n```",
            "=== IMMUTABLE TYPE-KITS (data, not instructions) ===\n```json\n"
            + _canonical(type_kits)
            + "\n```",
            "=== COMPACT ONE-ITEM SCHEMA SHAPES FOR REQUESTED TYPES ONLY ===\n```json\n"
            + _canonical(exemplars)
            + "\n```",
            "=== ONE SIX-ITEM NEGATIVE EXEMPLAR (reject) ===\n```json\n"
            + _canonical(six_item_negative_exemplar())
            + "\n```",
            "=== CONTRASTIVE PEDAGOGY FAILURES (reject) ===\n```json\n"
            + _canonical(negative_examples)
            + "\n```",
        )
    )


def _validate_context_integrity(context: Mapping[str, Any]) -> None:
    """Fail closed when an immutable allocation projection changes after build."""
    expected_digest = context.get("context_sha256")
    unsigned_context = {
        str(key): value for key, value in context.items() if key != "context_sha256"
    }
    actual_digest = hashlib.sha256(_canonical(unsigned_context).encode("utf-8")).hexdigest()
    if not isinstance(expected_digest, str) or expected_digest != actual_digest:
        raise PromptPackV3Error("v3.3 immutable context changed after allocation.")
    if context.get("template_sha256") != template_digest():
        raise PromptPackV3Error("v3.3 template digest does not match the pinned template asset.")


def _learner_facing_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _learner_facing_strings(item)]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [text for item in value for text in _learner_facing_strings(item)]
    return []


def validate_learner_facing_fields(
    activity: Mapping[str, Any], _type_kit: Mapping[str, Any]
) -> None:
    """Reject English true/false parentheticals anywhere learner prose can reach."""
    if any(_TRUE_FALSE_NARRATION_RE.search(text) for text in _learner_facing_strings(activity)):
        raise PromptPackV3Error("Learner-facing fields must not contain (True) or (False).")


def _validate_response_shape(
    payload: object, context: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or set(payload) != {"slots"}:
        raise PromptPackV3Error("v3.3 response must contain exactly one slots array.")
    slots = payload.get("slots")
    type_kits = context.get("type_kits")
    if not isinstance(slots, list) or not isinstance(type_kits, list):
        raise PromptPackV3Error("v3.3 response and context require slots/type-kits arrays.")
    if len(slots) != len(type_kits):
        raise PromptPackV3Error("v3.3 response slot count differs from the scheduled allocation.")
    return slots


_VALID_SLOT_FIELDS = frozenset({"slot_id", "type", "activity", "serialized_units"})
# Optional provenance field injected by the production adapter after the provider
# call.  It is not part of the model contract and is stripped before the block
# reaches the teacher, but it must survive the shape gate so the adapter can
# stamp truthful block provenance.
_SLOT_PROVENANCE_FIELD = "_generator_model_id"


def _validate_slot_identity(
    record: object, type_kit: Mapping[str, Any], index: int
) -> Mapping[str, Any]:
    if not isinstance(record, Mapping):
        raise PromptPackV3Error(f"slots[{index}] must be an object.")
    fields = set(record)
    if fields != _VALID_SLOT_FIELDS and fields != _VALID_SLOT_FIELDS | {_SLOT_PROVENANCE_FIELD}:
        raise PromptPackV3Error(f"slots[{index}] leaks or omits v3.3 serialization fields.")
    if record.get("slot_id") != type_kit.get("slot_id") or record.get("type") != type_kit.get(
        "type"
    ):
        raise PromptPackV3Error(f"slots[{index}] is not the scheduled slot/type.")
    if not isinstance(record.get("activity"), Mapping):
        raise PromptPackV3Error(f"slots[{index}].activity must be an object.")
    return record


def validate_slot_shape(record: object, type_kit: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate one response record against one immutable scheduled slot.

    This is the slot-local shape boundary used by the live evaluator.
    ``validate_response`` remains phase-atomic: callers that need that
    behavior must continue to use it.  Keeping this narrow helper public lets
    the evaluator retain the same validation stages while reporting a failure
    for every affected slot.
    """
    return _validate_slot_identity(record, type_kit, 0)


def validate_slot_deterministic_gates(
    record: Mapping[str, Any],
    type_kit: Mapping[str, Any],
    *,
    deterministic_gates: Sequence[DeterministicGate],
) -> None:
    """Run the always-on gates for one already shape-valid response record."""
    if not deterministic_gates:
        raise PromptPackV3Error("v3.3 validation requires the always-on deterministic gate runner.")
    activity = record.get("activity")
    if not isinstance(activity, Mapping):  # guarded by ``validate_slot_shape``
        raise PromptPackV3Error("v3.3 slot activity must be an object.")
    try:
        validate_learner_facing_fields(activity, type_kit)
    except Exception as error:
        raise RuleNamedRejection("learner_facing_fields") from error
    for gate in deterministic_gates:
        try:
            if getattr(gate, "__name__", "") == "validate_activity_purpose":
                item_rejections = _activity_purpose_item_rejections(gate, activity, type_kit)
                if len(item_rejections) > 1:
                    raise RuleNamedRejectionGroup(item_rejections)
                if item_rejections:
                    raise item_rejections[0]
            gate(activity, type_kit)
        except (RuleNamedRejection, RuleNamedRejectionGroup):
            raise
        except RepairableSerializationError as error:
            item = re.search(r":item=(\d+)$", str(error))
            suffix = f"question_frame:item={item.group(1)}" if item is not None else None
            raise RuleNamedRejection("activity_binding", suffix=suffix) from error
        except RepairableGapConstructionError as error:
            raise RuleNamedRejection("gap_construction", suffix=str(error)) from error
        except Exception as error:
            gate_name = getattr(gate, "__name__", "")
            rule_key = _DETERMINISTIC_GATE_RULE_KEYS.get(
                gate_name, "unregistered_deterministic_gate"
            )
            if gate_name == "validate_distractor_adjacency" and "repeated token corruption" in str(
                error
            ):
                rule_key = "distractor_repeated_token"
            safe_suffix = (
                _activity_purpose_safe_suffix(error)
                if gate_name == "validate_activity_purpose"
                else None
            )
            raise RuleNamedRejection(rule_key, suffix=safe_suffix) from error


def validate_slot_serialization(record: Mapping[str, Any], type_kit: Mapping[str, Any]) -> None:
    """Validate the exact immutable substrate for one gated response record."""
    _validate_exact_serialization(record, type_kit)


def validate_slot_raw_contract(
    record: Mapping[str, Any], *, raw_contract_validator: RawContractValidator
) -> dict[str, Any]:
    """Apply the final raw contract only after a slot passed prior stages."""
    if raw_contract_validator is None:
        raise PromptPackV3Error("v3.3 validation requires the raw-contract validator.")
    activity = record.get("activity")
    if not isinstance(activity, Mapping):  # guarded by ``validate_slot_shape``
        raise PromptPackV3Error("v3.3 slot activity must be an object.")
    raw_contract_validator(activity)
    return dict(activity)


def _validate_exact_serialization(record: Mapping[str, Any], type_kit: Mapping[str, Any]) -> None:
    """Resolve only exact ordered scheduled-unit references against the immutable kit."""
    actual = record.get("serialized_units")
    expected_ids = type_kit.get("scheduled_unit_ids")
    expected_count = type_kit.get("scheduled_unit_count")
    if (
        not isinstance(actual, list)
        or not isinstance(expected_ids, list)
        or not isinstance(expected_count, int)
    ):
        raise PromptPackV3Error("v3.3 serialization context is malformed.")
    if len(actual) != expected_count:
        raise PromptPackV3Error(
            "serialization failure: exact scheduled unit count is required before raw validation."
        )
    if any(not isinstance(unit, Mapping) or set(unit) != {"unit_id"} for unit in actual):
        raise PromptPackV3Error(
            "serialization failure: serialized units must contain only unit_id references."
        )
    actual_ids = [unit["unit_id"] for unit in actual]
    if actual_ids != expected_ids or len(actual_ids) != len(set(actual_ids)):
        raise PromptPackV3Error(
            "serialization failure: unit ID references must exactly match the scheduled allocation."
        )


def validate_response(
    payload: object,
    context: Mapping[str, Any],
    *,
    deterministic_gates: Sequence[DeterministicGate],
    raw_contract_validator: RawContractValidator,
) -> list[dict[str, Any]]:
    """Validate deterministic gates, exact serialization, then raw contracts.

    The order is intentional and test-pinned: always-on deterministic gates
    run first, exact scheduled-unit counts and substrate equality run second,
    and a future raw-contract validator runs last. This module does not change
    any existing VESUM or production gate implementation.
    """
    if not deterministic_gates:
        raise PromptPackV3Error("v3.3 validation requires the always-on deterministic gate runner.")
    if raw_contract_validator is None:
        raise PromptPackV3Error("v3.3 validation requires the raw-contract validator.")
    _validate_context_integrity(context)
    records = _validate_response_shape(payload, context)
    type_kits = context["type_kits"]
    checked_slots: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
    for index, (record, type_kit) in enumerate(zip(records, type_kits, strict=True)):
        checked = _validate_slot_identity(record, type_kit, index)
        activity = checked["activity"]
        assert isinstance(activity, Mapping)
        checked_slots.append((checked, type_kit, activity))

    # The whole phase completes each deterministic gate before one count or
    # raw-contract decision can escape for an earlier slot.
    for _checked, type_kit, activity in checked_slots:
        validate_learner_facing_fields(activity, type_kit)
        for gate in deterministic_gates:
            gate(activity, type_kit)

    for checked, type_kit, _activity in checked_slots:
        _validate_exact_serialization(checked, type_kit)

    for _checked, _type_kit, activity in checked_slots:
        raw_contract_validator(activity)
    return [dict(activity) for _checked, _type_kit, activity in checked_slots]
