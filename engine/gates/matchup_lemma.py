"""Match-up left sides must be the CITATION FORM of an anchor word.

Bake-off 2026-07-10 (#54 item 2): left sides arrived inflected — «привітною»,
«розкачайте», «спливуть». A match-up board teaches a lexical equivalence, so the
left side belongs in the form a dictionary lists (nominative singular /
masculine nominative singular / infinitive), not in whatever form the sentence
happened to use.

This gate replaces a surface-verbatim grounding test with a lemma-level one,
because the two requirements are otherwise mutually exclusive: an anchor
containing «привітною» can be quoted verbatim OR cited in its lemma form
«привітний», never both. Warning on each would leave the generator no winning
move and would park every match-up in the review tray.

Grounding is therefore: does ANY surface form of the left side's lemma occur in
the anchor? That accepts «привітний» for an anchor that says «привітною», still
rejects a left side invented from nowhere, and lets the citation-form check be a
separate, honest signal.

Every verdict is warn-or-pass. A single-word left that VESUM cannot resolve
WARNS: the VESUM token gate runs only on the RIGHT side of match-up pairs, so
this gate is the only grounding check the left side gets — a fabricated left
must not sail through with a green detail (cross-family review of PR #193,
blocker #1).
"""

from __future__ import annotations

import re

from .. import paths
from ..linguistics import verify_lemma
from . import vesum_tags
from .vesum import is_anchor_verbatim

_WORD_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’-]+", re.UNICODE)
_PASS = {"status": "pass", "detail": "Left side is the citation form of an anchor word."}
_NOT_JUDGED = {"status": "pass", "detail": "Left side not judged (phrase or empty)."}


def _single_word(left: str) -> str | None:
    """The one Cyrillic word in ``left``, or None for a phrase/empty side."""
    tokens = _WORD_RE.findall(left or "")
    return tokens[0] if len(tokens) == 1 else None


def _lemma_forms_in_anchor(lemmas: set[str], anchor_body: str, db_path) -> bool:
    """Whether any inflected form of any candidate lemma occurs in the anchor."""
    for lemma in lemmas:
        for row in verify_lemma(lemma, db_path=db_path):
            form = row.get("word_form")
            if isinstance(form, str) and is_anchor_verbatim(form, anchor_body):
                return True
    return False


def check_left_side(left: str, anchor_body: str, *, db_path=None) -> dict:
    """Warn when a match-up left side is inflected, unresolvable, or ungrounded.

    A multi-word or empty left side returns a neutral "not judged" pass — this
    gate only speaks where it has evidence. A single word VESUM cannot resolve
    WARNS: nothing else judges the left side, so silence would green-light a
    fabricated word.
    """
    word = _single_word(left)
    if word is None:
        return _NOT_JUDGED
    resolved_db = db_path if db_path is not None else paths.vesum_db()
    parses = vesum_tags.parse_word(word, db_path=resolved_db)
    if not parses:
        # The VESUM token gate covers only the RIGHT side of match-up pairs,
        # so an unresolvable left gets no second look anywhere else.
        return {
            "status": "warn",
            "detail": (
                f"Left word '{word}' has no VESUM parse — "
                "verify grounding before accepting."
            ),
        }

    lemmas = {
        str(parse["lemma"]).casefold()
        for parse in parses
        if isinstance(parse.get("lemma"), str)
    }
    if not lemmas:
        return {
            "status": "warn",
            "detail": (
                f"Left word '{word}' has no usable VESUM lemma — "
                "verify grounding before accepting."
            ),
        }

    grounded = is_anchor_verbatim(word, anchor_body) or _lemma_forms_in_anchor(
        lemmas, anchor_body, resolved_db
    )
    if not grounded:
        return {
            "status": "warn",
            "detail": (
                f"Left word '{word}' is not an anchor word in any form — "
                "verify grounding before accepting."
            ),
        }
    if word.casefold() not in lemmas:
        citation = ", ".join(f"'{lemma}'" for lemma in sorted(lemmas))
        return {
            "status": "warn",
            "detail": (
                f"Left word '{word}' is an inflected form; a match-up left side must be "
                f"the citation form ({citation}). Teacher-confirm or relemmatize."
            ),
        }
    return _PASS
