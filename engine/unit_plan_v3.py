"""Formal immutable unit-plan IR for the pre-cutover v3 contract layer."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from .teacher_ready_density_v3 import floor_for

_SPACE_RE: Final = re.compile(r"\s+")
_DISTINCTNESS_FIELD: Final[dict[str, str]] = {
    "true-false": "stem",
    "quiz": "stem",
    "cloze": "gap",
    "match-up": "pair",
    "fill-in": "stem",
    "error-correction": "stem",
    "text-questions": "stem",
    "mark-the-words": "target",
    "short-writing": "stem",
}
_TEXT_QUESTION_CATEGORIES: Final = (
    "comprehension",
    "explanation_inference",
    "anchored_application",
)


def _normalized_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Distinctness text must be a string.")
    normalized = _SPACE_RE.sub(" ", unicodedata.normalize("NFC", value).casefold()).strip()
    if not normalized:
        raise ValueError("Distinctness text cannot be blank.")
    return normalized


def _normalized_value(value: object) -> object:
    if isinstance(value, str):
        return _normalized_text(value)
    if isinstance(value, Mapping):
        return {
            _normalized_text(key): _normalized_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_normalized_value(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    raise ValueError(f"Unsupported distinctness value: {type(value).__name__}.")


def _validate_position(value: object, *, kind: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"A {kind} distinctness value needs a source position.")
    sentence_id = _normalized_text(value.get("sentence_id"))
    token_id = _normalized_text(value.get("token_id"))
    if not sentence_id or not token_id:
        raise ValueError(f"A {kind} distinctness value needs sentence_id and token_id.")


def _validate_pair(value: object) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("A match-up distinctness value needs an Atlas pair.")
    _normalized_text(value.get("left"))
    _normalized_text(value.get("right"))


def normalize_distinctness_key(activity_type: str, distinctness: Mapping[str, object]) -> str:
    """Return the stable identity counted toward one v3 activity floor.

    Builders provide a deterministic ``semantic_target`` when multiple surface
    phrasings share one source fact or kit target.  That makes paraphrase
    padding count once even when its learner-facing text is different.
    """
    floor_for(activity_type)
    if not isinstance(distinctness, Mapping):
        raise ValueError("A certified unit needs a distinctness mapping.")
    field = _DISTINCTNESS_FIELD[activity_type]
    value = distinctness.get("semantic_target", distinctness.get(field))
    if value is None:
        raise ValueError(f"A {activity_type} unit needs {field!r} distinctness data.")
    if "semantic_target" not in distinctness:
        if field in {"gap", "target"}:
            _validate_position(value, kind=field)
        elif field == "pair":
            _validate_pair(value)
        else:
            _normalized_text(value)
    canonical = json.dumps(
        _normalized_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    basis = "semantic_target" if "semantic_target" in distinctness else field
    return f"{activity_type}:{basis}:{canonical}"


def _has_text_question_categories(units: Sequence[CertifiedUnit]) -> bool:
    """Require the locked 3+3+2 composition before a plan is certifiable."""
    minima = floor_for("text-questions").category_minima
    assert minima is not None
    required = {
        "comprehension": minima.comprehension,
        "explanation_inference": minima.explanation_inference,
        "anchored_application": minima.anchored_application,
    }
    counts = {category: 0 for category in _TEXT_QUESTION_CATEGORIES}
    for unit in units:
        category = unit.distinctness.get("question_category")
        if not isinstance(category, str) or category not in counts:
            return False
        counts[category] += 1
    return all(counts[category] >= minimum for category, minimum in required.items())


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ResourceClaim:
    """One inventory resource a future exact-cover allocator must reserve."""

    kind: str
    resource_id: str

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.resource_id.strip():
            raise ValueError("Resource claims need a kind and stable resource ID.")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "resource_id": self.resource_id}


@dataclass(frozen=True)
class UnitAnchor:
    """The evidence or deterministic kit anchor for one certified unit."""

    kind: Literal["evidence", "kit"]
    anchor_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"evidence", "kit"} or not self.anchor_id.strip():
            raise ValueError("A unit anchor must be named evidence or kit with a stable ID.")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "anchor_id": self.anchor_id}


@dataclass(frozen=True)
class ExpectedKeyRule:
    """The deterministic answer key or closed grading rule for one unit."""

    kind: Literal["key", "rule"]
    value: str
    certified_error_count: int = 0

    def __post_init__(self) -> None:
        if self.kind not in {"key", "rule"} or not self.value.strip():
            raise ValueError("Expected keys/rules must be closed, named values.")
        if self.certified_error_count < 0:
            raise ValueError("Certified error counts cannot be negative.")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "kind": self.kind,
            "value": self.value,
            "certified_error_count": self.certified_error_count,
        }


@dataclass(frozen=True)
class Citation:
    """One deterministic citation target to carry into the renderer/gate."""

    source_id: str
    locator: str

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.locator.strip():
            raise ValueError("Citations need a stable source ID and locator.")

    def to_dict(self) -> dict[str, str]:
        return {"source_id": self.source_id, "locator": self.locator}


@dataclass(frozen=True)
class CertifiedTargetToken:
    """One closed mark-the-words target, located in its verbatim anchor span."""

    sentence_id: str
    token_id: str
    start_offset: int
    end_offset: int
    surface: str

    def __post_init__(self) -> None:
        if not self.sentence_id.strip() or not self.token_id.strip() or not self.surface.strip():
            raise ValueError(
                "Certified target tokens need sentence, token, and surface identities."
            )
        if self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("Certified target token offsets must be ordered, non-negative bounds.")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "sentence_id": self.sentence_id,
            "token_id": self.token_id,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "surface": self.surface,
        }


@dataclass(frozen=True)
class CertifiedUnit:
    """One immutable, allocatable unit of a v3 activity plan."""

    unit_id: str
    resource_claims: tuple[ResourceClaim, ...]
    anchor: UnitAnchor
    allowed_forms: tuple[str, ...]
    expected_key_or_rule: ExpectedKeyRule
    citation_plan: tuple[Citation, ...]
    distinctness: Mapping[str, object]
    rendering_surface: str | None = None

    def __post_init__(self) -> None:
        if not self.unit_id.strip():
            raise ValueError("Certified units need stable IDs.")
        if not self.resource_claims:
            raise ValueError("Certified units need at least one resource claim.")
        if not self.citation_plan:
            raise ValueError("Certified units need at least one citation.")
        if any(not form.strip() for form in self.allowed_forms):
            raise ValueError("Allowed forms cannot contain blanks.")
        if self.rendering_surface is not None and (
            not isinstance(self.rendering_surface, str) or not self.rendering_surface.strip()
        ):
            raise ValueError("Rendering surfaces must be non-blank strings when present.")
        object.__setattr__(self, "resource_claims", tuple(self.resource_claims))
        object.__setattr__(self, "allowed_forms", tuple(self.allowed_forms))
        object.__setattr__(self, "citation_plan", tuple(self.citation_plan))
        object.__setattr__(self, "distinctness", _freeze(dict(self.distinctness)))

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "resource_claims": [claim.to_dict() for claim in self.resource_claims],
            "anchor": self.anchor.to_dict(),
            "allowed_forms": list(self.allowed_forms),
            "expected_key_or_rule": self.expected_key_or_rule.to_dict(),
            "citation_plan": [citation.to_dict() for citation in self.citation_plan],
            "distinctness": _thaw(self.distinctness),
            "rendering_surface": self.rendering_surface,
        }


@dataclass(frozen=True)
class UnitPlan:
    """A pure builder result: a complete certified plan or ``unavailable``."""

    slot_id: str
    phase: int
    activity_type: str
    disposition: Literal["certified", "unavailable"]
    units: tuple[CertifiedUnit, ...]
    registered_constraints: tuple[str, ...] = ()
    certified_target_tokens: tuple[CertifiedTargetToken, ...] = ()

    def __post_init__(self) -> None:
        floor = floor_for(self.activity_type)
        if not self.slot_id.strip() or self.phase < 1:
            raise ValueError("Unit plans need a stable slot ID and positive phase.")
        if self.disposition not in {"certified", "unavailable"}:
            raise ValueError("Unit plans are certified or unavailable only.")
        object.__setattr__(self, "units", tuple(self.units))
        object.__setattr__(self, "registered_constraints", tuple(self.registered_constraints))
        object.__setattr__(self, "certified_target_tokens", tuple(self.certified_target_tokens))
        if self.disposition == "unavailable":
            if self.units or self.certified_target_tokens:
                raise ValueError("Unavailable plans cannot carry partial certified substrate.")
            return
        if len(self.units) < floor.minimum_units:
            raise ValueError("Certified plans must meet their complete activity floor.")
        unit_ids = [unit.unit_id for unit in self.units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("Certified unit IDs must be distinct.")
        normalized = [
            normalize_distinctness_key(self.activity_type, unit.distinctness) for unit in self.units
        ]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Duplicate or paraphrased units cannot satisfy a v3 floor.")
        if self.activity_type == "text-questions" and not _has_text_question_categories(self.units):
            raise ValueError("Text-question plans must retain the complete 3+3+2 composition.")
        constraints = {_normalized_text(constraint) for constraint in self.registered_constraints}
        if len(constraints) != len(self.registered_constraints):
            raise ValueError("Registered deterministic constraints must be distinct.")
        if len(constraints) < floor.minimum_registered_constraints:
            raise ValueError("The productive-task constraint floor is not met.")
        if floor.certified_errors_per_unit and any(
            unit.expected_key_or_rule.certified_error_count != floor.certified_errors_per_unit
            for unit in self.units
        ):
            raise ValueError("Each error-correction unit needs exactly one certified error.")
        if self.activity_type != "mark-the-words" and self.certified_target_tokens:
            raise ValueError("Only mark-the-words plans may carry certified target tokens.")
        if self.activity_type == "mark-the-words":
            try:
                token_positions = {
                    (token.sentence_id, token.token_id) for token in self.certified_target_tokens
                }
                unit_positions = {
                    (
                        str(unit.distinctness["target"]["sentence_id"]),
                        str(unit.distinctness["target"]["token_id"]),
                    )
                    for unit in self.units
                }
            except (AttributeError, KeyError, TypeError) as exc:
                raise ValueError(
                    "Mark-the-words units need target positions matching their token records."
                ) from exc
            if len(token_positions) != len(self.certified_target_tokens):
                raise ValueError("Certified mark-the-words target tokens must be unique.")
            if len(self.certified_target_tokens) < floor.minimum_units:
                raise ValueError("Mark-the-words plans need their complete certified target list.")
            if token_positions != unit_positions:
                raise ValueError(
                    "Target-token records must match the certified mark-the-words units."
                )

    @property
    def floor_met(self) -> bool:
        return self.disposition == "certified"

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_id": self.slot_id,
            "phase": self.phase,
            "type": self.activity_type,
            "disposition": self.disposition,
            "units": [unit.to_dict() for unit in self.units],
            "registered_constraints": list(self.registered_constraints),
            "certified_target_tokens": [
                target.to_dict() for target in self.certified_target_tokens
            ],
            "floor_met": self.floor_met,
        }


def _is_certifiable(
    activity_type: str,
    units: tuple[CertifiedUnit, ...],
    registered_constraints: tuple[str, ...],
) -> bool:
    floor = floor_for(activity_type)
    if len(units) < floor.minimum_units:
        return False
    if len({unit.unit_id for unit in units}) != len(units):
        return False
    try:
        keys = [normalize_distinctness_key(activity_type, unit.distinctness) for unit in units]
        constraints = [_normalized_text(constraint) for constraint in registered_constraints]
    except ValueError:
        return False
    if len(set(keys)) != len(keys) or len(set(constraints)) != len(constraints):
        return False
    if activity_type == "text-questions" and not _has_text_question_categories(units):
        return False
    if len(constraints) < floor.minimum_registered_constraints:
        return False
    return not floor.certified_errors_per_unit or all(
        unit.expected_key_or_rule.certified_error_count == floor.certified_errors_per_unit
        for unit in units
    )


def certify_unit_plan(
    *,
    slot_id: str,
    phase: int,
    activity_type: str,
    units: Sequence[CertifiedUnit],
    registered_constraints: Sequence[str] = (),
    certified_target_tokens: Sequence[CertifiedTargetToken] = (),
) -> UnitPlan:
    """Return a complete immutable plan or the only allowed alternative: unavailable."""
    floor_for(activity_type)
    candidate_units = tuple(units)
    constraints = tuple(registered_constraints)
    targets = tuple(certified_target_tokens)
    if not _is_certifiable(activity_type, candidate_units, constraints):
        return UnitPlan(
            slot_id=slot_id,
            phase=phase,
            activity_type=activity_type,
            disposition="unavailable",
            units=(),
        )
    try:
        return UnitPlan(
            slot_id=slot_id,
            phase=phase,
            activity_type=activity_type,
            disposition="certified",
            units=candidate_units,
            registered_constraints=constraints,
            certified_target_tokens=targets,
        )
    except (KeyError, TypeError, ValueError):
        return UnitPlan(
            slot_id=slot_id,
            phase=phase,
            activity_type=activity_type,
            disposition="unavailable",
            units=(),
        )
