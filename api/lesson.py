"""Trusted API-owned lesson metadata and fixture-template materialization."""

from __future__ import annotations

import copy
import hashlib
import uuid
from typing import TYPE_CHECKING, Any

from .store import now_iso

if TYPE_CHECKING:
    from .store import JobRecord


# These budgets deliberately mirror the frozen TTT plan in the demo and the
# engine adapter. Blocks beyond a phase budget are still in ``lesson.blocks``:
# the review UI renders them as reserve/homework instead of discarding them.
REVIEW_PHASE_BUDGETS: dict[int, dict[int, int]] = {
    45: {1: 2, 2: 3, 3: 1},
    60: {1: 3, 2: 4, 3: 2},
    90: {1: 4, 2: 5, 3: 3},
}
TEACHER_REMOVAL_REASON = "вилучено вчителем"
RESTORED_WARNING_NOTE = "повернено з відхилених — погляньте ще раз"
EDITED_WARNING_NOTE = "змінено вчителем — підтвердьте ще раз перед прийняттям"


def materialize_lesson(template: dict[str, Any], job: JobRecord) -> dict[str, Any]:
    """Bind an engine template to its durable job without changing block content."""
    lesson = copy.deepcopy(template)
    lesson.pop("anchor_diagnostics", None)  # engine-out artifact only, not wire lesson
    _normalize_rejected_entries(lesson)
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


def _normalize_rejected_entries(lesson: dict[str, Any]) -> None:
    """Convert engine-template rejection markers into frozen lesson entries."""
    normalized = []
    for entry in lesson.get("rejected", []):
        activity = entry.get("activity") or {}
        activity_type = activity.get("type")
        entry_type = activity_type if entry.get("type") == "gate-failed" else entry.get("type")
        normalized.append({"type": entry_type, "activity": activity, "reason": entry["reason"]})
    lesson["rejected"] = normalized


def split_review_blocks(
    lesson: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the ordered visible and reserve blocks for the selected duration."""
    try:
        budgets = REVIEW_PHASE_BUDGETS[lesson["duration"]]
        blocks = lesson["blocks"]
    except (KeyError, TypeError) as error:
        raise ValueError("Lesson has no valid review duration or blocks.") from error
    if not isinstance(blocks, list):
        raise ValueError("Lesson blocks must be a list.")

    seen = {1: 0, 2: 0, 3: 0}
    visible: list[dict[str, Any]] = []
    reserve: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("phase") not in seen:
            raise ValueError("Lesson contains an invalid review block.")
        phase = block["phase"]
        if seen[phase] < budgets[phase]:
            visible.append(block)
            seen[phase] += 1
        else:
            reserve.append(block)
    return visible, reserve


def move_block(lesson: dict[str, Any], block_id: str, direction: str) -> None:
    """Perform the demo's one-step up/down move without changing content."""
    blocks = _require_blocks(lesson)
    index = _block_index(blocks, block_id)
    delta = -1 if direction == "up" else 1 if direction == "down" else 0
    if delta == 0:
        raise ValueError("Unsupported block move direction.")
    block = blocks[index]
    neighbor_index = index + delta
    if 0 <= neighbor_index < len(blocks) and blocks[neighbor_index].get("phase") == block.get(
        "phase"
    ):
        blocks[index], blocks[neighbor_index] = blocks[neighbor_index], block
        return
    target_phase = block.get("phase", 0) + delta
    if target_phase not in {1, 2, 3}:
        raise ValueError("Block cannot move beyond the lesson phases.")
    block["phase"] = target_phase


def remove_block_to_rejected(lesson: dict[str, Any], block_id: str) -> None:
    """Move a visible/reserve block into the frozen rejected-tray shape."""
    blocks = _require_blocks(lesson)
    removed = blocks.pop(_block_index(blocks, block_id))
    rejected = lesson.get("rejected")
    if not isinstance(rejected, list):
        raise ValueError("Lesson rejected tray must be a list.")
    rejected.append(
        {
            "type": removed["type"],
            "activity": copy.deepcopy(removed["activity"]),
            "reason": TEACHER_REMOVAL_REASON,
        }
    )


def include_reserve_block(lesson: dict[str, Any], block_id: str) -> None:
    """Move a reserve block to the front of its phase's visible plan."""
    blocks = _require_blocks(lesson)
    _, reserve = split_review_blocks(lesson)
    reserve_ids = {block.get("id") for block in reserve}
    if block_id not in reserve_ids:
        raise ValueError("Block is not currently in reserve.")
    index = _block_index(blocks, block_id)
    block = blocks.pop(index)
    phase = block["phase"]
    blocks.insert(_phase_start_index(blocks, phase), block)


def restore_rejected_entry(lesson: dict[str, Any], rejected_index: int, phase: int) -> str:
    """Restore one frozen rejected draft as a fresh warning block."""
    rejected = lesson.get("rejected")
    if not isinstance(rejected, list) or not 0 <= rejected_index < len(rejected):
        raise IndexError("Rejected entry not found.")
    entry = rejected.pop(rejected_index)
    if not isinstance(entry, dict) or not isinstance(entry.get("activity"), dict):
        raise ValueError("Rejected entry is invalid.")
    activity = copy.deepcopy(entry["activity"])
    block_id = f"restored-{uuid.uuid4().hex}"
    blocks = _require_blocks(lesson)
    blocks.insert(
        _phase_start_index(blocks, phase),
        {
            "id": block_id,
            "phase": phase,
            "type": activity["type"],
            "mode": "письмово",
            "activity": activity,
            "answer_key": copy.deepcopy(activity["answer_key"]),
            "mark": "warn",
            "note": RESTORED_WARNING_NOTE,
            "edited": False,
            "provenance": {
                "source": "teacher",
                "generator": "teacher-restore",
                "gates": [],
                "external_options": False,
            },
        },
    )
    return block_id


def replace_block_activity(
    lesson: dict[str, Any], block_id: str, replacement: dict[str, Any]
) -> bool:
    """Replace only activity content and report whether a warning was edited."""
    try:
        activity_type = replacement["type"]
        answer_key = replacement["answer_key"]
    except KeyError as error:
        raise ValueError("Replacement activity is incomplete.") from error
    blocks = _require_blocks(lesson)
    block = blocks[_block_index(blocks, block_id)]
    block["type"] = activity_type
    block["activity"] = copy.deepcopy(replacement)
    block["answer_key"] = copy.deepcopy(answer_key)
    block["edited"] = True
    is_warning = block.get("mark") == "warn"
    if is_warning:
        # A teacher edit is never an acknowledgement of a warning.
        block["note"] = EDITED_WARNING_NOTE
    return is_warning


def select_duration(lesson: dict[str, Any], duration: int) -> None:
    """Change only the rendering budget; all block documents stay durable."""
    if duration not in REVIEW_PHASE_BUDGETS:
        raise ValueError("Unsupported lesson duration.")
    lesson["duration"] = duration


def _require_blocks(lesson: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = lesson.get("blocks")
    if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
        raise ValueError("Lesson blocks must be a list of objects.")
    return blocks


def _block_index(blocks: list[dict[str, Any]], block_id: str) -> int:
    for index, block in enumerate(blocks):
        if block.get("id") == block_id:
            return index
    raise KeyError(block_id)


def _phase_start_index(blocks: list[dict[str, Any]], phase: int) -> int:
    for index, block in enumerate(blocks):
        if block["phase"] == phase:
            return index
    lower_phase_indexes = [index for index, block in enumerate(blocks) if block["phase"] < phase]
    return lower_phase_indexes[-1] + 1 if lower_phase_indexes else 0


def anchor_fingerprint(anchor: str) -> str:
    """A content-only, stable anchor identity required by the public schema."""
    return hashlib.sha256(anchor.encode("utf-8")).hexdigest()


def title_from_anchor(anchor: str) -> str:
    """Use the first sentence, cut at a word boundary to the contract's 52 chars."""
    first_sentence = anchor.split(".", maxsplit=1)[0].strip() or "Урок української мови"
    if len(first_sentence) <= 52:
        return first_sentence
    cut = first_sentence[:52].rsplit(" ", maxsplit=1)[0].strip()
    return cut or first_sentence[:52]
