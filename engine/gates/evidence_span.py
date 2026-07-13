"""§6a evidence-span gate — the backbone of "anchor-derived" (span-REPAIR).

Model-emitted char offsets are UNRELIABLE (fleet P0: trusting them mass-fails
6a). So we IGNORE the model's offsets entirely and RECOMPUTE them by locating
the quote in the anchor:

  1. NFC-normalize both anchor and quote.
  2. Exact substring search -> recompute char_start/end.
  3. Whitespace-tolerant search (collapse quote whitespace runs to `\\s+`) ->
     recompute char_start/end from the real match position.
  4. Genuinely ABSENT from the anchor -> for EXTRACTIVE types this is a FAIL
     (hallucinated item, must not reach teacher review), not a warn.

Returns a per-item verdict dict:
  {status: 'pass'|'fail', kind: 'literal'|'absent', char_start, char_end, detail}
"""

from __future__ import annotations

import re
import unicodedata


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _ws_tolerant_pattern(quote: str) -> re.Pattern:
    parts = [re.escape(p) for p in quote.split()]
    return re.compile(r"\s+".join(parts))


def locate_quote(quote: str, anchor_body: str) -> tuple[int, int] | None:
    """Return (char_start, char_end) of `quote` in `anchor_body` (NFC),
    recomputed from the real match — exact first, then whitespace-tolerant.
    None if the quote is genuinely absent.
    """
    anchor = _nfc(anchor_body)
    q = _nfc(quote).strip()
    if not q:
        return None
    idx = anchor.find(q)
    if idx >= 0:
        return idx, idx + len(q)
    pattern = _ws_tolerant_pattern(q)
    if pattern.pattern:
        m = pattern.search(anchor)
        if m:
            return m.start(), m.end()
    return None


def check_evidence(quote: str, anchor_body: str, *, extractive: bool = True) -> dict:
    """Span-repair verdict for one evidence quote (slice-1 §6a)."""
    if not isinstance(quote, str) or not quote.strip():
        return {
            "status": "fail",
            "kind": "absent",
            "char_start": None,
            "char_end": None,
            "detail": "Empty or missing evidence quote — cannot anchor this item.",
        }
    span = locate_quote(quote, anchor_body)
    if span is not None:
        start, end = span
        return {
            "status": "pass",
            "kind": "literal",
            "char_start": start,
            "char_end": end,
            "detail": f"Quote located in anchor at [{start}:{end}] (offsets recomputed).",
        }
    # Absent.
    if extractive:
        return {
            "status": "fail",
            "kind": "absent",
            "char_start": None,
            "char_end": None,
            "detail": (
                "Evidence quote not found in the anchor — hallucinated item for an "
                "extractive type; must not reach teacher review."
            ),
        }
    return {
        "status": "warn",
        "kind": "inference",
        "char_start": None,
        "char_end": None,
        "detail": "Evidence quote not literal; allowed only for inferential types.",
    }


_WORD_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’-]+", re.UNICODE)
_APOSTROPHE_TRANSLATION = str.maketrans({"’": "'", "ʼ": "'"})


def _normalize_token(t: str) -> str:
    return _nfc(t).translate(_APOSTROPHE_TRANSLATION).casefold()


def _tokenize(text: str) -> list[str]:
    """Cyrillic word tokens (including hyphens), casefolded and normalized."""
    if not isinstance(text, str):
        return []
    return [_normalize_token(t) for t in _WORD_RE.findall(_nfc(text))]


def check_option_in_quote(option: str, quote: str) -> dict:
    """Verify that the option is supported by the quote at token boundaries."""
    if not isinstance(option, str) or not option.strip():
        return {
            "status": "fail",
            "detail": "Empty or missing option — cannot match.",
        }
    if not isinstance(quote, str) or not quote.strip():
        return {
            "status": "fail",
            "detail": "Empty or missing quote — cannot match.",
        }

    opt_tokens = _tokenize(option)
    quote_tokens = _tokenize(quote)

    if not opt_tokens:
        return {
            "status": "fail",
            "detail": f"Option '{option}' contains no Cyrillic word tokens.",
        }

    n = len(opt_tokens)
    m = len(quote_tokens)
    for i in range(m - n + 1):
        if quote_tokens[i : i + n] == opt_tokens:
            return {
                "status": "pass",
                "detail": f"Option '{option}' matches the quote at token boundaries.",
            }

    return {
        "status": "fail",
        "detail": f"Option '{option}' not found in the quote at token boundaries.",
    }
