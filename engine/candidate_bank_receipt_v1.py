"""Content-free receipts for complete deterministic candidate banks.

Receipt construction is intentionally a capture-only hook.  It never mutates
an inventory and normal runtime callers do not invoke it.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from .lesson_profile_45_v1 import lesson_profile_45
from .teacher_ready_density_v3 import floor_for
from .unit_builders_v3 import CertificationInventory

RECEIPT_VERSION: Final = "HramatkaCandidateBankReceipt.v1"
OriginKind = Literal["deterministic-local-inventory"]


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class BankReceiptContext:
    """Authority values supplied by the qualification caller, never by a bank."""

    manifest_digest: str
    source_identity: str
    origin_kind: OriginKind = "deterministic-local-inventory"

    def __post_init__(self) -> None:
        if (
            len(self.manifest_digest) != 64
            or not self.source_identity
            or self.origin_kind != "deterministic-local-inventory"
        ):
            raise ValueError("Bank receipt context has invalid authority metadata.")


@dataclass(frozen=True)
class _CompleteInventorySeal:
    inventory: CertificationInventory
    context: BankReceiptContext
    digest: str


def seal_complete_inventory(
    inventory: CertificationInventory, *, context: BankReceiptContext
) -> _CompleteInventorySeal:
    """Create the opaque receipt capability at the one post-build boundary."""
    members = _members_by_bank(inventory)
    if ("short-writing", 1) not in members:
        raise ValueError("Candidate bank receipt capture requires the short-writing bank.")
    _require_complete_authorized_bank_set(members)
    profile = lesson_profile_45()
    return _CompleteInventorySeal(
        inventory=inventory,
        context=context,
        digest=sha256(
            {
                "manifest_digest": context.manifest_digest,
                "source_identity": context.source_identity,
                "profile_digest": profile.digest,
                "banks": [
                    {"bank_id": f"{kind}:{group}", "candidate_ids": ids}
                    for (kind, group), ids in sorted(members.items())
                ],
            }
        ),
    )


@dataclass(frozen=True)
class CandidateBankReceipt:
    bank_id: str
    activity_type: str
    response_demand_tier: str
    candidate_ids: tuple[str, ...]
    count: int
    candidate_offsets: tuple[tuple[str, int], ...]
    candidate_provenance: tuple[tuple[str, str], ...]
    eligible_placements: tuple[tuple[str, bool], ...]
    origin_commitment: str
    authority_digest: str
    manifest_digest: str
    source_identity: str
    inventory_seal: str

    def __post_init__(self) -> None:
        if self.count != len(self.candidate_ids) or (
            not self.bank_id
            or not self.candidate_ids
            or len(self.candidate_ids) != len(set(self.candidate_ids))
        ):
            raise ValueError(
                "Candidate bank receipt requires one complete unique ordered membership."
            )
        if len(self.candidate_ids) < floor_for(self.activity_type).minimum_units:
            raise ValueError("Candidate bank receipt cannot certify a partial bank.")
        if (
            tuple(candidate_id for candidate_id, _offset in self.candidate_offsets)
            != self.candidate_ids
        ):
            raise ValueError(
                "Candidate bank receipt offsets must cover ordered membership exactly."
            )
        if tuple(offset for _candidate_id, offset in self.candidate_offsets) != tuple(
            range(self.count)
        ):
            raise ValueError("Candidate bank receipt offsets must be canonical and contiguous.")
        if (
            tuple(candidate_id for candidate_id, _digest in self.candidate_provenance)
            != self.candidate_ids
        ):
            raise ValueError(
                "Candidate bank receipt provenance must cover ordered membership exactly."
            )
        if any(len(digest) != 64 for _candidate_id, digest in self.candidate_provenance):
            raise ValueError("Candidate bank receipt provenance must be digest-pinned.")
        if not self.eligible_placements or len(
            {slot for slot, _shared in self.eligible_placements}
        ) != len(self.eligible_placements):
            raise ValueError("Candidate bank receipt placements must be complete and unique.")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": RECEIPT_VERSION,
            "bank_id": self.bank_id,
            "activity_type": self.activity_type,
            "response_demand_tier": self.response_demand_tier,
            "candidate_ids": list(self.candidate_ids),
            "count": self.count,
            "candidate_offsets": [
                {"candidate_id": candidate_id, "unit_offset": offset}
                for candidate_id, offset in self.candidate_offsets
            ],
            "candidate_provenance": [
                {"candidate_id": candidate_id, "commitment": commitment}
                for candidate_id, commitment in self.candidate_provenance
            ],
            "eligible_placements": [
                {"slot_id": slot_id, "mutually_exclusive": shared}
                for slot_id, shared in self.eligible_placements
            ],
            "origin_kind": "deterministic-local-inventory",
            "origin_commitment": self.origin_commitment,
            "authority_digest": self.authority_digest,
            "manifest_digest": self.manifest_digest,
            "source_identity": self.source_identity,
            "inventory_seal": self.inventory_seal,
        }

    @property
    def digest(self) -> str:
        return sha256(self.to_dict())


def _bank_group(identifier: str, activity_type: str) -> int:
    parts = identifier.split(":")
    if len(parts) < 2 or parts[0] != activity_type or not parts[1].isdigit():
        raise ValueError("Deterministic inventory contains an unrecognized bank identifier.")
    group = int(parts[1])
    if group < 1:
        raise ValueError("Deterministic inventory contains a non-positive bank group.")
    return group


def _members_by_bank(
    inventory: CertificationInventory,
) -> Mapping[tuple[str, int], tuple[str, ...]]:
    grouped: dict[tuple[str, int], list[str]] = defaultdict(list)
    for candidate in inventory.candidates:
        key = (
            candidate.activity_type,
            _bank_group(candidate.candidate_id, candidate.activity_type),
        )
        grouped[key].append(candidate.candidate_id)
    for pair in inventory.atlas_pairs:
        grouped[("match-up", _bank_group(pair.pair_id, "match-up"))].append(pair.pair_id)
    for task in inventory.writing_tasks:
        grouped[("short-writing", _bank_group(task.task_id, "short-writing"))].append(task.task_id)
    return {key: tuple(value) for key, value in grouped.items()}


def _require_complete_authorized_bank_set(
    members: Mapping[tuple[str, int], tuple[str, ...]],
) -> None:
    """Require a closed inventory that can cover every 45-minute slot.

    The only intentionally shared bank is ``fill-in:1`` for the two middle
    slots.  A capability is therefore never issued for a subset such as the
    short-writing bank alone, nor for a set missing another active placement.
    """
    profile = lesson_profile_45()
    if ("fill-in", 1) not in members:
        raise ValueError("Candidate bank receipt capture requires the shared fallback bank.")
    covered_slots: set[str] = set()
    for activity_type, group_number in members:
        try:
            placements = profile.placements_for(activity_type, group_number)
        except ValueError as exc:
            raise ValueError("Candidate bank receipt capture has an unauthorized bank.") from exc
        covered_slots.update(slot_id for slot_id, _shared in placements)
    if {slot.slot_id for slot in profile.slots} - covered_slots:
        raise ValueError(
            "Candidate bank receipt capture requires the complete authorized bank set."
        )


def receipts_for_inventory(
    inventory: CertificationInventory,
    *,
    context: BankReceiptContext,
    seal: _CompleteInventorySeal,
) -> tuple[CandidateBankReceipt, ...]:
    """Issue receipts from a complete creation-time inventory before selection."""
    authority = lesson_profile_45()
    members = _members_by_bank(inventory)
    if seal.inventory is not inventory or seal.context != context:
        raise ValueError("Candidate bank receipt capture requires its creation-boundary seal.")
    if ("short-writing", 1) not in members:
        raise ValueError("Candidate bank receipt capture requires the short-writing bank.")
    _require_complete_authorized_bank_set(members)
    rows: list[CandidateBankReceipt] = []
    for (activity_type, group_number), candidate_ids in sorted(members.items()):
        placements = authority.placements_for(activity_type, group_number)
        payload = {
            "origin_kind": context.origin_kind,
            "source_identity": context.source_identity,
            "bank_id": f"{activity_type}:{group_number}",
            "candidate_ids": candidate_ids,
            "placements": placements,
            "authority_digest": authority.digest,
        }
        rows.append(
            CandidateBankReceipt(
                bank_id=f"{activity_type}:{group_number}",
                activity_type=activity_type,
                response_demand_tier=authority.tier_for(activity_type),
                candidate_ids=candidate_ids,
                count=len(candidate_ids),
                candidate_offsets=tuple(
                    (candidate_id, offset) for offset, candidate_id in enumerate(candidate_ids)
                ),
                candidate_provenance=tuple(
                    (
                        candidate_id,
                        sha256(
                            {
                                "bank_id": f"{activity_type}:{group_number}",
                                "candidate_id": candidate_id,
                                "unit_offset": offset,
                                "source_identity": context.source_identity,
                            }
                        ),
                    )
                    for offset, candidate_id in enumerate(candidate_ids)
                ),
                eligible_placements=placements,
                origin_commitment=sha256(payload),
                authority_digest=authority.digest,
                manifest_digest=context.manifest_digest,
                source_identity=context.source_identity,
                inventory_seal=seal.digest,
            )
        )
    if not rows:
        raise ValueError("Candidate bank receipt capture requires a complete non-empty inventory.")
    return tuple(rows)


ReceiptSink = Callable[[tuple[CandidateBankReceipt, ...]], None]
