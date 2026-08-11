"""Closed deterministic short-writing constraints for the isolated v3 preflight."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from .gates.vesum_tags import CASE_CODES

CONSTRAINT_REGISTRY_VERSION: Final = "short-writing-constraints.v1"
_WORD_RE: Final = re.compile(r"[А-Яа-яІіЇїЄєҐґ'’]+")


@dataclass(frozen=True)
class VesumToken:
    """A token plus the VESUM parses supplied by deterministic canonicalization."""

    surface: str
    start_offset: int
    end_offset: int
    parses: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if (
            not self.surface.strip()
            or self.start_offset < 0
            or self.end_offset <= self.start_offset
        ):
            raise ValueError("VESUM tokens need a surface and ordered offsets.")
        object.__setattr__(self, "parses", tuple(self.parses))


@dataclass(frozen=True)
class ConstraintSpec:
    """One declarative instance of a named closed constraint implementation."""

    kind: str
    params: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.kind.strip() or not isinstance(self.params, Mapping):
            raise ValueError("Constraint specifications need a known name and parameters.")


ConstraintValidator = Callable[[ConstraintSpec, Sequence[VesumToken]], bool]


def _normalised_lemmas(value: object) -> set[str] | None:
    if not isinstance(value, Sequence) or isinstance(value, (bytes, str)):
        return None
    lemmas = {item.casefold().strip() for item in value if isinstance(item, str) and item.strip()}
    return lemmas or None


def _known_lemma_set(tokens: Sequence[VesumToken]) -> set[str]:
    return {
        str(parse["lemma"]).casefold()
        for token in tokens
        for parse in token.parses
        if isinstance(parse.get("lemma"), str) and parse["lemma"].strip()
    }


def _contains_lemma_set(spec: ConstraintSpec, tokens: Sequence[VesumToken]) -> bool:
    if set(spec.params) != {"lemmas"}:
        return False
    expected = _normalised_lemmas(spec.params.get("lemmas"))
    return expected is not None and expected <= _known_lemma_set(tokens)


def _min_verb_count(spec: ConstraintSpec, tokens: Sequence[VesumToken]) -> bool:
    if set(spec.params) != {"minimum"}:
        return False
    minimum = spec.params.get("minimum")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        return False
    return (
        sum(any(parse.get("pos") == "verb" for parse in token.parses) for token in tokens)
        >= minimum
    )


def _target_case_usage(spec: ConstraintSpec, tokens: Sequence[VesumToken]) -> bool:
    allowed = {"case", "minimum", "lemmas"}
    if not {"case", "minimum"} <= set(spec.params) or set(spec.params) - allowed:
        return False
    case = spec.params.get("case")
    minimum = spec.params.get("minimum")
    lemmas = _normalised_lemmas(spec.params["lemmas"]) if "lemmas" in spec.params else None
    if (
        not isinstance(case, str)
        or case not in CASE_CODES.values()
        or not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or minimum < 1
        or ("lemmas" in spec.params and lemmas is None)
    ):
        return False
    return (
        sum(
            any(
                parse.get("case") == case
                and (lemmas is None or str(parse.get("lemma", "")).casefold() in lemmas)
                for parse in token.parses
            )
            for token in tokens
        )
        >= minimum
    )


def _word_count_range(spec: ConstraintSpec, tokens: Sequence[VesumToken]) -> bool:
    if set(spec.params) != {"minimum", "maximum"}:
        return False
    minimum, maximum = spec.params.get("minimum"), spec.params.get("maximum")
    if (
        not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or minimum < 1
        or maximum < minimum
    ):
        return False
    word_count = len(_WORD_RE.findall(" ".join(token.surface for token in tokens)))
    return minimum <= word_count <= maximum


def _source_proposition(spec: ConstraintSpec, tokens: Sequence[VesumToken]) -> bool:
    """Bind a communicative writing task to one exact visible source claim."""
    if set(spec.params) != {"text"}:
        return False
    text = spec.params.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    proposition_words = tuple(word.casefold() for word in _WORD_RE.findall(text))
    token_words = tuple(token.surface.casefold() for token in tokens)
    return len(proposition_words) >= 4 and proposition_words == token_words


CONSTRAINT_REGISTRY: Final[Mapping[str, ConstraintValidator]] = MappingProxyType(
    {
        "contains_lemma_set": _contains_lemma_set,
        "min_verb_count": _min_verb_count,
        "source_proposition": _source_proposition,
        "target_case_usage": _target_case_usage,
        "word_count_range": _word_count_range,
    }
)


def validate_constraints(specs: Sequence[ConstraintSpec], tokens: Sequence[VesumToken]) -> bool:
    """Evaluate only registered regex/VESUM rules; unknown names fail closed."""
    return bool(specs) and all(
        (validator := CONSTRAINT_REGISTRY.get(spec.kind)) is not None and validator(spec, tokens)
        for spec in specs
    )


def registered_constraint_names(specs: Sequence[ConstraintSpec]) -> tuple[str, ...] | None:
    """Return canonical names only when all configurations are closed and valid."""
    names = tuple(spec.kind for spec in specs)
    if len(names) != len(set(names)) or not names:
        return None
    return names if all(name in CONSTRAINT_REGISTRY for name in names) else None
