"""Source-safe inventories for the locked TeacherReadyDensity.v3 slice-2 tests.

Every Ukrainian string comes from the already-committed ``anchor01.txt``
fixture.  The inventory deliberately derives candidate records from its
verbatim tokens rather than adding new Ukrainian prose to this regression
fixture.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from hramatka.engine.gates import vesum_tags
from hramatka.engine.short_writing_constraints_v3 import (
    ConstraintSpec,
    VesumToken,
)
from hramatka.engine.teacher_ready_density_v3 import floor_for
from hramatka.engine.unit_builders_v3 import (
    AnchorSentence,
    AnchorToken,
    AtlasPassPair,
    CertificationInventory,
    EvidenceCandidate,
    MarkTheWordsRequest,
    ShortWritingTask,
    TrueFalseFact,
)

_FIXTURES = Path(__file__).resolve().parent
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")
_TOKEN_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ'’]+")


def _sentences_and_tokens() -> tuple[tuple[AnchorSentence, ...], tuple[AnchorToken, ...]]:
    anchor = (_FIXTURES / "anchor01.txt").read_text(encoding="utf-8")
    sentences: list[AnchorSentence] = []
    tokens: list[AnchorToken] = []
    for sentence_number, sentence_match in enumerate(_SENTENCE_RE.finditer(anchor), start=1):
        sentence_id = f"s-{sentence_number}"
        text = sentence_match.group(0).strip()
        sentence_tokens: list[AnchorToken] = []
        for token_number, token_match in enumerate(_TOKEN_RE.finditer(text), start=1):
            surface = token_match.group(0)
            parses = tuple(vesum_tags.parse_word(surface))
            if not parses:
                continue
            token = AnchorToken(
                sentence_id=sentence_id,
                token_id=f"{sentence_id}:t-{token_number}",
                surface=surface,
                start_offset=token_match.start(),
                end_offset=token_match.end(),
                vesum_parses=parses,
            )
            sentence_tokens.append(token)
            tokens.append(token)
        sentences.append(
            AnchorSentence(sentence_id=sentence_id, text=text, tokens=tuple(sentence_tokens))
        )
    return tuple(sentences), tuple(tokens)


def complete_inventory() -> CertificationInventory:
    """Return one deterministic, all-type inventory derived from ``anchor01``."""
    sentences, tokens = _sentences_and_tokens()
    usable_tokens = tuple(token for token in tokens if len(token.surface) > 1)
    selected = usable_tokens[:8]
    sentence_by_id = {sentence.sentence_id: sentence for sentence in sentences}

    generic_candidates = []
    for activity_type in ("quiz", "cloze", "fill-in", "error-correction"):
        for index, token in enumerate(selected, start=1):
            sentence = sentence_by_id[token.sentence_id]
            replacement = usable_tokens[(index + 8) % len(usable_tokens)]
            derived_surface = (
                sentence.text[: token.start_offset]
                + replacement.surface
                + sentence.text[token.end_offset :]
                if activity_type == "error-correction"
                else None
            )
            generic_candidates.append(
                EvidenceCandidate(
                    activity_type=activity_type,
                    candidate_id=f"{activity_type}-{index}",
                    sentence_id=token.sentence_id,
                    token_id=token.token_id,
                    literal_evidence=sentence.text,
                    expected_key=token.surface,
                    semantic_target=derived_surface
                    or f"{activity_type}:{token.sentence_id}:{token.token_id}",
                    certified_error_count=1 if activity_type == "error-correction" else 0,
                    derived_surface=derived_surface,
                )
            )

    categories = (
        ("comprehension",) * 3 + ("explanation_inference",) * 3 + ("anchored_application",) * 2
    )
    for index, (token, category) in enumerate(zip(selected, categories, strict=True), start=1):
        generic_candidates.append(
            EvidenceCandidate(
                activity_type="text-questions",
                candidate_id=f"text-questions-{index}",
                sentence_id=token.sentence_id,
                token_id=token.token_id,
                literal_evidence=sentence_by_id[token.sentence_id].text,
                expected_key=token.surface,
                semantic_target=f"text-questions:{token.sentence_id}:{token.token_id}",
                category=category,
            )
        )

    true_false_facts = []
    for index, token in enumerate(selected, start=1):
        sentence = sentence_by_id[token.sentence_id]
        replacement = usable_tokens[(index + 8) % len(usable_tokens)]
        true_false_facts.append(
            TrueFalseFact(
                fact_id=f"true-false-{index}",
                sentence_id=token.sentence_id,
                literal_evidence=sentence.text,
                source_surface=token.surface,
                replacement_surface=replacement.surface,
                truth_value=index % 2 == 0,
            )
        )

    atlas_pairs = tuple(
        AtlasPassPair(
            pair_id=f"atlas-{index}",
            sentence_id=token.sentence_id,
            left=token.surface,
            right=usable_tokens[(index + 8) % len(usable_tokens)].surface,
            literal_evidence=sentence_by_id[token.sentence_id].text,
            atlas_pass=True,
        )
        for index, token in enumerate(selected, start=1)
    )

    noun_tokens = tuple(
        token
        for token in usable_tokens
        if any(parse.get("pos") == "noun" for parse in token.vesum_parses)
    )[:8]
    mark_request = MarkTheWordsRequest(
        request_id="mark-1",
        sentence_ids=tuple(sentence.sentence_id for sentence in sentences[:4]),
        criterion="pos=noun",
        target_token_ids=tuple(token.token_id for token in noun_tokens),
    )

    writing_tokens = tuple(
        VesumToken(
            surface=token.surface,
            start_offset=token.start_offset,
            end_offset=token.end_offset,
            parses=token.vesum_parses,
        )
        for token in usable_tokens
    )
    writing_task = ShortWritingTask(
        task_id="writing-1",
        sentence_id=sentences[0].sentence_id,
        prompt=sentences[0].text,
        constraints=(
            ConstraintSpec(
                "contains_lemma_set", {"lemmas": (writing_tokens[0].parses[0]["lemma"],)}
            ),
            ConstraintSpec("min_verb_count", {"minimum": 1}),
            ConstraintSpec("word_count_range", {"minimum": 1, "maximum": 200}),
        ),
        sample_tokens=writing_tokens,
    )
    return CertificationInventory(
        source_id="engine-test-anchor01",
        sentences=sentences,
        candidates=tuple(generic_candidates),
        true_false_facts=tuple(true_false_facts),
        atlas_pairs=atlas_pairs,
        mark_requests=(mark_request,),
        writing_tasks=(writing_task,),
    )


def insufficient_inventory(activity_type: str) -> CertificationInventory:
    """Return the existing-source fixture with one fewer resource than its floor."""
    inventory = complete_inventory()
    count = floor_for(activity_type).minimum_units - 1
    if activity_type in {"quiz", "cloze", "fill-in", "error-correction", "text-questions"}:
        selected = [
            candidate
            for candidate in inventory.candidates
            if candidate.activity_type == activity_type
        ]
        retained = [
            candidate
            for candidate in inventory.candidates
            if candidate.activity_type != activity_type
        ]
        return replace(inventory, candidates=tuple([*retained, *selected[:count]]))
    if activity_type == "true-false":
        return replace(inventory, true_false_facts=inventory.true_false_facts[:count])
    if activity_type == "match-up":
        return replace(inventory, atlas_pairs=inventory.atlas_pairs[:count])
    if activity_type == "mark-the-words":
        return replace(
            inventory,
            mark_requests=tuple(
                replace(request, target_token_ids=request.target_token_ids[:count])
                for request in inventory.mark_requests
            ),
        )
    return replace(
        inventory,
        writing_tasks=tuple(
            replace(task, constraints=task.constraints[:count]) for task in inventory.writing_tasks
        ),
    )
