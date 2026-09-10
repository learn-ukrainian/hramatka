"""Shared visible-sentence segmentation for long lesson passages."""

from __future__ import annotations

import re
from typing import Final

# Keep closing quotation/bracket marks with the sentence-ending punctuation.
# Otherwise a normal quoted final sentence becomes an orphan ``»`` fragment,
# which is not a usable cloze carrier and can make an otherwise admissible
# 350–450-word source fail the every-sentence reconstruction contract.
_SENTENCE_RE: Final = re.compile(r"[^\n.!?…]+[.!?…]?(?:[»”\"')\]]+)?")


def sentence_spans(text: str) -> tuple[str, ...]:
    """Return non-empty visible sentence spans in display order."""
    return tuple(
        match.group(0).strip()
        for match in _SENTENCE_RE.finditer(text.strip())
        if match.group(0).strip()
    )
