"""Latin JSON-schema vocabulary must never reach teacher-visible task text.

Bake-off 2026-07-10 (#54): both engines verbalised the response schema into the
learner-facing instruction — «правдивими (True), чи хибними (False)». The
generators read `"correct": true` next to `"instruction": "…"` and narrated the
boolean. That breaks the B1 immersion policy (#M-13): the learner sees English
metadata in a Ukrainian task.

The VESUM token gate cannot catch this. Its tokenizer (`gates.vesum.
content_tokens`) matches Cyrillic only, so a Latin word is not merely valid —
it is *invisible*. This module is the missing deterministic check.

Scope — the model's OWN teacher-visible prose. Two things are deliberately not
leakage:

* **Anchor-derived Latin.** A schema word that occurs in the teacher's own
  source text is a quotation, not a leak. This mirrors `gates.vesum.
  external_tokens`: anchor-verbatim content is trusted published text.
* **Machine contract fields.** `type` and mark-the-words `criteria`
  (`pos=verb`) are Latin by design and never rendered as task prose, so callers
  simply do not submit them.

The top-level `instruction` is likewise not checked here: `instruction_bank`
already *repairs* it during projection by overwriting it with the canonical
Ukrainian string, so failing the activity would drop good items over a defect
that deterministically self-heals. This gate covers what the bank does not
touch — per-item prose.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# JSON boolean literals plus the response-schema key vocabulary. Deliberately
# excludes words that carry a legitimate non-schema sense in teacher-visible
# text: `left`/`right` (a match-up gloss may be an English hint by policy),
# `text`, `gap` (the `{gap}` cloze marker), and `type`/`criteria` (machine
# fields). Membership must stay narrow enough that a hit is unambiguous.
SCHEMA_TOKENS: frozenset[str] = frozenset(
    {
        "true",
        "false",
        "correct",
        "answer",
        "options",
        "statement",
        "evidence",
        "instruction",
        "question",
        "explanation",
        "items",
        "pairs",
        "blanks",
        "criteria",
    }
)

_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
# Structural placeholders the templates require verbatim; their Latin is ours,
# not the model's narration of the schema.
_PLACEHOLDER_RE = re.compile(r"\{gap\d*\}|\{\{\d+\}\}|\{answer\}")


def _anchor_latin_words(anchor_body: str) -> set[str]:
    """Latin words the teacher's own source text already contains."""
    return {word.casefold() for word in _LATIN_WORD_RE.findall(anchor_body or "")}


def find_schema_tokens(text: object, anchor_body: str = "") -> list[str]:
    """Return the schema words ``text`` leaks, lowercased and de-duplicated.

    A token is leakage only when the model introduced it: Latin that occurs in
    ``anchor_body`` is a source quotation and never reported.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    stripped = _PLACEHOLDER_RE.sub(" ", text)
    from_anchor = _anchor_latin_words(anchor_body)
    leaked = {
        word.casefold()
        for word in _LATIN_WORD_RE.findall(stripped)
        if word.casefold() in SCHEMA_TOKENS and word.casefold() not in from_anchor
    }
    return sorted(leaked)


def check_strings(strings: Iterable[object], anchor_body: str = "") -> dict:
    """Fail when any teacher-visible string narrates the response schema."""
    leaked = sorted({token for text in strings for token in find_schema_tokens(text, anchor_body)})
    if not leaked:
        return {"status": "pass", "detail": "No schema vocabulary in teacher-visible text."}
    sample = ", ".join(f"«{token}»" for token in leaked)
    return {
        "status": "fail",
        "detail": (
            f"Teacher-visible text contains response-schema vocabulary ({sample}) — "
            "instructions and items must be Ukrainian only (B1 immersion); "
            "use «правильно»/«неправильно» (П/Н), never true/false."
        ),
    }
