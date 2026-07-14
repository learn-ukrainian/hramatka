"""Canonical ви-form learner instructions for projected B1 activities.

Generation may emit mixed-register ty-forms; projection replaces them with
this bank so one lesson never drifts between «Обери» and «Оберіть».
"""

from __future__ import annotations

import re
from typing import Final

from hramatka.contracts import PILOT_ACTIVITY_TYPES

# Imperative ty-forms the bank must never contain (ви-form policy).
_TI_IMPERATIVE_RE: Final = re.compile(
    r"\b(?:Обери|Познач|Заповни|Виправ|Напиши|З['ʼ]єднай|Вибери|Прочитай|Дай)\b",
    re.UNICODE,
)

CANONICAL_INSTRUCTIONS: Final[dict[str, str]] = {
    "true-false": "Позначте, чи правильні твердження за текстом.",
    "quiz": "Оберіть правильну відповідь за текстом.",
    "cloze": "Заповніть пропуск словом із тексту.",
    "match-up": "З'єднайте слово з опори з його значенням.",
    "mark-the-words": "Позначте слова за вказаним критерієм.",
    "error-correction": (
        "У кожному реченні навмисно допущено одну помилку. "
        "Знайдіть її та оберіть правильну форму."
    ),
    "fill-in": "Оберіть правильну форму.",
    "text-questions": "Обговоріть запитання за текстом.",
    "short-writing": "Напишіть короткий текст за опорою.",
}

_MARK_WORDS_BY_CRITERIA: Final[dict[str, str]] = {
    "pos=verb": "Позначте всі дієслова.",
    "pos=noun": "Позначте всі іменники.",
    "pos=adj": "Позначте всі прикметники.",
    "pos=noun;case=gen": "Позначте іменники в родовому відмінку.",
    "pos=adj;case=gen": "Позначте прикметники в родовому відмінку.",
    "pos=adj;case=loc": "Позначте прикметники в місцевому відмінку.",
    "pos=verb;tense=pres": "Позначте дієслова в теперішньому часі.",
}


def mark_the_words_instruction(criteria: object) -> str:
    """Return the canonical mark-the-words instruction for one VESUM criterion."""
    if isinstance(criteria, str):
        normalized = criteria.strip()
        if normalized in _MARK_WORDS_BY_CRITERIA:
            return _MARK_WORDS_BY_CRITERIA[normalized]
    return CANONICAL_INSTRUCTIONS["mark-the-words"]


def canonical_instruction(activity: dict) -> str:
    """Map one projected activity to its canonical ви-form instruction."""
    activity_type = activity.get("type", "")
    if activity_type == "mark-the-words":
        return mark_the_words_instruction(activity.get("criteria"))
    return CANONICAL_INSTRUCTIONS.get(
        activity_type,
        str(activity.get("instruction") or "").strip(),
    )


def apply_instruction_bank(activity: dict) -> dict:
    """Replace the activity instruction with the canonical bank string."""
    activity["instruction"] = canonical_instruction(activity)
    return activity


def bank_has_no_ty_forms() -> bool:
    """True when every bank string avoids ty-register imperatives."""
    strings = list(CANONICAL_INSTRUCTIONS.values()) + list(_MARK_WORDS_BY_CRITERIA.values())
    return all(_TI_IMPERATIVE_RE.search(text) is None for text in strings)


if set(CANONICAL_INSTRUCTIONS) != set(PILOT_ACTIVITY_TYPES):
    raise RuntimeError("Canonical instruction bank must cover every pilot activity type")
if not bank_has_no_ty_forms():
    raise RuntimeError("Canonical instruction bank contains ty-register imperatives")
