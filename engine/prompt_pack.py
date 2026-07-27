# ruff: noqa: E501
"""Feature-gated engineered Gemma prompt-pack support.

The pack is deliberately an *assembler* protocol.  It prepares a complete,
immutable local input before a phase call and validates the response envelope
before the ordinary raw contracts and gates see an activity.  Nothing in this
module asks the model to perform morphology, semantic lookup, or source
retrieval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from . import content_density, flags
from .gates import derived, matchup_semantics, schema_tokens, vesum_tags
from .prompts import (
    active_writer_prompt_version,
    grounding_mode_for_type,
    mode_split_authoring_active,
)

PROMPT_PACK_VERSION = "PromptPackInput.v3"
TEMPLATE_VERSION = "gemma-phase-pack.v3"
INPUT_TOKEN_CEILING = 16_000
DERIVED_ALLOWED_VOCABULARY_CAP = 400
_TOKEN_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’-]+", re.UNICODE)
_CLOZE_GAP_RE = re.compile(r"\{gap(?:\d+)?\}|\{\{\d+\}\}")
_FORBIDDEN_ACTIVITY_KEYS = {
    "slot_id",
    "source_sentence_ids",
    "requirements",
    "learner_answer",
    "learner_response",
}
_ERROR_CORRECTION_INSTRUCTION = (
    "У кожному реченні навмисно допущено одну помилку. Знайдіть її та оберіть правильну форму."
)


def active_template_version() -> str:
    """Fingerprint identity for the pack template path under E4 composition."""
    if mode_split_authoring_active():
        return (
            f"{TEMPLATE_VERSION}+"
            f"{active_writer_prompt_version(extractive_fallback='extractive-v5:unused')}"
        )
    return TEMPLATE_VERSION



class PromptPackError(ValueError):
    """A deterministic pack preflight or response-envelope failure."""


def enabled() -> bool:
    """Return whether the replacement protocol is explicitly enabled."""
    return os.environ.get("HRAMATKA_PROMPT_PACK") == "1"


def phase_candidate_quota(visible_slots: int) -> int:
    """Return ``max(2, visible_slots)`` capped at six candidates per phase."""
    if visible_slots < 1:
        raise ValueError("A phase must have at least one visible slot.")
    return max(2, min(6, visible_slots))


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _focus_tokens(focus: str | None) -> list[str]:
    if not isinstance(focus, str):
        return []
    return [token.casefold() for token in _TOKEN_RE.findall(focus) if len(token) > 2]


def _sentence_inventory(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    inventory = []
    for ordinal, sentence in enumerate(snapshot.get("sentences", []), start=1):
        if not isinstance(sentence, Mapping) or not isinstance(sentence.get("text"), str):
            continue
        inventory.append(
            {
                "id": f"S{ordinal:02d}",
                "ordinal": ordinal,
                "text": sentence["text"],
                "char_start": sentence.get("char_start"),
                "char_end": sentence.get("char_end"),
                "source_sentence_id": sentence.get("id"),
            }
        )
    if not inventory:
        raise PromptPackError("Anchor has no sentence inventory for prompt-pack generation.")
    return inventory


def _verified_forms(inventory: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Batch VESUM validation once, retaining compact form/POS evidence."""
    forms = sorted(
        {
            token
            for sentence in inventory
            for token in _TOKEN_RE.findall(str(sentence["text"]))
            if len(token) > 1
        },
        key=str.casefold,
    )
    # ``parse_word`` is the structured VESUM boundary and respects the active
    # digest-pinned bundle.  The unique set keeps this one bounded local pass.
    parsed: dict[str, list[dict[str, Any]]] = {form: vesum_tags.parse_word(form) for form in forms}
    return parsed


def _focus_support(
    focus: str | None,
    *,
    inventory: Sequence[Mapping[str, Any]],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
    numeral_inventory: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """State exactly why a requested focus is, or is not, source-supported."""
    if not isinstance(focus, str) or not focus.strip():
        return {"requested": None, "status": "not-requested", "sentence_ids": [], "terms": []}

    terms = _focus_tokens(focus)
    anchor_words = {
        token.casefold()
        for sentence in inventory
        for token in _TOKEN_RE.findall(str(sentence["text"]))
    }
    sentence_ids = [
        str(sentence["id"])
        for sentence in inventory
        if any(term in str(sentence["text"]).casefold() for term in terms)
    ]
    reasons: list[str] = []
    if sentence_ids:
        reasons.append("literal-focus-term")
    if (
        any(term.startswith("числів") or term.startswith("кількіс") for term in terms)
        and numeral_inventory
    ):
        reasons.append("verified-numeral-inventory")
        sentence_ids.extend(
            str(sentence["id"])
            for sentence in inventory
            if any(char.isdigit() for char in str(sentence["text"]))
        )
    pos_by_form = {
        form.casefold(): {str(parse.get("pos")) for parse in parses}
        for form, parses in parsed_forms.items()
    }
    if any(term.startswith("прикмет") for term in terms) and any(
        "adj" in pos for pos in pos_by_form.values()
    ):
        reasons.append("verified-adjective-forms")
    if any(term.startswith("дієслов") for term in terms) and any(
        "verb" in pos for pos in pos_by_form.values()
    ):
        reasons.append("verified-verb-forms")
    if any(term in anchor_words for term in terms):
        reasons.append("anchor-token")

    sentence_ids = list(dict.fromkeys(sentence_ids))
    if reasons:
        return {
            "requested": focus.strip(),
            "status": "supported",
            "support_reasons": reasons,
            "sentence_ids": sentence_ids,
            "terms": terms,
        }
    return {
        "requested": focus.strip(),
        "status": "unsupported",
        "sentence_ids": [],
        "terms": terms,
        "notice_uk": (
            f"Опора не містить достатньо перевіреного матеріалу для фокусу «{focus.strip()}». "
            "Вправи спираються лише на текст-опору; додайте приклади або змініть фокус."
        ),
    }


def _allowed_forms(
    evidence_rows: Sequence[Mapping[str, Any]],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[str]:
    forms = []
    for sentence in evidence_rows:
        for token in _TOKEN_RE.findall(str(sentence["text"])):
            if parsed_forms.get(token):
                forms.append(token)
    return list(dict.fromkeys(forms))[:48]


def _mark_kit(
    inventory: Sequence[Mapping[str, Any]],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    primary_id: str | None = None,
) -> dict[str, Any] | None:
    starts = list(range(max(0, len(inventory) - 1)))
    if primary_id is not None:
        preferred = next(
            (index for index, row in enumerate(inventory[:-1]) if str(row["id"]) == primary_id),
            None,
        )
        if preferred is not None:
            starts = [preferred, *(index for index in starts if index != preferred)]
    for start in starts:
        span = inventory[start : start + 2]
        text = " ".join(str(row["text"]) for row in span)
        for pos, criterion, instruction in (
            ("verb", "pos=verb", "Позначте всі дієслова у фрагменті."),
            ("noun", "pos=noun", "Позначте всі іменники у фрагменті."),
        ):
            targets = []
            for token in _TOKEN_RE.findall(text):
                if any(parse.get("pos") == pos for parse in parsed_forms.get(token, ())):
                    targets.append(token)
            targets = list(dict.fromkeys(targets))
            if len(targets) >= 4:
                return {
                    "span_sentence_ids": [row["id"] for row in span],
                    "text": text,
                    "criterion": criterion,
                    "instruction": instruction,
                    "expected_target_words": targets,
                }
    return None


def _match_pairs(
    inventory: Sequence[Mapping[str, Any]],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
    atlas_lookup: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Certify Atlas-pass pairs, keyed and presented on the LEMMA (#54 item 2).

    The board's left side is the citation form, never the sentence form the
    token happened to take: «привітною» in the anchor becomes «привітний» here.
    Keying on the lemma also collapses two inflections of one word (книжки /
    книжок) into a single pair instead of two boards' worth of the same lemma.
    """
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for sentence in inventory:
        for surface in _TOKEN_RE.findall(str(sentence["text"])):
            parses = parsed_forms.get(surface, ())
            lemmas = {str(parse.get("lemma", "")).casefold() for parse in parses}
            for lemma in lemmas:
                record = atlas_lookup.get(lemma)
                if not isinstance(record, Mapping):
                    continue
                for right in record.get("synonyms", []) or []:
                    if not isinstance(right, str) or not right.strip():
                        continue
                    verdict = matchup_semantics.check_pair(lemma, right, atlas_lookup=atlas_lookup)
                    key = (lemma, right.casefold())
                    if verdict.get("status") == "pass" and key not in seen:
                        pairs.append(
                            {
                                "evidence_id": sentence["id"],
                                "evidence": sentence["text"],
                                "left": lemma,
                                "right": right,
                                "semantic_status": "pass",
                                "anchor_form": surface,
                                "atlas_lemma_pair": [lemma, right],
                            }
                        )
                        seen.add(key)
                    if len(pairs) >= 6:
                        return pairs
    return pairs


def _source_forms(
    inventory: Sequence[Mapping[str, Any]],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[str]:
    """Return distinct VESUM-attested surface forms in anchor order."""
    forms = []
    seen: set[str] = set()
    for row in inventory:
        for token in _TOKEN_RE.findall(str(row["text"])):
            key = token.casefold()
            if key not in seen and parsed_forms.get(token):
                forms.append(token)
                seen.add(key)
    return forms


def _row_tokens(
    row: Mapping[str, Any], parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]]
) -> list[str]:
    """Return one spelling of each verified form in a literal source row."""
    tokens = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(str(row["text"])):
        key = token.casefold()
        if key not in seen and parsed_forms.get(token):
            tokens.append(token)
            seen.add(key)
    return tokens


def _replace_first_token(text: str, token: str, replacement: str) -> str | None:
    for match in _TOKEN_RE.finditer(text):
        if match.group(0).casefold() == token.casefold():
            return text[: match.start()] + replacement + text[match.end() :]
    return None


def _contiguous_evidence_rows(
    inventory: Sequence[Mapping[str, Any]], slot_index: int
) -> list[Mapping[str, Any]]:
    """Choose up to four source rows without wrapping a literal span."""
    count = min(4, len(inventory))
    start = min(slot_index % len(inventory), len(inventory) - count)
    return list(inventory[start : start + count])


def _evidence_rows_for_primary(
    inventory: Sequence[Mapping[str, Any]],
    *,
    activity_type: str,
    primary_id: str,
) -> list[Mapping[str, Any]]:
    """Build a literal evidence plan whose first row is the assigned primary.

    Itemized families may cite distinct literal rows, while cloze keeps its
    two-sentence source span contiguous.  This makes the source allocation
    explicit instead of letting overlapping four-row windows accidentally
    assign the same selector primary to two same-phase slots.
    """
    primary_index = next(
        (index for index, row in enumerate(inventory) if str(row["id"]) == primary_id), None
    )
    if primary_index is None:
        raise PromptPackError("Prompt-pack primary is absent from sentence inventory.")
    if activity_type == "cloze":
        start = min(primary_index, len(inventory) - 2)
        return list(inventory[start : start + 2])
    if activity_type == "short-writing":
        return [inventory[primary_index]]
    required = content_density.registry_item_targets()[activity_type]
    if activity_type in {"error-correction", "fill-in"}:
        start = min(primary_index, len(inventory) - required)
        return list(inventory[start : start + required])
    ordered = [inventory[primary_index], *inventory[:primary_index], *inventory[primary_index + 1 :]]
    return ordered[:required]


def _allocate_phase_primaries(
    inventory: Sequence[Mapping[str, Any]],
    expanded_types: Sequence[str],
    *,
    phase: int,
    slot_index: int,
    prior_operations: Mapping[str, set[str]],
    prior_usage: Mapping[str, int],
) -> list[str]:
    """Assign distinct phase primaries without repeating an operation cross-phase.

    The invariant is deterministic: every phase slot gets a distinct primary
    when the inventory has enough rows; a sentence used in an earlier phase is
    not reused for the same cognitive operation where another row is available.
    Evidence remains literal and each family retains its own density contract.
    """
    ids = [str(row["id"]) for row in inventory]
    if not ids:
        raise PromptPackError("Prompt-pack primary allocation requires sentence inventory.")
    used: set[str] = set()
    allocated: list[str] = []
    for offset, activity_type in enumerate(expanded_types):
        operation = content_density.COGNITIVE_OPERATION.get(activity_type, activity_type)
        ordered = ids[(slot_index + offset) % len(ids) :] + ids[: (slot_index + offset) % len(ids)]
        if activity_type in {"cloze", "mark-the-words"} and len(ids) > 1:
            ordered = [sentence_id for sentence_id in ordered if sentence_id != ids[-1]]
        eligible = [
            sentence_id
            for sentence_id in ordered
            if sentence_id not in used
            and prior_usage.get(sentence_id, 0) < 2
            and operation not in prior_operations.get(sentence_id, set())
        ]
        if not eligible:
            eligible = [
                sentence_id
                for sentence_id in ordered
                if sentence_id not in used and prior_usage.get(sentence_id, 0) < 2
            ]
        if not eligible:
            eligible = ordered
        primary_id = eligible[0]
        used.add(primary_id)
        allocated.append(primary_id)
    return allocated


def _quiz_kit(
    evidence_rows: Sequence[Mapping[str, Any]],
    source_forms: Sequence[str],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
    slot_index: int,
) -> dict[str, Any] | None:
    """Precompute unambiguous, VESUM-attested option sets for every quiz item."""
    if len(evidence_rows) < content_density.registry_item_targets()["quiz"]:
        return None
    items = []
    for row_index, row in enumerate(evidence_rows):
        evidence = str(row["text"])
        row_forms = _row_tokens(row, parsed_forms)
        answer = row_forms[(slot_index + row_index) % len(row_forms)] if row_forms else None
        quoted = {token.casefold() for token in _TOKEN_RE.findall(evidence)}
        distractors = [
            form
            for form in source_forms
            if form.casefold() != answer.casefold() and form.casefold() not in quoted
        ] if answer else []
        if answer is None or len(distractors) < 2:
            return None
        items.append(
            {
                "evidence_id": row["id"],
                "evidence": evidence,
                "options": [answer, *distractors[:2]],
                "correct": 0,
                "question_exemplar": "Оберіть слово, яке є в реченні.",
            }
        )
    return {"items": items}


def _cloze_kit(
    evidence_rows: Sequence[Mapping[str, Any]],
    source_forms: Sequence[str],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """Build a multi-sentence cloze at the canonical generation target."""
    if len(evidence_rows) < 2:
        return None
    target = content_density.registry_item_targets()["cloze"]
    evidence = " ".join(str(row["text"]) for row in evidence_rows)
    candidates = []
    seen: set[str] = set()
    for row in evidence_rows:
        for token in _row_tokens(row, parsed_forms):
            if token.casefold() not in seen:
                candidates.append(token)
                seen.add(token.casefold())
            if len(candidates) == target:
                break
        if len(candidates) == target:
            break
    if len(candidates) < target:
        return None
    display = evidence
    blanks = []
    for index, answer in enumerate(candidates, start=1):
        marker = "{gap}" if index == 1 else f"{{gap{index}}}"
        display = _replace_first_token(display, answer, marker)
        if display is None:
            return None
        distractors = [form for form in source_forms if form.casefold() != answer.casefold()]
        if len(distractors) < 2:
            return None
        blanks.append(
            {"id": index, "answer": answer, "options": [answer, *distractors[:2]]}
        )
    return {
        "evidence": evidence,
        "display_text": display,
        "blanks": blanks,
        "sentence_ids": [row["id"] for row in evidence_rows],
    }


def _fill_in_kit(
    evidence_rows: Sequence[Mapping[str, Any]],
    source_forms: Sequence[str],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
    slot_index: int,
    *,
    derived: bool,
    reserved_pairs: set[tuple[str, str]],
) -> dict[str, Any] | None:
    """Precompute fill-in form plans and their mode-specific proof fields."""
    items = []
    for row_index, row in enumerate(evidence_rows):
        row_forms = _row_tokens(row, parsed_forms)
        answer = None
        distractors: list[str] = []
        display = None
        ordered_forms = row_forms[row_index + slot_index :] + row_forms[: row_index + slot_index]
        for candidate in ordered_forms:
            if (str(row["text"]).casefold(), candidate.casefold()) in reserved_pairs:
                continue
            answer_pos = {str(parse.get("pos")) for parse in parsed_forms.get(candidate, ())}
            candidate_distractors = [
                form
                for form in source_forms
                if form.casefold() != candidate.casefold()
                and answer_pos.intersection(
                    str(parse.get("pos")) for parse in parsed_forms.get(form, ())
                )
            ]
            candidate_display = _replace_first_token(str(row["text"]), candidate, "____")
            if candidate_display is not None and len(candidate_distractors) >= 2:
                answer = candidate
                distractors = candidate_distractors
                display = candidate_display
                break
        if answer is None or display is None:
            continue
        if derived:
            answer_lemmas = sorted(
                {
                    str(parse.get("lemma", "")).strip()
                    for parse in parsed_forms.get(answer, ())
                    if str(parse.get("lemma", "")).strip()
                }
            )
            if not answer_lemmas:
                continue
            item = {
                "evidence_id": row["id"],
                "evidence": row["text"],
                "answer": answer,
                "options": [answer, *distractors[:2]],
                "kit_anchors": {
                    "lemmas": answer_lemmas,
                    "witness_span": row["text"],
                },
            }
        else:
            # Keep the legacy injection payload (including mapping order) byte
            # stable while grounding-mode v1 is disabled.
            item = {
                "evidence_id": row["id"],
                "evidence": row["text"],
                "sentence": display,
                "answer": answer,
                "options": [answer, *distractors[:2]],
            }
        items.append(item)
    return {"items": items} if len(items) >= 3 else None


def _short_writing_kit(
    evidence_rows: Sequence[Mapping[str, Any]],
    source_forms: Sequence[str],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    derived: bool,
) -> dict[str, Any] | None:
    if not evidence_rows or not source_forms:
        return None
    evidence_forms = _row_tokens(evidence_rows[0], parsed_forms)
    if len(evidence_forms) < 3:
        return None
    topic = next((form for form in evidence_forms if "читан" in form.casefold()), evidence_forms[0])
    requirements = [form for form in evidence_forms if form.casefold() != topic.casefold()][:2]
    if len(requirements) < 2:
        return None
    fragments = [
        f"(1) Опишіть «{requirements[0]}».",
        f"(2) Поясніть «{requirements[1]}».",
    ]
    kit = {
        "evidence_id": evidence_rows[0]["id"],
        "evidence": evidence_rows[0]["text"],
        "required_prompt_fragments": fragments,
        "prompt_exemplar": f"{topic}. {fragments[0]} {fragments[1]}",
        "word_count_guidance": "30–40 слів",
    }
    if derived:
        topic_lemmas = sorted(
            {
                str(parse.get("lemma", "")).strip()
                for parse in parsed_forms.get(topic, ())
                if str(parse.get("lemma", "")).strip()
            }
        )
        if not topic_lemmas:
            return None
        kit["kit_anchors"] = {
            "lemmas": topic_lemmas,
            "witness_span": evidence_rows[0]["text"],
        }
        kit["constraints"] = [
            "Використайте обидві зазначені вимоги.",
            "Напишіть 30–40 слів.",
        ]
    return kit


def _density_contract(activity_type: str, *, grounding_mode: str) -> dict[str, Any]:
    """Return a slot view of the one TeacherReadyDensity.v2 authority."""
    target = content_density.registry_item_targets()[activity_type]
    contracts = {
        "true-false": {
            "collection": "items",
            "minimum": target,
            "evidence_sentence_minimum": target,
            "literal_item_plan": True,
        },
        "quiz": {
            "collection": "items",
            "minimum": target,
            "evidence_sentence_minimum": target,
            "literal_item_plan": True,
        },
        "cloze": {
            "collection": "blanks",
            "minimum": target,
            "evidence_sentence_minimum": 2,
            "multi_sentence_display": 2,
            "literal_display_evidence_plan": True,
        },
        "match-up": {
            "collection": "pairs",
            "minimum": target,
            "evidence_sentence_minimum": 1,
            "literal_pair_plan": True,
        },
        "mark-the-words": {
            "minimum_targets": target,
            "multi_sentence_display": 2,
            "exact_target_set": True,
        },
        "error-correction": {
            "collection": "items",
            "minimum": target,
            "evidence_sentence_minimum": target,
        },
        "fill-in": (
            {
                "collection": "items",
                "minimum": target,
                "kit_anchor_item_plan": True,
            }
            if grounding_mode == "derived"
            else {
                "collection": "items",
                "minimum": target,
                "evidence_sentence_minimum": target,
                "literal_item_plan": True,
            }
        ),
        "text-questions": {
            "collection": "items",
            "minimum": target,
            "evidence_sentence_minimum": target,
        },
        "short-writing": {
            "minimum_response_units": target,
            "prompt_requirements": 2,
            "prompt_requirement_strings": True,
            **(
                {"kit_anchor_plan": True, "constraints_required": True}
                if grounding_mode == "derived"
                else {}
            ),
        },
    }
    return {
        "version": content_density.TEACHER_READY_DENSITY_VERSION,
        "digest": content_density.teacher_ready_density_digest(),
        **contracts[activity_type],
    }


def _sentence_count(text: object) -> int:
    return sum(1 for part in re.findall(r"[^.!?…]+[.!?…]?", str(text or "")) if part.strip())


def _preflight_density_contract(kit: Mapping[str, Any]) -> list[str]:
    """Validate a slot's supplied material before it reaches the model."""
    if not kit.get("available"):
        return []
    contract = kit.get("density_contract")
    if not isinstance(contract, Mapping):
        return ["slot lacks a density_contract"]
    minimum = contract.get("minimum")
    evidence_minimum = contract.get("evidence_sentence_minimum")
    if isinstance(evidence_minimum, int) and len(kit.get("evidence_items", [])) < evidence_minimum:
        return ["slot lacks enough literal evidence items for its density contract"]
    activity_type = kit.get("type")
    if activity_type == "quiz" and (
        not isinstance(minimum, int) or len(kit.get("quiz", {}).get("items", [])) < minimum
    ):
        return ["quiz slot lacks enough certified item plans"]
    if activity_type == "cloze":
        cloze = kit.get("cloze", {})
        target = content_density.registry_item_targets()["cloze"]
        if (
            len(cloze.get("blanks", [])) < target
            or _sentence_count(cloze.get("display_text")) < 2
        ):
            return [f"cloze slot lacks a {target}-gap multi-sentence display plan"]
    if activity_type == "fill-in" and (
        not isinstance(minimum, int) or len(kit.get("fill_in", {}).get("items", [])) < minimum
    ):
        return ["fill-in slot lacks enough same-POS item plans"]
    if activity_type == "mark-the-words":
        mark = kit.get("mark", {})
        if (
            len(mark.get("expected_target_words", [])) < int(contract["minimum_targets"])
            or _sentence_count(mark.get("text")) < 2
        ):
            return ["mark-the-words slot lacks its exact two-sentence target set"]
    if activity_type == "short-writing":
        short_writing = kit.get("short_writing", {})
        if len(short_writing.get("required_prompt_fragments", [])) != 2:
            return ["short-writing slot lacks two output prompt requirements"]
        if contract.get("constraints_required") and (
            not isinstance(short_writing.get("constraints"), list)
            or not short_writing["constraints"]
            or any(
                not isinstance(constraint, str) or not constraint.strip()
                for constraint in short_writing["constraints"]
            )
        ):
            return ["derived short-writing slot lacks non-empty constraints"]
    return []


def _with_allowed_evidence(
    kit: Mapping[str, Any],
    evidence_rows: Sequence[Mapping[str, Any]],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Align citation eligibility with a type kit's literal evidence plan."""
    return {
        **kit,
        "allowed_sentence_ids": [row["id"] for row in evidence_rows],
        "evidence_items": [
            {"evidence_id": row["id"], "evidence": row["text"]} for row in evidence_rows
        ],
        "allowed_forms": _allowed_forms(evidence_rows, parsed_forms),
    }


def _type_kit(
    slot_id: str,
    activity_type: str,
    *,
    inventory: Sequence[Mapping[str, Any]],
    parsed_forms: Mapping[str, Sequence[Mapping[str, Any]]],
    numeral_inventory: Sequence[Mapping[str, Any]],
    atlas_lookup: Mapping[str, Any],
    slot_index: int,
    primary_id: str,
    reserved_pairs: set[tuple[str, str]],
) -> dict[str, Any]:
    # Productive phase-three slots arrive after the receptive slots.  Stagger
    # their source window so composition retains an independently reusable
    # evidence sentence instead of repeatedly clamping to the final span.
    evidence_rows = _evidence_rows_for_primary(
        inventory, activity_type=activity_type, primary_id=primary_id
    )
    forms = _allowed_forms(evidence_rows, parsed_forms)
    source_forms = _source_forms(inventory, parsed_forms)
    grounding_mode = grounding_mode_for_type(activity_type)
    kit: dict[str, Any] = {
        "slot_id": slot_id,
        "type": activity_type,
        "kit_version": "PromptPackKit.v2",
        "allowed_sentence_ids": [row["id"] for row in evidence_rows],
        "evidence_items": [
            {"evidence_id": row["id"], "evidence": row["text"]} for row in evidence_rows
        ],
        "allowed_forms": forms,
        "density_contract": _density_contract(activity_type, grounding_mode=grounding_mode),
        "forbidden": [
            {"pattern": "invented-source", "reason": "evidence must be literal inventory text"},
            {"key": "learner_response", "reason": "not in the public raw activity contract"},
        ],
    }
    if activity_type == "mark-the-words":
        mark = _mark_kit(inventory, parsed_forms, primary_id=primary_id)
        if mark is None:
            return {**kit, "available": False, "unsupported_reason": "no complete VESUM target set"}
        mark_rows = [row for row in inventory if row["id"] in mark["span_sentence_ids"]]
        return {
            **_with_allowed_evidence(kit, mark_rows, parsed_forms),
            "available": True,
            "mark": mark,
            "citation_plan": {"text": mark["span_sentence_ids"]},
        }
    if activity_type == "match-up":
        pairs = _match_pairs(inventory, parsed_forms, atlas_lookup)
        if len(pairs) < 4:
            return {
                **kit,
                "available": False,
                "unsupported_reason": "fewer than four Atlas-pass pairs",
            }
        certified_pairs = pairs[:4]
        pair_ids = list(dict.fromkeys(pair["evidence_id"] for pair in certified_pairs))
        pair_rows = [row for row in inventory if row["id"] in pair_ids]
        return {
            **_with_allowed_evidence(kit, pair_rows, parsed_forms),
            "available": True,
            "pairs": certified_pairs,
            "citation_plan": {
                f"pairs[{index}]": [pair["evidence_id"]]
                for index, pair in enumerate(certified_pairs)
            },
        }
    if activity_type == "error-correction":
        # The current engine has a deterministic numeral gate but no inverse
        # inflector.  Refuse to invent an error mutation locally.
        if len(numeral_inventory) < 2:
            return {
                **kit,
                "available": False,
                "unsupported_reason": "fewer than two numeral proofs",
            }
        return {
            **kit,
            "available": False,
            "unsupported_reason": "no proved inverse numeral mutation builder",
            "numeral_proofs": list(numeral_inventory)[:4],
        }
    if not evidence_rows or not forms:
        return {
            **kit,
            "available": False,
            "unsupported_reason": "no VESUM-attested anchor forms",
        }
    if activity_type == "quiz":
        quiz = _quiz_kit(evidence_rows, source_forms, parsed_forms, slot_index)
        if quiz is None:
            return {**kit, "available": False, "unsupported_reason": "no certified quiz option plan"}
        return {
            **kit,
            "available": True,
            "quiz": quiz,
            "citation_plan": {
                f"items[{index}]": [item["evidence_id"]]
                for index, item in enumerate(quiz["items"])
            },
        }
    if activity_type == "cloze":
        cloze = _cloze_kit(evidence_rows, source_forms, parsed_forms)
        if cloze is None:
            target = content_density.registry_item_targets()["cloze"]
            return {
                **kit,
                "available": False,
                "unsupported_reason": f"no {target}-gap multi-sentence source plan",
            }
        return {
            **kit,
            "available": True,
            "cloze": cloze,
            "citation_plan": {"text": cloze["sentence_ids"]},
        }
    if activity_type == "fill-in":
        fill_in = _fill_in_kit(
            evidence_rows,
            source_forms,
            parsed_forms,
            slot_index,
            derived=grounding_mode == "derived",
            reserved_pairs=reserved_pairs,
        )
        if fill_in is None:
            return {**kit, "available": False, "unsupported_reason": "no same-POS fill-in option plan"}
        return {
            **kit,
            "available": True,
            "fill_in": fill_in,
            "citation_plan": {
                f"items[{index}]": [item["evidence_id"]]
                for index, item in enumerate(fill_in["items"])
            },
        }
    if activity_type == "short-writing":
        short_writing = _short_writing_kit(
            evidence_rows,
            source_forms,
            parsed_forms,
            derived=grounding_mode == "derived",
        )
        if short_writing is None:
            return {**kit, "available": False, "unsupported_reason": "no grounded writing requirement plan"}
        return {
            **kit,
            "available": True,
            "short_writing": short_writing,
            "citation_plan": {"text": [short_writing["evidence_id"]]},
        }
    if activity_type in {"true-false", "text-questions"}:
        target = content_density.registry_item_targets()[activity_type]
        if len(evidence_rows) < target:
            return {
                **kit,
                "available": False,
                "unsupported_reason": "not enough distinct literal evidence rows",
            }
        return {
            **kit,
            "available": True,
            "citation_plan": {
                f"items[{index}]": [evidence_rows[index]["id"]]
                for index in range(target)
            },
        }
    return {**kit, "available": False, "unsupported_reason": "unsupported registered activity type"}


def _shared_form_policy() -> dict[str, Any]:
    return {
        "language": "uk",
        "address_form": "ви",
        "allowed_instruction_patterns": [
            "Визначте",
            "Заповніть",
            "З'єднайте",
            "Позначте",
            "Напишіть",
        ],
        "allowed_question_stems": ["Що", "Хто", "Коли", "Чому", "Як"],
        "error_correction_instruction": _ERROR_CORRECTION_INSTRUCTION,
        "forbidden_global": sorted(_FORBIDDEN_ACTIVITY_KEYS),
        # #54 item 1: both bake-off engines narrated the schema into the
        # learner-facing instruction («правдивими (True), чи хибними (False)»).
        "forbidden_teacher_visible_vocabulary": sorted(schema_tokens.SCHEMA_TOKENS),
        "true_false_wording": "правильно/неправильно (П/Н), ніколи true/false і не «правдивий»",
    }


def build_shared_input(
    *,
    snapshot: Mapping[str, Any],
    grounding: Mapping[str, Any],
    duration_minutes: int,
    focus: str | None,
    phase_count_plans: Mapping[int, Mapping[str, int]],
    visible_slots_by_phase: Mapping[int, int],
) -> dict[str, Any]:
    """Build all deterministic, immutable injection material once per lesson."""
    inventory = _sentence_inventory(snapshot)
    parsed_forms = _verified_forms(inventory)
    numeral_inventory = list(grounding.get("numeral_inventory", []))
    atlas_lookup = grounding.get("atlas_lookup", {})
    slot_rows: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    slot_index = 0
    prior_operations: dict[str, set[str]] = {}
    prior_usage: Counter[str] = Counter()
    reserved_pairs: set[tuple[str, str]] = set()
    for phase in sorted(phase_count_plans):
        count_plan = phase_count_plans[phase]
        expanded = [
            activity_type for activity_type, count in count_plan.items() for _ in range(count)
        ]
        phase_primaries = _allocate_phase_primaries(
            inventory,
            expanded,
            phase=phase,
            slot_index=slot_index,
            prior_operations=prior_operations,
            prior_usage=prior_usage,
        )
        slots = []
        for position, (activity_type, primary_id) in enumerate(
            zip(expanded, phase_primaries, strict=True), start=1
        ):
            slot_id = f"P{phase}-A{position}"
            kit = _type_kit(
                slot_id,
                activity_type,
                inventory=inventory,
                parsed_forms=parsed_forms,
                numeral_inventory=numeral_inventory,
                atlas_lookup=atlas_lookup if isinstance(atlas_lookup, Mapping) else {},
                slot_index=slot_index,
                primary_id=primary_id,
                reserved_pairs=reserved_pairs,
            )
            preflight_errors = _preflight_density_contract(kit)
            if preflight_errors:
                raise PromptPackError(
                    f"Prompt-pack density preflight rejected {slot_id}: "
                    + "; ".join(preflight_errors)
                )
            slot_index += 1
            if activity_type == "quiz":
                for item in kit.get("quiz", {}).get("items", []):
                    options = item.get("options", [])
                    correct = item.get("correct")
                    if isinstance(options, list) and isinstance(correct, int) and 0 <= correct < len(options):
                        reserved_pairs.add(
                            (str(item.get("evidence", "")).casefold(), str(options[correct]).casefold())
                        )
            elif activity_type == "fill-in":
                for item in kit.get("fill_in", {}).get("items", []):
                    reserved_pairs.add(
                        (str(item.get("evidence", "")).casefold(), str(item.get("answer", "")).casefold())
                    )
            operation = content_density.COGNITIVE_OPERATION.get(activity_type, activity_type)
            prior_operations.setdefault(primary_id, set()).add(operation)
            prior_usage[primary_id] += 1
            slot_rows.append({"slot_id": slot_id, "type": activity_type, "kit": kit})
            slots.append({"slot_id": slot_id, "type": activity_type})
        phases.append(
            {
                "phase": phase,
                "name": "Teach" if phase == 2 else "Test",
                "purpose": (
                    "Діагностувати початкове розуміння."
                    if phase == 1
                    else "Опрацювати форму в опорі."
                    if phase == 2
                    else "Перевірити перенесення в новому завданні."
                ),
                "visible_slots": int(visible_slots_by_phase[phase]),
                "requested_candidate_slots": len(slots),
                "all_slot_ids": [row["slot_id"] for row in slots],
                "types": [row["type"] for row in slots],
                "candidate_policy": "max(2, visible_slots), capped at 6",
            }
        )
    focus_support = _focus_support(
        focus,
        inventory=inventory,
        parsed_forms=parsed_forms,
        numeral_inventory=numeral_inventory,
    )
    base = {
        "pack_version": PROMPT_PACK_VERSION,
        "teacher_ready_density": {
            **content_density.teacher_ready_density_record(),
            "digest": content_density.teacher_ready_density_digest(),
            "duration": duration_minutes,
        },
        "lesson_plan": {
            "lesson_id": str(snapshot["anchor_id"]),
            "level": "B1",
            "duration_minutes": duration_minutes,
            "method": "TTT",
            "focus": focus_support,
            "phases": phases,
        },
        "anchor_sentence_inventory": inventory,
        "shared_form_policy": _shared_form_policy(),
        "grounding_digest": _sha(
            {"text": grounding.get("text", ""), "numerals": numeral_inventory}
        ),
        "slots": slot_rows,
        "provenance": {
            "anchor_sha256": snapshot.get("hash"),
            "template_version": active_template_version(),
            "registry_version": "pack-delegates-to-runtime-registry",
        },
    }
    # Slice 2 adds the closed, pre-verified kit to the generated phase context
    # only when its own flag is on.  The legacy prompt-pack payload is otherwise
    # byte-identical.
    if flags.kit_enrichment_v1_enabled():
        base["kit_enrichment"] = grounding.get("kit")
    # A derived type needs the effective mode map even in the grounding-only
    # composition.  ``writer_prompt_v2`` still controls the template identity;
    # it must not hide the runtime contract selected by grounding_mode_v1.
    has_derived_types = any(
        grounding_mode_for_type(str(row["type"])) == "derived" for row in slot_rows
    )
    if mode_split_authoring_active() or has_derived_types:
        base["authoring"] = {
            "mode": "mode-split-v2" if mode_split_authoring_active() else "grounding-mode-v1",
            "grounding_modes": {
                str(row["type"]): grounding_mode_for_type(str(row["type"]))
                for row in slot_rows
            },
        }
        if mode_split_authoring_active():
            base["authoring"]["writer_prompt_version"] = active_writer_prompt_version(
                extractive_fallback="extractive-v5:unused"
            )
    base["provenance"]["injection_sha256"] = _sha(base)
    return base


def _phase_slots(shared: Mapping[str, Any], phase: int) -> list[dict[str, Any]]:
    prefix = f"P{phase}-"
    return [row for row in shared["slots"] if str(row["slot_id"]).startswith(prefix)]


def phase_context(
    shared: Mapping[str, Any],
    *,
    phase: int,
    requested_types: Sequence[str] | None = None,
    requested_slot_ids: Sequence[str] | None = None,
    repair_failures: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Render the narrow current-phase view without mutating shared input."""
    slots = _phase_slots(shared, phase)
    if requested_slot_ids is not None:
        wanted = list(requested_slot_ids)
        by_id = {str(slot["slot_id"]): slot for slot in slots}
        missing = [slot_id for slot_id in wanted if slot_id not in by_id]
        if missing:
            raise PromptPackError(f"Repair requested unavailable slot ids: {missing!r}")
        slots = [by_id[slot_id] for slot_id in wanted]
    elif requested_types is not None:
        remaining = list(requested_types)
        selected = []
        for slot in slots:
            activity_type = str(slot["type"])
            if activity_type in remaining:
                selected.append(slot)
                remaining.remove(activity_type)
        if remaining:
            raise PromptPackError(f"Repair requested unavailable slot types: {sorted(remaining)!r}")
        slots = selected
    unavailable = [slot for slot in slots if not slot["kit"].get("available")]
    if unavailable:
        detail = ", ".join(
            f"{slot['slot_id']}:{slot['kit'].get('unsupported_reason', 'unsupported')}"
            for slot in unavailable
        )
        raise PromptPackError(f"Prompt-pack preflight rejected unsupported slot material: {detail}")
    type_kits = [slot["kit"] for slot in slots]
    phase_plan = next(row for row in shared["lesson_plan"]["phases"] if row["phase"] == phase)
    mode = "repair" if repair_failures else "initial"
    request_slots = [
        {
            "slot_id": slot["slot_id"],
            "type": slot["type"],
            "required_item_count": _required_item_count(str(slot["type"])),
            "allowed_sentence_ids": slot["kit"]["allowed_sentence_ids"],
        }
        for slot in slots
    ]
    context = {
        "shared": shared,
        "phase": phase,
        "lesson_plan": shared["lesson_plan"],
        "teacher_ready_density": shared["teacher_ready_density"],
        "phase_request": {
            "phase": phase,
            "phase_name": phase_plan["name"],
            "mode": mode,
            "requested_slots": request_slots,
            "response_order": [slot["slot_id"] for slot in slots],
        },
        "type_kits": type_kits,
        "allowed_and_forbidden_forms": [
            {
                "slot_id": kit["slot_id"],
                "allowed": {
                    "forms": kit["allowed_forms"],
                    "evidence_ids": kit["allowed_sentence_ids"],
                },
                "forbidden": kit["forbidden"],
            }
            for kit in type_kits
        ],
        "repair_failures": [dict(item) for item in repair_failures],
        "provenance": dict(shared["provenance"]),
    }
    # The shared injection digest identifies inputs, but a qualification
    # receipt also needs the literal instruction template actually rendered
    # for this phase/repair request. The rendered prompt contains both.
    context["provenance"]["rendered_prompt_sha256"] = hashlib.sha256(
        render_phase_prompt(context).encode("utf-8")
    ).hexdigest()
    return context


def _required_item_count(activity_type: str) -> int:
    return content_density.registry_item_targets()[activity_type]


_GOLD_EXEMPLARS = r"""
ЗРАЗКИ JSON-ФОРМ (ілюстративні; не копіюйте їхніх слів у відповідь):
{"type":"true-false","instruction":"Визначте, чи правильні твердження за текстом.","items":[{"statement":"…","correct":true,"explanation":"…","evidence":"…"}]}
{"type":"quiz","instruction":"Оберіть правильну відповідь за текстом.","items":[{"question":"…","options":["…","…","…"],"correct":0,"evidence":"…"}]}
{"type":"cloze","instruction":"Заповніть пропуски словами з тексту.","text":"… {gap} …","blanks":[{"id":1,"answer":"…","options":["…","…","…"]}],"evidence":"…"}
{"type":"match-up","instruction":"З'єднайте слово з опори з його відповідником.","pairs":[{"left":"…","right":"…","evidence":"…"}]}
{"type":"mark-the-words","instruction":"Позначте всі дієслова у фрагменті.","text":"…","criteria":"pos=verb","target_words":["…"],"evidence":"…"}
{"type":"error-correction","instruction":"У кожному реченні навмисно допущено одну помилку. Знайдіть її та оберіть правильну форму.","items":[{"sentence":"…","error":"…","correction":"…","options":["…","…","…"],"explanation":"…","evidence":"…"}]}
{"type":"fill-in","instruction":"Заповніть пропуски правильними формами.","items":[{"sentence":"… ____ …","answer":"…","options":["…","…","…"],"explanation":"…","evidence":"…"}]}
{"type":"text-questions","instruction":"Обговоріть запитання за текстом.","source_ref":"Текст-опора","items":[{"question":"…","model_answer":"…","evidence":"…"}]}
{"type":"short-writing","instruction":"Напишіть короткий текст за опорою.","prompt":"… 1) … 2) …","source_ref":"Текст-опора","word_count_guidance":"35–50 слів","evidence":"…"}
""".strip()


_CERTIFIED_KIT_GUIDE = r"""
ВИКОРИСТАННЯ ПЕРЕВІРЕНОГО КОМПЛЕКТУ:
- У кожному slot density_contract уже пройшов перевірку ДО генерації; ця сама перевірка буде застосована ПІСЛЯ відповіді.
- quiz: для кожного items[i] дослівно перенесіть evidence, options і correct із quiz.items[i]. Лише question можна сформулювати самостійно. Рівно один option має бути в його evidence.
- cloze: дослівно перенесіть cloze.display_text у text, cloze.evidence у evidence та весь cloze.blanks. Це один суцільний фрагмент із 2+ речень і трьома {gapN}; не скорочуйте речення.
- fill-in: для кожного items[i] дослівно перенесіть sentence, answer, options і evidence із fill_in.items[i]. Це вже VESUM-перевірені варіанти однієї частини мови.
- match-up: дослівно перенесіть усі Atlas-pass pairs; не додавайте п'яту пару і не перефразуйте right. left уже подано в словниковій формі — не змінюйте його на форму з речення.
- mark-the-words: дослівно перенесіть mark.text у text і evidence, mark.criterion у criteria та ПОВНИЙ список mark.expected_target_words у target_words. Це точний список для двох речень, без пропусків, перестановок і повторів.
- short-writing: вставте ОБИДВА short_writing.required_prompt_fragments дослівно в публічне поле prompt; не створюйте поле requirements. Дослівно перенесіть evidence і word_count_guidance з short_writing.
- citations: для кожної activity дослівно скопіюйте її citation_plan у citations[i].sentence_ids. Один locator text для cloze/mark може містити 2+ ID; не скорочуйте цей список до одного allowed ID.

НЕГАТИВНІ ПРИКЛАДИ (не робіть так):
- true-false: твердження без дослівного evidence → помилка evidence.
- quiz: два варіанти з evidence або повторений варіант → неоднозначний ключ.
- cloze: «… {gap} …» без одного з трьох пропусків, або evidence лише з першого речення → помилка cloze/evidence.
- fill-in: вигаданий або іншої частини мови дистрактор → VESUM/POS помилка.
- mark-the-words: менший список слів або одне речення → неповна множина цілей.
- short-writing: довгий перелік вимог поза prompt або поле requirements → B1/контрактна помилка.
- text-questions: рівно три запитання; у фінальній фазі послідовно: розуміння, пояснення/висновок, особисте або прикладне застосування з опорою на текст.
- error-correction: не вигадуйте зміну; якщо kit unavailable, цього типу в response_order немає.
""".strip()

_LEGACY_FILL_IN_GUIDE = (
    "- fill-in: для кожного items[i] дослівно перенесіть sentence, answer, options і evidence "
    "із fill_in.items[i]. Це вже VESUM-перевірені варіанти однієї частини мови."
)
_DERIVED_FILL_IN_GUIDE = (
    "- fill-in [derived]: складіть нове sentence з пропуском ____ лише з матеріалу KIT; "
    "використайте answer, options і kit_anchors із fill_in.items[i]; НЕ переносіть evidence. "
    "Стебло не може бути відновленням речення опори."
)
_LEGACY_SHORT_WRITING_GUIDE = (
    "- short-writing: вставте ОБИДВА short_writing.required_prompt_fragments дослівно в публічне "
    "поле prompt; не створюйте поле requirements. Дослівно перенесіть evidence і "
    "word_count_guidance з short_writing."
)
_DERIVED_SHORT_WRITING_GUIDE = (
    "- short-writing [derived]: вставте ОБИДВА short_writing.required_prompt_fragments дослівно "
    "в prompt та перенесіть word_count_guidance, constraints і kit_anchors; НЕ емітуйте evidence."
)


def _certified_kit_guide() -> str:
    """Render the response contract for the currently effective type modes."""
    if grounding_mode_for_type("fill-in") != "derived":
        return _CERTIFIED_KIT_GUIDE
    return (
        _CERTIFIED_KIT_GUIDE.replace(_LEGACY_FILL_IN_GUIDE, _DERIVED_FILL_IN_GUIDE)
        .replace(_LEGACY_SHORT_WRITING_GUIDE, _DERIVED_SHORT_WRITING_GUIDE)
    )


_LEGACY_FILL_IN_EXAMPLE = (
    '{"type":"fill-in","instruction":"Заповніть пропуски правильними формами.",'
    '"items":[{"sentence":"… ____ …","answer":"…","options":["…","…","…"],'
    '"explanation":"…","evidence":"…"}]}'
)
_DERIVED_FILL_IN_EXAMPLE = (
    '{"type":"fill-in","instruction":"Заповніть пропуски правильними формами.",'
    '"items":[{"sentence":"… ____ …","answer":"…","options":["…","…","…"],'
    '"explanation":"…","kit_anchors":{"lemmas":["…"],"witness_span":"…"}}]}'
)
_LEGACY_SHORT_WRITING_EXAMPLE = (
    '{"type":"short-writing","instruction":"Напишіть короткий текст за опорою.",'
    '"prompt":"… 1) … 2) …","source_ref":"Текст-опора","word_count_guidance":"35–50 слів",'
    '"evidence":"…"}'
)
_DERIVED_SHORT_WRITING_EXAMPLE = (
    '{"type":"short-writing","instruction":"Напишіть короткий текст за опорою.",'
    '"prompt":"… 1) … 2) …","source_ref":"Текст-опора","word_count_guidance":"35–50 слів",'
    '"constraints":["…"],"kit_anchors":{"lemmas":["…"],"witness_span":"…"}}'
)


def _gold_exemplars() -> str:
    """Keep legacy exemplars byte-stable unless derived contracts are active."""
    if grounding_mode_for_type("fill-in") != "derived":
        return _GOLD_EXEMPLARS
    return (
        _GOLD_EXEMPLARS.replace(_LEGACY_FILL_IN_EXAMPLE, _DERIVED_FILL_IN_EXAMPLE)
        .replace(_LEGACY_SHORT_WRITING_EXAMPLE, _DERIVED_SHORT_WRITING_EXAMPLE)
    )


_MODE_SPLIT_PACK_RULES = r"""
РЕЖИМИ ОБҐРУНТУВАННЯ (grounding_mode_v1; writer_prompt_v2, якщо увімкнено):
- У phase_request кожен тип має режим quoting або derived (див. authoring.grounding_modes).
- quoting: evidence лише з інвентаря речень; true-false неправильні = спотворення сенсу;
  cloze — цитатне походження лише для span пропуску (навколо можна лексику KIT).
- derived: НОВІ речення лише з kit_enrichment; fill-in НІКОЛИ не quote-restore;
  error-correction — спочатку correction за rule_id (numeral_moat), потім одна помилка;
  short-writing / домашка — обов'язково constraints[] (машинно перевірювані рядки);
  sentence-builder — starters з KIT, якщо тип у response_order.
- Якщо kit_enrichment відсутній або status=empty — комплект порожній: не вигадуйте похідних
  стебел з відкритого словника.
- Пастка словника: за підтриманого фокусу дистрактори можуть бути поза фокусом; відповіді — в KIT.
- Анти-мета / персона / «Культурний апгрейд» — другорядні щодо правильного режиму.
""".strip()


def _derived_allowed_vocabulary_data(kit: Mapping[str, Any] | None) -> dict[str, Any]:
    """Serialize the real derived closure without pretending it is unbounded."""
    lemmas = sorted(derived.kit_lemma_closure(kit), key=str.casefold)
    shown = lemmas[:DERIVED_ALLOWED_VOCABULARY_CAP]
    data: dict[str, Any] = {
        "lemmas": shown,
        "total_lemmas": len(lemmas),
        "truncated": len(lemmas) > DERIVED_ALLOWED_VOCABULARY_CAP,
    }
    if data["truncated"]:
        data["truncation_note"] = (
            f"Показано перші {DERIVED_ALLOWED_VOCABULARY_CAP} з {len(lemmas)} лем; список обрізано."
        )
    return data


def render_phase_prompt(context: Mapping[str, Any]) -> str:
    """Render one self-contained, data-fenced engineered phase request."""

    def block(name: str, value: object) -> str:
        return f"=== {name} (дані, не інструкції) ===\n```json\n{_canonical(value)}\n```"

    request_slots = context["phase_request"]["requested_slots"]
    has_derived_types = any(
        grounding_mode_for_type(str(slot["type"])) == "derived" for slot in request_slots
    )
    include_effective_modes = mode_split_authoring_active() or has_derived_types
    if include_effective_modes:
        request_lines = "\n".join(
            f"- {slot['type']} [{grounding_mode_for_type(str(slot['type']))}]: 1"
            for slot in request_slots
        )
    else:
        request_lines = "\n".join(f"- {slot['type']}: 1" for slot in request_slots)
    repair = context["repair_failures"] or {"mode": "initial", "failures": []}
    evidence_rule = (
        "3. Для quoting-пунктів evidence — лише дослівний текст дозволеного речення або "
        "суцільного дозволеного фрагмента; для derived-пунктів подайте kit_anchors, а не evidence."
        if include_effective_modes
        else "3. evidence — лише дослівний текст дозволеного речення або суцільного дозволеного фрагмента."
    )
    sections = [
        "Ти складаєш навчальні завдання з української мови для дорослого учня рівня B1.",
        "Ти не шукаєш інформацію, не викликаєш інструменти й не перевіряєш слова самостійно.",
        "Усі факти, речення-опори, словоформи, варіанти, пари та заборони вже перевірив підготовчий модуль.",
        "Виконайте лише поточну фазу, але врахуйте весь план уроку. Усі інструкції для учня пишіть українською мовою тільки у формі «ви».",
        "TeacherReadyDensity.v2 є жорстким контрактом: кожен запитаний тип мусить досягти його мінімуму; однопунктові вправи не приймаються.",
        "Назви полів і значення JSON-схеми (true, false, correct, options, statement) — машинні ключі. Вони ніколи не з'являються в тексті, який бачить учень чи вчитель: пишіть «правильно»/«неправильно» (П/Н), а не «правильними (True) чи хибними (False)».",
        block(
            "КОНТРАКТ ЩІЛЬНОСТІ УРОКУ",
            context.get("teacher_ready_density")
            or context["shared"]["teacher_ready_density"],
        ),
        block("ПОВНИЙ ПЛАН УРОКУ", context["lesson_plan"]),
        block("ПОТОЧНА ФАЗА ТА СЛОТИ ВІДПОВІДІ", context["phase_request"]),
        block("ПОВНИЙ НУМЕРОВАНИЙ ТЕКСТ-ОПОРА", context["shared"]["anchor_sentence_inventory"]),
        block("ДОЗВОЛЕНІ ТА ЗАБОРОНЕНІ ФОРМИ", context["allowed_and_forbidden_forms"]),
        block("ПЕРЕВІРЕНІ КОМПЛЕКТИ ДЛЯ ПОТОЧНИХ СЛОТІВ", context["type_kits"]),
        block("РЕЖИМ ПОТОЧНОГО ЗАПИТУ", repair),
    ]
    if include_effective_modes:
        authoring = context["shared"].get("authoring")
        if authoring is not None:
            sections.append(block("РЕЖИМИ ТА ВЕРСІЯ АВТОРИНГУ", authoring))
        kit = context["shared"].get("kit_enrichment")
        if kit is None:
            sections.append(
                "КОМПЛЕКТ KIT: порожній або відсутній — похідні (derived) типи не складайте "
                "з відкритого словника; статус empty fail-closed далі."
            )
        else:
            sections.append(block("KIT ENRICHMENT (похідний субстрат)", kit))
            sections.append(
                block(
                    "ДОЗВОЛЕНА ЛЕКСИКА (закритий список)",
                    _derived_allowed_vocabulary_data(kit),
                )
            )
        sections.append(_MODE_SPLIT_PACK_RULES)
    sections.extend(
        [
            "ПРАВИЛА СКЛАДАННЯ:\n"
            "1. Створіть рівно одну activity для кожного slot_id у response_order, у тому ж порядку.\n"
            "2. Виконайте density_contract і точні поля відповідного kit; сертифіковані значення не перефразуйте.\n"
            f"{evidence_rule}\n"
            "4. Не додавайте slot_id, source_sentence_ids, requirements, learner_answer, learner_response або інших полів.\n"
            "5. Дотримуйтеся контракту з блоку вище: не замінюйте його скороченим переліком або іншими числовими порогами.\n"
            "6. Відповідайте рівно одним JSON-об'єктом без Markdown чи пояснення.",
            "ВІДПОВІДЬ МАЄ МАТИ РІВНО ЦЮ ОБОЛОНКУ:\n"
            '{"activities":[...],"citations":[{"activity_index":0,"sentence_ids":{"items[0]":["S01"]}}]}\n'
            "Для cloze, mark-the-words і short-writing locator — text; для item/pair — items[i]/pairs[i].",
            _certified_kit_guide(),
            _gold_exemplars(),
            "ТЕСТОВИЙ ПЛАН КІЛЬКОСТІ (дані):\n" + request_lines,
        ]
    )
    prompt = "\n\n".join(sections)
    estimated_tokens = len(prompt.encode("utf-8")) // 4
    if estimated_tokens > INPUT_TOKEN_CEILING:
        raise PromptPackError(
            f"Prompt-pack preflight exceeds {INPUT_TOKEN_CEILING} token design ceiling ({estimated_tokens})."
        )
    return prompt


def _expected_locators(activity: Mapping[str, Any]) -> set[str]:
    activity_type = activity.get("type")
    if activity_type in {"true-false", "quiz", "error-correction", "fill-in", "text-questions"}:
        items = activity.get("items")
        if not isinstance(items, list):
            raise PromptPackError("citation envelope activity has no items list")
        return {f"items[{index}]" for index in range(len(items))}
    if activity_type == "match-up":
        pairs = activity.get("pairs")
        if not isinstance(pairs, list):
            raise PromptPackError("citation envelope match-up has no pairs list")
        return {f"pairs[{index}]" for index in range(len(pairs))}
    if activity_type in {"cloze", "mark-the-words", "short-writing"}:
        return {"text"}
    raise PromptPackError(f"citation envelope activity has unsupported type {activity_type!r}")


def _require_exact_fields(
    actual: Mapping[str, Any], expected: Mapping[str, Any], fields: Sequence[str], label: str
) -> None:
    for field in fields:
        if actual.get(field) != expected.get(field):
            raise PromptPackError(f"density_contract {label} must copy certified {field} exactly.")


def _validate_post_density_contract(activity: Mapping[str, Any], kit: Mapping[str, Any]) -> None:
    """Validate each output against the immutable, slot-scoped density contract."""
    contract = kit.get("density_contract")
    if not isinstance(contract, Mapping):
        raise PromptPackError("density_contract is absent from the requested slot.")
    collection = contract.get("collection")
    minimum = contract.get("minimum")
    if isinstance(collection, str) and isinstance(minimum, int):
        values = activity.get(collection)
        if not isinstance(values, list) or len(values) < minimum:
            raise PromptPackError(
                f"density_contract requires at least {minimum} {collection} for {kit['type']}."
            )

    activity_type = kit.get("type")
    if activity_type == "quiz":
        expected_items = kit["quiz"]["items"]
        actual_items = activity.get("items")
        if not isinstance(actual_items, list) or len(actual_items) != len(expected_items):
            raise PromptPackError("density_contract quiz requires the three certified item plans.")
        for actual, expected in zip(actual_items, expected_items, strict=True):
            if not isinstance(actual, Mapping):
                raise PromptPackError("density_contract quiz item must be an object.")
            _require_exact_fields(actual, expected, ("evidence", "options", "correct"), "quiz")
    elif activity_type == "cloze":
        cloze = kit["cloze"]
        _require_exact_fields(
            activity,
            {
                "text": cloze["display_text"],
                "evidence": cloze["evidence"],
                "blanks": cloze["blanks"],
            },
            ("text", "evidence", "blanks"),
            "cloze",
        )
        if _sentence_count(activity.get("text")) < int(contract["multi_sentence_display"]):
            raise PromptPackError("density_contract cloze display must contain the certified sentences.")
        if len(_CLOZE_GAP_RE.findall(str(activity.get("text", "")))) != len(cloze["blanks"]):
            raise PromptPackError("density_contract cloze display must retain every certified gap.")
    elif activity_type == "fill-in":
        expected_items = kit["fill_in"]["items"]
        actual_items = activity.get("items")
        if not isinstance(actual_items, list) or len(actual_items) != len(expected_items):
            raise PromptPackError("density_contract fill-in requires the three certified item plans.")
        for actual, expected in zip(actual_items, expected_items, strict=True):
            if not isinstance(actual, Mapping):
                raise PromptPackError("density_contract fill-in item must be an object.")
            if grounding_mode_for_type("fill-in") == "derived":
                if "evidence" in actual:
                    raise PromptPackError(
                        "density_contract derived fill-in must use kit_anchors, not evidence."
                    )
                _require_exact_fields(
                    actual,
                    expected,
                    ("answer", "options", "kit_anchors"),
                    "derived fill-in",
                )
                if not isinstance(actual.get("sentence"), str) or "____" not in actual["sentence"]:
                    raise PromptPackError(
                        "density_contract derived fill-in requires a new sentence with one blank."
                    )
            else:
                _require_exact_fields(
                    actual, expected, ("sentence", "answer", "options", "evidence"), "fill-in"
                )
    elif activity_type == "match-up":
        expected_pairs = [
            {field: pair[field] for field in ("left", "right", "evidence")}
            for pair in kit["pairs"]
        ]
        if activity.get("pairs") != expected_pairs:
            raise PromptPackError("density_contract match-up must copy its Atlas-pass pair plan exactly.")
    elif activity_type == "mark-the-words":
        mark = kit["mark"]
        _require_exact_fields(
            activity,
            {
                "text": mark["text"],
                "evidence": mark["text"],
                "criteria": mark["criterion"],
                "target_words": mark["expected_target_words"],
            },
            ("text", "evidence", "criteria", "target_words"),
            "mark-the-words",
        )
        if _sentence_count(activity.get("text")) < int(contract["multi_sentence_display"]):
            raise PromptPackError("density_contract mark-the-words needs its two-sentence display.")
    elif activity_type == "short-writing":
        short_writing = kit["short_writing"]
        if grounding_mode_for_type("short-writing") == "derived":
            if "evidence" in activity:
                raise PromptPackError(
                    "density_contract derived short-writing must use kit_anchors, not evidence."
                )
            _require_exact_fields(
                activity,
                short_writing,
                ("word_count_guidance", "constraints", "kit_anchors"),
                "derived short-writing",
            )
        elif activity.get("evidence") != short_writing["evidence"]:
            raise PromptPackError("density_contract short-writing must retain its literal evidence.")
        if activity.get("word_count_guidance") != short_writing["word_count_guidance"]:
            raise PromptPackError("density_contract short-writing must retain its word guidance.")
        prompt = activity.get("prompt")
        required = short_writing["required_prompt_fragments"]
        if not isinstance(prompt, str) or not all(fragment in prompt for fragment in required):
            raise PromptPackError(
                "density_contract short-writing requirements must appear in prompt, not a metadata field."
            )


def _validate_citation_plan(sentence_ids: Mapping[str, Any], kit: Mapping[str, Any]) -> None:
    expected = kit.get("citation_plan")
    if expected is not None and sentence_ids != expected:
        raise PromptPackError("density_contract citations must match the certified source plan.")


def validate_response_envelope(payload: object, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate pack-only citations, then return raw activities with no leaked keys."""
    if not isinstance(payload, Mapping):
        raise PromptPackError("Prompt-pack response must be a JSON object.")
    activities = payload.get("activities")
    citations = payload.get("citations")
    slots = context["phase_request"]["requested_slots"]
    if not isinstance(activities, list) or not isinstance(citations, list):
        raise PromptPackError("Prompt-pack response requires activities and citations arrays.")
    if len(activities) != len(slots) or len(citations) != len(slots):
        raise PromptPackError("Prompt-pack response count does not match requested response_order.")
    known_ids = {row["id"] for row in context["shared"]["anchor_sentence_inventory"]}
    cleaned: list[dict[str, Any]] = []
    type_kits = context["type_kits"]
    for index, (activity, citation, slot, kit) in enumerate(
        zip(activities, citations, slots, type_kits, strict=True)
    ):
        if not isinstance(activity, dict):
            raise PromptPackError(f"activities[{index}] must be an object.")
        if activity.get("type") != slot["type"]:
            raise PromptPackError(f"activities[{index}] type does not match requested slot.")
        leaked = _FORBIDDEN_ACTIVITY_KEYS & set(activity)
        if leaked:
            raise PromptPackError(f"activities[{index}] leaks pack-only keys: {sorted(leaked)!r}")
        if not isinstance(citation, Mapping) or citation.get("activity_index") != index:
            raise PromptPackError(f"citations[{index}] does not cite its activity index.")
        sentence_ids = citation.get("sentence_ids")
        if not isinstance(sentence_ids, Mapping):
            raise PromptPackError(f"citations[{index}] requires sentence_ids object.")
        expected_locators = _expected_locators(activity)
        if set(sentence_ids) != expected_locators:
            raise PromptPackError(
                f"citations[{index}] locators do not match activity evidence locators."
            )
        allowed = set(slot["allowed_sentence_ids"])
        for locator, ids in sentence_ids.items():
            if (
                not isinstance(ids, list)
                or not ids
                or not all(isinstance(item, str) for item in ids)
            ):
                raise PromptPackError(f"citations[{index}].{locator} must be a non-empty ID list.")
            if not set(ids) <= known_ids or not set(ids) <= allowed:
                raise PromptPackError(
                    f"citations[{index}].{locator} cites an unknown or disallowed sentence ID."
                )
        _validate_citation_plan(sentence_ids, kit)
        _validate_post_density_contract(activity, kit)
        cleaned.append(dict(activity))
    return cleaned


def repair_failures_from_activities(
    activities: Sequence[Any], requested_types: Mapping[str, int]
) -> list[dict[str, Any]]:
    """Normalize only failed type deficits; raw content never enters an injection."""
    by_type: dict[str, list[str]] = {}
    for activity in activities:
        activity_type = activity.activity.get("type") if hasattr(activity, "activity") else None
        if not isinstance(activity_type, str):
            continue
        checks = getattr(getattr(activity, "gate_result", None), "checks", [])
        failed = [check for check in checks if getattr(check, "status", None) == "fail"]
        if failed:
            by_type.setdefault(activity_type, []).append(
                str(getattr(failed[0], "gate", "raw_contract"))
            )
    failures = []
    for activity_type, count in requested_types.items():
        for index in range(count):
            gate = by_type.get(activity_type, ["raw_contract"])[
                index % max(1, len(by_type.get(activity_type, [])))
            ]
            failures.append(
                {
                    "slot_id": f"repair-{activity_type}-{index + 1}",
                    "prior_activity_index": None,
                    "locator": None,
                    "gate": gate,
                    "normalized_detail": f"{gate}: deterministic gate rejected the prior slot",
                    "required_fix": "emit one contract-valid replacement using only this kit",
                }
            )
    return failures


def focus_selector_context(shared: Mapping[str, Any]) -> dict[str, Any] | None:
    focus = shared["lesson_plan"].get("focus", {})
    if focus.get("status") != "supported":
        return None
    return {"terms": focus.get("terms", []), "sentence_ids": focus.get("sentence_ids", [])}
