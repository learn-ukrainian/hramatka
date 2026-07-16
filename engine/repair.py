"""Bounded post-selection slot repair planning.

This module deliberately contains no provider or API policy.  It turns the
whole-lesson selector outcome into exact immutable prompt-pack slot contracts;
the adapter owns invoking those contracts through the normal pipeline.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from . import content_density, schema

MAX_ROUNDS = 2
MAX_SLOT_ATTEMPTS = 2
MAX_CALLS = 6
MAX_WALL_SECONDS = 20 * 60
HARD_DEADLINE_GUARD_SECONDS = 8 * 60
_hard_deadline: ContextVar[float | None] = ContextVar("hramatka_repair_hard_deadline", default=None)
_MAX_REPAIR_VALUE_CHARS = 80
_ITEM_LOCATOR = re.compile(r"^(items|pairs|blanks)\[(\d+)\]$")
_OBSERVED_VALUE_FIELDS = (
    "statement",
    "sentence",
    "left",
    "right",
    "question",
    "prompt",
    "text",
    "answer",
    "correction",
)


def enabled() -> bool:
    """Return the explicit, default-off repair switch."""
    return os.environ.get("HRAMATKA_SLOT_REPAIR", "").strip().lower() in {"1", "true", "yes", "on"}


def set_hard_deadline(deadline: float | None):
    """Install the runner deadline without exposing repair internals to the API."""
    return _hard_deadline.set(deadline)


def reset_hard_deadline(token) -> None:
    _hard_deadline.reset(token)


def hard_deadline() -> float | None:
    return _hard_deadline.get()


def _bounded_text(value: object) -> str:
    """Return a one-field repair hint without serializing a candidate body."""
    if value is None:
        return "<missing>"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return re.sub(r"\s+", " ", str(value)).strip()[:_MAX_REPAIR_VALUE_CHARS] or "<empty>"
    return f"<{type(value).__name__}>"


def _value_at_locator(raw_candidate: Mapping[str, Any], locator: str | None) -> object:
    """Read only the field identified by a stable gate locator."""
    if locator is None:
        return raw_candidate.get("type")
    if locator in raw_candidate:
        return raw_candidate[locator]
    match = _ITEM_LOCATOR.match(locator)
    if match is None:
        return None
    collection, index_text = match.groups()
    rows = raw_candidate.get(collection)
    index = int(index_text)
    if not isinstance(rows, list) or index >= len(rows):
        return None
    return rows[index]


def _bounded_observed(raw_candidate: Mapping[str, Any] | None, locator: str | None) -> str:
    """Produce a bounded offending value, never a raw activity serialization."""
    if raw_candidate is None:
        return "<missing>"
    value = _value_at_locator(raw_candidate, locator)
    if isinstance(value, Mapping):
        # Item and pair locators identify an object.  Expose one relevant scalar
        # only; evidence and the rest of the candidate stay outside the prompt.
        for field in _OBSERVED_VALUE_FIELDS:
            if field in value:
                return _bounded_text(value[field])
        return "<object>"
    return _bounded_text(value)


@dataclass(frozen=True)
class RepairSlot:
    slot_id: str
    phase: int
    activity_type: str
    focus_required: bool = False


@dataclass(frozen=True)
class RepairRequest:
    phase: int
    round: int
    slots: tuple[RepairSlot, ...]


def bounded_gate_failures(
    activities: Sequence[schema.HramatkaActivity],
) -> dict[str, list[dict[str, Any]]]:
    """Stable repair diagnostics: codes and bounded fields, never raw prose."""
    failures: dict[str, list[dict[str, Any]]] = {}
    for activity in activities:
        activity_type = str(activity.activity.get("type") or "unknown")
        for check in activity.gate_result.checks:
            if check.status != "fail":
                continue
            detail: dict[str, Any] = {"gate": str(check.gate), "field": check.locator or "activity"}
            # Gate code, locator, and pass/fail status are stable protocol
            # fields.  The observed value is scoped to that locator and bounded;
            # never carry validator prose or a dead candidate into the prompt.
            detail["observed"] = _bounded_observed(activity.raw_candidate, check.locator)
            detail["expected"] = f"{check.gate}: pass"[:_MAX_REPAIR_VALUE_CHARS]
            failures.setdefault(activity_type, []).append(detail)
            break
    return failures


@dataclass
class RepairPlanner:
    """Plan at most one exact-slot request per phase per round."""

    shared_pack: Mapping[str, Any]
    started_at: float = field(default_factory=time.monotonic)
    hard_deadline: float | None = None
    calls: int = 0
    attempts: Counter[str] = field(default_factory=Counter)

    def _expired(self, now: float) -> bool:
        if now - self.started_at >= MAX_WALL_SECONDS:
            return True
        return (
            self.hard_deadline is not None
            and now >= self.hard_deadline - HARD_DEADLINE_GUARD_SECONDS
        )

    def _slots_for_phase(self, phase: int) -> list[RepairSlot]:
        focus = self.shared_pack.get("lesson_plan", {}).get("focus", {})
        focus_required = focus.get("status") == "supported"
        rows = [
            row
            for row in self.shared_pack.get("slots", [])
            if str(row.get("slot_id", "")).startswith(f"P{phase}-")
        ]
        return [
            RepairSlot(str(row["slot_id"]), phase, str(row["type"]), focus_required)
            for row in rows
        ]

    def plan(
        self,
        *,
        round: int,
        selected_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]],
        slots_by_phase: Mapping[int, int],
        now: float | None = None,
    ) -> list[RepairRequest]:
        if round > MAX_ROUNDS or self.calls >= MAX_CALLS:
            return []
        now = time.monotonic() if now is None else now
        if self._expired(now):
            return []
        requests: list[RepairRequest] = []
        for phase, visible_slots in sorted(slots_by_phase.items()):
            missing = max(0, int(visible_slots) - len(selected_by_phase.get(phase, ())))
            if not missing:
                continue
            candidates = [
                slot
                for slot in self._slots_for_phase(phase)
                if self.attempts[slot.slot_id] < MAX_SLOT_ATTEMPTS
            ]
            candidates.sort(
                key=lambda slot: (not slot.focus_required, slot.activity_type, slot.slot_id)
            )
            chosen = tuple(candidates[:missing])
            if chosen:
                requests.append(RepairRequest(phase=phase, round=round, slots=chosen))
        return requests[: max(0, MAX_CALLS - self.calls)]

    def scheduled(self, request: RepairRequest) -> None:
        self.calls += 1
        for slot in request.slots:
            self.attempts[slot.slot_id] += 1


def floor_ready(
    selected: Sequence[schema.HramatkaActivity],
    selected_by_phase: Mapping[int, Sequence[schema.HramatkaActivity]],
    *,
    duration: int,
    planned_blocks: int,
) -> bool:
    return len(selected) >= planned_blocks and content_density.meets_lesson_floor(
        list(selected), duration=duration, selected_by_phase=selected_by_phase
    )


def merge_matchup_pairs(
    preserved: Sequence[Mapping[str, Any]], additions: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Merge a private two-pair remainder with disjoint answer forms.

    The caller must send the merged raw activity through ``gate_activity``;
    this helper never makes a partial board selectable.
    """
    merged: list[dict[str, Any]] = []
    answers: set[str] = set()
    for pair in [*preserved, *additions]:
        right = pair.get("right") if isinstance(pair, Mapping) else None
        if not isinstance(right, str):
            continue
        answer = re.sub(r"\s+", " ", unicodedata.normalize("NFC", right).casefold()).strip()
        if not answer or answer in answers:
            continue
        answers.add(answer)
        merged.append(dict(pair))
    return merged
