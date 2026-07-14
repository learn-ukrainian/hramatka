"""Versioned activity registry for the typed candidate-bank pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hramatka.contracts import PILOT_ACTIVITY_TYPES

from . import content_density, retrieval, schema
from .gates import evidence_span, matchup_semantics, numeral, vesum_tags
from .gates import vesum as vesum_gate
from .prompts import load_extractive_template

REGISTRY_VERSION = "wave1b.registry.v1"
EXTRACTIVE_PROMPT_VERSION = "extractive-v5:89b97c528e30"

PromptBuilder = Callable[..., str]
EvidenceLocator = Callable[[dict[str, Any]], tuple[str, ...]]
RawValidator = Callable[[object], list[str]]
ActivityGate = Callable[
    [dict[str, Any], list[schema.Evidence], str, schema.GateResult, dict | None], None
]
EvidenceAnswerPairs = Callable[[schema.HramatkaActivity], list[tuple[str, str]]]
_SPACE_RE = re.compile(r"\s+")
_UA_WORD_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’]+", re.UNICODE)
_FILL_BLANK_RE = re.compile(r"____|\{answer\}")
_OPEN_STEM_MAX_TOKENS = 18


def build_extractive_v1_prompt(
    anchor: str,
    level: str,
    types: list[str],
    grounding_pack: str,
    *,
    counts: dict[str, int] | None = None,
    anchor_snapshot: dict | None = None,
) -> str:
    """Build the count-aware extractive prompt shared by registered types."""
    requested_counts = counts or {activity_type: 1 for activity_type in types}
    request_lines = "\n".join(
        f"- {activity_type}: {requested_counts[activity_type]}" for activity_type in types
    )
    density_lines = []
    for activity_type in types:
        entry = ACTIVITY_REGISTRY[activity_type]
        target = content_density.scaled_generation_target(entry, anchor_snapshot)
        if target > entry.minimum_survivors:
            density_lines.append(
                f"- {activity_type}: орієнтир {target} пунктів/пар/пропусків на одне завдання"
            )
    template = load_extractive_template().replace("{{REQUESTED_ACTIVITY_COUNTS}}", request_lines)
    density_block = ""
    if density_lines:
        density_block = (
            "\n\nЩільність (орієнтири генерації, не мінімум):\n" + "\n".join(density_lines)
        )
    return (
        f"{template}{density_block}\n\n"
        f"=== GROUNDING PACK ===\n{grounding_pack}\n\n"
        f"=== ТЕКСТ-ОПОРА (anchor, рівень {level}) ===\n{anchor}\n"
    )


def _validate_basics(raw: object, expected_type: str) -> list[str]:
    if not isinstance(raw, dict):
        return ["candidate must be a JSON object"]
    if raw.get("type") != expected_type:
        return [f"candidate type must be {expected_type!r}"]
    if not isinstance(raw.get("instruction"), str) or not raw["instruction"].strip():
        return ["candidate requires a non-empty Ukrainian instruction"]
    return []


def _validate_true_false(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "true-false"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        errors.append("candidate requires a non-empty items list")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"items[{index}] must be an object")
                continue
            if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                errors.append(f"items[{index}] requires a non-empty evidence quote")
            if not isinstance(item.get("statement"), str) or not item["statement"].strip():
                errors.append(f"items[{index}] requires a non-empty statement")
            if not isinstance(item.get("correct"), bool):
                errors.append(f"items[{index}] requires boolean correct")
    return errors


def _validate_quiz(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "quiz"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        errors.append("quiz requires a non-empty items list")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"items[{index}] must be an object")
                continue
            if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                errors.append(f"items[{index}] requires a non-empty evidence quote")
            if not isinstance(item.get("question"), str) or not item["question"].strip():
                errors.append(f"items[{index}] requires a non-empty question")
            options = item.get("options")
            if not isinstance(options, list) or not 3 <= len(options) <= 4:
                errors.append(f"items[{index}] requires 3–4 options (one key plus 2–3 distractors)")
            elif any(not isinstance(option, str) or not option.strip() for option in options):
                errors.append(f"items[{index}] options must be non-empty strings")
            correct = item.get("correct")
            if isinstance(correct, bool) or not isinstance(correct, int):
                errors.append(f"items[{index}] requires an integer correct index")
            elif isinstance(options, list) and not 0 <= correct < len(options):
                errors.append(f"items[{index}] correct index must select an option")
    return errors


def _validate_cloze(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "cloze"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    if not isinstance(raw.get("evidence"), str) or not raw["evidence"].strip():
        errors.append("candidate requires a non-empty activity evidence quote")
    if not isinstance(raw.get("text"), str) or not raw["text"].strip():
        errors.append("cloze requires non-empty text")
    blanks = raw.get("blanks")
    if not isinstance(blanks, list) or not blanks:
        errors.append("cloze requires a non-empty blanks list")
    else:
        for index, blank in enumerate(blanks):
            if not isinstance(blank, dict):
                errors.append(f"blanks[{index}] must be an object")
                continue
            if not isinstance(blank.get("id"), int):
                errors.append(f"blanks[{index}] requires integer id")
            if not isinstance(blank.get("answer"), str) or not blank["answer"].strip():
                errors.append(f"blanks[{index}] requires a non-empty answer")
            if not isinstance(blank.get("options"), list) or not blank["options"]:
                errors.append(f"blanks[{index}] requires non-empty options")
    return errors


def _validate_match_up(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "match-up"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    pairs = raw.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        errors.append("candidate requires a non-empty pairs list")
    else:
        for index, pair in enumerate(pairs):
            if not isinstance(pair, dict):
                errors.append(f"pairs[{index}] must be an object")
                continue
            if not isinstance(pair.get("evidence"), str) or not pair["evidence"].strip():
                errors.append(f"pairs[{index}] requires a non-empty evidence quote")
            for side in ("left", "right"):
                if not isinstance(pair.get(side), str) or not pair[side].strip():
                    errors.append(f"pairs[{index}] requires a non-empty {side}")
    return errors


def _validate_mark_the_words(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "mark-the-words"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    if not isinstance(raw.get("evidence"), str) or not raw["evidence"].strip():
        errors.append("mark-the-words requires a non-empty activity evidence quote")
    if not isinstance(raw.get("text"), str) or not raw["text"].strip():
        errors.append("mark-the-words requires non-empty text")
    if not isinstance(raw.get("criteria"), str) or not raw["criteria"].strip():
        errors.append("mark-the-words requires a non-empty VESUM criterion")
    target_words = raw.get("target_words")
    if not isinstance(target_words, list) or not target_words:
        errors.append("mark-the-words requires a non-empty target_words list")
    elif any(not isinstance(word, str) or not word.strip() for word in target_words):
        errors.append("mark-the-words target_words must be non-empty strings")
    return errors


def _validate_error_correction(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "error-correction"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        errors.append("error-correction requires a non-empty items list")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"items[{index}] must be an object")
                continue
            for field in ("sentence", "error", "correction", "explanation", "evidence"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(f"items[{index}] requires a non-empty {field}")
            options = item.get("options")
            if not isinstance(options, list) or not 3 <= len(options) <= 4:
                errors.append(f"items[{index}] requires 3–4 correction options")
            elif any(not isinstance(option, str) or not option.strip() for option in options):
                errors.append(f"items[{index}] options must be non-empty strings")
    return errors


def _validate_fill_in(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "fill-in"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        errors.append("fill-in requires a non-empty items list")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"items[{index}] must be an object")
                continue
            for field in ("sentence", "answer", "evidence"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(f"items[{index}] requires a non-empty {field}")
            options = item.get("options")
            if not isinstance(options, list) or not 3 <= len(options) <= 4:
                errors.append(f"items[{index}] requires 3–4 options")
            elif any(not isinstance(option, str) or not option.strip() for option in options):
                errors.append(f"items[{index}] options must be non-empty strings")
    return errors


def _validate_text_questions(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "text-questions"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    if not isinstance(raw.get("source_ref"), str) or not raw["source_ref"].strip():
        errors.append("text-questions requires a non-empty source_ref")
    items = raw.get("items")
    if not isinstance(items, list) or not 2 <= len(items) <= 3:
        errors.append("text-questions requires 2–3 items")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"items[{index}] must be an object")
                continue
            for field in ("question", "evidence"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(f"items[{index}] requires a non-empty {field}")
            if "model_answer" in item and (
                not isinstance(item["model_answer"], str) or not item["model_answer"].strip()
            ):
                errors.append(
                    f"items[{index}] model_answer must be a non-empty string when present"
                )
    if "teacher_guidance" in raw and (
        not isinstance(raw["teacher_guidance"], str) or not raw["teacher_guidance"].strip()
    ):
        errors.append("text-questions teacher_guidance must be a non-empty string when present")
    if {"learner_answer", "learner_response"} & set(raw):
        errors.append("text-questions must not carry a learner response")
    return errors


def _validate_short_writing(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "short-writing"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    for field in ("prompt", "source_ref", "word_count_guidance", "evidence"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            errors.append(f"short-writing requires a non-empty {field}")
    for field in ("model_answer", "rubric_hint", "teacher_guidance"):
        if field in raw and (not isinstance(raw[field], str) or not raw[field].strip()):
            errors.append(f"short-writing {field} must be a non-empty string when present")
    if {"learner_answer", "learner_response"} & set(raw):
        errors.append("short-writing must not carry a learner response")
    return errors


def _item_locators(raw: dict[str, Any]) -> tuple[str, ...]:
    return tuple(f"items[{index}]" for index, _item in enumerate(raw.get("items", [])))


def _pair_locators(raw: dict[str, Any]) -> tuple[str, ...]:
    return tuple(f"pairs[{index}]" for index, _pair in enumerate(raw.get("pairs", [])))


def _text_locator(_raw: dict[str, Any]) -> tuple[str, ...]:
    return ("text",)


def _signature_value(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "").casefold()).strip()


def _true_false_evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    return [
        (
            _signature_value(evidence_by_locator.get(f"items[{index}]")),
            _signature_value(item.get("correct")),
        )
        for index, item in enumerate(ir.activity.get("items", []))
    ]


def _quiz_evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    pairs = []
    for index, item in enumerate(ir.activity.get("items", [])):
        options = item.get("options", [])
        correct = item.get("correct")
        answer = options[correct] if isinstance(correct, int) and correct < len(options) else ""
        pairs.append(
            (_signature_value(evidence_by_locator.get(f"items[{index}]")), _signature_value(answer))
        )
    return pairs


def _cloze_evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    answer = "|".join(
        _signature_value(blank.get("answer")) for blank in ir.activity.get("blanks", [])
    )
    return [(_signature_value(evidence_by_locator.get("text")), answer)]


def _match_up_evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    return [
        (
            _signature_value(evidence_by_locator.get(f"pairs[{index}]")),
            _signature_value(pair.get("right")),
        )
        for index, pair in enumerate(ir.activity.get("pairs", []))
    ]


def _mark_the_words_evidence_answer_pairs(
    ir: schema.HramatkaActivity,
) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    marked = sorted(_signature_value(word) for word in ir.activity.get("target_words", []))
    return [(_signature_value(evidence_by_locator.get("text")), "|".join(marked))]


def _error_correction_evidence_answer_pairs(
    ir: schema.HramatkaActivity,
) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    return [
        (
            _signature_value(evidence_by_locator.get(f"items[{index}]")),
            _signature_value(item.get("correction")),
        )
        for index, item in enumerate(ir.activity.get("items", []))
    ]


def _fill_in_evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    return [
        (
            _signature_value(evidence_by_locator.get(f"items[{index}]")),
            _signature_value(item.get("answer")),
        )
        for index, item in enumerate(ir.activity.get("items", []))
    ]


def _text_questions_evidence_answer_pairs(
    ir: schema.HramatkaActivity,
) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    return [
        (
            _signature_value(evidence_by_locator.get(f"items[{index}]")),
            _signature_value(item.get("question")),
        )
        for index, item in enumerate(ir.activity.get("items", []))
    ]


def _short_writing_evidence_answer_pairs(
    ir: schema.HramatkaActivity,
) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    return [
        (
            _signature_value(evidence_by_locator.get("text")),
            _signature_value(ir.activity.get("prompt")),
        )
    ]


def _numeral_phrases(text: str) -> list[str]:
    return [d["raw_span"] for d in retrieval.extract_numeral_inventory(text) if d["following_noun"]]


def _run_numeral_gate(text: str, gr: schema.GateResult, locator: str) -> None:
    for phrase in _numeral_phrases(text):
        result = numeral.check_numeral_government(phrase)
        status = result["status"]
        if status in ("fail", "warn"):
            gr.add(
                "numeral",
                status,
                f"[{result['rule']}] {result['detail']}"
                + (f" expected={result['expected']}" if result.get("expected") else ""),
                locator=locator,
            )


def _single_ua_word(value: object) -> str | None:
    """Return one bare Ukrainian word, or None for a phrase/punctuated value."""
    if not isinstance(value, str):
        return None
    matches = list(_UA_WORD_RE.finditer(value.strip()))
    if len(matches) != 1 or matches[0].span() != (0, len(value.strip())):
        return None
    return matches[0].group(0)


def _replace_single_word(sentence: str, error: str, correction: str) -> str | None:
    """Replace exactly one word-form occurrence without matching substrings."""
    matches = [
        match
        for match in _UA_WORD_RE.finditer(sentence)
        if _signature_value(match.group(0)) == _signature_value(error)
    ]
    if len(matches) != 1:
        return None
    match = matches[0]
    return sentence[: match.start()] + correction + sentence[match.end() :]


def _add_verified_vesum_tokens(
    tokens: list[str],
    anchor_body: str,
    gr: schema.GateResult,
    locator: str,
    *,
    missing_gate: str,
    atlas_lookup: dict | None,
) -> None:
    """Require VESUM forms while retaining the standard heritage warning path."""
    if not tokens:
        gr.add(
            missing_gate,
            "fail",
            "No Ukrainian word form is available to verify.",
            locator=locator,
        )
        return
    verdicts = vesum_gate.check_tokens(tokens, anchor_body, atlas_lookup=atlas_lookup)
    worst = vesum_gate.worst_status(verdicts)
    if worst != "pass":
        bad = [verdict for verdict in verdicts if verdict["status"] == worst]
        gr.add(
            "vesum_token",
            worst,
            "; ".join(verdict["detail"] for verdict in bad),
            locator=locator,
        )
    for token in dict.fromkeys(tokens):
        if not vesum_tags.parse_word(token):
            gr.add(
                missing_gate,
                "fail",
                f"Token '{token}' has no VESUM form.",
                locator=locator,
            )


def _set_evidence_verdict(
    evidence_item: schema.Evidence | None,
    anchor_body: str,
    gr: schema.GateResult,
    locator: str,
) -> str:
    quote = evidence_item.quote if evidence_item else ""
    verdict = evidence_span.check_evidence(quote, anchor_body)
    gr.add("evidence_span", verdict["status"], verdict["detail"], locator=locator)
    if evidence_item is not None:
        evidence_item.char_start = verdict["char_start"]
        evidence_item.char_end = verdict["char_end"]
        evidence_item.kind = verdict["kind"]
    return quote


def _check_open_task_stem(
    stem: str,
    evidence_quote: str,
    anchor_body: str,
    gr: schema.GateResult,
    locator: str,
    *,
    atlas_lookup: dict | None,
) -> None:
    """Validate only teacher-authored task wording, never a learner response."""
    tokens = vesum_gate.content_tokens(stem)
    if not 2 <= len(tokens) <= _OPEN_STEM_MAX_TOKENS:
        gr.add(
            "open_task_b1",
            "fail",
            f"Task stem needs 2–{_OPEN_STEM_MAX_TOKENS} content tokens for the B1 task contract.",
            locator=locator,
        )
    _add_verified_vesum_tokens(
        tokens,
        anchor_body,
        gr,
        locator,
        missing_gate="open_task_vesum",
        atlas_lookup=atlas_lookup,
    )
    evidence_tokens = {
        _signature_value(token) for token in vesum_gate.content_tokens(evidence_quote)
    }
    if not evidence_tokens.intersection(_signature_value(token) for token in tokens):
        gr.add(
            "open_task_grounding",
            "fail",
            "Task stem must reuse at least one content word from its literal evidence.",
            locator=locator,
        )


def _numeral_failures(text: str) -> list[dict[str, str]]:
    return [
        result
        for phrase in _numeral_phrases(text)
        if (result := numeral.check_numeral_government(phrase))["status"] == "fail"
    ]


def _gate_true_false(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    ev_by_loc = {e.locator: e for e in evidence}
    for i, item in enumerate(activity.get("items", [])):
        loc = f"items[{i}]"
        statement = item.get("statement", "")
        ev = ev_by_loc.get(loc)
        verdict = evidence_span.check_evidence(ev.quote if ev else "", anchor_body)
        gr.add("evidence_span", verdict["status"], verdict["detail"], locator=loc)
        if ev is not None:
            ev.char_start = verdict["char_start"]
            ev.char_end = verdict["char_end"]
            ev.kind = verdict["kind"]
        token_verdicts = vesum_gate.check_tokens(
            vesum_gate.content_tokens(statement), anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator=loc)
        _run_numeral_gate(statement, gr, loc)
        if item.get("correct") is False:
            gr.add(
                "false_statement",
                "warn",
                "FALSE statement — deterministic falsity check out of scope; teacher-confirm.",
                locator=loc,
            )


def _gate_quiz(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    ev_by_loc = {e.locator: e for e in evidence}
    for i, item in enumerate(activity.get("items", [])):
        loc = f"items[{i}]"
        ev = ev_by_loc.get(loc)
        quote = ev.quote if ev else ""
        verdict = evidence_span.check_evidence(quote, anchor_body)
        gr.add("evidence_span", verdict["status"], verdict["detail"], locator=loc)
        if ev is not None:
            ev.char_start = verdict["char_start"]
            ev.char_end = verdict["char_end"]
            ev.kind = verdict["kind"]

        options = item.get("options", [])
        correct_index = item.get("correct")
        if not isinstance(correct_index, int) or not 0 <= correct_index < len(options):
            # The raw contract normally catches this, but retain a fail-closed
            # gate for direct callers and future raw-contract changes.
            gr.add("quiz_key", "fail", "Quiz has no valid correct-option index.", locator=loc)
            continue

        normalized_options = [_signature_value(option) for option in options]
        if len(set(normalized_options)) != len(normalized_options):
            gr.add(
                "quiz_ambiguous_key",
                "fail",
                "Quiz options repeat a form, so the single correct key is ambiguous.",
                locator=loc,
            )

        correct_verdict = evidence_span.check_option_in_quote(options[correct_index], quote)
        gr.add(
            "quiz_correct_evidence",
            correct_verdict["status"],
            correct_verdict["detail"],
            locator=loc,
        )
        supported_indices = [
            index
            for index, option in enumerate(options)
            if evidence_span.check_option_in_quote(option, quote)["status"] == "pass"
        ]
        if supported_indices != [correct_index]:
            gr.add(
                "quiz_ambiguous_key",
                "fail",
                "Exactly one option must be literally supported by this item's evidence, "
                "and it must be the marked key.",
                locator=loc,
            )

        option_tokens = []
        for option in options:
            tokens = vesum_gate.content_tokens(option)
            if not tokens:
                gr.add(
                    "quiz_option_vesum",
                    "fail",
                    f"Quiz option '{option}' contains no Ukrainian word form to verify.",
                    locator=loc,
                )
            option_tokens.extend(tokens)
        token_verdicts = vesum_gate.check_tokens(
            option_tokens, anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator=loc)
        for token in sorted(set(option_tokens), key=str.casefold):
            if not vesum_tags.parse_word(token):
                gr.add(
                    "quiz_option_vesum",
                    "fail",
                    f"Quiz option token '{token}' has no VESUM form.",
                    locator=loc,
                )
        _run_numeral_gate(item.get("question", ""), gr, loc)
        _run_numeral_gate(" ".join(options), gr, loc)


def _gate_error_correction(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    """Gate a single, source-restoring correction with deterministic proof.

    A non-VESUM error form is an intentional typo and may appear among the
    options, but is not itself VESUM-verified. A VESUM-attested error form is
    accepted only when the numeral-government gate proves the malformed source
    phrase wrong and the correction restores an evidence sentence that passes.
    This deliberately fails closed for agreement/word errors outside a rule the
    engine can actually decide.
    """
    evidence_by_locator = {item.locator: item for item in evidence}
    for index, item in enumerate(activity.get("items", [])):
        loc = f"items[{index}]"
        evidence_item = evidence_by_locator.get(loc)
        quote = _set_evidence_verdict(evidence_item, anchor_body, gr, loc)
        sentence = item.get("sentence", "")
        error = item.get("error", "")
        correction = item.get("correction", "")
        error_word = _single_ua_word(error)
        correction_word = _single_ua_word(correction)
        if error_word is None or correction_word is None:
            gr.add(
                "error_correction_form",
                "fail",
                "Error and correction must each be one Ukrainian word form.",
                locator=loc,
            )
            continue

        restored = _replace_single_word(sentence, error_word, correction_word)
        if restored is None:
            gr.add(
                "error_correction_single",
                "fail",
                "Sentence must contain the declared erroneous form exactly once.",
                locator=loc,
            )
        elif _signature_value(restored) != _signature_value(quote):
            gr.add(
                "error_correction_source",
                "fail",
                "Replacing the one error with the correction must restore the literal evidence.",
                locator=loc,
            )

        options = item.get("options", [])
        option_words = [_single_ua_word(option) for option in options]
        if any(option is None for option in option_words):
            gr.add(
                "error_correction_options",
                "fail",
                "Every correction option must be exactly one Ukrainian word form.",
                locator=loc,
            )
            continue
        words = [option for option in option_words if option is not None]
        normalized = [_signature_value(option) for option in words]
        if len(set(normalized)) != len(normalized):
            gr.add(
                "error_correction_options",
                "fail",
                "Correction options repeat a form, so the key is ambiguous.",
                locator=loc,
            )
        if normalized.count(_signature_value(correction_word)) != 1:
            gr.add(
                "error_correction_options",
                "fail",
                "The declared correction must occur exactly once in the options.",
                locator=loc,
            )
        error_is_non_vesum = not vesum_tags.parse_word(error_word)
        vesum_words = (
            [word for word in words if _signature_value(word) != _signature_value(error_word)]
            if error_is_non_vesum
            else words
        )
        _add_verified_vesum_tokens(
            vesum_words,
            anchor_body,
            gr,
            loc,
            missing_gate="error_correction_vesum",
            atlas_lookup=atlas_lookup,
        )

        matching_options = [
            option
            for option in words
            if (candidate := _replace_single_word(sentence, error_word, option)) is not None
            and _signature_value(candidate) == _signature_value(quote)
        ]
        if len(matching_options) != 1 or (
            matching_options
            and _signature_value(matching_options[0]) != _signature_value(correction_word)
        ):
            gr.add(
                "error_correction_ambiguous",
                "fail",
                "Exactly the declared correction must restore the source sentence.",
                locator=loc,
            )

        if error_is_non_vesum:
            continue
        if not _numeral_failures(sentence) or _numeral_failures(quote):
            gr.add(
                "error_correction_error",
                "fail",
                "A VESUM-valid error needs a failed numeral-government proof "
                "that the correction fixes.",
                locator=loc,
            )


def _gate_fill_in(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    evidence_by_locator = {item.locator: item for item in evidence}
    for index, item in enumerate(activity.get("items", [])):
        loc = f"items[{index}]"
        quote = _set_evidence_verdict(evidence_by_locator.get(loc), anchor_body, gr, loc)
        sentence = item.get("sentence", "")
        answer = item.get("answer", "")
        markers = list(_FILL_BLANK_RE.finditer(sentence))
        if len(markers) != 1:
            gr.add(
                "fill_in_blank",
                "fail",
                "Fill-in sentence must contain exactly one ____ or {answer} marker.",
                locator=loc,
            )
            continue
        marker = markers[0]
        restored = sentence[: marker.start()] + answer + sentence[marker.end() :]
        if _signature_value(restored) != _signature_value(quote):
            gr.add(
                "fill_in_source",
                "fail",
                "Replacing the blank with the key must restore the literal evidence.",
                locator=loc,
            )

        answer_word = _single_ua_word(answer)
        option_words = [_single_ua_word(option) for option in item.get("options", [])]
        if answer_word is None or any(option is None for option in option_words):
            gr.add(
                "fill_in_options",
                "fail",
                "The key and every option must be exactly one Ukrainian word form.",
                locator=loc,
            )
            continue
        words = [option for option in option_words if option is not None]
        normalized = [_signature_value(option) for option in words]
        if len(set(normalized)) != len(normalized):
            gr.add(
                "fill_in_options",
                "fail",
                "Fill-in options repeat a form, so the key is ambiguous.",
                locator=loc,
            )
        if normalized.count(_signature_value(answer_word)) != 1:
            gr.add(
                "fill_in_options",
                "fail",
                "The answer must occur exactly once in its option list.",
                locator=loc,
            )
        _add_verified_vesum_tokens(
            words,
            anchor_body,
            gr,
            loc,
            missing_gate="fill_in_vesum",
            atlas_lookup=atlas_lookup,
        )

        answer_pos = {parsed["pos"] for parsed in vesum_tags.parse_word(answer_word)}
        for option in words:
            if _signature_value(option) == _signature_value(answer_word):
                continue
            option_pos = {parsed["pos"] for parsed in vesum_tags.parse_word(option)}
            if not answer_pos.intersection(option_pos):
                gr.add(
                    "fill_in_pos",
                    "fail",
                    f"Distractor '{option}' does not share a VESUM part of speech with the key.",
                    locator=loc,
                )

        matching_options = [
            option
            for option in words
            if _signature_value(sentence[: marker.start()] + option + sentence[marker.end() :])
            == _signature_value(quote)
        ]
        if len(matching_options) != 1 or (
            matching_options
            and _signature_value(matching_options[0]) != _signature_value(answer_word)
        ):
            gr.add(
                "fill_in_ambiguous",
                "fail",
                "Exactly the marked answer must reconstruct the source sentence.",
                locator=loc,
            )


def _gate_text_questions(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    evidence_by_locator = {item.locator: item for item in evidence}
    for index, item in enumerate(activity.get("items", [])):
        loc = f"items[{index}]"
        quote = _set_evidence_verdict(evidence_by_locator.get(loc), anchor_body, gr, loc)
        _check_open_task_stem(
            item.get("question", ""),
            quote,
            anchor_body,
            gr,
            loc,
            atlas_lookup=atlas_lookup,
        )


def _gate_short_writing(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    loc = "text"
    evidence_item = next((item for item in evidence if item.locator == loc), None)
    quote = _set_evidence_verdict(evidence_item, anchor_body, gr, loc)
    _check_open_task_stem(
        activity.get("prompt", ""),
        quote,
        anchor_body,
        gr,
        loc,
        atlas_lookup=atlas_lookup,
    )


def _gate_cloze(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    text = activity.get("text", "")
    ev = next((e for e in evidence if e.locator == "text"), None)
    verdict = evidence_span.check_evidence(ev.quote if ev else "", anchor_body)
    gr.add("evidence_span", verdict["status"], verdict["detail"], locator="text")
    if ev is not None:
        ev.char_start = verdict["char_start"]
        ev.char_end = verdict["char_end"]
        ev.kind = verdict["kind"]
    has_gap = "{gap}" in text or "{{" in text
    if not has_gap:
        gr.add("cloze_gap", "fail", "Cloze text has no {gap}/{{N}} marker.", locator="text")
    for index, blank in enumerate(activity.get("blanks", []) or []):
        answer = blank.get("answer", "")
        options = blank.get("options", []) or []
        if answer and ev and answer not in ev.quote:
            gr.add(
                "cloze_answer",
                "fail",
                f"Cloze answer '{answer}' is not present in the source sentence.",
                locator=f"blanks[{index}]",
            )
        if answer and answer not in options:
            gr.add(
                "cloze_answer",
                "fail",
                f"Cloze answer '{answer}' is not among its own options.",
                locator=f"blanks[{index}]",
            )
        distractors = [o for o in options if o != answer]
        token_verdicts = vesum_gate.check_tokens(
            distractors, anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add(
                "vesum_token",
                worst,
                "; ".join(v["detail"] for v in bad),
                locator=f"blanks[{index}]",
            )


def _gate_mark_the_words(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    _atlas_lookup: dict | None = None,
) -> None:
    """Verify a noticing-task answer set against VESUM, including completeness.

    This is deliberately atomic: because the answer is a complete set, an
    omitted matching word invalidates the whole activity rather than admitting
    the rest as a partially useful task.
    """
    loc = "text"
    text = activity.get("text", "")
    ev = next((item for item in evidence if item.locator == loc), None)
    quote = ev.quote if ev else ""
    verdict = evidence_span.check_evidence(quote, anchor_body)
    gr.add("evidence_span", verdict["status"], verdict["detail"], locator=loc)
    if ev is not None:
        ev.char_start = verdict["char_start"]
        ev.char_end = verdict["char_end"]
        ev.kind = verdict["kind"]
    if evidence_span.check_evidence(text, anchor_body)["status"] != "pass":
        gr.add(
            "mark_words_source",
            "fail",
            "Mark-the-words text must itself be a literal span of the anchor.",
            locator=loc,
        )
    if _signature_value(quote) != _signature_value(text):
        gr.add(
            "mark_words_source",
            "fail",
            "Mark-the-words evidence must be the exact displayed text.",
            locator=loc,
        )

    criterion = vesum_tags.parse_criterion(activity.get("criteria"))
    if criterion is None:
        gr.add(
            "mark_words_criterion",
            "fail",
            "Criterion is not in the supported VESUM form "
            "(pos=noun|adj|verb with optional case=… or tense=…).",
            locator=loc,
        )
        return

    text_tokens = vesum_gate.content_tokens(text)
    parses_by_token = {
        _signature_value(token): vesum_tags.parse_word(token)
        for token in dict.fromkeys(text_tokens)
    }
    for token, parses in parses_by_token.items():
        if not parses:
            gr.add(
                "mark_words_vesum",
                "fail",
                f"Cannot verify anchor token '{token}' against VESUM, so completeness is unknown.",
                locator=loc,
            )

    expected = {
        token
        for token, parses in parses_by_token.items()
        if any(vesum_tags.matches_criterion(parsed, criterion) for parsed in parses)
    }
    if not expected:
        gr.add(
            "mark_words_criterion",
            "fail",
            "The VESUM criterion matches no words in the displayed text.",
            locator=loc,
        )

    marked: set[str] = set()
    for target in activity.get("target_words", []):
        target_tokens = vesum_gate.content_tokens(target)
        if len(target_tokens) != 1 or _signature_value(target_tokens[0]) != _signature_value(
            target
        ):
            gr.add(
                "mark_words_target",
                "fail",
                f"Marked target '{target}' must be exactly one word from the text.",
                locator=loc,
            )
            continue
        token = _signature_value(target_tokens[0])
        target_verdict = evidence_span.check_evidence(target, text)
        gr.add(
            "evidence_span",
            target_verdict["status"],
            target_verdict["detail"],
            locator=loc,
        )
        if target_verdict["status"] != "pass" or token not in parses_by_token:
            gr.add(
                "mark_words_target",
                "fail",
                f"Marked target '{target}' does not occur as a word in the displayed text.",
                locator=loc,
            )
            continue
        if not any(
            vesum_tags.matches_criterion(parsed, criterion) for parsed in parses_by_token[token]
        ):
            gr.add(
                "mark_words_tag",
                "fail",
                f"Marked target '{target}' does not match criterion '{activity.get('criteria')}'.",
                locator=loc,
            )
        if token in marked:
            gr.add(
                "mark_words_target",
                "fail",
                f"Marked target '{target}' is duplicated; target_words is a set.",
                locator=loc,
            )
        marked.add(token)

    missing = sorted(expected - marked)
    if missing:
        gr.add(
            "mark_words_completeness",
            "fail",
            "Unmarked VESUM-matching word(s): " + ", ".join(missing) + ".",
            locator=loc,
        )
    extra = sorted(marked - expected)
    if extra:
        gr.add(
            "mark_words_tag",
            "fail",
            "Marked word(s) do not satisfy the criterion: " + ", ".join(extra) + ".",
            locator=loc,
        )


def _gate_match_up(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    ev_by_loc = {e.locator: e for e in evidence}
    for i, pair in enumerate(activity.get("pairs", [])):
        loc = f"pairs[{i}]"
        left = pair.get("left", "")
        right = pair.get("right", "")
        ev = ev_by_loc.get(loc)
        quote = ev.quote if ev else left
        verdict = evidence_span.check_evidence(quote, anchor_body)
        gr.add("evidence_span", verdict["status"], verdict["detail"], locator=loc)
        if ev is not None:
            ev.char_start = verdict["char_start"]
            ev.char_end = verdict["char_end"]
            ev.kind = verdict["kind"]
        token_verdicts = vesum_gate.check_tokens(
            vesum_gate.content_tokens(right), anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator=loc)
        semantic_verdict = matchup_semantics.check_pair(left, right, atlas_lookup=atlas_lookup)
        if semantic_verdict["status"] != "pass":
            gr.add(
                "matchup_semantics",
                semantic_verdict["status"],
                semantic_verdict["detail"],
                locator=loc,
            )


@dataclass(frozen=True)
class ActivityRegistryEntry:
    """Everything an activity type owns in the candidate-bank pipeline."""

    activity_type: str
    prompt_builder: PromptBuilder
    prompt_version: str
    raw_schema_version: str
    raw_validator: RawValidator
    evidence_locator: EvidenceLocator
    gate: ActivityGate
    evidence_answer_pairs: EvidenceAnswerPairs
    assessment_mode: str
    gate_chain: str
    gate_version: str
    partition_key: str | None
    minimum_survivors: int
    ttt_phases: tuple[int, ...]
    item_budget: int
    is_puzzle: bool
    public_projector: Callable[[schema.HramatkaActivity], dict]

    def fingerprint(self) -> dict[str, Any]:
        return {
            "type": self.activity_type,
            "prompt_version": self.prompt_version,
            "raw_schema_version": self.raw_schema_version,
            "gate_chain": self.gate_chain,
            "gate_version": self.gate_version,
            "assessment_mode": self.assessment_mode,
            "partition_key": self.partition_key,
            "minimum_survivors": self.minimum_survivors,
            "ttt_phases": self.ttt_phases,
            "item_budget": self.item_budget,
            "is_puzzle": self.is_puzzle,
        }


_ACTIVITY_ENTRIES: dict[str, ActivityRegistryEntry] = {
    "true-false": ActivityRegistryEntry(
        activity_type="true-false",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="true-false.raw.v1",
        raw_validator=_validate_true_false,
        evidence_locator=_item_locators,
        gate=_gate_true_false,
        evidence_answer_pairs=_true_false_evidence_answer_pairs,
        assessment_mode="auto_gradable",
        gate_chain="true-false.extractive.v1",
        gate_version="true-false.gates.v1",
        partition_key="items",
        minimum_survivors=1,
        ttt_phases=(1, 2, 3),
        item_budget=5,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "quiz": ActivityRegistryEntry(
        activity_type="quiz",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="quiz.raw.v1",
        raw_validator=_validate_quiz,
        evidence_locator=_item_locators,
        gate=_gate_quiz,
        evidence_answer_pairs=_quiz_evidence_answer_pairs,
        assessment_mode="auto_gradable",
        gate_chain="quiz.extractive.v1",
        gate_version="quiz.gates.v1",
        partition_key="items",
        minimum_survivors=3,
        ttt_phases=(1, 2, 3),
        item_budget=4,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "error-correction": ActivityRegistryEntry(
        activity_type="error-correction",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="error-correction.raw.v1",
        raw_validator=_validate_error_correction,
        evidence_locator=_item_locators,
        gate=_gate_error_correction,
        evidence_answer_pairs=_error_correction_evidence_answer_pairs,
        assessment_mode="auto_gradable",
        gate_chain="error-correction.extractive.v1",
        gate_version="error-correction.gates.v1",
        partition_key="items",
        minimum_survivors=2,
        ttt_phases=(1, 2, 3),
        item_budget=4,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "fill-in": ActivityRegistryEntry(
        activity_type="fill-in",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="fill-in.raw.v1",
        raw_validator=_validate_fill_in,
        evidence_locator=_item_locators,
        gate=_gate_fill_in,
        evidence_answer_pairs=_fill_in_evidence_answer_pairs,
        assessment_mode="auto_gradable",
        gate_chain="fill-in.extractive.v1",
        gate_version="fill-in.gates.v1",
        partition_key="items",
        minimum_survivors=3,
        ttt_phases=(1, 2, 3),
        item_budget=4,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "cloze": ActivityRegistryEntry(
        activity_type="cloze",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="cloze.raw.v1",
        raw_validator=_validate_cloze,
        evidence_locator=_text_locator,
        gate=_gate_cloze,
        evidence_answer_pairs=_cloze_evidence_answer_pairs,
        assessment_mode="auto_gradable",
        gate_chain="cloze.extractive.v1",
        gate_version="cloze.gates.v1",
        partition_key="blanks",
        minimum_survivors=1,
        ttt_phases=(1, 2, 3),
        item_budget=3,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "match-up": ActivityRegistryEntry(
        activity_type="match-up",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="match-up.raw.v1",
        raw_validator=_validate_match_up,
        evidence_locator=_pair_locators,
        gate=_gate_match_up,
        evidence_answer_pairs=_match_up_evidence_answer_pairs,
        assessment_mode="auto_gradable",
        gate_chain="match-up.extractive.v1",
        gate_version="match-up.gates.v2",
        partition_key="pairs",
        minimum_survivors=2,
        ttt_phases=(1, 2),
        item_budget=4,
        is_puzzle=True,
        public_projector=schema.project_to_b1,
    ),
    "mark-the-words": ActivityRegistryEntry(
        activity_type="mark-the-words",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="mark-the-words.raw.v1",
        raw_validator=_validate_mark_the_words,
        evidence_locator=_text_locator,
        gate=_gate_mark_the_words,
        evidence_answer_pairs=_mark_the_words_evidence_answer_pairs,
        assessment_mode="auto_gradable",
        gate_chain="mark-the-words.extractive.v1",
        gate_version="mark-the-words.gates.v1",
        partition_key=None,
        minimum_survivors=1,
        ttt_phases=(1, 2, 3),
        item_budget=4,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "text-questions": ActivityRegistryEntry(
        activity_type="text-questions",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="text-questions.raw.v1",
        raw_validator=_validate_text_questions,
        evidence_locator=_item_locators,
        gate=_gate_text_questions,
        evidence_answer_pairs=_text_questions_evidence_answer_pairs,
        assessment_mode="teacher_assessed",
        gate_chain="text-questions.open.v1",
        gate_version="text-questions.gates.v1",
        partition_key="items",
        minimum_survivors=3,
        ttt_phases=(1, 2, 3),
        item_budget=3,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "short-writing": ActivityRegistryEntry(
        activity_type="short-writing",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_schema_version="short-writing.raw.v1",
        raw_validator=_validate_short_writing,
        evidence_locator=_text_locator,
        gate=_gate_short_writing,
        evidence_answer_pairs=_short_writing_evidence_answer_pairs,
        assessment_mode="teacher_assessed",
        gate_chain="short-writing.open.v1",
        gate_version="short-writing.gates.v1",
        partition_key=None,
        minimum_survivors=1,
        ttt_phases=(2, 3),
        item_budget=1,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
}

if set(_ACTIVITY_ENTRIES) != set(PILOT_ACTIVITY_TYPES):
    raise RuntimeError("Baker activity entries must match the frozen pilot registry")

ACTIVITY_REGISTRY: dict[str, ActivityRegistryEntry] = {
    activity_type: _ACTIVITY_ENTRIES[activity_type] for activity_type in PILOT_ACTIVITY_TYPES
}


def entries_for(types: list[str]) -> list[ActivityRegistryEntry]:
    unknown = sorted(set(types) - set(ACTIVITY_REGISTRY))
    if unknown:
        raise ValueError(f"Unsupported activity type(s): {', '.join(unknown)}")
    return [ACTIVITY_REGISTRY[activity_type] for activity_type in types]


def registry_fingerprint(types: list[str]) -> dict[str, Any]:
    entries = [entry.fingerprint() for entry in entries_for(types)]
    packed = json.dumps(entries, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "version": REGISTRY_VERSION,
        "entries": entries,
        "digest": hashlib.sha256(packed.encode("utf-8")).hexdigest(),
    }
