"""Offline capture of one content-free 45-minute proof certificate."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hramatka.engine import data
from hramatka.engine.anchor_inventory_v3 import inventory_from_anchor
from hramatka.engine.candidate_bank_receipt_v1 import BankReceiptContext, CandidateBankReceipt
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

from .manifest import RuntimeAnchor, load_manifest
from .proof_schema_v1 import (
    CONTENT_MANIFEST_DIGEST,
    ExpectedAuthority,
    ProofCertificate,
    ProofSchemaError,
    frozen_content_authority,
    implementation_inventory_digest,
    sha256,
)


class ProofCaptureError(RuntimeError):
    """Capture could not rebuild a complete local qualification lesson."""


def _capture_inventory_digest(repository: Path, commit: str) -> str:
    """Bind capture to the hardened, exact executing implementation surface."""
    try:
        return implementation_inventory_digest(repository, commit)
    except ProofSchemaError as exc:
        raise ProofCaptureError("Capture Git authority cannot be read locally.") from exc


def _verify_frozen_bundle(
    bundle: data.DataBundle, policy: Mapping[str, Mapping[str, object]]
) -> dict[str, str]:
    """Capture-side mounted-byte validation against Git-derived policy only."""
    root = bundle.root.resolve()
    inputs = bundle.manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(policy):
        raise ProofCaptureError("Capture data manifest does not match frozen Git policy.")
    digests: dict[str, str] = {}
    for name, row in policy.items():
        manifest_row = inputs[name]
        expected_path = root / str(row["path"])
        if (
            not isinstance(manifest_row, dict)
            or manifest_row.get("path") != row["path"]
            or bundle.path(name) != expected_path
        ):
            raise ProofCaptureError("Capture data manifest does not match frozen Git policy.")
        path = bundle.path(name)
        if path.is_symlink() or not path.is_file() or path.parent.resolve() != root:
            raise ProofCaptureError("Capture mounted data path does not match frozen Git policy.")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != row["sha256"] or size != row["size"]:
            raise ProofCaptureError("Capture mounted data does not match frozen Git policy.")
        digests[name] = digest.hexdigest()
    return digests


def _capture_execution_bundle(
    bundle: data.DataBundle, policy: Mapping[str, Mapping[str, object]]
) -> data.DataBundle:
    """Detach the engine's data authority from a caller-owned manifest."""
    root = bundle.root.resolve()
    inputs = bundle.manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(policy):
        raise ProofCaptureError("Capture data manifest does not match frozen Git policy.")
    frozen_inputs: dict[str, dict[str, object]] = {}
    for name, row in policy.items():
        manifest_row = inputs[name]
        expected_path = root / str(row["path"])
        if (
            not isinstance(manifest_row, dict)
            or manifest_row.get("path") != row["path"]
            or bundle.path(name) != expected_path
        ):
            raise ProofCaptureError("Capture data manifest does not match frozen Git policy.")
        frozen_inputs[name] = {
            "path": row["path"],
            "sha256": row["sha256"],
            "size": row["size"],
            "required": True,
        }
    return data.DataBundle(root=root, manifest={"inputs": frozen_inputs})


def _inventory_commitment(inventory: object) -> str:
    return sha256(
        {
            "candidate_ids": [candidate.candidate_id for candidate in inventory.candidates],
            "pair_ids": [pair.pair_id for pair in inventory.atlas_pairs],
            "writing_ids": [task.task_id for task in inventory.writing_tasks],
            "sentence_ids": [sentence.sentence_id for sentence in inventory.sentences],
        }
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
            {"kind": claim["kind"], "resource_id": claim["resource_id"]} for claim in claims
        ],
        "locators": locators,
        "plan_digest": sha256({"unit_id": unit.unit_id, "claims": claims, "locators": locators}),
    }


def _allocation_projection(allocation: LessonAllocation) -> dict[str, object]:
    return {
        "paragraph_ids": list(allocation.paragraph_ids),
        "slots": [
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
            for slot in allocation.slots
        ],
        "canonical_allocation_digest": hashlib.sha256(allocation.canonical_bytes()).hexdigest(),
    }


def _witness(allocation: LessonAllocation) -> dict[str, object]:
    allocation_projection = _allocation_projection(allocation)
    return {
        "rows": [
            {
                "slot_id": row["slot_id"],
                "phase": row["phase"],
                "activity_type": row["scheduled_type"],
                "units": list(row["units"]),
            }
            for row in allocation_projection["slots"]
        ],
        "eligible_replacement_edges": [
            {
                "slot_id": slot.slot_id,
                "types": [item.activity_type for item in slot.conditional_replacements],
            }
            for slot in allocation.slots
        ],
        "canonical_allocation_digest": allocation_projection["canonical_allocation_digest"],
    }


def _domain_commitments(
    receipts: tuple[CandidateBankReceipt, ...], allocation: LessonAllocation
) -> dict[str, object]:
    """Capture-side domain denominator; replay constructs its own equivalent."""
    rows = _allocation_projection(allocation)
    slots = rows["slots"]
    units = [unit for slot in slots for unit in slot["units"]]
    claims = [claim for unit in units for claim in unit["claims"]]
    locators = [locator for unit in units for locator in unit["locators"]]
    replacements = [item for slot in slots for item in slot["conditional_replacements"]]
    domains = {
        "candidate": [
            {
                "bank_id": receipt.bank_id,
                "candidate_id": candidate_id,
                "offset": offset,
            }
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
        "locator": locators,
        "plan": [{"unit_id": unit["unit_id"], "digest": unit["plan_digest"]} for unit in units],
        "reservation": [
            reservation for unit in units for reservation in unit["reservations"]
        ],
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
        "witness": _witness(allocation)["rows"],
    }
    return {name: {"count": len(value), "digest": sha256(value)} for name, value in domains.items()}


@dataclass(frozen=True)
class CaptureInputs:
    anchor: RuntimeAnchor
    bundle: data.DataBundle
    repository: Path
    expected: ExpectedAuthority


def capture_certificate(inputs: CaptureInputs) -> ProofCertificate:
    """Capture one exact-anchor certificate without provider or network activity."""
    frozen_manifest_digest, frozen_data_digests = frozen_content_authority(inputs.repository)
    manifest = load_manifest()
    if manifest.sha256 != CONTENT_MANIFEST_DIGEST or manifest.sha256 != frozen_manifest_digest:
        raise ProofCaptureError("Pinned qualification manifest does not match content baseline.")
    manifest_anchor = {row.id: row for row in manifest.anchors}.get(inputs.anchor.id)
    if manifest_anchor is None or manifest_anchor.source_identity != inputs.anchor.source_identity:
        raise ProofCaptureError("Capture anchor is outside the pinned qualification manifest.")
    if hashlib.sha256(inputs.anchor.text.encode("utf-8")).hexdigest() != manifest_anchor.sha256:
        raise ProofCaptureError("Capture anchor bytes do not match the pinned manifest.")
    execution_bundle = _capture_execution_bundle(inputs.bundle, frozen_data_digests)
    bundle_digests = _verify_frozen_bundle(execution_bundle, frozen_data_digests)
    if bundle_digests != dict(inputs.expected.data_digests):
        raise ProofCaptureError("Capture data authority does not match verifier policy.")
    slots = lesson_profile_45().slots
    receipts: list[tuple[CandidateBankReceipt, ...]] = []
    with data.use_bundle(execution_bundle):
        inventory = inventory_from_anchor(
            inputs.anchor.text,
            scheduled_types=inventory_candidate_types_45(),
            replacement_types=inventory_replacement_types_45(),
            duration_minutes=45,
            receipt_context=BankReceiptContext(
                manifest_digest=manifest.sha256,
                source_identity=inputs.anchor.source_identity,
            ),
            receipt_sink=receipts.append,
        )
        if len(receipts) != 1:
            raise ProofCaptureError("Capture did not issue exactly one creation-time receipt set.")
        preflight = preflight_lesson(
            AnchorWindow((AnchorParagraph(inputs.anchor.id, inventory),), 0, 0),
            duration_minutes=45,
            slots=slots,
            builders=slot_builders_45(),
        )
    if preflight.allocation is None:
        raise ProofCaptureError("Capture anchor cannot produce a complete deterministic lesson.")
    allocated_units = sum(len(slot.plan.units) for slot in preflight.allocation.slots)
    if allocated_units != 27:
        raise ProofCaptureError("Capture allocation does not meet the pinned unit denominator.")
    certificate = ProofCertificate(
        anchor_id=inputs.anchor.id,
        manifest_digest=manifest.sha256,
        source_identity=inputs.anchor.source_identity,
        repository_commit=inputs.expected.implementation_commit,
        profile_digest=lesson_profile_45().digest,
        data_digests=bundle_digests,
        implementation_inventory_digest=_capture_inventory_digest(
            inputs.repository, inputs.expected.implementation_commit
        ),
        inventory_commitment=_inventory_commitment(inventory),
        bank_receipts=tuple(receipt.to_dict() for receipt in receipts[0]),
        allocation=_allocation_projection(preflight.allocation),
        witness=_witness(preflight.allocation),
        domain_commitments=_domain_commitments(receipts[0], preflight.allocation),
    )
    if ProofCertificate.from_bytes(certificate.to_bytes()).digest != certificate.digest:
        raise ProofSchemaError("Captured certificate did not round-trip canonically.")
    return certificate


def capture_manifest_certificates(
    *,
    anchors: Mapping[str, RuntimeAnchor],
    bundle: data.DataBundle,
    repository: Path,
    expected: ExpectedAuthority,
) -> tuple[ProofCertificate, ...]:
    """Capture exactly the three pinned manifest anchors in manifest order."""
    frozen_manifest_digest, _frozen_data_digests = frozen_content_authority(repository)
    active_manifest = load_manifest()
    if (
        active_manifest.sha256 != CONTENT_MANIFEST_DIGEST
        or active_manifest.sha256 != frozen_manifest_digest
    ):
        raise ProofCaptureError("Pinned qualification manifest does not match content baseline.")
    validated = active_manifest.validate_runtime_anchors(anchors)
    return tuple(
        capture_certificate(CaptureInputs(anchor, bundle, repository, expected))
        for anchor in validated
    )


def persist_certificate_create_only(path: Path, certificate: ProofCertificate) -> None:
    """Atomically persist canonical certificate bytes without replacing an output."""
    target = Path(path)
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ProofCaptureError("Certificate output directory is unavailable.")
    payload = certificate.to_bytes()
    temporary = parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # ``link`` is create-only: an existing target fails without replacing
        # it, and the completed temporary inode becomes visible atomically.
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError as error:
        raise ProofCaptureError("Certificate output already exists.") from error
    except OSError as error:
        raise ProofCaptureError("Certificate output could not be persisted.") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
