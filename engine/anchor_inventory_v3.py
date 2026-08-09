"""Deterministic teacher-anchor canonicalization for the live v3 preflight."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import replace

from .gates import vesum_tags
from .short_writing_constraints_v3 import ConstraintSpec, VesumToken
from .unit_builders_v3 import (
    AnchorSentence,
    AnchorToken,
    AtlasPassPair,
    CertificationInventory,
    EvidenceCandidate,
    ShortWritingTask,
    TrueFalseFact,
)

_SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]?")
_TOKEN_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ'’]+")
_LIST_TYPES = frozenset(
    {
        "true-false",
        "quiz",
        "cloze",
        "match-up",
        "error-correction",
        "fill-in",
        "text-questions",
    }
)
_TEXT_QUESTION_CATEGORIES = (
    "comprehension",
    "comprehension",
    "comprehension",
    "explanation_inference",
    "explanation_inference",
    "explanation_inference",
    "anchored_application",
    "anchored_application",
)


def _source_id(anchor: str) -> str:
    return f"teacher-anchor:{hashlib.sha256(anchor.encode('utf-8')).hexdigest()}"


def _sentences(anchor: str) -> tuple[AnchorSentence, ...]:
    rows: list[AnchorSentence] = []
    for sentence_number, text in enumerate(_SENTENCE_RE.findall(anchor), start=1):
        if not text.strip():
            continue
        tokens: list[AnchorToken] = []
        for token_match in _TOKEN_RE.finditer(text):
            surface = token_match.group(0)
            parses = tuple(vesum_tags.parse_word(surface))
            tokens.append(
                AnchorToken(
                    sentence_id=f"s-{sentence_number}",
                    token_id=f"s-{sentence_number}:t-{len(tokens) + 1}",
                    surface=surface,
                    start_offset=token_match.start(),
                    end_offset=token_match.end(),
                    vesum_parses=parses,
                )
            )
        rows.append(
            AnchorSentence(sentence_id=f"s-{sentence_number}", text=text, tokens=tuple(tokens))
        )
    return tuple(rows)


def _token_groups(sentences: Iterable[AnchorSentence]) -> tuple[tuple[AnchorToken, ...], ...]:
    """Return one eight-token resource group per usable source sentence.

    A group stays within one sentence.  This makes its evidence claim explicit
    to the exact-cover allocator instead of allowing two activity builders to
    quietly borrow a shared sentence's tokens.
    """
    return tuple(sentence.tokens[:8] for sentence in sentences if len(sentence.tokens) >= 8)


def _replacement_surface(group: Sequence[AnchorToken], index: int, fallback: AnchorToken) -> str:
    for offset in range(1, len(group)):
        candidate = group[(index + offset) % len(group)]
        if candidate.surface != fallback.surface:
            return candidate.surface
    return fallback.surface


def inventory_from_anchor(anchor: str, *, scheduled_types: Sequence[str]) -> CertificationInventory:
    """Build only literal-evidence candidates required by one v3 lesson shape.

    The result may be incomplete.  That is an expected preflight outcome: the
    allocator will return ``insufficient_anchor_capacity`` before any model
    call rather than lowering a type's floor or inventing source material.
    """
    sentences = _sentences(anchor)
    by_id = {sentence.sentence_id: sentence for sentence in sentences}
    groups = _token_groups(sentences)
    required = Counter(scheduled_types)
    group_index = 0

    groups_by_type: dict[str, list[tuple[AnchorToken, ...]]] = defaultdict(list)
    for activity_type in scheduled_types:
        if activity_type == "short-writing" and groups_by_type[activity_type]:
            continue
        if not groups:
            break
        groups_by_type[activity_type].append(groups[group_index % len(groups)])
        group_index += 1

    candidates: list[EvidenceCandidate] = []
    true_false_facts: list[TrueFalseFact] = []
    atlas_pairs: list[AtlasPassPair] = []
    writing_tasks: list[ShortWritingTask] = []
    for activity_type, _count in sorted(required.items()):
        if activity_type not in _LIST_TYPES | {"short-writing"}:
            continue
        type_groups = groups_by_type.get(activity_type, ())
        for group_number, group in enumerate(type_groups, start=1):
            sentence = by_id[group[0].sentence_id]
            for token_number, token in enumerate(group, start=1):
                candidate_id = f"{activity_type}:{group_number}:{token_number}"
                replacement = _replacement_surface(group, token_number - 1, token)
                if activity_type in {"quiz", "cloze", "fill-in", "error-correction"}:
                    derived = (
                        sentence.text[: token.start_offset]
                        + replacement
                        + sentence.text[token.end_offset :]
                        if activity_type == "error-correction" and replacement != token.surface
                        else None
                    )
                    candidates.append(
                        EvidenceCandidate(
                            activity_type=activity_type,
                            candidate_id=candidate_id,
                            sentence_id=sentence.sentence_id,
                            token_id=token.token_id,
                            literal_evidence=sentence.text,
                            expected_key=token.surface,
                            semantic_target=f"{activity_type}:{token.token_id}",
                            certified_error_count=1 if derived is not None else 0,
                            derived_surface=derived,
                        )
                    )
                elif activity_type == "text-questions":
                    candidates.append(
                        EvidenceCandidate(
                            activity_type=activity_type,
                            candidate_id=candidate_id,
                            sentence_id=sentence.sentence_id,
                            token_id=token.token_id,
                            literal_evidence=sentence.text,
                            expected_key=token.surface,
                            semantic_target=f"{activity_type}:{token.token_id}",
                            category=_TEXT_QUESTION_CATEGORIES[token_number - 1],
                        )
                    )
                elif activity_type == "true-false" and replacement != token.surface:
                    true_false_facts.append(
                        TrueFalseFact(
                            fact_id=candidate_id,
                            sentence_id=sentence.sentence_id,
                            literal_evidence=sentence.text,
                            source_surface=token.surface,
                            replacement_surface=replacement,
                            truth_value=token_number % 2 == 0,
                        )
                    )
                elif activity_type == "match-up" and replacement != token.surface:
                    atlas_pairs.append(
                        AtlasPassPair(
                            pair_id=candidate_id,
                            sentence_id=sentence.sentence_id,
                            left=token.surface,
                            right=replacement,
                            literal_evidence=sentence.text,
                            atlas_pass=True,
                        )
                    )
            if activity_type == "short-writing":
                lemma = next(
                    (
                        str(parse["lemma"])
                        for token in group
                        for parse in token.vesum_parses
                        if isinstance(parse.get("lemma"), str) and parse["lemma"].strip()
                    ),
                    None,
                )
                if lemma is not None:
                    writing_tasks.append(
                        ShortWritingTask(
                            task_id=f"short-writing:{group_number}",
                            sentence_id=sentence.sentence_id,
                            prompt=sentence.text,
                            constraints=(
                                ConstraintSpec("contains_lemma_set", {"lemmas": (lemma,)}),
                                ConstraintSpec("word_count_range", {"minimum": 1, "maximum": 200}),
                            ),
                            sample_tokens=tuple(
                                VesumToken(
                                    surface=token.surface,
                                    start_offset=token.start_offset,
                                    end_offset=token.end_offset,
                                    parses=token.vesum_parses,
                                )
                                for token in group
                            ),
                        )
                    )
    return CertificationInventory(
        source_id=_source_id(anchor),
        sentences=sentences,
        candidates=tuple(candidates),
        true_false_facts=tuple(true_false_facts),
        atlas_pairs=tuple(atlas_pairs),
        writing_tasks=tuple(writing_tasks),
    )


def inventory_for_group(
    inventory: CertificationInventory, *, activity_type: str, group_number: int
) -> CertificationInventory:
    """Return one slot's literal resource group without weakening its floor.

    Repeated lesson types receive different preselected source groups.  Feeding
    only that group to the existing pure builder keeps the exact-cover search
    bounded and makes every slot's resource claims explicit before generation.
    """
    prefix = f"{activity_type}:{group_number}:"
    task_id = f"{activity_type}:{group_number}"
    return replace(
        inventory,
        candidates=tuple(
            candidate
            for candidate in inventory.candidates
            if candidate.activity_type != activity_type or candidate.candidate_id.startswith(prefix)
        ),
        true_false_facts=tuple(
            fact
            for fact in inventory.true_false_facts
            if activity_type != "true-false" or fact.fact_id.startswith(prefix)
        ),
        atlas_pairs=tuple(
            pair
            for pair in inventory.atlas_pairs
            if activity_type != "match-up" or pair.pair_id.startswith(prefix)
        ),
        writing_tasks=tuple(
            task
            for task in inventory.writing_tasks
            if activity_type != "short-writing" or task.task_id == task_id
        ),
    )
