"""Pure per-type TeacherReadyDensity.v3 certification builders.

This pre-cutover module consumes only canonical anchor/kit inventories and
returns a complete immutable ``UnitPlan`` or ``unavailable``.  It neither calls
models nor imports the production planner, prompt pack, pipeline, or API.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

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


@dataclass(frozen=True)
class TrueFalseFact:
    fact_id: str
    sentence_id: str
    literal_evidence: str
    source_surface: str
    replacement_surface: str
    truth_value: bool


@dataclass(frozen=True)
class AtlasPassPair:
    pair_id: str
    sentence_id: str
    left: str
    right: str
    literal_evidence: str
    atlas_pass: bool


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


def _has_one_certified_error(candidate: EvidenceCandidate) -> bool:
    if candidate.derived_surface is None or candidate.derived_surface == candidate.literal_evidence:
        return False
    evidence_tokens = _TOKEN_RE.findall(candidate.literal_evidence)
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
    if candidate.activity_type == "cloze":
        distinctness = {
            "gap": {"sentence_id": candidate.sentence_id, "token_id": candidate.token_id},
            "semantic_target": candidate.semantic_target,
        }
    else:
        distinctness = {
            "stem": candidate.candidate_id,
            "semantic_target": candidate.semantic_target,
        }
        if candidate.activity_type == "text-questions":
            distinctness["question_category"] = candidate.category
    return CertifiedUnit(
        unit_id=candidate.candidate_id,
        resource_claims=(
            ResourceClaim("sentence", candidate.sentence_id),
            ResourceClaim("candidate", candidate.candidate_id),
        ),
        anchor=UnitAnchor("evidence", f"{inventory.source_id}:{candidate.sentence_id}"),
        allowed_forms=(candidate.derived_surface or candidate.expected_key, candidate.expected_key)
        if error_count
        else (candidate.expected_key,),
        expected_key_or_rule=ExpectedKeyRule(
            "rule" if error_count else "key", candidate.expected_key, error_count
        ),
        citation_plan=(Citation(inventory.source_id, f"sentence:{candidate.sentence_id}"),),
        distinctness=distinctness,
        rendering_surface=(
            candidate.literal_evidence if candidate.activity_type == "fill-in" else None
        ),
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
        and (activity_type != "error-correction" or _has_one_certified_error(candidate))
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
        rule_id = "replace-one-surface.v1"
        if fact.truth_value:
            statement, expected = fact.literal_evidence, ExpectedKeyRule("key", "true")
        else:
            binding = MutationBinding(
                sentence_id=fact.sentence_id,
                literal_evidence=fact.literal_evidence,
                source_surface=fact.source_surface,
                replacement_surface=fact.replacement_surface,
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
                    "mutation_rule_id": rule_id if not fact.truth_value else "identity.evidence.v1",
                },
            )
        )
    return certify_unit_plan(
        slot_id=slot_id, phase=phase, activity_type="true-false", units=tuple(units)
    )


def _build_match_up(inventory: CertificationInventory, *, slot_id: str, phase: int) -> UnitPlan:
    sentences = _sentences(inventory)
    units = []
    for pair in inventory.atlas_pairs:
        sentence = sentences.get(pair.sentence_id)
        if (
            not pair.atlas_pass
            or sentence is None
            or pair.literal_evidence not in sentence.text
            or not pair.pair_id
            or not pair.left.strip()
            or not pair.right.strip()
        ):
            continue
        units.append(
            CertifiedUnit(
                unit_id=pair.pair_id,
                resource_claims=(ResourceClaim("atlas_pair", pair.pair_id),),
                anchor=UnitAnchor("kit", f"atlas:{pair.pair_id}"),
                allowed_forms=(pair.left, pair.right),
                expected_key_or_rule=ExpectedKeyRule("key", pair.right),
                citation_plan=(Citation(inventory.source_id, f"sentence:{pair.sentence_id}"),),
                distinctness={"pair": {"left": pair.left, "right": pair.right}},
            )
        )
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
        criterion = vesum_tags.parse_criterion(request.criterion)
        if criterion is None or len(set(request.target_token_ids)) != len(request.target_token_ids):
            continue
        target_records: list[CertifiedTargetToken] = []
        units: list[CertifiedUnit] = []
        for token_id in request.target_token_ids:
            token = tokens.get(token_id)
            if token is None or token.sentence_id not in request.sentence_ids:
                break
            if not any(
                vesum_tags.matches_criterion(parse, criterion) for parse in token.vesum_parses
            ):
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
                        "target": {"sentence_id": token.sentence_id, "token_id": token.token_id}
                    },
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
            if set(spec.params) != {"lemmas"} or not isinstance(lemmas, (list, tuple)) or not all(
                isinstance(lemma, str) and lemma.strip() for lemma in lemmas
            ):
                return None
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
                f"до {maximum} слів"
                if minimum == 1
                else f"від {minimum} до {maximum} слів"
            )
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
            markers.append(
                f"мінімум {minimum} слів у {_UKRAINIAN_CASE_FORMS[case]} відмінку"
            )
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
        prompt_markers = _short_writing_prompt_markers(task.constraints)
        if (
            sentence is None
            or task.prompt not in sentence.text
            or names is None
            or prompt_markers is None
            or "word_count_range" not in names
            or not validate_constraints(task.constraints, task.sample_tokens)
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
            },
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
