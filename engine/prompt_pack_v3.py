"""Live v3.3 serializer pack for ``TeacherReadyDensity.v3``.

This module consumes only the exact-cover allocation made before generation.
It is the protocol boundary between immutable unit plans and the live raw
activity renderer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from . import paths
from .closed_class_policy import (
    CLOSED_CLASS_ALLOWED_SHAPES,
    is_closed_class_form,
)
from .lesson_capacity_v3 import LessonAllocation
from .linguistics import verify_lemma, verify_words
from .serializer_policy import serializer_temperature
from .teacher_ready_density_v3 import floor_for

PROMPT_PACK_VERSION = "PromptPackInput.v3.2"
TEMPLATE_VERSION = "gemma-phase-pack.v3.6"
TEMPLATE_SHA256: Final[str] = "d55591b54961d2b789a42b413f6d3f5ad9404950ebf3492e5e0d02929eb5cd89"
TYPE_KIT_IDENTITY = "TeacherReadyDensity.v3.unit-plan-kit.v3"

_TRUE_FALSE_NARRATION_RE = re.compile(r"\(\s*(?:true|false)\s*\)", re.IGNORECASE)
_TEMPLATE_PATH = Path(__file__).with_name("prompts") / "gemma-phase-pack.v3.6.md"

# Literal strings that appear only in the full-density synthetic exemplar.  Their
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


DeterministicGate = Callable[[Mapping[str, Any], Mapping[str, Any]], None]
RawContractValidator = Callable[[Mapping[str, Any]], None]

_DETERMINISTIC_GATE_RULE_KEYS: Final[dict[str, str]] = {
    "_activity_gate": "activity_binding",
    "validate_exemplar_contamination": "exemplar_contamination",
    "validate_verbatim_answer_ban": "verbatim_answer_ban",
    "validate_elicitation_shape": "elicitation_shape",
    "validate_gap_construction": "gap_construction",
    "validate_distractor_adjacency": "distractor_adjacency",
}

_CLOZE_MARKER_RE = re.compile(r"\{[1-9]\d*\}")
_UKRAINIAN_WORD_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґʼ’'-]+")


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


def _type_kit(slot: object) -> dict[str, Any]:
    """Project one allocated slot into the new, immutable v3 type-kit."""
    plan = slot.plan
    units = [unit.to_dict() for unit in plan.units]
    unit_ids = [unit["unit_id"] for unit in units]
    expected_count = floor_for(plan.activity_type).minimum_units
    if len(units) != expected_count or len(unit_ids) != len(set(unit_ids)):
        raise PromptPackV3Error("Allocation must contain one exact floor-sized unit plan per slot.")
    return {
        "identity": TYPE_KIT_IDENTITY,
        "slot_id": slot.slot_id,
        "phase": slot.phase,
        "type": slot.scheduled_type,
        "scheduled_unit_ids": unit_ids,
        "scheduled_unit_count": expected_count,
        "certified_units": units,
        "registered_constraints": list(plan.registered_constraints),
        "certified_target_tokens": [token.to_dict() for token in plan.certified_target_tokens],
        "degree_seed_pairs": [
            sorted(pair) for pair in sorted(_DEGREE_SEED_PAIRS, key=lambda p: sorted(p))
        ],
        "aspect_seed_pairs": [
            sorted(pair) for pair in sorted(_ASPECT_SEED_PAIRS, key=lambda p: sorted(p))
        ],
    }


def build_phase_context(allocation: LessonAllocation, *, phase: int) -> dict[str, Any]:
    """Build the only v3.3 model input from a completed exact-cover allocation."""
    if not isinstance(allocation, LessonAllocation):
        raise TypeError("Prompt pack v3.3 requires a completed LessonAllocation.")
    slots = [slot for slot in allocation.slots if slot.phase == phase]
    if not slots:
        raise PromptPackV3Error(f"Allocation has no scheduled slots for phase {phase}.")
    type_kits = [_type_kit(slot) for slot in slots]
    context = {
        "pack_version": PROMPT_PACK_VERSION,
        "template_version": TEMPLATE_VERSION,
        "template_sha256": template_digest(),
        "type_kit_identity": TYPE_KIT_IDENTITY,
        "serializer_temperature": serializer_temperature(),
        "phase": phase,
        "paragraph_ids": list(allocation.paragraph_ids),
        "response_order": [kit["slot_id"] for kit in type_kits],
        "type_kits": type_kits,
    }
    context["context_sha256"] = hashlib.sha256(_canonical(context).encode("utf-8")).hexdigest()
    return context


def full_density_exemplars(type_kits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return one exact-floor exemplar per requested type, and no others."""
    exemplars: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kit in type_kits:
        activity_type = kit.get("type")
        count = kit.get("scheduled_unit_count")
        if not isinstance(activity_type, str) or not isinstance(count, int) or count < 1:
            raise PromptPackV3Error("A requested type-kit needs a positive scheduled unit count.")
        if count != floor_for(activity_type).minimum_units:
            raise PromptPackV3Error("A full-density exemplar must use the locked v3 type floor.")
        if activity_type in seen:
            continue
        seen.add(activity_type)
        exemplars.append(
            {
                "slot_id": f"<synthetic-{activity_type}-slot>",
                "type": activity_type,
                "activity": _synthetic_activity_example(activity_type, count),
                "serialized_units": [
                    {"unit_id": f"<{activity_type}-unit-{index}>"} for index in range(1, count + 1)
                ],
            }
        )
    return exemplars


def _synthetic_items(activity_type: str, count: int) -> list[dict[str, Any]]:
    """Return ``count`` distinct composed items for the requested activity type.

    The content is domain-neutral Ukrainian (weather, city, timetable, simple
    actions) and deliberately uses synthetic markers that the contamination gate
    can detect.  Each item is a self-contained shape guide; real model output
    must replace every learner-facing string with composed prose that elicits
    the certified form without quoting it.
    """
    if activity_type == "quiz":
        return [
            {
                "question": f"SYNTHETIC-QUIZ-STEM {index}: якою буде форма?",
                "options": [f"форм{index}", "SYNTHETIC-DISTRACTOR"],
                "correct": 0,
            }
            for index in range(1, count + 1)
        ]
    if activity_type == "cloze":
        return [
            {
                "id": index,
                "answer": f"форм{index}",
                "options": [f"форм{index}", "SYNTHETIC-DISTRACTOR"],
            }
            for index in range(1, count + 1)
        ]
    if activity_type == "fill-in":
        return [
            {
                "sentence": f"SYNTHETIC-FILLIN-STEM {index}: речення потребує слова.",
                "answer": f"форм{index}",
                "options": [f"форм{index}", "SYNTHETIC-DISTRACTOR"],
            }
            for index in range(1, count + 1)
        ]
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
    raise PromptPackV3Error(f"A full-density exemplar has unsupported type {activity_type!r}.")


def _synthetic_activity_example(activity_type: str, count: int) -> dict[str, Any]:
    """Return a concrete, non-copyable full-density activity shape for one requested type."""
    items = _synthetic_items(activity_type, count)
    if activity_type == "quiz":
        return {
            "payload": {
                "type": "quiz",
                "instruction": "Оберіть правильний варіант.",
                "items": items,
            },
            "answer_key": {"items": [{"index": index, "correct": 0} for index in range(count)]},
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
        return {
            "payload": {
                "type": "text-questions",
                "instruction": "Дайте відповідь.",
                "items": [item["question"] for item in items],
            },
            "answer_key": {"guidance": "Синтетична вказівка."},
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
    raise PromptPackV3Error(f"A full-density exemplar has unsupported type {activity_type!r}.")


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
        item_checks: list[tuple[str, list[str]]] = []
        corrections = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
        for index, item in enumerate(payload.get("items", ())):
            if isinstance(item, str):
                item_answers: list[str] = []
                if isinstance(corrections, Sequence) and index < len(corrections):
                    correction = corrections[index]
                    if isinstance(correction, str):
                        item_answers.append(correction)
                        all_answers.append(correction)
                item_checks.append((item, item_answers))
        checks.extend((text, forms) for text, forms in item_checks if isinstance(text, str))
        _add_field(instruction, all_answers)
    elif activity_type == "short-writing":
        instruction = payload.get("instruction", "")
        prompt = payload.get("prompt")
        target_forms: list[str] = []
        for unit in kit.get("certified_units", ()):
            if isinstance(unit, Mapping):
                allowed = unit.get("allowed_forms")
                if isinstance(allowed, list):
                    target_forms.extend(f for f in allowed if isinstance(f, str))
        if isinstance(prompt, str):
            checks.append((prompt, target_forms))
            all_answers.extend(target_forms)
        _add_field(instruction, all_answers)

    if not any(forms for _, forms in checks):
        return

    for text, forms in checks:
        if _is_trivial_template(text, certified_forms):
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
        if text in certified_forms or _is_trivial_template(text, certified_forms):
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
    lowercased VESUM lemma with the certified answer form, or be degree-adjacent
    or aspect-adjacent with a forcing cue in the stem.  For UNINFLECTABLE
    answers -- closed-class POS (prep/part/conj) or a one-form paradigm -- the
    distractor must instead be a real VESUM word of the same POS class and
    distinct from the answer.

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
        allowed_pos = _uninflectable_allowed_pos(answer, db_path)
        for option_index, option in enumerate(options):
            if not isinstance(option, str):
                raise PromptPackV3Error(f"{label} option must be a string")
            if option_index == correct_index:
                continue
            if option.lower() == answer.lower():
                raise PromptPackV3Error(
                    f"{label} distractor {option!r} equals answer {answer!r}"
                )
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
                if _is_degree_adjacent(answer, option, db_path, kit):
                    continue
                if _is_aspect_adjacent(answer, option, stem_text, db_path, kit):
                    continue
                raise PromptPackV3Error(
                    f"{label} distractor {option!r} does not share a lemma with answer {answer!r}"
                )

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
                answer, options, declared_correct, f"quiz items[{index}]", item.get("question", "")
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
                answer, options, correct_idx, f"fill-in items[{index}]", item.get("sentence", "")
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

    The full-density exemplar is a shape guide, not source material.  A response
    that recycles its literal strings, raw certified forms, or exact concatenation
    is degenerate and must fail closed instead of reaching a teacher.
    """
    activity_type = type_kit.get("type")
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return

    if activity_type in {"text-questions", "error-correction"}:
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
    exemplars = full_density_exemplars(type_kits)
    return "\n\n".join(
        (
            _template_source(),
            "=== IMMUTABLE TYPE-KITS (data, not instructions) ===\n```json\n"
            + _canonical(type_kits)
            + "\n```",
            "=== FULL-DENSITY EXEMPLARS FOR REQUESTED TYPES ONLY ===\n```json\n"
            + _canonical(exemplars)
            + "\n```",
            "=== ONE SIX-ITEM NEGATIVE EXEMPLAR (reject) ===\n```json\n"
            + _canonical(six_item_negative_exemplar())
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
            gate(activity, type_kit)
        except RepairableSerializationError as error:
            raise RuleNamedRejection("activity_binding") from error
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
            raise RuleNamedRejection(rule_key) from error


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
