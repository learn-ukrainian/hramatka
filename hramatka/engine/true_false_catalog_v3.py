"""Closed, literal-evidence-bound false-statement mutations for v3."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

MUTATION_CATALOG_VERSION: Final = "true-false-mutations.v2"
_WORD_CHAR: Final = r"А-Яа-яІіЇїЄєҐґ'’"


@dataclass(frozen=True)
class MutationBinding:
    """The exact literal evidence and one closed substitution input."""

    sentence_id: str
    literal_evidence: str
    source_surface: str
    replacement_surface: str
    source_start_offset: int | None = None
    source_end_offset: int | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.sentence_id,
                self.literal_evidence,
                self.source_surface,
                self.replacement_surface,
            )
        ):
            raise ValueError("A mutation binding needs literal evidence and non-blank surfaces.")
        if (self.source_start_offset is None) != (self.source_end_offset is None):
            raise ValueError("Mutation offsets must be supplied together.")
        if self.source_start_offset is not None and (
            self.source_start_offset < 0
            or self.source_end_offset is None
            or self.source_end_offset <= self.source_start_offset
            or self.literal_evidence[self.source_start_offset : self.source_end_offset]
            != self.source_surface
        ):
            raise ValueError("Mutation offsets must bind the exact source surface.")


MutationConstructor = Callable[[MutationBinding], str | None]


def _surface_matches(text: str, surface: str) -> list[re.Match[str]]:
    return list(
        re.finditer(
            rf"(?<![{_WORD_CHAR}]){re.escape(surface)}(?![{_WORD_CHAR}])",
            text,
        )
    )


def _replace_one_surface(binding: MutationBinding) -> str | None:
    """Replace exactly one literal token; no paraphrase or free-form text exists."""
    if binding.source_surface == binding.replacement_surface:
        return None
    matches = _surface_matches(binding.literal_evidence, binding.source_surface)
    if len(matches) != 1:
        return None
    match = matches[0]
    return (
        binding.literal_evidence[: match.start()]
        + binding.replacement_surface
        + binding.literal_evidence[match.end() :]
    )


def _negate_asserted_predicate(binding: MutationBinding) -> str | None:
    """Insert scoped ``не`` at one certified finite-predicate offset.

    The inventory, not this constructor, proves assertedness and scope.  This
    closed constructor only performs the exact reversible edit.  At sentence
    start it transfers capitalization from the predicate to ``Не`` so the
    result remains ordinary Ukrainian rather than ``не Ночуватимуть``.
    """
    start = binding.source_start_offset
    end = binding.source_end_offset
    if start is None or end is None:
        return None
    source = binding.literal_evidence
    predicate = source[start:end]
    if not predicate or source[max(0, start - 3) : start].casefold().strip().endswith("не"):
        return None
    prefix = source[:start]
    if predicate[:1].isupper():
        predicate = predicate[:1].lower() + predicate[1:]
        last_alpha = max(
            (index for index, character in enumerate(prefix) if character.isalpha()),
            default=-1,
        )
        last_clause_boundary = max(
            (prefix.rfind(boundary) for boundary in (".", "!", "?", ":", ";", "\n", "—", "–")),
            default=-1,
        )
        clause_initial = last_alpha == -1 or last_alpha < last_clause_boundary
        negation = "Не" if clause_initial else "не"
        return prefix + negation + " " + predicate + source[end:]
    return prefix + "не " + predicate + source[end:]


@dataclass(frozen=True)
class MutationRule:
    """A versioned deterministic constructor and its literal verifier."""

    rule_id: str
    constructor: MutationConstructor

    def construct(self, binding: MutationBinding) -> str | None:
        return self.constructor(binding)

    def verify(self, binding: MutationBinding, statement: str) -> bool:
        expected = self.construct(binding)
        return (
            expected is not None and statement == expected and statement != binding.literal_evidence
        )


MUTATION_CATALOG: Final[Mapping[str, MutationRule]] = MappingProxyType(
    {
        "replace-one-surface.v1": MutationRule(
            rule_id="replace-one-surface.v1", constructor=_replace_one_surface
        ),
        "negate-asserted-predicate.v1": MutationRule(
            rule_id="negate-asserted-predicate.v1", constructor=_negate_asserted_predicate
        ),
    }
)


def construct_false_statement(rule_id: str, binding: MutationBinding) -> str | None:
    """Build only a catalogued false statement; unknown rules fail closed."""
    rule = MUTATION_CATALOG.get(rule_id)
    return None if rule is None else rule.construct(binding)


def verify_false_statement(rule_id: str, binding: MutationBinding, statement: object) -> bool:
    """Verify an exact catalogued output against the original evidence literal."""
    rule = MUTATION_CATALOG.get(rule_id)
    return isinstance(statement, str) and rule is not None and rule.verify(binding, statement)
