"""Immutable authority for the existing 45-minute v3 lesson profile.

This module deliberately owns scheduling policy only.  The density module
remains the only authority for floors and phase shape.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from .lesson_capacity_v3 import LessonSlot
from .teacher_ready_density_v3 import density_floor_fingerprint, floor_for, phase_shape_for

PROFILE_VERSION: Final = "Hramatka45MinuteProfile.v1"
ResponseDemandTier = Literal[
    "selected-response",
    "bounded-production",
    "source-grounded-open-response",
    "extended-writing",
]

_TIER_BY_TYPE: Final[Mapping[str, ResponseDemandTier]] = MappingProxyType(
    {
        "quiz": "selected-response",
        "match-up": "selected-response",
        "cloze": "bounded-production",
        "fill-in": "bounded-production",
        "error-correction": "bounded-production",
        "text-questions": "source-grounded-open-response",
        "short-writing": "extended-writing",
    }
)
_SCHEDULED_TYPES: Final = (
    "quiz",
    "cloze",
    "match-up",
    "error-correction",
    "text-questions",
    "short-writing",
)
_REPLACEMENTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"match-up": ("fill-in",), "error-correction": ("fill-in",)}
)


@dataclass(frozen=True)
class Profile45:
    """The behavior-preserving 45-minute schedule and its canonical bytes."""

    slots: tuple[LessonSlot, ...]
    occurrence_by_slot_type: Mapping[tuple[str, str], int]

    def tier_for(self, activity_type: str) -> ResponseDemandTier:
        try:
            return _TIER_BY_TYPE[activity_type]
        except KeyError as exc:
            raise ValueError("45-minute profile has an unknown activity type.") from exc

    def group_number_for(self, slot_id: str, activity_type: str) -> int:
        try:
            return self.occurrence_by_slot_type[(slot_id, activity_type)]
        except KeyError as exc:
            raise ValueError("45-minute profile has an unknown slot activity.") from exc

    def fallback_to_group_one(self, slot_id: str, activity_type: str) -> bool:
        return (
            activity_type == "fill-in"
            and self.group_number_for(slot_id, activity_type) > 1
            and slot_id in {"P2-A1", "P2-A2"}
        )

    def placements_for(self, activity_type: str, group_number: int) -> tuple[tuple[str, bool], ...]:
        if activity_type == "fill-in" and group_number == 1:
            return (("P2-A1", True), ("P2-A2", True))
        rows = tuple(
            slot_id
            for (slot_id, kind), occurrence in self.occurrence_by_slot_type.items()
            if kind == activity_type and occurrence == group_number
        )
        if len(rows) != 1:
            raise ValueError("45-minute profile has no placement for this bank.")
        return ((rows[0], False),)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "version": PROFILE_VERSION,
            "density_floor_fingerprint": density_floor_fingerprint(),
            "slots": [
                {
                    "slot_id": slot.slot_id,
                    "phase": slot.phase,
                    "requested_type": slot.requested_type,
                    "replacement_types": list(slot.replacement_types),
                    "requested_tier": self.tier_for(slot.requested_type),
                    "replacement_tiers": [self.tier_for(item) for item in slot.replacement_types],
                }
                for slot in self.slots
            ],
            "occurrences": [
                {"slot_id": slot_id, "activity_type": activity_type, "group_number": number}
                for (slot_id, activity_type), number in sorted(self.occurrence_by_slot_type.items())
            ],
            "shared_fallback": {
                "activity_type": "fill-in",
                "group_number": 1,
                "placements": ["P2-A1", "P2-A2"],
                "mutually_exclusive": True,
            },
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _build_profile() -> Profile45:
    shape = phase_shape_for(45)
    scheduled = iter(_SCHEDULED_TYPES)
    slots = tuple(
        LessonSlot(
            slot_id=f"P{phase}-A{position}",
            phase=phase,
            requested_type=(activity_type := next(scheduled)),
            replacement_types=_REPLACEMENTS.get(activity_type, ()),
        )
        for phase, count in sorted(shape.phase_slots.items())
        for position in range(1, count + 1)
    )
    if next(scheduled, None) is not None:  # pragma: no cover - static guard
        raise RuntimeError("45-minute profile does not consume its schedule.")
    occurrences: Counter[str] = Counter()
    mapping: dict[tuple[str, str], int] = {}
    for slot in slots:
        occurrences[slot.requested_type] += 1
        mapping[(slot.slot_id, slot.requested_type)] = occurrences[slot.requested_type]
    for slot in slots:
        for activity_type in slot.replacement_types:
            occurrences[activity_type] += 1
            mapping[(slot.slot_id, activity_type)] = occurrences[activity_type]
    for slot in slots:
        floor_for(slot.requested_type)
        for activity_type in slot.replacement_types:
            floor_for(activity_type)
    return Profile45(slots, MappingProxyType(mapping))


PROFILE_45: Final = _build_profile()


def lesson_profile_45() -> Profile45:
    """Return the sole immutable 45-minute scheduling authority."""
    return PROFILE_45


def inventory_candidate_types_45() -> tuple[str, ...]:
    return tuple(slot.requested_type for slot in PROFILE_45.slots)


def inventory_replacement_types_45() -> tuple[str, ...]:
    return tuple(kind for slot in PROFILE_45.slots for kind in slot.replacement_types)


def slot_builders_45() -> Mapping[str, Callable[..., object]]:
    """Return pure builders without importing the API adapter."""
    from .anchor_inventory_v3 import inventory_for_group
    from .unit_builders_v3 import BUILDERS

    def builder_for(activity_type: str) -> Callable[..., object]:
        def build(inventory: object, *, slot_id: str, phase: int) -> object:
            group = PROFILE_45.group_number_for(slot_id, activity_type)
            selected = inventory_for_group(
                inventory, activity_type=activity_type, group_number=group
            )
            plan = BUILDERS[activity_type](selected, slot_id=slot_id, phase=phase)
            if PROFILE_45.fallback_to_group_one(slot_id, activity_type) and not plan.floor_met:
                selected = inventory_for_group(
                    inventory, activity_type=activity_type, group_number=1
                )
                plan = BUILDERS[activity_type](selected, slot_id=slot_id, phase=phase)
            return plan

        return build

    return {activity_type: builder_for(activity_type) for activity_type in BUILDERS}
