"""Independent offline replay for prospective #552 certificates.

It intentionally does not import the capture module or any production proof
parser.  Replay reconstructs inventory, bank provenance, every exclusion, and
the six-slot witness from source text.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

from hramatka.engine import data
from hramatka.engine.anchor_inventory_v3 import inventory_from_anchor
from hramatka.engine.candidate_bank_receipt_v1 import (
    BankReceiptContext,
    CandidateBankReceipt,
)
from hramatka.engine.lesson_capacity_v3 import (
    AnchorParagraph,
    AnchorWindow,
    LessonAllocation,
    preflight_lesson,
)
from hramatka.engine.lesson_profile_45_v1 import (
    inventory_candidate_types_45,
    inventory_replacement_types_45,
    lesson_profile_45,
    slot_builders_45,
)
from hramatka.engine.teacher_ready_density_v3 import density_floor_fingerprint
from hramatka.engine.unit_builders_v3 import CertificationInventory

from .proof_schema_v1 import ProofSchemaError, frozen_content_authority
from .prospective_manifest_v1 import (
    ProspectiveManifest,
    ProspectiveSource,
    validate_manifest_source,
)
from .prospective_proof_schema_v1 import (
    COMPOSITE_RULE_DIGEST,
    DENSITY_FLOOR_DIGEST,
    PROFILE_DIGEST,
    ProspectiveCertificate,
    ProspectiveProofSchemaError,
    sha256,
)

_RANK = {
    "selected-response": 1,
    "bounded-production": 2,
    "source-grounded-open-response": 3,
    "extended-writing": 4,
}


class ProspectiveReplayError(RuntimeError):
    pass


class ProspectiveSourceInfeasible(ValueError):
    """The frozen source cannot produce the two complete certified banks."""


def _verified_engine_bundle(bundle: data.DataBundle, repository: Path) -> data.DataBundle:
    if os.environ.get("HRAMATKA_ALLOW_DATA_DRIFT"):
        raise ProspectiveReplayError(
            "HRAMATKA_ALLOW_DATA_DRIFT is forbidden for prospective proof."
        )
    try:
        _manifest, policy = frozen_content_authority(repository)
    except ProofSchemaError as exc:
        raise ProspectiveReplayError("Frozen engine authority cannot be read locally.") from exc
    inputs = bundle.manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(policy):
        raise ProspectiveReplayError("Engine bundle does not match frozen PR #547 authority.")
    for name, row in policy.items():
        actual = inputs.get(name)
        if not isinstance(actual, dict) or any(actual.get(key) != row[key] for key in row):
            raise ProspectiveReplayError("Engine bundle policy does not match PR #547 authority.")
    detached = data.DataBundle(
        root=bundle.root.resolve(),
        manifest={
            "inputs": {
                name: {
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "size": row["size"],
                    "required": True,
                }
                for name, row in policy.items()
            }
        },
    )
    try:
        detached.verify(allow_drift=False)
    except data.DataDriftError as exc:
        raise ProspectiveReplayError("Engine bundle has drifted.") from exc
    return detached


def _banks(receipts: tuple[CandidateBankReceipt, ...]) -> list[dict[str, object]]:
    by_id = {receipt.bank_id: receipt for receipt in receipts}
    if len(by_id) != len(receipts):
        raise ProspectiveSourceInfeasible("Candidate-bank receipt tuple has duplicate bank IDs.")
    rows = []
    for bank_id in ("quiz:1", "cloze:1"):
        receipt = by_id.get(bank_id)
        if receipt is None:
            raise ProspectiveSourceInfeasible(f"{bank_id} receipt is missing.")
        ids = receipt.candidate_ids
        if len(ids) != 8 or len(set(ids)) != 8:
            raise ProspectiveSourceInfeasible(
                f"{bank_id} is not a complete certified eight-member bank."
            )
        rows.append(
            {
                "bank_id": bank_id,
                "receipt": receipt.to_dict(),
            }
        )
    return rows


def _remove(
    inventory: CertificationInventory, quiz_id: str, cloze_id: str
) -> CertificationInventory:
    return replace(
        inventory,
        candidates=tuple(
            item for item in inventory.candidates if item.candidate_id not in {quiz_id, cloze_id}
        ),
    )


def _unit_projection(unit: object) -> dict[str, object]:
    claims = [
        {"kind": claim.kind, "resource_id": claim.resource_id} for claim in unit.resource_claims
    ]
    locators = [citation.locator for citation in unit.citation_plan]
    return {
        "unit_id": unit.unit_id,
        "claims": claims,
        "reservations": [
            {"kind": row["kind"], "resource_id": row["resource_id"]} for row in claims
        ],
        "locators": locators,
        "plan_digest": sha256({"unit_id": unit.unit_id, "claims": claims, "locators": locators}),
    }


def _allocation(allocation: LessonAllocation) -> dict[str, object]:
    profile = lesson_profile_45()
    rows = []
    for slot in allocation.slots:
        if (
            _RANK[profile.tier_for(slot.scheduled_type)]
            < _RANK[profile.tier_for(slot.requested_type)]
        ):
            raise ProspectiveReplayError(
                "Replacement tier downgraded the requested response demand."
            )
        if (
            slot.requested_type in {"text-questions", "short-writing"}
            and slot.scheduled_type != slot.requested_type
        ):
            raise ProspectiveReplayError("Open response or extended writing was replaced.")
        rows.append(
            {
                "slot_id": slot.slot_id,
                "phase": slot.phase,
                "requested_type": slot.requested_type,
                "scheduled_type": slot.scheduled_type,
                "substitution_reason": slot.substitution_reason,
                "units": [_unit_projection(unit) for unit in slot.plan.units],
                "conditional_replacements": [
                    {
                        "activity_type": item.activity_type,
                        "units": [_unit_projection(unit) for unit in item.plan.units],
                    }
                    for item in slot.conditional_replacements
                ],
            }
        )
    if len(rows) != 6 or sum(len(row["units"]) for row in rows) != 27:
        raise ProspectiveReplayError("Existing six-slot/unit denominator authority has drifted.")
    return {
        "paragraph_ids": list(allocation.paragraph_ids),
        "slots": rows,
        "canonical_allocation_digest": hashlib.sha256(allocation.canonical_bytes()).hexdigest(),
    }


def _witness(allocation: LessonAllocation) -> dict[str, object]:
    projection = _allocation(allocation)
    return {
        "rows": [
            {
                "slot_id": row["slot_id"],
                "phase": row["phase"],
                "activity_type": row["scheduled_type"],
                "units": list(row["units"]),
            }
            for row in projection["slots"]
        ],
        "eligible_replacement_edges": [
            {
                "slot_id": slot.slot_id,
                "types": [item.activity_type for item in slot.conditional_replacements],
            }
            for slot in allocation.slots
        ],
        "canonical_allocation_digest": projection["canonical_allocation_digest"],
    }


def _domain_commitments(
    receipts: tuple[CandidateBankReceipt, ...], allocation: LessonAllocation
) -> dict[str, object]:
    projection = _allocation(allocation)
    slots = projection["slots"]
    units = [unit for slot in slots for unit in slot["units"]]
    claims = [claim for unit in units for claim in unit["claims"]]
    domains = {
        "candidate": [
            {"bank_id": receipt.bank_id, "candidate_id": candidate_id, "offset": offset}
            for receipt in receipts
            for offset, candidate_id in enumerate(receipt.candidate_ids)
        ],
        "provenance": [
            {
                "bank_id": receipt.bank_id,
                "candidate_id": candidate_id,
                "commitment": receipt.candidate_provenance[offset][1],
            }
            for receipt in receipts
            for offset, candidate_id in enumerate(receipt.candidate_ids)
        ],
        "bank": [receipt.bank_id for receipt in receipts],
        "placement": [
            {"bank_id": receipt.bank_id, "slot_id": slot_id, "exclusive": exclusive}
            for receipt in receipts
            for slot_id, exclusive in receipt.eligible_placements
        ],
        "slot": [{"slot_id": slot["slot_id"], "phase": slot["phase"]} for slot in slots],
        "unit": [unit["unit_id"] for unit in units],
        "claim": claims,
        "locator": [locator for unit in units for locator in unit["locators"]],
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
        "replacement": [item for slot in slots for item in slot["conditional_replacements"]],
        "witness": _witness(allocation)["rows"],
    }
    return {name: {"count": len(value), "digest": sha256(value)} for name, value in domains.items()}


def _replay_cell(
    inventory: CertificationInventory,
    source: ProspectiveSource,
    receipts: tuple[CandidateBankReceipt, ...],
    quiz_id: str,
    cloze_id: str,
) -> dict[str, object]:
    identity = {"cloze_candidate_id": cloze_id, "quiz_candidate_id": quiz_id}
    result = preflight_lesson(
        AnchorWindow(
            (AnchorParagraph(str(source.sqlite_id), _remove(inventory, quiz_id, cloze_id)),), 0, 0
        ),
        duration_minutes=45,
        slots=lesson_profile_45().slots,
        builders=slot_builders_45(),
    )
    if result.allocation is None:
        reason = result.event.code if result.event is not None else "no-allocation-witness"
        blank = sha256({"identity": identity, "failure": reason})
        return {
            "identity": identity,
            "outcome": "failed",
            "allocation": {"failure_digest": blank},
            "witness": {"failure_digest": blank},
            "domain_commitments": {"failure_digest": blank},
            "failure_reason": reason,
        }
    allocation = _allocation(result.allocation)
    return {
        "identity": identity,
        "outcome": "passed",
        "allocation": allocation,
        "witness": _witness(result.allocation),
        "domain_commitments": _domain_commitments(receipts, result.allocation),
        "failure_reason": None,
    }


def replay_source(
    certificate_bytes: bytes,
    manifest: ProspectiveManifest,
    source: ProspectiveSource,
    extracted_text: str,
    *,
    bundle: data.DataBundle,
    repository: Path,
) -> ProspectiveCertificate:
    try:
        certificate = ProspectiveCertificate.from_bytes(certificate_bytes)
    except ProspectiveProofSchemaError as exc:
        raise ProspectiveReplayError(
            "Prospective certificate parser rejected the artifact."
        ) from exc
    validate_manifest_source(manifest, source, extracted_text)
    if (
        certificate.payload["manifest_digest"] != manifest.digest
        or certificate.payload["source"] != source.to_dict()
    ):
        raise ProspectiveReplayError(
            "Certificate source authority does not match the prospective manifest."
        )
    if (
        lesson_profile_45().digest != PROFILE_DIGEST
        or density_floor_fingerprint() != DENSITY_FLOOR_DIGEST
    ):
        raise ProspectiveReplayError("Existing profile or density floor authority has drifted.")
    engine_bundle = _verified_engine_bundle(bundle, repository)
    try:
        receipts = []
        with data.use_bundle(engine_bundle):
            inventory = inventory_from_anchor(
                extracted_text,
                scheduled_types=inventory_candidate_types_45(),
                replacement_types=inventory_replacement_types_45(),
                duration_minutes=45,
                receipt_context=BankReceiptContext(
                    manifest_digest=manifest.digest,
                    source_identity=str(source.sqlite_id),
                ),
                receipt_sink=receipts.append,
            )
            if len(receipts) != 1:
                raise ProspectiveSourceInfeasible(
                    "Candidate-bank receipt emission was not exactly once."
                )
            banks = _banks(receipts[0])
    except (ValueError, ProspectiveSourceInfeasible) as exc:
        expected_failure = {
            "version": "HramatkaProspectiveCertificate.v1",
            "manifest_digest": manifest.digest,
            "source": source.to_dict(),
            "composite_rule_digest": COMPOSITE_RULE_DIGEST,
            "profile_digest": PROFILE_DIGEST,
            "density_floor_digest": DENSITY_FLOOR_DIGEST,
                "status": "failed",
                "failure_reason": f"bank-certification:{exc}",
                "bank_receipts": [],
                "banks": [],
            "cells": [],
        }
        if certificate.payload != expected_failure:
            raise ProspectiveReplayError("Certificate concealed an infeasible source.") from exc
        return certificate
    memberships = {row["bank_id"]: row["receipt"]["candidate_ids"] for row in banks}
    with data.use_bundle(engine_bundle):
        rebuilt_cells = [
            _replay_cell(inventory, source, receipts[0], quiz_id, cloze_id)
            for cloze_id in memberships["cloze:1"]
            for quiz_id in memberships["quiz:1"]
        ]
    failed = [row for row in rebuilt_cells if row["outcome"] != "passed"]
    rebuilt = {
        "version": "HramatkaProspectiveCertificate.v1",
        "manifest_digest": manifest.digest,
        "source": source.to_dict(),
        "composite_rule_digest": COMPOSITE_RULE_DIGEST,
        "profile_digest": PROFILE_DIGEST,
        "density_floor_digest": DENSITY_FLOOR_DIGEST,
        "status": "failed" if failed else "passed",
        "failure_reason": None if not failed else f"loss-grid:{len(failed)}-no-witness-cells",
        "bank_receipts": [receipt.to_dict() for receipt in receipts[0]],
        "banks": banks,
        "cells": rebuilt_cells,
    }
    if certificate.payload != rebuilt:
        raise ProspectiveReplayError(
            "Certificate bank, allocation, witness, claim, or domain proof does not replay."
        )
    return certificate
