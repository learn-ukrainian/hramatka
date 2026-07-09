"""§6b VESUM token gate — validity of model-INTRODUCED tokens.

Every content token the model introduces that is NOT a verbatim anchor
substring (a cloze distractor, a match-up gloss, a restated word) is checked
against VESUM. Not found -> FAIL (fabricated / misspelled form). Anchor-
verbatim tokens are trusted (published text). Russianism -> WARN (never a hard
block in the MVP), sourced from the atlas heritage field when a lookup is
provided.

This module is `engine.gates.vesum` — distinct from `scripts.verification.
vesum` (the DB layer), which it consumes via `verify_words`.
"""

from __future__ import annotations

import re
import unicodedata

from .. import paths
from . import _bootstrap_sys_path

_bootstrap_sys_path()

from scripts.verification.vesum import verify_words

_WORD_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’]+", re.UNICODE)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def content_tokens(text: str) -> list[str]:
    """Cyrillic word tokens (len>1), NFC-normalized, order-preserving."""
    return [t for t in _WORD_RE.findall(_nfc(text)) if len(t) > 1]


def _is_anchor_verbatim(token: str, anchor_body_lower: str) -> bool:
    return token.lower() in anchor_body_lower


def check_tokens(
    tokens: list[str],
    anchor_body: str,
    *,
    atlas_lookup: dict | None = None,
) -> list[dict]:
    """Verify each INTRODUCED token. Returns a per-token verdict list:
      {token, status: 'pass'|'warn'|'fail', detail}

    - anchor-verbatim token  -> pass (trusted published text), no VESUM call
    - not in VESUM           -> fail (fabricated/misspelled form)
    - russianism (atlas)     -> warn
    - otherwise              -> pass
    """
    anchor_lower = _nfc(anchor_body).lower()
    introduced = [t for t in tokens if not _is_anchor_verbatim(t, anchor_lower)]
    verdicts: list[dict] = []

    vesum_results = (
        verify_words([t.lower() for t in introduced], db_path=paths.VESUM_DB)
        if introduced
        else {}
    )

    for token in tokens:
        if _is_anchor_verbatim(token, anchor_lower):
            verdicts.append(
                {"token": token, "status": "pass", "detail": "anchor-verbatim (trusted)"}
            )
            continue
        matches = vesum_results.get(token.lower(), [])
        if not matches:
            verdicts.append(
                {
                    "token": token,
                    "status": "fail",
                    "detail": f"'{token}' not found in VESUM — fabricated or misspelled form.",
                }
            )
            continue
        heritage = None
        if atlas_lookup:
            for m in matches:
                rec = atlas_lookup.get(m["lemma"].lower())
                if rec and rec.get("heritage"):
                    heritage = rec["heritage"]
                    break
        if heritage and str(heritage).lower() not in ("", "standard", "native", "ok"):
            verdicts.append(
                {
                    "token": token,
                    "status": "warn",
                    "detail": f"'{token}' flagged heritage/russianism='{heritage}' (teacher-confirm).",
                }
            )
        else:
            verdicts.append(
                {"token": token, "status": "pass", "detail": f"'{token}' valid in VESUM."}
            )
    return verdicts


def worst_status(verdicts: list[dict]) -> str:
    """fail > warn > pass across a verdict list."""
    statuses = {v["status"] for v in verdicts}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"
