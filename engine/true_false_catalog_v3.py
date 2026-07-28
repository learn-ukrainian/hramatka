"""Closed, literal-evidence-bound false-statement mutations for v3."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

MUTATION_CATALOG_VERSION: Final = "true-false-mutations.v1"
_WORD_CHAR: Final = r"А-Яа-яІіЇїЄєҐґ'’"


@dataclass(frozen=True)
class MutationBinding:
    """The exact literal evidence and one closed substitution input."""

    sentence_id: str
    literal_evidence: str
    source_surface: str
    replacement_surface: str

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
        )
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
