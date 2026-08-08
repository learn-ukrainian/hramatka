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
from typing import Any

from . import paths
from .lesson_capacity_v3 import LessonAllocation
from .linguistics import verify_words
from .serializer_policy import serializer_temperature
from .teacher_ready_density_v3 import floor_for

PROMPT_PACK_VERSION = "PromptPackInput.v3.1"
TEMPLATE_VERSION = "gemma-phase-pack.v3.3"
TYPE_KIT_IDENTITY = "TeacherReadyDensity.v3.unit-plan-kit.v2"

_TRUE_FALSE_NARRATION_RE = re.compile(r"\(\s*(?:true|false)\s*\)", re.IGNORECASE)
_TEMPLATE_PATH = Path(__file__).with_name("prompts") / "gemma-phase-pack.v3.3.md"

# Literal strings that appear only in the full-density synthetic exemplar.  Their
# presence in a model response means the serializer copied the exemplar instead
# of generating teacher-ready content from the certified substrate.
EXEMPLAR_ONLY_STRINGS = frozenset({
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
    "SYNTHETIC-MARK-TEXT",
    "SYNTHETIC-DISTRACTOR",
    "Синтетична вказівка.",
})
_EXEMPLAR_QUIZ_QUESTION_TEMPLATE = "Вкажіть правильну форму: {}"


class PromptPackV3Error(ValueError):
    """A deterministic v3.3 pack serialization or validation failure."""


DeterministicGate = Callable[[Mapping[str, Any], Mapping[str, Any]], None]
RawContractValidator = Callable[[Mapping[str, Any]], None]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
                    {"unit_id": f"<{activity_type}-unit-{index}>"}
                    for index in range(1, count + 1)
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
                "source": f"SYNTHETIC-ERROR-SOURCE {index}: речення з помилкою.",
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
        return [{"prompt": "SYNTHETIC-WRITING-PROMPT: напишіть речення."}]
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
                    {"index": index, "correct": items[index]["correct"]}
                    for index in range(count)
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
        return {
            "payload": {
                "type": "short-writing",
                "prompt": items[0]["prompt"],
            },
            "answer_key": {"guidance": "Синтетична вказівка."},
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


def validate_verbatim_answer_ban(
    activity: Mapping[str, Any], kit: Mapping[str, Any]
) -> None:
    """Reject learner-facing prose that contains, names, or quotes a correct answer.

    The certified substrate supplies the correct form; the model must elicit it
    through composed Ukrainian prose.  Any verbatim occurrence of a correct
    answer in a prose field is a leak.  Displayed option lists and match-up
    pairs are not prose fields, so they are excluded from this check.
    """
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return
    activity_type = payload.get("type")
    answer_key = activity.get("answer_key")
    certified_forms = _certified_answer_forms(kit)

    prose_fields: list[str] = []
    answer_forms: list[str] = []

    if activity_type == "quiz":
        instruction = payload.get("instruction", "")
        if isinstance(instruction, str):
            prose_fields.append(instruction)
        key_items = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
        for index, item in enumerate(payload.get("items", ())):
            if not isinstance(item, Mapping):
                continue
            question = item.get("question")
            if isinstance(question, str):
                prose_fields.append(question)
            options = item.get("options", ())
            correct = item.get("correct")
            key_correct = correct
            if isinstance(key_items, Sequence) and index < len(key_items):
                key_item = key_items[index]
                if isinstance(key_item, Mapping) and isinstance(key_item.get("correct"), int):
                    key_correct = key_item["correct"]
            if (
                isinstance(options, Sequence)
                and not isinstance(options, (bytes, bytearray, str))
                and isinstance(key_correct, int)
                and 0 <= key_correct < len(options)
            ):
                answer = options[key_correct]
                if isinstance(answer, str):
                    answer_forms.append(answer)
                if certified_forms:
                    certified_match = [f for f in certified_forms if f in options]
                    if certified_match:
                        answer_forms.append(certified_match[0])
    elif activity_type == "cloze":
        instruction = payload.get("instruction", "")
        if isinstance(instruction, str):
            prose_fields.append(instruction)
        text = payload.get("text")
        if isinstance(text, str):
            prose_fields.append(text)
        for blank in payload.get("blanks", ()):
            if isinstance(blank, Mapping):
                answer = blank.get("answer")
                if isinstance(answer, str):
                    answer_forms.append(answer)
    elif activity_type == "fill-in":
        instruction = payload.get("instruction", "")
        if isinstance(instruction, str):
            prose_fields.append(instruction)
        for item in payload.get("items", ()):
            if isinstance(item, Mapping):
                sentence = item.get("sentence")
                if isinstance(sentence, str):
                    prose_fields.append(sentence)
                answer = item.get("answer")
                if isinstance(answer, str):
                    answer_forms.append(answer)
    elif activity_type == "error-correction":
        instruction = payload.get("instruction", "")
        if isinstance(instruction, str):
            prose_fields.append(instruction)
        corrections = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
        for index, item in enumerate(payload.get("items", ())):
            if isinstance(item, str):
                prose_fields.append(item)
                if isinstance(corrections, Sequence) and index < len(corrections):
                    correction = corrections[index]
                    if isinstance(correction, str):
                        answer_forms.append(correction)

    if not answer_forms:
        return

    for text in prose_fields:
        if _is_trivial_template(text, certified_forms):
            raise PromptPackV3Error(
                f"learner-facing text is a bare answer form or template: {text!r}"
            )
        for form in answer_forms:
            if _contains_form(text, form):
                raise PromptPackV3Error(
                    f"learner-facing text contains answer form {form!r}: {text!r}"
                )


def _token_count(text: str) -> int:
    return len(text.split())


def validate_elicitation_shape(
    activity: Mapping[str, Any], kit: Mapping[str, Any]
) -> None:
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


def _lemma_set(form: str, db_path: Path) -> set[str]:
    """Return the lowercased lemma set for ``form`` according to VESUM.

    Checks the exact spelling, lowercase, and first-letter-uppercase variants
    so capitalized sentence-initial forms resolve to their lemma.
    """
    variants = {form, form.lower()}
    if form:
        variants.add(form[:1].upper() + form[1:])
    results = verify_words(sorted(variants), db_path=db_path)
    lemmas: set[str] = set()
    for variant in variants:
        for match in results.get(variant, []):
            lemma = match.get("lemma")
            if isinstance(lemma, str):
                lemmas.add(lemma.lower())
    return lemmas


def validate_distractor_adjacency(
    activity: Mapping[str, Any], kit: Mapping[str, Any]
) -> None:
    """Require every distractor to be a real VESUM form sharing a lemma with the answer.

    The answer form must sit at the index declared by the answer key.  Every
    other option must be present in VESUM and share at least one lowercased
    lemma with the certified answer form for that item.
    """
    db_path = paths.vesum_db()
    payload = activity.get("payload")
    if not isinstance(payload, Mapping):
        return
    activity_type = payload.get("type")
    answer_key = activity.get("answer_key")

    def _validate_options(
        answer: str, options: Sequence[Any], correct_index: int, label: str
    ) -> None:
        if not isinstance(options, Sequence) or isinstance(options, (bytes, bytearray, str)):
            raise PromptPackV3Error(f"{label} options must be a list")
        if correct_index < 0 or correct_index >= len(options):
            raise PromptPackV3Error(f"{label} correct index {correct_index} is out of range")
        if options[correct_index] != answer:
            raise PromptPackV3Error(
                f"{label} answer form is not at the declared correct index"
            )
        answer_lemmas = _lemma_set(answer, db_path)
        if not answer_lemmas:
            raise PromptPackV3Error(f"{label} answer form {answer!r} is not in VESUM")
        for option_index, option in enumerate(options):
            if not isinstance(option, str):
                raise PromptPackV3Error(f"{label} option must be a string")
            if option_index == correct_index:
                continue
            option_lemmas = _lemma_set(option, db_path)
            if not option_lemmas:
                raise PromptPackV3Error(f"{label} distractor {option!r} is not in VESUM")
            if not (answer_lemmas & option_lemmas):
                raise PromptPackV3Error(
                    f"{label} distractor {option!r} does not share a lemma with answer {answer!r}"
                )

    if activity_type == "quiz":
        key_items = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
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
            _validate_options(answer, options, declared_correct, f"quiz items[{index}]")
    elif activity_type == "cloze":
        key_blanks = answer_key.get("blanks", []) if isinstance(answer_key, Mapping) else []
        for index, blank in enumerate(payload.get("blanks", ())):
            if not isinstance(blank, Mapping):
                continue
            answer = blank.get("answer")
            if not isinstance(answer, str):
                raise PromptPackV3Error(f"cloze blanks[{index}] lacks answer")
            # Cloze answer_key index is the blank id; assume ordered identity.
            _validate_options(answer, blank.get("options"), 0, f"cloze blanks[{index}]")
            if isinstance(key_blanks, Sequence) and index < len(key_blanks):
                key_blank = key_blanks[index]
                if isinstance(key_blank, Mapping) and key_blank.get("answer") != answer:
                    raise PromptPackV3Error(f"cloze blanks[{index}] answer key mismatch")
    elif activity_type == "fill-in":
        key_forms = answer_key.get("items", []) if isinstance(answer_key, Mapping) else []
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
            _validate_options(answer, item.get("options"), 0, f"fill-in items[{index}]")


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
        raise PromptPackV3Error("Prompt pack input version is not PromptPackInput.v3.1.")
    if context.get("template_version") != TEMPLATE_VERSION:
        raise PromptPackV3Error("Prompt context does not select gemma-phase-pack.v3.3.")
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
        raise PromptPackV3Error(
            "Learner-facing fields must not contain (True) or (False)."
        )


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
    if (
        record.get("slot_id") != type_kit.get("slot_id")
        or record.get("type") != type_kit.get("type")
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
    validate_learner_facing_fields(activity, type_kit)
    for gate in deterministic_gates:
        gate(activity, type_kit)


def validate_slot_serialization(
    record: Mapping[str, Any], type_kit: Mapping[str, Any]
) -> None:
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
