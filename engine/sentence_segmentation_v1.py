"""Shared visible-sentence segmentation for long lesson passages."""

from __future__ import annotations

import re
from typing import Final

_SENTENCE_RE: Final = re.compile(r"[^\n.!?…]+[.!?…]?")


def sentence_spans(text: str) -> tuple[str, ...]:
    """Return non-empty visible sentence spans in display order."""
    return tuple(
        match.group(0).strip()
        for match in _SENTENCE_RE.finditer(text.strip())
        if match.group(0).strip()
    )
