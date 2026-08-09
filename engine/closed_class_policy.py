"""Shared closed-class policy for allocation preflight and validation gates.

This module defines the canonical set of closed-class parts of speech and
the whitelist of allowed gap-eliciting activity shapes for closed-class targets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from . import paths
from .linguistics import verify_words

# Closed-class parts of speech in VESUM: prepositions, particles, conjunctions.
CLOSED_CLASS_POS: Final[frozenset[str]] = frozenset({"prep", "part", "conj"})

# Activity shapes that naturally elicit function words via gaps.
# Closed-class targets MAY ONLY be allocated to these shapes.
CLOSED_CLASS_ALLOWED_SHAPES: Final[frozenset[str]] = frozenset(
    {"quiz", "cloze", "fill-in", "error-correction"}
)


def vesum_matches(form: str, db_path: Path | None = None) -> list[dict]:
    """Return every VESUM analysis for ``form`` across common capitalisations."""
    if db_path is None:
        db_path = paths.vesum_db()
    variants = {form, form.lower()}
    if form:
        variants.add(form[:1].upper() + form[1:])
    results = verify_words(sorted(variants), db_path=db_path)
    matches: list[dict] = []
    for variant in variants:
        matches.extend(results.get(variant, []))
    return matches


def pos_set(form: str, db_path: Path | None = None) -> set[str]:
    """Return the set of VESUM POS tags for ``form``."""
    return {pos for pos in (match.get("pos") for match in vesum_matches(form, db_path)) if pos}


_CONTENT_POS: Final[frozenset[str]] = frozenset({"noun", "verb", "adj", "numr", "adv"})


def is_closed_class_form(form: str, db_path: Path | None = None) -> bool:
    """True if any VESUM parse is closed-class (prep/part/conj) without a content POS."""
    poses = pos_set(form, db_path)
    if not poses:
        return False
    return bool(poses & CLOSED_CLASS_POS) and not bool(poses & _CONTENT_POS)


def target_forms_from(unit_or_candidate: Mapping[str, object]) -> list[str]:
    """Extract candidate answer forms from a certified unit or candidate mapping."""
    forms: list[str] = []
    allowed = unit_or_candidate.get("allowed_forms")
    if isinstance(allowed, Sequence) and not isinstance(allowed, (str, bytes)):
        forms.extend(f for f in allowed if isinstance(f, str))
    expected = unit_or_candidate.get("expected_key")
    if isinstance(expected, str):
        forms.append(expected)
    answer = unit_or_candidate.get("answer")
    if isinstance(answer, str):
        forms.append(answer)
    return forms


def is_closed_class_target(
    unit_or_candidate: Mapping[str, object], db_path: Path | None = None
) -> bool:
    """True if target forms / expected key of a unit or candidate are closed-class."""
    certified_pos = None
    if isinstance(unit_or_candidate, Mapping):
        cert = unit_or_candidate.get("certified_analysis")
        if isinstance(cert, Mapping):
            certified_pos = cert.get("pos")
        elif isinstance(cert, str):
            certified_pos = cert
        if not certified_pos:
            certified_pos = unit_or_candidate.get("pos")
    elif hasattr(unit_or_candidate, "pos"):
        certified_pos = unit_or_candidate.pos
    elif hasattr(unit_or_candidate, "certified_analysis"):
        cert = unit_or_candidate.certified_analysis
        if isinstance(cert, Mapping):
            certified_pos = cert.get("pos")
        elif isinstance(cert, str):
            certified_pos = cert

    if certified_pos and isinstance(certified_pos, str):
        return certified_pos in CLOSED_CLASS_POS

    forms = target_forms_from(unit_or_candidate)
    return any(is_closed_class_form(f, db_path) for f in forms)
