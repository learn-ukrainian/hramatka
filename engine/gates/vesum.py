"""§6b VESUM token gate — validity of model-INTRODUCED tokens + heritage.

Every content token the model introduces that is NOT a verbatim anchor
substring (a cloze distractor, a match-up gloss, a restated word) is checked
against VESUM. Not found -> FAIL (fabricated / misspelled form). Russianism ->
WARN (never a hard block in the MVP), sourced from the atlas heritage field
when a lookup is provided.

Anchor-verbatim tokens are trusted published text and are NOT re-validated
against VESUM — BUT a verbatim token whose atlas record flags a heritage/
russianism still earns a WARN diagnostic (Sol defect 5 / defect 7): the source
error stays quoted, yet the engine must not badge it as verified-standard.

This module is `engine.gates.vesum` — distinct from `scripts.verification.
vesum` (the DB layer), which it consumes via `verify_words`.
"""

from __future__ import annotations

import re
import unicodedata

from .. import paths
from ..linguistics import verify_words

_WORD_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’]+", re.UNICODE)
_APOSTROPHE_TRANSLATION = str.maketrans({"’": "'", "ʼ": "'"})


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _lookup_form(text: str) -> str:
    """Canonical VESUM lookup form: NFC plus the DB's straight apostrophe."""
    return _nfc(text).translate(_APOSTROPHE_TRANSLATION)


def content_tokens(text: str) -> list[str]:
    """Cyrillic word tokens (len>1), NFC-normalized, order-preserving."""
    return [t for t in _WORD_RE.findall(_nfc(text)) if len(t) > 1]


def _is_anchor_verbatim(token: str, anchor_body_lower: str) -> bool:
    return _lookup_form(token).lower() in anchor_body_lower


def _lookup_matches(tokens: list[str]) -> dict[str, list[dict]]:
    """Batch VESUM lookups under the normalizations users actually emit.

    VESUM's ``word_form`` index is byte-exact.  A form may be stored with an
    initial capital (for example, ``Карпатах``), while models can emit either
    case.  Query both the normalized original and lowercase spelling, then
    retain both result sets for the token verdict.
    """
    lookup_forms = {
        variant
        for token in tokens
        for variant in (_lookup_form(token), _lookup_form(token).lower())
    }
    return verify_words(sorted(lookup_forms), db_path=paths.vesum_db()) if lookup_forms else {}


def _matches_for_token(token: str, results: dict[str, list[dict]]) -> list[dict]:
    """Return all exact-case and lowercase matches for one normalized token."""
    form = _lookup_form(token)
    return [match for key in dict.fromkeys((form, form.lower())) for match in results.get(key, [])]


# Heritage classifications that are NOT a russianism/calque flag.
_OK_HERITAGE = ("", "standard", "native", "ok")


def _heritage_flag(matches: list[dict], atlas_lookup: dict | None) -> str | None:
    """The atlas heritage/russianism classification for a token's lemma(s), or
    None when unflagged/unknown. Keyed by VESUM lemma (lowercased)."""
    if not atlas_lookup:
        return None
    for m in matches:
        rec = atlas_lookup.get(m["lemma"].lower())
        heritage = rec.get("heritage") if rec else None
        if heritage and str(heritage).lower() not in _OK_HERITAGE:
            return heritage
    return None


def check_tokens(
    tokens: list[str],
    anchor_body: str,
    *,
    atlas_lookup: dict | None = None,
) -> list[dict]:
    """Verify each token. Returns a per-token verdict list:
      {token, status: 'pass'|'warn'|'fail', detail}

    - anchor-verbatim, unflagged -> pass (trusted published text)
    - anchor-verbatim, russianism -> warn (quoted source error; NOT verified)
    - introduced, not in VESUM    -> fail (fabricated/misspelled form)
    - introduced, russianism      -> warn (teacher-confirm)
    - introduced, valid           -> pass

    VESUM is consulted for ALL tokens (one batched query) so a verbatim token's
    lemma is available for the heritage lookup; the verbatim verdict still never
    depends on VESUM membership, only on the atlas heritage flag.
    """
    anchor_lower = _lookup_form(anchor_body).lower()
    vesum_results = _lookup_matches(tokens)
    verdicts: list[dict] = []

    for token in tokens:
        matches = _matches_for_token(token, vesum_results)
        heritage = _heritage_flag(matches, atlas_lookup)
        if _is_anchor_verbatim(token, anchor_lower):
            if heritage:
                verdicts.append(
                    {
                        "token": token,
                        "status": "warn",
                        "detail": (
                            f"'{token}' is quoted verbatim from the anchor but atlas flags "
                            f"heritage/russianism='{heritage}' — kept as a source quote, NOT "
                            "engine-verified as standard. Teacher-confirm."
                        ),
                    }
                )
            else:
                verdicts.append(
                    {"token": token, "status": "pass", "detail": "anchor-verbatim (trusted)"}
                )
            continue
        if not matches:
            verdicts.append(
                {
                    "token": token,
                    "status": "fail",
                    "detail": f"'{token}' not found in VESUM — fabricated or misspelled form.",
                }
            )
            continue
        if heritage:
            verdicts.append(
                {
                    "token": token,
                    "status": "warn",
                    "detail": (
                        f"'{token}' flagged heritage/russianism='{heritage}' "
                        "(teacher-confirm)."
                    ),
                }
            )
        else:
            verdicts.append(
                {"token": token, "status": "pass", "detail": f"'{token}' valid in VESUM."}
            )
    return verdicts


def anchor_baseline_diagnostics(
    anchor_body: str,
    *,
    atlas_lookup: dict | None = None,
) -> list[dict]:
    """Return source-baseline warnings without treating the anchor as task text.

    Teacher-pasted and teacher-linked anchors are quotations, not an assertion
    that every form in them is normative Ukrainian.  The task-language gate
    deliberately leaves verbatim source text intact; this companion diagnostic
    makes any form that VESUM cannot verify (or that Atlas marks as heritage /
    russianism) visible as ``flagged-not-verified`` in the private document.

    This is intentionally *not* a GateResult check.  A questionable source
    quote must be visible to the teacher but must not make otherwise B1 task
    wording fail merely because its anchor is C1, dialectal, or contains an
    original-source error.
    """
    tokens = content_tokens(anchor_body)
    matches_by_token = _lookup_matches(tokens)
    diagnostics: list[dict] = []
    seen: set[str] = set()

    for token in tokens:
        key = _lookup_form(token).lower()
        if key in seen:
            continue
        seen.add(key)
        matches = _matches_for_token(token, matches_by_token)
        heritage = _heritage_flag(matches, atlas_lookup)
        if not matches:
            diagnostics.append(
                {
                    "form": token,
                    "status": "flagged-not-verified",
                    "reason": (
                        "Форму в опорному тексті не підтверджено VESUM; це цитата, "
                        "а не перевірена нормативна форма."
                    ),
                }
            )
        elif heritage:
            diagnostics.append(
                {
                    "form": token,
                    "status": "flagged-not-verified",
                    "reason": (
                        "Форму в опорному тексті позначено як "
                        f"{heritage}; це цитата, а не перевірена нормативна форма."
                    ),
                }
            )
    return diagnostics


def worst_status(verdicts: list[dict]) -> str:
    """fail > warn > pass across a verdict list."""
    statuses = {v["status"] for v in verdicts}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"
