"""Source-safe inventories for the locked TeacherReadyDensity.v3 slice-2 tests.

The inventory deliberately derives every candidate from one explicit,
non-learner fixture body rather than hiding extra Ukrainian prose in builders.
"""

from __future__ import annotations

import re
from dataclasses import replace

from hramatka.engine.closed_class_policy import is_closed_class_form
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

_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")
_TOKEN_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ'’]+")
DENSITY_V3_ANCHOR = """На думку вчених, читання є одним з найскладніших завдань для мозку.
Під час читання активізуються одразу 17 ділянок головного мозку.
Третина українців за рік не прочитує жодної книжки.
Дві третини українців щодня знаходять час увімкнути телевізор.
Регулярне читання знижує ризик розвитку хвороби Альцгеймера.
Багато людей втратили насолоду від неспішного читання книжок.
Студенти прочитають книги про користь регулярного читання для мозку.
Люди прочитають книги про розвиток мозку за два роки.
Українці читають книги про розвиток мозку щодня.
Студенти працюють зі словами в тексті про читання.
Багато людей прочитують книги за двадцять хвилин щодня.
Третина студентів прочитає книгу за дві години.
Дві третини людей читають книги про роботу мозку.
Під час читання люди працюють зі словами в тексті.
Регулярне читання активізується в ділянках головного мозку.
Студенти знаходять час прочитати книги про розвиток мозку.
Люди знаходять насолоду в неспішному читанні книжок.
Українці частіше читають увечері, бо тоді мають більше вільного часу.
Студенти роблять нотатки, адже так краще запам'ятовують зміст книжки.
Люди повертаються до складних уривків, оскільки прагнуть зрозуміти аргументи автора.
Студенти читають книги про користь пробіжки для серця.
Під час пробіжки активізуються ділянки головного мозку.
Регулярне читання знижує ризик для серця і мозку.
Люди працюють зі словами і читають книги щодня.
Студенти прочитають три книги за три дні.
Люди прочитують книги про користь читання для мозку.
Українці працюють зі словами в тексті щодня.
Під час читання студенти знаходять насолоду в книгах.
Під час читання активізується головний мозок людини.
Студенти прочитають чотири книги за два роки.
Люди читають книги про хвороби серця і мозку.
Українці знаходять час для регулярного читання книг.
"""


def _sentences_and_tokens() -> tuple[tuple[AnchorSentence, ...], tuple[AnchorToken, ...]]:
    anchor = DENSITY_V3_ANCHOR
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
    """Return one deterministic, all-type inventory derived from the density anchor."""
    from hramatka.engine.anchor_inventory_v3 import _certified_choice_bank

    sentences, tokens = _sentences_and_tokens()
    usable_tokens = tuple(
        token
        for token in tokens
        if len(token.surface) > 1 and not is_closed_class_form(token.surface)
    )
    sentence_by_id = {sentence.sentence_id: sentence for sentence in sentences}
    selected = tuple(
        token
        for sentence in sentences[:8]
        for token in tuple(
            item for item in usable_tokens if item.sentence_id == sentence.sentence_id
        )[:1]
    )
    if len(selected) != 8:
        raise RuntimeError("density v3 fixture needs one usable token from eight sentences")
    bank_selected = tuple(
        token
        for sentence in sentences[:8]
        for token in tuple(
            item
            for item in usable_tokens
            if item.sentence_id == sentence.sentence_id
            and _certified_choice_bank(item) is not None
        )[:1]
    )
    if len(bank_selected) != 8:
        raise RuntimeError("density v3 fixture needs one banked token from eight sentences")
    cloze_passage = " ".join(sentence.text for sentence in sentences[:8])
    cloze_sentence_starts: dict[str, int] = {}
    cursor = 0
    for sentence in sentences[:8]:
        cloze_sentence_starts[sentence.sentence_id] = cursor
        cursor += len(sentence.text) + 1
    match_selected = tuple(
        token
        for sentence in sentences[:4]
        for token in tuple(
            item
            for item in usable_tokens
            if item.sentence_id == sentence.sentence_id
            and any(
                isinstance(parse.get("lemma"), str)
                and str(parse["lemma"]).casefold() != item.surface.casefold()
                for parse in item.vesum_parses
            )
        )[:2]
    )
    if len(match_selected) != 8:
        raise RuntimeError("density v3 fixture needs diverse form-to-lemma pairs")

    generic_candidates = []
    for activity_type in ("quiz", "cloze", "fill-in", "error-correction"):
        activity_tokens = (
            bank_selected if activity_type in {"quiz", "cloze", "fill-in"} else selected
        )
        if activity_type == "error-correction":
            activity_tokens = tuple(
                next(
                    token
                    for token in usable_tokens
                    if token.sentence_id == sentence.sentence_id and token.start_offset > 0
                )
                for sentence in sentences[:8]
            )
        for index, token in enumerate(activity_tokens, start=1):
            sentence = sentence_by_id[token.sentence_id]
            bank_row = (
                _certified_choice_bank(token)
                if activity_type in {"quiz", "cloze", "fill-in"}
                else None
            )
            if activity_type in {"quiz", "cloze", "fill-in"} and bank_row is None:
                raise RuntimeError("density v3 fixture needs a certified choice bank")
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
                    rendering_surface=cloze_passage if activity_type == "cloze" else None,
                    target_start_offset=(
                        cloze_sentence_starts[token.sentence_id] + token.start_offset
                        if activity_type == "cloze"
                        else token.start_offset
                        if activity_type in {"quiz", "fill-in", "error-correction"}
                        else None
                    ),
                    target_end_offset=(
                        cloze_sentence_starts[token.sentence_id] + token.end_offset
                        if activity_type == "cloze"
                        else token.end_offset
                        if activity_type in {"quiz", "fill-in", "error-correction"}
                        else None
                    ),
                    morphology_class=(
                        ("case", "number", "gender", "person")[index % 4]
                        if activity_type == "error-correction"
                        else None
                    ),
                    choice_bank=bank_row[0] if bank_row is not None else (),
                    exclusion_warrants=bank_row[1] if bank_row is not None else (),
                )
            )

    categories = (
        ("comprehension",) * 3 + ("explanation_inference",) * 3 + ("anchored_application",) * 2
    )
    question_selected = (
        *selected[:3],
        *(
            next(token for token in usable_tokens if token.sentence_id == f"s-{number}")
            for number in range(18, 21)
        ),
        *selected[3:5],
    )
    intents = (
        ("fact-recovery",) * 3 + ("explicit-causal",) * 3 + ("realistic-transfer",) * 2
    )
    for index, (token, category, intent) in enumerate(
        zip(question_selected, categories, intents, strict=True), start=1
    ):
        sentence = sentence_by_id[token.sentence_id]
        generic_candidates.append(
            EvidenceCandidate(
                activity_type="text-questions",
                candidate_id=f"text-questions-{index}",
                sentence_id=token.sentence_id,
                token_id=token.token_id,
                literal_evidence=sentence.text,
                expected_key=sentence.text,
                semantic_target=f"text-questions:{token.sentence_id}:{token.token_id}",
                category=category,
                question_intent=intent,
                semantic_warrant=(
                    "source carrier contains an explicit causal connective"
                    if intent == "explicit-causal"
                    else "source carrier states the fact to recover"
                    if intent == "fact-recovery"
                    else "source carrier anchors one realistic transfer prompt"
                ),
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
            right=next(
                str(parse["lemma"])
                for parse in token.vesum_parses
                if isinstance(parse.get("lemma"), str)
                and str(parse["lemma"]).casefold() != token.surface.casefold()
            ),
            literal_evidence=sentence_by_id[token.sentence_id].text,
            atlas_pass=True,
            relation="atlas_antonym.v1",
        )
        for index, token in enumerate(match_selected, start=1)
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
            ConstraintSpec("word_count_range", {"minimum": 60, "maximum": 80}),
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
