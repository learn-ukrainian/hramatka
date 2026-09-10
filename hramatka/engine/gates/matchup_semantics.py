"""Conservative semantic grounding for MatchUp pairs.

VESUM establishes that a proposed gloss is Ukrainian morphology; it cannot say
that the gloss means the left-hand term.  Atlas synonym data is the available
offline semantic evidence in the slice-1 runtime bundle.  A pair is clean only
when its left/right lemmas are connected by that evidence.  A valid but
unrelated (or merely unproven definition-style) gloss is therefore a WARN, not
a clean pass: it ships for a teacher to inspect, but cannot be auto-accepted.

The gate also carries a narrow set of calibration-backed dictionary
refutations and rejects form-level pairings that are deterministically unsafe
for a synonym exercise.  VESUM supplies lemma and part-of-speech evidence; it
does not expose roots, so prefixed-verb detection uses exact known-prefix
segmentation rather than claiming a VESUM root analysis.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

from .. import paths
from ..linguistics import verify_words

_WORD_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’-]+", re.UNICODE)
_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*$")
_CONTENT_POS = {"noun", "verb", "adj", "adv", "numr"}
# Removing one of these prefixes must produce the other fully VESUM-resolved
# verb lemma before the derivation veto applies.
_VERB_PREFIXES = (
    "пере",
    "недо",
    "від",
    "над",
    "під",
    "при",
    "про",
    "роз",
    "ви",
    "до",
    "за",
    "на",
    "об",
    "по",
    "уз",
    "в",
    "з",
    "о",
    "с",
    "у",
)
# These dictionary distinctions were independently calibrated as non-equivalent
# B1 pairs. They are explicit negative evidence, unlike an absent Atlas link.
# Source: AGY MatchUp calibration review, 2026-07-12.
_CALIBRATED_REFUTATIONS = {
    frozenset(("начинка", "фарш")): "hypernym/hyponym culinary mismatch",
    frozenset(("година", "час")): "translation-bridge mismatch",
    frozenset(("володіти", "мати")): "degree/government mismatch",
}


def _normalise(text: str) -> str:
    """Comparable Atlas phrase form (case-insensitive, editorial notes gone)."""
    text = unicodedata.normalize("NFC", text).casefold().strip()
    text = _PARENTHETICAL_RE.sub("", text)
    return " ".join(text.split())


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text)
    return [token for token in _WORD_RE.findall(text) if len(token) > 1]


def _lemma_pos(tokens: Iterable[str]) -> dict[str, set[str]]:
    """Resolve content-word lemmas and their VESUM parts of speech."""
    surfaces = sorted({token.casefold() for token in tokens})
    if not surfaces:
        return {}
    rows_by_surface = verify_words(surfaces, db_path=paths.vesum_db())
    resolved: dict[str, set[str]] = {}
    for rows in rows_by_surface.values():
        for row in rows:
            lemma = row.get("lemma")
            pos = row.get("pos")
            if pos not in _CONTENT_POS or not isinstance(lemma, str):
                continue
            resolved.setdefault(lemma.casefold(), set()).add(pos)
    return resolved


def _lemmas(tokens: Iterable[str]) -> set[str]:
    """Resolve content-word lemmas through the pinned VESUM adapter."""
    return set(_lemma_pos(tokens))


def _known_prefix_verb_pair(
    left_lemma_pos: Mapping[str, set[str]], right_lemma_pos: Mapping[str, set[str]]
) -> tuple[str, str] | None:
    """Return ``(prefixed, base)`` for an exact known-prefix verb form pair."""
    for prefixed_lemma, prefixed_pos in left_lemma_pos.items():
        if "verb" not in prefixed_pos:
            continue
        for base_lemma, base_pos in right_lemma_pos.items():
            if "verb" not in base_pos:
                continue
            if any(prefixed_lemma == prefix + base_lemma for prefix in _VERB_PREFIXES):
                return prefixed_lemma, base_lemma
    for prefixed_lemma, prefixed_pos in right_lemma_pos.items():
        if "verb" not in prefixed_pos:
            continue
        for base_lemma, base_pos in left_lemma_pos.items():
            if "verb" not in base_pos:
                continue
            if any(prefixed_lemma == prefix + base_lemma for prefix in _VERB_PREFIXES):
                return prefixed_lemma, base_lemma
    return None


def _calibrated_refutation(
    left_lemmas: Iterable[str], right_lemmas: Iterable[str]
) -> tuple[str, str, str] | None:
    """Return explicit dictionary refutation evidence for a resolved lemma pair."""
    for left_lemma in left_lemmas:
        for right_lemma in right_lemmas:
            reason = _CALIBRATED_REFUTATIONS.get(frozenset((left_lemma, right_lemma)))
            if reason is not None:
                return left_lemma, right_lemma, reason
    return None


def _record_synonyms(record: Mapping) -> list[str]:
    synonyms = record.get("synonyms")
    if not isinstance(synonyms, list):
        return []
    return [synonym for synonym in synonyms if isinstance(synonym, str) and _normalise(synonym)]


def _canonical_left_keys(
    left: str, left_lemmas: set[str], atlas_lookup: Mapping[str, Mapping]
) -> set[str]:
    """Atlas records normally key on a VESUM lemma.  The literal-key fallback
    keeps a compact fixture or a caller-supplied canonical left term usable if
    its particular surface form is absent from the local VESUM subset.
    """
    keys = {lemma for lemma in left_lemmas if lemma in atlas_lookup}
    literal = _normalise(left)
    if literal in atlas_lookup:
        keys.add(literal)
    return keys


def _synonym_relation(
    *,
    left: str,
    right: str,
    left_lemmas: set[str],
    right_lemmas: set[str],
    atlas_lookup: Mapping[str, Mapping],
) -> tuple[str, str] | None:
    """Return ``(atlas_key, synonym)`` when either direction has synonym evidence.

    The primary direction is ``left → right``.  The reverse direction also
    accepts an Atlas record for a right-hand term that explicitly lists the
    left, which matters when the compact grounding lookup contains only that
    record.  Full-phrase equality is preferred; lemma overlap supports an
    inflected synonym embedded in a short definition phrase.
    """
    left_keys = _canonical_left_keys(left, left_lemmas, atlas_lookup)
    right_phrase = _normalise(right)
    left_phrase = _normalise(left)

    for key in left_keys:
        for synonym in _record_synonyms(atlas_lookup[key]):
            synonym_phrase = _normalise(synonym)
            if synonym_phrase == right_phrase:
                return key, synonym
            synonym_lemmas = _lemmas(_tokens(synonym))
            if synonym_lemmas and synonym_lemmas <= right_lemmas:
                return key, synonym

    # Atlas can expose the related term in the reverse direction.  Do not use
    # arbitrary right records: only VESUM-resolved right lemmas are eligible.
    for key in right_lemmas:
        record = atlas_lookup.get(key)
        if not isinstance(record, Mapping):
            continue
        for synonym in _record_synonyms(record):
            synonym_phrase = _normalise(synonym)
            if synonym_phrase == left_phrase:
                return key, synonym
            synonym_lemmas = _lemmas(_tokens(synonym))
            if synonym_lemmas and synonym_lemmas <= left_lemmas:
                return key, synonym
    return None


def check_pair(left: str, right: str, *, atlas_lookup: Mapping[str, Mapping] | None) -> dict:
    """Check whether Atlas evidence relates a MatchUp pair.

    ``status == 'pass'`` means Atlas synonym evidence connects the sides.  A
    ``'fail'`` is reserved for calibrated negative evidence and deterministic
    duplicate or known-prefix verb-form shapes. A ``'warn'`` means the
    right text might be a valid Ukrainian definition, but its semantic relation
    is not evidenced by the pinned data and must be teacher-confirmed. Lexical
    invalidity remains the VESUM token gate's responsibility; this gate must
    never describe word validity as meaning.
    """
    lookup = atlas_lookup or {}
    left_normalised = _normalise(left)
    right_normalised = _normalise(right)
    if left_normalised == right_normalised:
        return {
            "status": "fail",
            "detail": (
                f"MatchUp sides normalize to the same text: {left!r} ↔ {right!r}; "
                "a pair must teach a distinct equivalent or definition."
            ),
        }

    left_lemma_pos = _lemma_pos(_tokens(left))
    right_lemma_pos = _lemma_pos(_tokens(right))
    refutation = _calibrated_refutation(left_lemma_pos, right_lemma_pos)
    if refutation is not None:
        refuted_left, refuted_right, reason = refutation
        return {
            "status": "fail",
            "detail": (
                f"MatchUp pair is rejected by calibrated dictionary evidence: {reason} "
                f"({refuted_left!r} ↔ {refuted_right!r})."
            ),
        }

    prefixed_pair = _known_prefix_verb_pair(left_lemma_pos, right_lemma_pos)
    if prefixed_pair is not None:
        prefixed, base = prefixed_pair
        return {
            "status": "fail",
            "detail": (
                "MatchUp pair is a known-prefix verb form "
                f"({prefixed!r} → {base!r}), not a synonym-equivalence exercise."
            ),
        }

    left_lemmas = set(left_lemma_pos)
    right_lemmas = set(right_lemma_pos)
    relation = _synonym_relation(
        left=left,
        right=right,
        left_lemmas=left_lemmas,
        right_lemmas=right_lemmas,
        atlas_lookup=lookup,
    )
    if relation is not None:
        atlas_key, synonym = relation
        return {
            "status": "pass",
            "detail": (
                f"MatchUp semantic relation verified by Atlas synonym {synonym!r} "
                f"for {atlas_key!r}."
            ),
        }

    left_keys = _canonical_left_keys(left, left_lemmas, lookup)
    if not left_keys:
        reason = "Atlas has no relation record for the left-hand term"
    else:
        reason = "Atlas lists no synonym relation between the pair sides"
    return {
        "status": "warn",
        "detail": (
            f"{reason}: {left!r} ↔ {right!r}. VESUM-valid wording is not semantic "
            "proof; teacher-confirm this MatchUp pair."
        ),
    }
