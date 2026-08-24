"""Strict, non-production certificate grammar for Hramatka #552."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

VERSION: Final = "HramatkaProspectiveCertificate.v1"
COMPOSITE_RULE_VERSION: Final = "HramatkaProspectiveCompositeRule.v1.1"
COMPOSITE_RULE_DIGEST: Final = "6a5b74d17f84345058977e2adfb389a793c4810faad2d8637f43d454c5a8aa70"
PROFILE_DIGEST: Final = "18886d4c219c4394521b8ca300046c51cacd18c41c620597574b6718d1947599"
DENSITY_FLOOR_DIGEST: Final = "a1e7943c8f5768a3376b082be4798600ae2aac4f86f8be671effb10667d56f21"
_SHA = re.compile(r"^[a-f0-9]{64}$")
_BANK_PLACEMENTS: Final = {
    "quiz:1": (("P1-A1", False),),
    "cloze:1": (("P1-A2", False),),
    "match-up:1": (("P2-A1", False),),
    "error-correction:1": (("P2-A2", False),),
    "text-questions:1": (("P2-A3", False),),
    "short-writing:1": (("P3-A1", False),),
    "fill-in:1": (("P2-A1", True), ("P2-A2", True)),
    "fill-in:2": (("P2-A2", False),),
}
_ACTIVITY_TIERS: Final = {
    "quiz": "selected-response",
    "cloze": "bounded-production",
    "match-up": "selected-response",
    "error-correction": "bounded-production",
    "text-questions": "source-grounded-open-response",
    "short-writing": "extended-writing",
    "fill-in": "bounded-production",
}
_SLOT_SPEC: Final = (
    ("P1-A1", 1, "quiz", ()),
    ("P1-A2", 1, "cloze", ()),
    ("P2-A1", 2, "match-up", ("fill-in",)),
    ("P2-A2", 2, "error-correction", ("fill-in",)),
    ("P2-A3", 2, "text-questions", ()),
    ("P3-A1", 3, "short-writing", ()),
)
_DOMAIN_NAMES: Final = frozenset(
    {
        "candidate",
        "provenance",
        "bank",
        "placement",
        "slot",
        "unit",
        "claim",
        "locator",
        "plan",
        "reservation",
        "allocation",
        "replacement",
        "witness",
    }
)

COMPOSITE_RULE: Final = {
    "allocation": "existing-preflight-lesson-with-authorized-slot-replacements-only",
    "loss_grid": {
        "bank_membership": "exactly-8-complete-certified-candidate-ids-each",
        "banks": ["quiz:1", "cloze:1"],
        "cell_identity": (
            "canonical-json-object-with-exact-keys-cloze_candidate_id-and-quiz_candidate_id"
        ),
        "cells": "cartesian-product-of-complete-certified-bank-membership",
        "exclusions_per_bank": 1,
        "required_cells": 64,
        "result_set": "exact-set-equality-and-unique-cell-identity-required",
    },
    "population_pass": "all-94-preregistered-sources-pass",
    "profile_digest": PROFILE_DIGEST,
    "source_pass": "all-64-unique-cartesian-cells-produce-existing-six-slot-complete-allocation",
    "tier_policy": "replacement-response-demand-tier-must-not-be-lower-than-requested-tier",
    "version": COMPOSITE_RULE_VERSION,
}


class ProspectiveProofSchemaError(ValueError):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ProspectiveProofSchemaError(f"{label} must be a SHA-256 digest.")
    return value


def _mapping(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProspectiveProofSchemaError(f"{label} has an invalid schema.")
    return value


def _identity(value: object) -> tuple[str, str]:
    row = _mapping(value, {"cloze_candidate_id", "quiz_candidate_id"}, "loss cell identity")
    if not all(isinstance(row[name], str) and row[name] for name in row):
        raise ProspectiveProofSchemaError("Loss cell identity is invalid.")
    return row["cloze_candidate_id"], row["quiz_candidate_id"]


def _bank_rows(
    value: object, *, manifest_digest: str, source_id: int
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, list) or len(value) != 2:
        raise ProspectiveProofSchemaError("Prospective proof needs exactly two certified banks.")
    rows = []
    for row in value:
        item = _mapping(row, {"bank_id", "receipt"}, "prospective bank receipt")
        receipt = _mapping(
            item["receipt"],
            {
                "version",
                "bank_id",
                "activity_type",
                "response_demand_tier",
                "candidate_ids",
                "count",
                "candidate_offsets",
                "candidate_provenance",
                "eligible_placements",
                "origin_kind",
                "origin_commitment",
                "authority_digest",
                "manifest_digest",
                "source_identity",
                "inventory_seal",
            },
            "PR-A candidate bank receipt",
        )
        if (
            item["bank_id"] != receipt["bank_id"]
            or item["bank_id"] not in {"quiz:1", "cloze:1"}
            or not isinstance(receipt["candidate_ids"], list)
        ):
            raise ProspectiveProofSchemaError("Prospective bank receipt is invalid.")
        ids = tuple(receipt["candidate_ids"])
        if (
            len(ids) != 8
            or len(set(ids)) != 8
            or not all(isinstance(entry, str) and entry for entry in ids)
        ):
            raise ProspectiveProofSchemaError(
                "Prospective bank membership must contain exactly eight IDs."
            )
        if receipt["version"] != "HramatkaCandidateBankReceipt.v1" or receipt["count"] != 8:
            raise ProspectiveProofSchemaError(
                "PR-A candidate-bank receipt version or count is invalid."
            )
        expected_type, expected_tier = (
            ("quiz", "selected-response")
            if item["bank_id"] == "quiz:1"
            else ("cloze", "bounded-production")
        )
        if (
            receipt["activity_type"] != expected_type
            or receipt["response_demand_tier"] != expected_tier
        ):
            raise ProspectiveProofSchemaError("PR-A candidate-bank type or tier is invalid.")
        provenance = receipt["candidate_provenance"]
        if not isinstance(provenance, list) or len(provenance) != 8:
            raise ProspectiveProofSchemaError("Prospective bank provenance is incomplete.")
        for expected, proof in zip(ids, provenance, strict=True):
            proof_row = _mapping(proof, {"candidate_id", "commitment"}, "candidate provenance")
            if proof_row["candidate_id"] != expected:
                raise ProspectiveProofSchemaError("Prospective bank provenance order has drifted.")
            _digest(proof_row["commitment"], "candidate provenance")
        offsets = receipt["candidate_offsets"]
        if not isinstance(offsets, list) or len(offsets) != 8:
            raise ProspectiveProofSchemaError("PR-A candidate-bank offsets are invalid.")
        for expected_offset, (expected_id, offset) in enumerate(zip(ids, offsets, strict=True)):
            offset_row = _mapping(offset, {"candidate_id", "unit_offset"}, "candidate offset")
            if (
                offset_row["candidate_id"] != expected_id
                or type(offset_row["unit_offset"]) is not int
                or offset_row["unit_offset"] != expected_offset
            ):
                raise ProspectiveProofSchemaError("PR-A candidate-bank offsets are invalid.")
        if (
            receipt["origin_kind"] != "deterministic-local-inventory"
            or receipt["manifest_digest"] != manifest_digest
            or receipt["source_identity"] != str(source_id)
            or receipt["authority_digest"] != PROFILE_DIGEST
        ):
            raise ProspectiveProofSchemaError("PR-A candidate-bank authority is invalid.")
        for name in ("origin_commitment", "authority_digest", "manifest_digest", "inventory_seal"):
            _digest(receipt[name], f"PR-A {name}")
        placements = receipt["eligible_placements"]
        if not isinstance(placements, list) or not placements:
            raise ProspectiveProofSchemaError("PR-A candidate-bank placements are invalid.")
        observed_slots = set()
        for placement in placements:
            placement_row = _mapping(
                placement, {"slot_id", "mutually_exclusive"}, "candidate-bank placement"
            )
            if (
                not isinstance(placement_row["slot_id"], str)
                or not placement_row["slot_id"]
                or type(placement_row["mutually_exclusive"]) is not bool
                or placement_row["slot_id"] in observed_slots
            ):
                raise ProspectiveProofSchemaError("PR-A candidate-bank placements are invalid.")
            observed_slots.add(placement_row["slot_id"])
        rows.append((item["bank_id"], ids))
    if {name for name, _ids in rows} != {"quiz:1", "cloze:1"}:
        raise ProspectiveProofSchemaError("Prospective bank IDs are not exact.")
    return tuple(rows)


def _full_receipts(
    value: object, *, manifest_digest: str, source_id: int
) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        raise ProspectiveProofSchemaError("Prospective full PR-A receipt set is missing.")
    rows: list[Mapping[str, object]] = []
    seen: set[str] = set()
    seals: set[str] = set()
    for value_row in value:
        receipt = _mapping(
            value_row,
            {
                "version",
                "bank_id",
                "activity_type",
                "response_demand_tier",
                "candidate_ids",
                "count",
                "candidate_offsets",
                "candidate_provenance",
                "eligible_placements",
                "origin_kind",
                "origin_commitment",
                "authority_digest",
                "manifest_digest",
                "source_identity",
                "inventory_seal",
            },
            "full PR-A bank receipt",
        )
        bank_id = receipt["bank_id"]
        if not isinstance(bank_id, str) or bank_id not in _BANK_PLACEMENTS or bank_id in seen:
            raise ProspectiveProofSchemaError("Full PR-A bank identities are invalid.")
        seen.add(bank_id)
        activity_type = bank_id.rsplit(":", 1)[0]
        if (
            receipt["version"] != "HramatkaCandidateBankReceipt.v1"
            or receipt["activity_type"] != activity_type
            or receipt["response_demand_tier"] != _ACTIVITY_TIERS[activity_type]
            or receipt["origin_kind"] != "deterministic-local-inventory"
            or receipt["authority_digest"] != PROFILE_DIGEST
            or receipt["manifest_digest"] != manifest_digest
            or receipt["source_identity"] != str(source_id)
        ):
            raise ProspectiveProofSchemaError("Full PR-A bank authority is invalid.")
        candidate_ids = receipt["candidate_ids"]
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(set(candidate_ids)) != len(candidate_ids)
            or not all(
                isinstance(candidate_id, str) and candidate_id.startswith(f"{bank_id}:")
                for candidate_id in candidate_ids
            )
            or type(receipt["count"]) is not int
            or receipt["count"] != len(candidate_ids)
        ):
            raise ProspectiveProofSchemaError("Full PR-A bank membership is invalid.")
        offsets = receipt["candidate_offsets"]
        provenance = receipt["candidate_provenance"]
        if (
            not isinstance(offsets, list)
            or not isinstance(provenance, list)
            or len(offsets) != len(candidate_ids)
            or len(provenance) != len(candidate_ids)
        ):
            raise ProspectiveProofSchemaError("Full PR-A bank member metadata is incomplete.")
        for offset, candidate_id in enumerate(candidate_ids):
            offset_row = _mapping(
                offsets[offset], {"candidate_id", "unit_offset"}, "bank candidate offset"
            )
            provenance_row = _mapping(
                provenance[offset], {"candidate_id", "commitment"}, "bank provenance"
            )
            expected_commitment = sha256(
                {
                    "bank_id": bank_id,
                    "candidate_id": candidate_id,
                    "unit_offset": offset,
                    "source_identity": str(source_id),
                }
            )
            if (
                offset_row != {"candidate_id": candidate_id, "unit_offset": offset}
                or provenance_row
                != {"candidate_id": candidate_id, "commitment": expected_commitment}
            ):
                raise ProspectiveProofSchemaError("Full PR-A bank member metadata has drifted.")
        expected_placements = [
            {"slot_id": slot_id, "mutually_exclusive": mutually_exclusive}
            for slot_id, mutually_exclusive in _BANK_PLACEMENTS[bank_id]
        ]
        if receipt["eligible_placements"] != expected_placements:
            raise ProspectiveProofSchemaError("Full PR-A bank placements have drifted.")
        expected_origin = sha256(
            {
                "origin_kind": "deterministic-local-inventory",
                "source_identity": str(source_id),
                "bank_id": bank_id,
                "candidate_ids": candidate_ids,
                "placements": [list(item) for item in _BANK_PLACEMENTS[bank_id]],
                "authority_digest": PROFILE_DIGEST,
            }
        )
        if receipt["origin_commitment"] != expected_origin:
            raise ProspectiveProofSchemaError("Full PR-A bank origin commitment has drifted.")
        _digest(receipt["inventory_seal"], "full PR-A inventory seal")
        seals.add(receipt["inventory_seal"])
        rows.append(receipt)
    expected_order = sorted(
        seen, key=lambda bank_id: (bank_id.rsplit(":", 1)[0], int(bank_id.rsplit(":", 1)[1]))
    )
    if [row["bank_id"] for row in rows] != expected_order or len(seals) != 1:
        raise ProspectiveProofSchemaError("Full PR-A receipt set order or seal has drifted.")
    expected_seal = sha256(
        {
            "manifest_digest": manifest_digest,
            "source_identity": str(source_id),
            "profile_digest": PROFILE_DIGEST,
            "banks": [
                {"bank_id": row["bank_id"], "candidate_ids": row["candidate_ids"]}
                for row in rows
            ],
        }
    )
    if seals != {expected_seal}:
        raise ProspectiveProofSchemaError("Full PR-A inventory seal does not bind all banks.")
    covered = {
        slot_id
        for row in rows
        for slot_id, _exclusive in _BANK_PLACEMENTS[row["bank_id"]]
    }
    if covered != {slot_id for slot_id, _phase, _activity, _replacements in _SLOT_SPEC}:
        raise ProspectiveProofSchemaError("Full PR-A receipts do not cover all six slots.")
    return rows


def _units(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        raise ProspectiveProofSchemaError(f"{label} must be a non-empty list.")
    rows: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for value_row in value:
        unit = _mapping(
            value_row,
            {"unit_id", "claims", "reservations", "locators", "plan_digest"},
            "prospective unit",
        )
        unit_id = unit["unit_id"]
        if not isinstance(unit_id, str) or not unit_id or unit_id in seen:
            raise ProspectiveProofSchemaError("Prospective unit identity is invalid.")
        seen.add(unit_id)
        claims = unit["claims"]
        locators = unit["locators"]
        if (
            not isinstance(claims, list)
            or not claims
            or not isinstance(locators, list)
            or not locators
        ):
            raise ProspectiveProofSchemaError("Prospective units need claims and locators.")
        for claim in claims:
            claim_row = _mapping(claim, {"kind", "resource_id"}, "prospective claim")
            if not all(isinstance(item, str) and item for item in claim_row.values()):
                raise ProspectiveProofSchemaError("Prospective claim is invalid.")
        if unit["reservations"] != claims:
            raise ProspectiveProofSchemaError("Prospective reservations must mirror claims.")
        if not all(isinstance(locator, str) and locator for locator in locators):
            raise ProspectiveProofSchemaError("Prospective locator is invalid.")
        if unit["plan_digest"] != sha256(
            {"unit_id": unit_id, "claims": claims, "locators": locators}
        ):
            raise ProspectiveProofSchemaError("Prospective plan digest has drifted.")
        rows.append(unit)
    return rows


def _geometry(
    allocation: object, witness: object, *, source_id: int
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    allocation_row = _mapping(
        allocation, {"paragraph_ids", "slots", "canonical_allocation_digest"}, "allocation"
    )
    if allocation_row["paragraph_ids"] != [str(source_id)]:
        raise ProspectiveProofSchemaError("Allocation paragraph authority has drifted.")
    _digest(allocation_row["canonical_allocation_digest"], "canonical allocation")
    slots = allocation_row["slots"]
    if not isinstance(slots, list) or len(slots) != 6:
        raise ProspectiveProofSchemaError("Allocation must contain six slots.")
    primary_count = 0
    for value_slot, (slot_id, phase, requested, allowed_replacements) in zip(
        slots, _SLOT_SPEC, strict=True
    ):
        slot = _mapping(
            value_slot,
            {
                "slot_id",
                "phase",
                "requested_type",
                "scheduled_type",
                "substitution_reason",
                "units",
                "conditional_replacements",
            },
            "allocation slot",
        )
        scheduled = slot["scheduled_type"]
        if (
            slot["slot_id"] != slot_id
            or slot["phase"] != phase
            or slot["requested_type"] != requested
            or scheduled not in {requested, *allowed_replacements}
            or (scheduled == requested) != (slot["substitution_reason"] is None)
            or (
                slot["substitution_reason"] is not None
                and (
                    not isinstance(slot["substitution_reason"], str)
                    or not slot["substitution_reason"]
                )
            )
        ):
            raise ProspectiveProofSchemaError("Allocation slot policy has drifted.")
        primary_count += len(_units(slot["units"], "allocation slot units"))
        replacements = slot["conditional_replacements"]
        if not isinstance(replacements, list):
            raise ProspectiveProofSchemaError("Conditional replacements must be a list.")
        replacement_types = []
        for replacement in replacements:
            replacement_row = _mapping(
                replacement, {"activity_type", "units"}, "conditional replacement"
            )
            if replacement_row["activity_type"] not in allowed_replacements:
                raise ProspectiveProofSchemaError("Conditional replacement crosses slot policy.")
            replacement_types.append(replacement_row["activity_type"])
            _units(replacement_row["units"], "conditional replacement units")
        if len(replacement_types) != len(set(replacement_types)):
            raise ProspectiveProofSchemaError("Conditional replacement types must be unique.")
    if primary_count != 27:
        raise ProspectiveProofSchemaError("Allocation unit denominator has drifted.")
    witness_row = _mapping(
        witness,
        {"rows", "eligible_replacement_edges", "canonical_allocation_digest"},
        "witness",
    )
    if witness_row["canonical_allocation_digest"] != allocation_row["canonical_allocation_digest"]:
        raise ProspectiveProofSchemaError("Witness allocation digest has drifted.")
    expected_rows = [
        {
            "slot_id": slot["slot_id"],
            "phase": slot["phase"],
            "activity_type": slot["scheduled_type"],
            "units": slot["units"],
        }
        for slot in slots
    ]
    expected_edges = [
        {
            "slot_id": slot["slot_id"],
            "types": [item["activity_type"] for item in slot["conditional_replacements"]],
        }
        for slot in slots
    ]
    if (
        witness_row["rows"] != expected_rows
        or witness_row["eligible_replacement_edges"] != expected_edges
    ):
        raise ProspectiveProofSchemaError("Witness does not mirror allocation geometry.")
    return allocation_row, witness_row


def _declared_domains(
    receipts: list[Mapping[str, object]],
    allocation: Mapping[str, object],
    witness: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    slots = allocation["slots"]
    units = [unit for slot in slots for unit in slot["units"]]
    claims = [claim for unit in units for claim in unit["claims"]]
    locators = [locator for unit in units for locator in unit["locators"]]
    replacements = [item for slot in slots for item in slot["conditional_replacements"]]
    domains = {
        "candidate": [
            {"bank_id": receipt["bank_id"], "candidate_id": candidate, "offset": offset}
            for receipt in receipts
            for offset, candidate in enumerate(receipt["candidate_ids"])
        ],
        "provenance": [
            {
                "bank_id": receipt["bank_id"],
                "candidate_id": candidate,
                "commitment": receipt["candidate_provenance"][offset]["commitment"],
            }
            for receipt in receipts
            for offset, candidate in enumerate(receipt["candidate_ids"])
        ],
        "bank": [receipt["bank_id"] for receipt in receipts],
        "placement": [
            {
                "bank_id": receipt["bank_id"],
                "slot_id": placement["slot_id"],
                "exclusive": placement["mutually_exclusive"],
            }
            for receipt in receipts
            for placement in receipt["eligible_placements"]
        ],
        "slot": [{"slot_id": slot["slot_id"], "phase": slot["phase"]} for slot in slots],
        "unit": [unit["unit_id"] for unit in units],
        "claim": claims,
        "locator": locators,
        "plan": [{"unit_id": unit["unit_id"], "digest": unit["plan_digest"]} for unit in units],
        "reservation": [item for unit in units for item in unit["reservations"]],
        "allocation": [
            {
                "slot_id": slot["slot_id"],
                "requested_type": slot["requested_type"],
                "scheduled_type": slot["scheduled_type"],
                "substitution_reason": slot["substitution_reason"],
            }
            for slot in slots
        ],
        "replacement": replacements,
        "witness": witness["rows"],
    }
    return {name: {"count": len(rows), "digest": sha256(rows)} for name, rows in domains.items()}


def _domains(
    value: object,
    receipts: list[Mapping[str, object]],
    allocation: Mapping[str, object],
    witness: Mapping[str, object],
) -> None:
    rows = _mapping(value, set(_DOMAIN_NAMES), "domain commitments")
    for domain in _DOMAIN_NAMES:
        item = _mapping(rows[domain], {"count", "digest"}, "domain commitment")
        if type(item["count"]) is not int or item["count"] < 0:
            raise ProspectiveProofSchemaError("Domain commitment count is invalid.")
        _digest(item["digest"], "domain commitment")
    if rows != _declared_domains(receipts, allocation, witness):
        raise ProspectiveProofSchemaError("Domain commitments do not bind declared geometry.")


def validate_certificate_payload(value: object) -> None:
    row = _mapping(
        value,
        {
            "version",
            "manifest_digest",
            "source",
            "composite_rule_digest",
            "profile_digest",
            "density_floor_digest",
            "status",
            "failure_reason",
            "bank_receipts",
            "banks",
            "cells",
        },
        "prospective certificate",
    )
    if row["version"] != VERSION or row["composite_rule_digest"] != COMPOSITE_RULE_DIGEST:
        raise ProspectiveProofSchemaError(
            "Prospective certificate version or composite rule has drifted."
        )
    if sha256(COMPOSITE_RULE) != COMPOSITE_RULE_DIGEST:
        raise ProspectiveProofSchemaError("Composite rule canonical bytes have drifted.")
    for name in ("manifest_digest", "profile_digest", "density_floor_digest"):
        _digest(row[name], name)
    if (
        row["profile_digest"] != PROFILE_DIGEST
        or row["density_floor_digest"] != DENSITY_FLOOR_DIGEST
    ):
        raise ProspectiveProofSchemaError("Prospective profile or density floor has drifted.")
    source = _mapping(
        row["source"],
        {
            "sqlite_id",
            "title",
            "url",
            "fetched_at",
            "database_text_sha256",
            "extracted_text_sha256",
            "extracted_token_count",
            "selection_rank",
        },
        "prospective source",
    )
    if (
        type(source["sqlite_id"]) is not int
        or source["sqlite_id"] < 1
        or type(source["selection_rank"]) is not int
        or not 1 <= source["selection_rank"] <= 94
        or type(source["extracted_token_count"]) is not int
        or not 370 <= source["extracted_token_count"] <= 600
    ):
        raise ProspectiveProofSchemaError("Prospective source identity is invalid.")
    for name in ("title", "url", "fetched_at"):
        if not isinstance(source[name], str) or not source[name]:
            raise ProspectiveProofSchemaError("Prospective source metadata is invalid.")
    for name in ("database_text_sha256", "extracted_text_sha256"):
        _digest(source[name], name)
    if row["status"] not in {"passed", "failed"} or (
        row["failure_reason"] is not None and not isinstance(row["failure_reason"], str)
    ):
        raise ProspectiveProofSchemaError("Prospective source status is invalid.")
    if row["status"] == "failed" and (
        not row["failure_reason"] or not isinstance(row["failure_reason"], str)
    ):
        raise ProspectiveProofSchemaError("Failed prospective source needs a fail-closed reason.")
    if row["status"] == "passed" and row["failure_reason"] is not None:
        raise ProspectiveProofSchemaError(
            "Passed prospective source cannot carry a failure reason."
        )
    if (
        row["banks"] == []
        and row["bank_receipts"] == []
        and row["cells"] == []
        and row["status"] == "failed"
    ):
        return
    full_receipts = _full_receipts(
        row["bank_receipts"],
        manifest_digest=row["manifest_digest"],
        source_id=source["sqlite_id"],
    )
    full_by_id = {receipt["bank_id"]: receipt for receipt in full_receipts}
    banks = dict(
        _bank_rows(
            row["banks"], manifest_digest=row["manifest_digest"], source_id=source["sqlite_id"]
        )
    )
    if any(
        full_by_id.get(bank["bank_id"]) != bank["receipt"] for bank in row["banks"]
    ):
        raise ProspectiveProofSchemaError("Loss banks are not bound to the full PR-A receipt set.")
    if not isinstance(row["cells"], list) or len(row["cells"]) != 64:
        raise ProspectiveProofSchemaError("Prospective source must contain 64 loss cells.")
    expected = {(cloze, quiz) for cloze in banks["cloze:1"] for quiz in banks["quiz:1"]}
    observed = set()
    for cell in row["cells"]:
        item = _mapping(
            cell,
            {
                "identity",
                "outcome",
                "allocation",
                "witness",
                "domain_commitments",
                "failure_reason",
            },
            "loss cell",
        )
        identity = _identity(item["identity"])
        if identity in observed or identity not in expected:
            raise ProspectiveProofSchemaError("Prospective loss-cell set is not exact.")
        observed.add(identity)
        if item["outcome"] not in {"passed", "failed"}:
            raise ProspectiveProofSchemaError("Prospective loss-cell outcome is invalid.")
        if (item["outcome"] == "passed") != (item["failure_reason"] is None):
            raise ProspectiveProofSchemaError("Prospective loss-cell witness status is invalid.")
        if item["outcome"] == "failed":
            failures = []
            for name in ("allocation", "witness", "domain_commitments"):
                failure_row = _mapping(item[name], {"failure_digest"}, f"failed {name}")
                failures.append(_digest(failure_row["failure_digest"], f"failed {name}"))
            expected_failure = sha256(
                {"identity": item["identity"], "failure": item["failure_reason"]}
            )
            if failures != [expected_failure, expected_failure, expected_failure]:
                raise ProspectiveProofSchemaError("Failed loss-cell commitments have drifted.")
        else:
            allocation, witness = _geometry(
                item["allocation"], item["witness"], source_id=source["sqlite_id"]
            )
            _domains(item["domain_commitments"], full_receipts, allocation, witness)
    if observed != expected:
        raise ProspectiveProofSchemaError("Prospective loss-cell Cartesian product is incomplete.")
    if row["status"] == "passed" and any(cell["outcome"] != "passed" for cell in row["cells"]):
        raise ProspectiveProofSchemaError("A failed loss cell cannot yield a passed source.")
    if row["status"] == "failed" and all(cell["outcome"] == "passed" for cell in row["cells"]):
        raise ProspectiveProofSchemaError("A failed source needs an explicit failed loss cell.")


@dataclass(frozen=True)
class ProspectiveCertificate:
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        value = json.loads(canonical_bytes(dict(self.payload)))
        validate_certificate_payload(value)
        object.__setattr__(self, "payload", value)

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.payload)

    @property
    def digest(self) -> str:
        return sha256(self.payload)

    @classmethod
    def from_bytes(cls, raw: bytes) -> ProspectiveCertificate:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProspectiveProofSchemaError("Prospective certificate is not JSON.") from exc
        certificate = cls(value)
        if certificate.to_bytes() != raw:
            raise ProspectiveProofSchemaError("Prospective certificate bytes are not canonical.")
        return certificate
