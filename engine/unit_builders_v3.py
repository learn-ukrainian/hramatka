"""Pure per-type TeacherReadyDensity.v3 certification builders.

This pre-cutover module consumes only canonical anchor/kit inventories and
returns a complete immutable ``UnitPlan`` or ``unavailable``.  It neither calls
models nor imports the production planner, prompt pack, pipeline, or API.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from .closed_class_policy import CLOSED_CLASS_ALLOWED_SHAPES, is_closed_class_target
from .gates import vesum_tags
from .short_writing_constraints_v3 import (
    ConstraintSpec,
    VesumToken,
    registered_constraint_names,
    validate_constraints,
)
from .teacher_ready_density_v3 import floor_for
from .true_false_catalog_v3 import (
    MUTATION_CATALOG_VERSION,
    MutationBinding,
    construct_false_statement,
    verify_false_statement,
)
from .unit_plan_v3 import (
    CertifiedTargetToken,
    CertifiedUnit,
    Citation,
    ExpectedKeyRule,
    ResourceClaim,
    UnitAnchor,
    UnitPlan,
    certify_unit_plan,
)

_TOKEN_RE: Final = re.compile(r"[А-Яа-яІіЇїЄєҐґ'’]+")
_TEXT_QUESTION_ALLOWED_PREFIXES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "comprehension": (
            "Що повідомляє уривок про",
            "Що сказано в уривку про",
            "Який факт подає уривок про",
        ),
        "explanation_inference": (
            "Чому",
            "З якої причини",
            "Як можна пояснити",
        ),
        "anchored_application": (
            "Як можна застосувати",
            "У якій подібній ситуації",
            "У якій реальній ситуації",
            "Чи доводилося вам",
            "З вашого досвіду",
        ),
    }
)
_TEXT_QUESTION_RELATION_PREFIXES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "causal-clause.v1": ("Чому", "З якої причини"),
        "purpose-clause.v1": ("З якою метою", "Навіщо"),
        "temporal-clause.v1": ("Коли", "До якого моменту"),
        "licensed-vid-cause.v1": ("Від чого", "Через що"),
    }
)
_UKRAINIAN_CASE_FORMS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "nom": "називному",
        "gen": "родовому",
        "dat": "давальному",
        "acc": "знахідному",
        "instr": "орудному",
        "loc": "місцевому",
        "voc": "кличному",
    }
)


@dataclass(frozen=True)
class AnchorToken:
    sentence_id: str
    token_id: str
    surface: str
    start_offset: int
    end_offset: int
    vesum_parses: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if not self.sentence_id.strip() or not self.token_id.strip() or not self.surface.strip():
            raise ValueError("Anchor tokens need stable sentence, token, and surface values.")
        if self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("Anchor-token offsets must be ordered and non-negative.")
        object.__setattr__(self, "vesum_parses", tuple(self.vesum_parses))


@dataclass(frozen=True)
class AnchorSentence:
    sentence_id: str
    text: str
    tokens: tuple[AnchorToken, ...]

    def __post_init__(self) -> None:
        if not self.sentence_id.strip() or not self.text.strip():
            raise ValueError("Anchor sentences need stable IDs and verbatim text.")
        object.__setattr__(self, "tokens", tuple(self.tokens))
        for token in self.tokens:
            if token.sentence_id != self.sentence_id:
                raise ValueError("Anchor tokens must belong to their enclosing sentence.")
            if self.text[token.start_offset : token.end_offset] != token.surface:
                raise ValueError("Anchor-token offsets must bind the verbatim sentence surface.")


@dataclass(frozen=True)
class EvidenceCandidate:
    activity_type: str
    candidate_id: str
    sentence_id: str
    token_id: str
    literal_evidence: str
    expected_key: str
    semantic_target: str
    category: str | None = None
    certified_error_count: int = 0
    derived_surface: str | None = None
    focus_alignment: str | None = None
    source_lemma: str | None = None
    kit_rule_id: str | None = None
    rendering_surface: str | None = None
    target_start_offset: int | None = None
    target_end_offset: int | None = None
    degree_class: str | None = None
    morphology_class: str | None = None
    choice_bank: tuple[str, ...] = ()
    frame_family: str | None = None
    semantic_warrant: str | None = None
    exclusion_warrants: tuple[tuple[str, str], ...] = ()
    question_intent: str | None = None
    answer_start_offset: int | None = None
    answer_end_offset: int | None = None
    topic_token_id: str | None = None
    topic_lemma: str | None = None


@dataclass(frozen=True)
class TrueFalseFact:
    fact_id: str
    sentence_id: str
    literal_evidence: str
    source_surface: str
    replacement_surface: str
    truth_value: bool
    mutation_rule_id: str = "replace-one-surface.v1"
    source_start_offset: int | None = None
    source_end_offset: int | None = None


@dataclass(frozen=True)
class AtlasPassPair:
    pair_id: str
    sentence_id: str
    left: str
    right: str
    literal_evidence: str
    atlas_pass: bool
    relation: str = "legacy.unspecified"


@dataclass(frozen=True)
class MarkTheWordsRequest:
    request_id: str
    sentence_ids: tuple[str, ...]
    criterion: str
    target_token_ids: tuple[str, ...]


@dataclass(frozen=True)
class ShortWritingTask:
    task_id: str
    sentence_id: str
    prompt: str
    constraints: tuple[ConstraintSpec, ...]
    sample_tokens: tuple[VesumToken, ...]
    focus_alignment: str | None = None
    attribute_warrants: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CertificationInventory:
    source_id: str
    sentences: tuple[AnchorSentence, ...]
    candidates: tuple[EvidenceCandidate, ...] = ()
    true_false_facts: tuple[TrueFalseFact, ...] = ()
    atlas_pairs: tuple[AtlasPassPair, ...] = ()
    mark_requests: tuple[MarkTheWordsRequest, ...] = ()
    writing_tasks: tuple[ShortWritingTask, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("Certification inventories need a stable source ID.")
        for name in (
            "sentences",
            "candidates",
            "true_false_facts",
            "atlas_pairs",
            "mark_requests",
            "writing_tasks",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))


def _sentences(inventory: CertificationInventory) -> dict[str, AnchorSentence]:
    result = {sentence.sentence_id: sentence for sentence in inventory.sentences}
    return result if len(result) == len(inventory.sentences) else {}


def _token_by_id(sentences: Mapping[str, AnchorSentence]) -> dict[str, AnchorToken]:
    tokens = {token.token_id: token for sentence in sentences.values() for token in sentence.tokens}
    return tokens


def _candidate_is_bound(
    candidate: EvidenceCandidate, sentences: Mapping[str, AnchorSentence]
) -> bool:
    sentence = sentences.get(candidate.sentence_id)
    return (
        sentence is not None
        and candidate.literal_evidence in sentence.text
        and any(token.token_id == candidate.token_id for token in sentence.tokens)
        and bool(candidate.candidate_id and candidate.expected_key and candidate.semantic_target)
    )


def _source_diverse(source_ids: tuple[str, ...], *, maximum_per_source: int = 2) -> bool:
    counts = Counter(source_ids)
    return len(counts) >= 4 and max(counts.values(), default=0) <= maximum_per_source


def _has_one_certified_error(candidate: EvidenceCandidate) -> bool:
    correct_surface = candidate.rendering_surface or candidate.literal_evidence
    if candidate.derived_surface is None or candidate.derived_surface == correct_surface:
        return False
    evidence_tokens = _TOKEN_RE.findall(correct_surface)
    derived_tokens = _TOKEN_RE.findall(candidate.derived_surface)
    if len(evidence_tokens) != len(derived_tokens):
        return False
    differences = [
        (evidence, derived)
        for evidence, derived in zip(evidence_tokens, derived_tokens, strict=True)
        if evidence != derived
    ]
    return len(differences) == 1 and differences[0][0] == candidate.expected_key


def _candidate_unit(
    inventory: CertificationInventory, candidate: EvidenceCandidate
) -> CertifiedUnit:
    error_count = (
        candidate.certified_error_count if candidate.activity_type == "error-correction" else 0
    )
    distinctness: dict[str, object]
    if candidate.activity_type in {"quiz", "cloze", "fill-in"}:
        rendering_surface = candidate.rendering_surface or candidate.literal_evidence
        if (
            candidate.target_start_offset is None
            or candidate.target_end_offset is None
            or rendering_surface[
                candidate.target_start_offset : candidate.target_end_offset
            ]
            != candidate.expected_key
        ):
            raise ValueError("Closed activity gap offsets must bind the certified answer.")
        if (
            len(candidate.choice_bank) < 3
            or len(candidate.choice_bank) != len(set(candidate.choice_bank))
            or candidate.expected_key not in candidate.choice_bank
            or {option for option, _warrant in candidate.exclusion_warrants}
            != set(candidate.choice_bank) - {candidate.expected_key}
            or not all(warrant.strip() for _option, warrant in candidate.exclusion_warrants)
        ):
            raise ValueError("Closed activity options need a complete exclusion-warranted bank.")
        if (
            candidate.activity_type == "cloze"
            and candidate.kit_rule_id is None
            and candidate.frame_family != "cross-gap-lexical.v1"
        ):
            raise ValueError("Generic cloze needs a certified cross-gap lexical bank.")
        distinctness = {
            "gap": {
                "sentence_id": candidate.sentence_id,
                "token_id": candidate.token_id,
                "start_offset": candidate.target_start_offset,
                "end_offset": candidate.target_end_offset,
            },
            "semantic_target": candidate.semantic_target,
        }
        if candidate.activity_type != "cloze":
            distinctness["stem"] = candidate.candidate_id
    else:
        distinctness = {
            "stem": candidate.candidate_id,
            "semantic_target": candidate.semantic_target,
        }
        if candidate.activity_type == "error-correction" and candidate.kit_rule_id is None:
            if (
                candidate.target_start_offset is None
                or candidate.morphology_class is None
                or candidate.frame_family != "contextual-mismatch.v1"
                or not candidate.semantic_warrant
            ):
                raise ValueError(
                    "Error-correction needs a locally warranted contextual mismatch."
                )
            distinctness["error_position"] = (
                "sentence-initial" if candidate.target_start_offset == 0 else "within-sentence"
            )
        if candidate.activity_type == "text-questions":
            distinctness["question_category"] = candidate.category
            prefixes = _TEXT_QUESTION_RELATION_PREFIXES.get(
                candidate.question_intent or ""
            ) or _TEXT_QUESTION_ALLOWED_PREFIXES.get(candidate.category or "")
            if prefixes is None:
                raise ValueError("Text-question category has no certified question frame.")
            if (
                candidate.answer_start_offset is not None
                or candidate.answer_end_offset is not None
                or candidate.topic_token_id is not None
                or candidate.topic_lemma is not None
            ):
                sentence = next(
                    (
                        item
                        for item in inventory.sentences
                        if item.sentence_id == candidate.sentence_id
                    ),
                    None,
                )
                topic = next(
                    (
                        token
                        for token in sentence.tokens
                        if token.token_id == candidate.topic_token_id
                    ),
                    None,
                ) if sentence is not None else None
                if (
                    sentence is None
                    or candidate.answer_start_offset is None
                    or candidate.answer_end_offset is None
                    or candidate.answer_start_offset < 0
                    or candidate.answer_end_offset <= candidate.answer_start_offset
                    or sentence.text[
                        candidate.answer_start_offset : candidate.answer_end_offset
                    ]
                    != candidate.expected_key
                    or topic is None
                    or not isinstance(candidate.topic_lemma, str)
                    or not candidate.topic_lemma.strip()
                ):
                    raise ValueError(
                        "Text-question answer span and named topic must bind one "
                        "source proposition."
                    )
                distinctness["answer_span"] = {
                    "sentence_id": candidate.sentence_id,
                    "start_offset": candidate.answer_start_offset,
                    "end_offset": candidate.answer_end_offset,
                    "text": candidate.expected_key,
                }
                distinctness["question_topic"] = {
                    "token_id": topic.token_id,
                    "surface": topic.surface,
                    "lemma": candidate.topic_lemma,
                }
            distinctness["question_frame"] = {
                "allowed_prefixes": list(prefixes),
                "category": candidate.category,
                "intent": candidate.question_intent,
            }
    if candidate.question_intent is not None:
        distinctness["question_intent"] = candidate.question_intent
    if candidate.focus_alignment is not None:
        distinctness["focus_alignment"] = candidate.focus_alignment
    if candidate.source_lemma is not None:
        distinctness["source_lemma"] = candidate.source_lemma
    if candidate.degree_class is not None:
        distinctness["degree_class"] = candidate.degree_class
    if candidate.morphology_class is not None:
        distinctness["morphology_class"] = candidate.morphology_class
    if candidate.choice_bank:
        distinctness["choice_bank"] = list(candidate.choice_bank)
    if candidate.frame_family is not None:
        distinctness["frame_family"] = candidate.frame_family
    if candidate.semantic_warrant is not None:
        distinctness["semantic_warrant"] = candidate.semantic_warrant
    if candidate.exclusion_warrants:
        distinctness["exclusion_warrants"] = dict(candidate.exclusion_warrants)
    if candidate.kit_rule_id is not None:
        if not candidate.source_lemma:
            raise ValueError("Degree-kit candidates need a source lemma claim.")
        resource_claims = (
            ResourceClaim("degree_unit", f"{candidate.kit_rule_id}:{candidate.source_lemma}"),
            ResourceClaim("candidate", candidate.candidate_id),
        )
        anchor = UnitAnchor("kit", f"{candidate.kit_rule_id}:{candidate.source_lemma}")
    else:
        resource_claims = (
            ResourceClaim("sentence", candidate.sentence_id),
            *(
                ()
                if candidate.activity_type == "text-questions"
                else (ResourceClaim("token", candidate.token_id),)
            ),
            ResourceClaim("candidate", candidate.candidate_id),
        )
        anchor = UnitAnchor("evidence", f"{inventory.source_id}:{candidate.sentence_id}")
    return CertifiedUnit(
        unit_id=candidate.candidate_id,
        resource_claims=resource_claims,
        anchor=anchor,
        allowed_forms=(candidate.derived_surface or candidate.expected_key, candidate.expected_key)
        if error_count
        else (candidate.expected_key,),
        expected_key_or_rule=ExpectedKeyRule(
            "rule" if error_count else "key", candidate.expected_key, error_count
        ),
        citation_plan=(Citation(inventory.source_id, f"sentence:{candidate.sentence_id}"),),
        distinctness=distinctness,
        rendering_surface=candidate.rendering_surface or candidate.literal_evidence,
    )


def _generic_builder(
    activity_type: str, inventory: CertificationInventory, *, slot_id: str, phase: int
) -> UnitPlan:
    sentences = _sentences(inventory)
    candidates = tuple(
        candidate
        for candidate in inventory.candidates
        if candidate.activity_type == activity_type
        and _candidate_is_bound(candidate, sentences)
        and (
            activity_type in CLOSED_CLASS_ALLOWED_SHAPES
            or activity_type == "text-questions"
            or not is_closed_class_target(
                {"expected_key": candidate.expected_key, "answer": candidate.expected_key}
            )
        )
        and (activity_type != "error-correction" or _has_one_certified_error(candidate))
    )
    if len({candidate.focus_alignment for candidate in candidates}) > 1:
        candidates = ()
    if activity_type == "cloze":
        sentence_rank = {sentence_id: index for index, sentence_id in enumerate(sentences)}
        token_offset = {
            token.token_id: token.start_offset
            for sentence in sentences.values()
            for token in sentence.tokens
        }
        candidates = tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.target_start_offset
                    if item.target_start_offset is not None
                    else sentence_rank[item.sentence_id],
                    item.target_end_offset
                    if item.target_end_offset is not None
                    else token_offset[item.token_id],
                    item.candidate_id,
                ),
            )
        )
    if activity_type == "text-questions":
        minima = floor_for(activity_type).category_minima
        assert minima is not None
        required = {
            "comprehension": minima.comprehension,
            "explanation_inference": minima.explanation_inference,
            "anchored_application": minima.anchored_application,
        }
        candidates = tuple(candidate for candidate in candidates if candidate.category in required)
        if any(
            sum(candidate.category == category for candidate in candidates) < minimum
            for category, minimum in required.items()
        ):
            candidates = ()
    if activity_type == "error-correction" and any(
        candidate.kit_rule_id is None for candidate in candidates
    ) and (
        len({candidate.morphology_class for candidate in candidates}) < 3
        or sum(candidate.target_start_offset == 0 for candidate in candidates) > 4
    ):
        candidates = ()
    evidence_candidates = tuple(item for item in candidates if item.kit_rule_id is None)
    if evidence_candidates:
        maximum_per_source = 2 if activity_type == "cloze" else 1
        if not _source_diverse(
            tuple(item.sentence_id for item in evidence_candidates),
            maximum_per_source=maximum_per_source,
        ):
            candidates = ()
    kit_lemmas = tuple(item.source_lemma for item in candidates if item.kit_rule_id is not None)
    if kit_lemmas and (None in kit_lemmas or len(kit_lemmas) != len(set(kit_lemmas))):
        candidates = ()
    return certify_unit_plan(
        slot_id=slot_id,
        phase=phase,
        activity_type=activity_type,
        units=tuple(_candidate_unit(inventory, candidate) for candidate in candidates),
    )


def _unavailable(activity_type: str, *, slot_id: str, phase: int) -> UnitPlan:
    return certify_unit_plan(slot_id=slot_id, phase=phase, activity_type=activity_type, units=())


def _closed(
    activity_type: str, *, slot_id: str, phase: int, build: Callable[[], UnitPlan]
) -> UnitPlan:
    try:
        return build()
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return _unavailable(activity_type, slot_id=slot_id, phase=phase)


def build_quiz(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    return _closed(
        "quiz",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _generic_builder("quiz", inventory, slot_id=slot_id, phase=phase),
    )


def build_cloze(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    return _closed(
        "cloze",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _generic_builder("cloze", inventory, slot_id=slot_id, phase=phase),
    )


def build_fill_in(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    return _closed(
        "fill-in",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _generic_builder("fill-in", inventory, slot_id=slot_id, phase=phase),
    )


def build_error_correction(
    inventory: CertificationInventory, *, slot_id: str, phase: int
) -> UnitPlan:
    return _closed(
        "error-correction",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _generic_builder("error-correction", inventory, slot_id=slot_id, phase=phase),
    )


def build_text_questions(
    inventory: CertificationInventory, *, slot_id: str, phase: int
) -> UnitPlan:
    return _closed(
        "text-questions",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _generic_builder("text-questions", inventory, slot_id=slot_id, phase=phase),
    )


def _build_true_false(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    sentences = _sentences(inventory)
    units: list[CertifiedUnit] = []
    for fact in inventory.true_false_facts:
        sentence = sentences.get(fact.sentence_id)
        if sentence is None or fact.literal_evidence not in sentence.text:
            continue
        rule_id = fact.mutation_rule_id
        if fact.truth_value:
            statement, expected = fact.literal_evidence, ExpectedKeyRule("key", "true")
        else:
            binding = MutationBinding(
                sentence_id=fact.sentence_id,
                literal_evidence=fact.literal_evidence,
                source_surface=fact.source_surface,
                replacement_surface=fact.replacement_surface,
                source_start_offset=fact.source_start_offset,
                source_end_offset=fact.source_end_offset,
            )
            statement = construct_false_statement(rule_id, binding)
            if statement is None or not verify_false_statement(rule_id, binding, statement):
                continue
            expected = ExpectedKeyRule("rule", f"false:{MUTATION_CATALOG_VERSION}:{rule_id}")
        units.append(
            CertifiedUnit(
                unit_id=fact.fact_id,
                resource_claims=(
                    ResourceClaim("sentence", fact.sentence_id),
                    ResourceClaim("fact_surface", fact.fact_id),
                    ResourceClaim("fact", fact.fact_id),
                ),
                anchor=UnitAnchor("evidence", f"{inventory.source_id}:{fact.sentence_id}"),
                allowed_forms=(statement,),
                expected_key_or_rule=expected,
                citation_plan=(Citation(inventory.source_id, f"sentence:{fact.sentence_id}"),),
                distinctness={
                    "stem": statement,
                    "semantic_target": f"true-false:{fact.fact_id}",
                    "literal_evidence": fact.literal_evidence,
                    "mutation_rule_id": rule_id,
                    "proposition_edge": {
                        "sentence_id": fact.sentence_id,
                        "predicate_start_offset": fact.source_start_offset,
                        "predicate_end_offset": fact.source_end_offset,
                        "truth_value": fact.truth_value,
                    },
                },
            )
        )
    fact_source_ids = tuple(fact.sentence_id for fact in inventory.true_false_facts)
    if units and (
        len(units) != 8
        or len(set(fact_source_ids)) != 8
        or sum(fact.truth_value for fact in inventory.true_false_facts) != 4
        or not _source_diverse(fact_source_ids, maximum_per_source=1)
    ):
        units = []
    normalized_statements = [" ".join(unit.allowed_forms[0].casefold().split()) for unit in units]
    if len(normalized_statements) != len(set(normalized_statements)):
        units = []
    return certify_unit_plan(
        slot_id=slot_id, phase=phase, activity_type="true-false", units=tuple(units)
    )


def _build_match_up(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    sentences = _sentences(inventory)
    units = []
    allowed_relations = {
        "atlas_antonym.v1",
        "atlas_synonym.v1",
        "vesum_degree_positive_comparative.v1",
        "vesum_degree_comparative_superlative.v1",
        "degree-comparison-paraphrase.v1",
        "degree-priority-recommendation.v2",
    }
    for pair in inventory.atlas_pairs:
        sentence = sentences.get(pair.sentence_id)
        if (
            not pair.atlas_pass
            or sentence is None
            or pair.literal_evidence not in sentence.text
            or pair.relation not in allowed_relations
            or not pair.pair_id
            or not pair.left.strip()
            or not pair.right.strip()
        ):
            continue
        degree_role = {
            "vesum_degree_positive_comparative.v1": "degree-positive-comparative",
            "vesum_degree_comparative_superlative.v1": "degree-comparative-superlative",
            "degree-comparison-paraphrase.v1": "degree-positive-comparative",
            "degree-priority-recommendation.v2": "degree-comparative-superlative",
        }.get(pair.relation)
        claim_kind = "degree_pair" if degree_role is not None else "atlas_pair"
        units.append(
            CertifiedUnit(
                unit_id=pair.pair_id,
                resource_claims=(
                    ResourceClaim("sentence", pair.sentence_id),
                    ResourceClaim(claim_kind, pair.pair_id),
                ),
                anchor=UnitAnchor(
                    "kit",
                    f"{'degree' if degree_role is not None else 'atlas'}:{pair.pair_id}",
                ),
                allowed_forms=(pair.left, pair.right),
                expected_key_or_rule=ExpectedKeyRule("key", pair.right),
                citation_plan=(Citation(inventory.source_id, f"sentence:{pair.sentence_id}"),),
                distinctness={
                    "pair": {
                        "left": pair.left,
                        "right": pair.right,
                        "relation": pair.relation,
                    },
                    **({"focus_alignment": degree_role} if degree_role is not None else {}),
                },
                rendering_surface=pair.literal_evidence,
            )
        )
    atlas_source_ids = tuple(
        pair.sentence_id
        for pair in inventory.atlas_pairs
        if pair.relation in {"atlas_antonym.v1", "atlas_synonym.v1"}
    )
    if units and atlas_source_ids and not _source_diverse(atlas_source_ids, maximum_per_source=3):
        units = []
    return certify_unit_plan(slot_id=slot_id, phase=phase, activity_type="match-up", units=units)


def _build_mark_the_words(
    inventory: CertificationInventory, *, slot_id: str, phase: int
) -> UnitPlan:
    sentences = _sentences(inventory)
    sentence_order = tuple(sentence.sentence_id for sentence in inventory.sentences)
    tokens = _token_by_id(sentences)
    for request in inventory.mark_requests:
        indexes = [
            sentence_order.index(sentence_id)
            for sentence_id in request.sentence_ids
            if sentence_id in sentences
        ]
        if len(indexes) != len(request.sentence_ids) or len(set(request.sentence_ids)) < 2:
            continue
        if indexes != list(range(min(indexes), max(indexes) + 1)):
            continue
        degree_criterion = request.criterion == "degree=comparison"
        criterion = None if degree_criterion else vesum_tags.parse_criterion(request.criterion)
        if (not degree_criterion and criterion is None) or len(
            set(request.target_token_ids)
        ) != len(request.target_token_ids):
            continue
        target_records: list[CertifiedTargetToken] = []
        units: list[CertifiedUnit] = []
        for token_id in request.target_token_ids:
            token = tokens.get(token_id)
            if token is None or token.sentence_id not in request.sentence_ids:
                break
            if degree_criterion:
                matches = any(
                    parse.get("pos") == "adj"
                    and any(marker in str(parse.get("raw", "")) for marker in ("compc", "comps"))
                    for parse in token.vesum_parses
                )
            else:
                assert criterion is not None
                matches = any(
                    vesum_tags.matches_criterion(parse, criterion) for parse in token.vesum_parses
                )
            if not matches:
                break
            target = CertifiedTargetToken(
                sentence_id=token.sentence_id,
                token_id=token.token_id,
                start_offset=token.start_offset,
                end_offset=token.end_offset,
                surface=token.surface,
            )
            target_records.append(target)
            units.append(
                CertifiedUnit(
                    unit_id=f"{request.request_id}:{token.token_id}",
                    resource_claims=(ResourceClaim("target_token", token.token_id),),
                    anchor=UnitAnchor("evidence", f"{inventory.source_id}:{token.sentence_id}"),
                    allowed_forms=(token.surface,),
                    expected_key_or_rule=ExpectedKeyRule("rule", request.criterion),
                    citation_plan=(Citation(inventory.source_id, f"sentence:{token.sentence_id}"),),
                    distinctness={
                        "target": {"sentence_id": token.sentence_id, "token_id": token.token_id},
                        **({"focus_alignment": "degree-primary"} if degree_criterion else {}),
                    },
                    rendering_surface=sentences[token.sentence_id].text,
                )
            )
        else:
            return certify_unit_plan(
                slot_id=slot_id,
                phase=phase,
                activity_type="mark-the-words",
                units=units,
                certified_target_tokens=target_records,
            )
    return certify_unit_plan(slot_id=slot_id, phase=phase, activity_type="mark-the-words", units=())


def _short_writing_prompt_markers(
    constraints: tuple[ConstraintSpec, ...],
    *,
    degree_writing: bool = False,
) -> tuple[str, ...] | None:
    """Return the concrete learner-facing markers for the currently built task.

    Constraint registry names certify the plan, but are implementation labels,
    not text a learner should see.  The serializer instead receives complete,
    canonical Ukrainian prompt fragments for each configured constraint.  A
    one-word response already satisfies a minimum of one, so its exact
    learner-facing range is ``до N слів``; higher minima must state both ends.
    Unknown future constraint kinds stay unavailable until they gain an
    equally deterministic learner-facing fragment.
    """

    def verb_noun(minimum: int) -> str:
        remainder = minimum % 100
        if 11 <= remainder <= 14:
            return "дієслів"
        if minimum % 10 == 1:
            return "дієслово"
        if minimum % 10 in {2, 3, 4}:
            return "дієслова"
        return "дієслів"

    markers: list[str] = []
    for spec in constraints:
        if spec.kind == "contains_lemma_set":
            lemmas = spec.params.get("lemmas")
            if (
                set(spec.params) != {"lemmas"}
                or not isinstance(lemmas, (list, tuple))
                or not all(isinstance(lemma, str) and lemma.strip() for lemma in lemmas)
            ):
                return None
            if degree_writing:
                quoted = ", ".join(f"«{lemma}»" for lemma in lemmas)
                markers.append(f"утворіть потрібні форми від прикметників {quoted}")
            else:
                markers.extend(f"«{lemma}»" for lemma in lemmas if isinstance(lemma, str))
            continue
        if spec.kind == "word_count_range":
            minimum = spec.params.get("minimum")
            maximum = spec.params.get("maximum")
            if (
                set(spec.params) != {"minimum", "maximum"}
                or not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or minimum < 1
                or maximum < minimum
            ):
                return None
            markers.append(
                f"до {maximum} слів" if minimum == 1 else f"від {minimum} до {maximum} слів"
            )
            continue
        if spec.kind == "source_proposition":
            text = spec.params.get("text")
            if set(spec.params) != {"text"} or not isinstance(text, str) or not text.strip():
                return None
            markers.append(f"Спирайтеся на цю думку з тексту: «{text}»")
            continue
        if spec.kind == "min_verb_count":
            minimum = spec.params.get("minimum")
            if (
                set(spec.params) != {"minimum"}
                or not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or minimum < 1
            ):
                return None
            markers.append(f"мінімум {minimum} {verb_noun(minimum)}")
            continue
        if spec.kind == "target_case_usage":
            case = spec.params.get("case")
            minimum = spec.params.get("minimum")
            lemmas = spec.params.get("lemmas", ())
            if (
                not {"case", "minimum"} <= set(spec.params)
                or bool(set(spec.params) - {"case", "minimum", "lemmas"})
                or not isinstance(case, str)
                or case not in _UKRAINIAN_CASE_FORMS
                or not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or minimum < 1
                or not isinstance(lemmas, (list, tuple))
                or not all(isinstance(lemma, str) and lemma.strip() for lemma in lemmas)
            ):
                return None
            markers.append(f"мінімум {minimum} слів у {_UKRAINIAN_CASE_FORMS[case]} відмінку")
            markers.extend(f"«{lemma}»" for lemma in lemmas if isinstance(lemma, str))
            continue
        return None
    return tuple(markers) if markers else None


def _build_short_writing(
    inventory: CertificationInventory, *, slot_id: str, phase: int
) -> UnitPlan:
    sentences = _sentences(inventory)
    for task in inventory.writing_tasks:
        sentence = sentences.get(task.sentence_id)
        names = registered_constraint_names(task.constraints)
        degree_writing = task.focus_alignment == "degree-writing"
        prompt_markers = _short_writing_prompt_markers(
            task.constraints,
            degree_writing=degree_writing,
        )
        source_verifiable_constraints = tuple(
            spec for spec in task.constraints if spec.kind != "word_count_range"
        )
        constraints_to_validate = tuple(
            spec
            for spec in source_verifiable_constraints
            if not (degree_writing and spec.kind == "contains_lemma_set")
        )
        constraints_bind_source = (
            validate_constraints(constraints_to_validate, task.sample_tokens)
            if constraints_to_validate
            else degree_writing
        )
        attribute_warrants = dict(task.attribute_warrants)
        prompt_binds_source = task.prompt in sentence.text if sentence is not None else False
        if degree_writing:
            lemma_specs = [spec for spec in task.constraints if spec.kind == "contains_lemma_set"]
            required_lemmas = (
                tuple(lemma_specs[0].params.get("lemmas", ())) if len(lemma_specs) == 1 else ()
            )
            prompt_binds_source = (
                bool(required_lemmas)
                and set(attribute_warrants) == set(required_lemmas)
                and all(
                    isinstance(value, str) and value.strip()
                    for value in attribute_warrants.values()
                )
            )
        if (
            sentence is None
            or not prompt_binds_source
            or names is None
            or prompt_markers is None
            or "word_count_range" not in names
            or not source_verifiable_constraints
            or not constraints_bind_source
        ):
            continue
        unit = CertifiedUnit(
            unit_id=task.task_id,
            resource_claims=(
                ResourceClaim("sentence", task.sentence_id),
                ResourceClaim("writing_task", task.task_id),
            ),
            anchor=UnitAnchor("evidence", f"{inventory.source_id}:{task.sentence_id}"),
            allowed_forms=prompt_markers,
            expected_key_or_rule=ExpectedKeyRule("rule", "short-writing-constraints.v1"),
            citation_plan=(Citation(inventory.source_id, f"sentence:{task.sentence_id}"),),
            distinctness={
                "stem": task.task_id,
                "constraint_specs": [
                    {"kind": spec.kind, "params": dict(spec.params)} for spec in task.constraints
                ],
                **(
                    {"focus_alignment": task.focus_alignment}
                    if task.focus_alignment is not None
                    else {}
                ),
                **({"attribute_warrants": attribute_warrants} if attribute_warrants else {}),
            },
            rendering_surface=task.prompt,
        )
        return certify_unit_plan(
            slot_id=slot_id,
            phase=phase,
            activity_type="short-writing",
            units=(unit,),
            registered_constraints=names,
        )
    return certify_unit_plan(slot_id=slot_id, phase=phase, activity_type="short-writing", units=())


def build_true_false(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    return _closed(
        "true-false",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _build_true_false(inventory, slot_id=slot_id, phase=phase),
    )


def build_match_up(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    return _closed(
        "match-up",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _build_match_up(inventory, slot_id=slot_id, phase=phase),
    )


def build_mark_the_words(
    inventory: CertificationInventory, *, slot_id: str, phase: int
) -> UnitPlan:
    return _closed(
        "mark-the-words",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _build_mark_the_words(inventory, slot_id=slot_id, phase=phase),
    )


def build_short_writing(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    return _closed(
        "short-writing",
        slot_id=slot_id,
        phase=phase,
        build=lambda: _build_short_writing(inventory, slot_id=slot_id, phase=phase),
    )


Builder = Callable[[CertificationInventory], UnitPlan]
BUILDERS: Final[Mapping[str, Callable[..., UnitPlan]]] = MappingProxyType(
    {
        "true-false": build_true_false,
        "quiz": build_quiz,
        "cloze": build_cloze,
        "match-up": build_match_up,
        "fill-in": build_fill_in,
        "error-correction": build_error_correction,
        "text-questions": build_text_questions,
        "mark-the-words": build_mark_the_words,
        "short-writing": build_short_writing,
    }
)
