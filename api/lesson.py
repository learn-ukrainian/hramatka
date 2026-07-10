"""Trusted API-owned lesson metadata and fixture-template materialization."""

from __future__ import annotations

import copy
from typing import Any

from .store import JobRecord, now_iso


def materialize_lesson(template: dict[str, Any], job: JobRecord) -> dict[str, Any]:
    """Bind an engine template to its durable job without changing block content."""
    lesson = copy.deepcopy(template)
    timestamp = now_iso()
    lesson.update(
        {
            "schema": "lu.lesson.v1",
            "id": job.id,
            "title": title_from_anchor(job.anchor_text),
            "level": "B1",
            "method": "ttt",
            "focus": job.focus,
            "anchor": {
                "text": job.anchor_text,
                "source": job.anchor_source,
                "chars": len(job.anchor_text),
            },
            "duration": job.duration,
            "version": 1,
            "status": "ready",
            "last_error": None,
            "accepted": False,
            "created_at": job.created_at,
            "updated_at": timestamp,
        }
    )
    planned = {45: 6, 60: 9, 90: 12}[job.duration]
    if len(lesson["blocks"]) < planned:
        lesson["rejected"] = [
            *lesson.get("rejected", []),
            {
                "type": "shortfall",
                "activity": {},
                "reason": f"складено {len(lesson['blocks'])} із {planned} — додайте текст",
            },
        ]
    return lesson


def title_from_anchor(anchor: str) -> str:
    """Use the first sentence, cut at a word boundary to the contract's 52 chars."""
    first_sentence = anchor.split(".", maxsplit=1)[0].strip() or "Урок української мови"
    if len(first_sentence) <= 52:
        return first_sentence
    cut = first_sentence[:52].rsplit(" ", maxsplit=1)[0].strip()
    return cut or first_sentence[:52]
