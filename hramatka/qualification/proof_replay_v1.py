"""Independent, offline replay for content-free 45-minute proof records."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hramatka.engine import data
from hramatka.engine.anchor_inventory_v3 import inventory_from_anchor
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
from hramatka.engine.lesson_workload_45_v1 import validate_phase_pace

from .manifest import RuntimeAnchor, load_manifest
from .proof_schema_v1 import (
    CONTENT_MANIFEST_DIGEST,
    ExpectedAuthority,
    ProofCertificate,
    ProofSchemaError,
    authority_envelope,
    frozen_content_authority,
    implementation_inventory_digest,
    sha256,
)


class ProofReplayError(RuntimeError):
    """A certificate does not match independently rebuilt local authority."""


def _replay_inventory_digest(repository: Path, commit: str) -> str:
    """Bind replay to the hardened, exact executing implementation surface."""
    try:
        return implementation_inventory_digest(repository, commit)
    except ProofSchemaError as exc:
        raise ProofReplayError("Replay Git authority cannot be read locally.") from exc


def _verify_frozen_bundle(
    bundle: data.DataBundle, policy: Mapping[str, Mapping[str, object]]
) -> dict[str, str]:
    """Replay-owned mounted-byte validation against Git-derived policy only."""
    root = bundle.root.resolve()
    inputs = bundle.manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(policy):
        raise ProofReplayError("Replay data manifest does not match frozen Git policy.")
    digests: dict[str, str] = {}
    for name, row in policy.items():
        manifest_row = inputs[name]
        expected_path = root / str(row["path"])
        if (
            not isinstance(manifest_row, dict)
            or manifest_row.get("path") != row["path"]
            or bundle.path(name) != expected_path
        ):
            raise ProofReplayError("Replay data manifest does not match frozen Git policy.")
        path = bundle.path(name)
        if path.is_symlink() or not path.is_file() or path.parent.resolve() != root:
            raise ProofReplayError("Replay mounted data path does not match frozen Git policy.")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != row["sha256"] or size != row["size"]:
            raise ProofReplayError("Replay mounted data does not match frozen Git policy.")
        digests[name] = digest.hexdigest()
    return digests


def _replay_execution_bundle(
    bundle: data.DataBundle, policy: Mapping[str, Mapping[str, object]]
) -> data.DataBundle:
    """Detach replay execution from a caller-owned mutable data manifest."""
    root = bundle.root.resolve()
    inputs = bundle.manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(policy):
        raise ProofReplayError("Replay data manifest does not match frozen Git policy.")
    frozen_inputs: dict[str, dict[str, object]] = {}
    for name, row in policy.items():
        manifest_row = inputs[name]
        expected_path = root / str(row["path"])
        if (
            not isinstance(manifest_row, dict)
            or manifest_row.get("path") != row["path"]
            or bundle.path(name) != expected_path
        ):
            raise ProofReplayError("Replay data manifest does not match frozen Git policy.")
        frozen_inputs[name] = {
            "path": row["path"],
            "sha256": row["sha256"],
            "size": row["size"],
            "required": True,
        }
    return data.DataBundle(root=root, manifest={"inputs": frozen_inputs})


def _bank_group(identifier: str, activity_type: str) -> int:
    parts = identifier.split(":")
    if len(parts) < 2 or parts[0] != activity_type or not parts[1].isdigit() or int(parts[1]) < 1:
        raise ProofReplayError("Rebuilt inventory has an unauthorized bank identity.")
    return int(parts[1])


def _expected_bank_rows(
    inventory: object, *, manifest_digest: str, source_identity: str
) -> list[dict[str, object]]:
    profile = lesson_profile_45()
    grouped: dict[tuple[str, int], list[str]] = defaultdict(list)
    for candidate in inventory.candidates:
        key = (
            candidate.activity_type,
            _bank_group(candidate.candidate_id, candidate.activity_type),
        )
        grouped[key].append(candidate.candidate_id)
    for fact in inventory.true_false_facts:
        grouped[("true-false", _bank_group(fact.fact_id, "true-false"))].append(fact.fact_id)
    for pair in inventory.atlas_pairs:
        grouped[("match-up", _bank_group(pair.pair_id, "match-up"))].append(pair.pair_id)
    for task in inventory.writing_tasks:
        grouped[("short-writing", _bank_group(task.task_id, "short-writing"))].append(task.task_id)
    for request in inventory.mark_requests:
        group = _bank_group(request.request_id, "mark-the-words")
        grouped[("mark-the-words", group)].extend(request.target_token_ids)
    covered_slots: set[str] = set()
    for activity_type, group_number in grouped:
        try:
            covered_slots.update(
                slot_id for slot_id, _shared in profile.placements_for(activity_type, group_number)
            )
        except ValueError as exc:
            raise ProofReplayError("Rebuilt inventory has an unauthorized bank.") from exc
    if {slot.slot_id for slot in profile.slots} - covered_slots:
        raise ProofReplayError(
            "Rebuilt inventory does not contain the complete authorized bank set."
        )
    rows: list[dict[str, object]] = []
    for (activity_type, group_number), candidate_ids in sorted(grouped.items()):
        try:
            placements = profile.placements_for(activity_type, group_number)
        except ValueError as exc:
            raise ProofReplayError("Rebuilt bank has no authorized profile placement.") from exc
        bank_id = f"{activity_type}:{group_number}"
        origin_commitment = sha256(
            {
                "origin_kind": "deterministic-local-inventory",
                "source_identity": source_identity,
                "bank_id": bank_id,
                "candidate_ids": tuple(candidate_ids),
                "placements": placements,
                "authority_digest": profile.digest,
            }
        )
        rows.append(
            {
                "version": "HramatkaCandidateBankReceipt.v1",
                "bank_id": bank_id,
                "activity_type": activity_type,
                "response_demand_tier": profile.tier_for(activity_type),
                "candidate_ids": candidate_ids,
                "count": len(candidate_ids),
                "candidate_offsets": [
                    {"candidate_id": candidate_id, "unit_offset": offset}
                    for offset, candidate_id in enumerate(candidate_ids)
                ],
                "candidate_provenance": [
                    {
                        "candidate_id": candidate_id,
                        "commitment": sha256(
                            {
                                "bank_id": bank_id,
                                "candidate_id": candidate_id,
                                "unit_offset": offset,
                                "source_identity": source_identity,
                            }
                        ),
                    }
                    for offset, candidate_id in enumerate(candidate_ids)
                ],
                "eligible_placements": [
                    {"slot_id": slot_id, "mutually_exclusive": mutually_exclusive}
                    for slot_id, mutually_exclusive in placements
                ],
                "origin_kind": "deterministic-local-inventory",
                "origin_commitment": origin_commitment,
                "authority_digest": profile.digest,
                "manifest_digest": manifest_digest,
                "source_identity": source_identity,
            }
        )
    if not rows:
        raise ProofReplayError("Rebuilt inventory did not contain any candidate banks.")
    seal = sha256(
        {
            "manifest_digest": manifest_digest,
            "source_identity": source_identity,
            "profile_digest": profile.digest,
            "banks": [
                {"bank_id": row["bank_id"], "candidate_ids": row["candidate_ids"]} for row in rows
            ],
        }
    )
    for row in rows:
        row["inventory_seal"] = seal
    return rows


def _inventory_commitment(inventory: object) -> str:
    return sha256(
        {
            "candidate_ids": [candidate.candidate_id for candidate in inventory.candidates],
            "true_false_ids": [fact.fact_id for fact in inventory.true_false_facts],
            "pair_ids": [pair.pair_id for pair in inventory.atlas_pairs],
            "writing_ids": [task.task_id for task in inventory.writing_tasks],
            "mark_target_ids": [
                token_id
                for request in inventory.mark_requests
                for token_id in request.target_token_ids
            ],
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


def _replay_allocation_projection(allocation: LessonAllocation) -> dict[str, object]:
    """Independently enumerate replay geometry without capture's projection helper."""
    slot_rows: list[dict[str, object]] = []
    for slot in allocation.slots:
        units: list[dict[str, object]] = []
        for unit in slot.plan.units:
            resource_claims = [
                {"kind": claim.kind, "resource_id": claim.resource_id}
                for claim in unit.resource_claims
            ]
            citation_locators = [citation.locator for citation in unit.citation_plan]
            units.append(
                {
                    "unit_id": unit.unit_id,
                    "claims": resource_claims,
                    "reservations": list(resource_claims),
                    "locators": citation_locators,
                    "plan_digest": sha256(
                        {
                            "unit_id": unit.unit_id,
                            "claims": resource_claims,
                            "locators": citation_locators,
                        }
                    ),
                }
            )
        conditional_rows: list[dict[str, object]] = []
        for conditional in slot.conditional_replacements:
            conditional_units: list[dict[str, object]] = []
            for unit in conditional.plan.units:
                claims = [
                    {"kind": claim.kind, "resource_id": claim.resource_id}
                    for claim in unit.resource_claims
                ]
                locators = [citation.locator for citation in unit.citation_plan]
                conditional_units.append(
                    {
                        "unit_id": unit.unit_id,
                        "claims": claims,
                        "reservations": list(claims),
                        "locators": locators,
                        "plan_digest": sha256(
                            {"unit_id": unit.unit_id, "claims": claims, "locators": locators}
                        ),
                    }
                )
            conditional_rows.append(
                {"activity_type": conditional.activity_type, "units": conditional_units}
            )
        slot_rows.append(
            {
                "slot_id": slot.slot_id,
                "phase": slot.phase,
                "requested_type": slot.requested_type,
                "scheduled_type": slot.scheduled_type,
                "substitution_reason": slot.substitution_reason,
                "units": units,
                "conditional_replacements": conditional_rows,
            }
        )
    return {
        "paragraph_ids": [paragraph for paragraph in allocation.paragraph_ids],
        "slots": slot_rows,
        "canonical_allocation_digest": hashlib.sha256(allocation.canonical_bytes()).hexdigest(),
    }


def _witness(allocation: LessonAllocation) -> dict[str, object]:
    projection = _replay_allocation_projection(allocation)
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


def _rebuilt_domain_commitments(
    bank_rows: list[dict[str, object]], allocation: LessonAllocation
) -> dict[str, object]:
    """Replay-owned reconstruction of every certified structural domain."""
    allocation_rows = _replay_allocation_projection(allocation)
    slot_rows = allocation_rows["slots"]
    unit_rows = [unit for slot in slot_rows for unit in slot["units"]]
    claim_rows = [claim for unit in unit_rows for claim in unit["claims"]]
    locator_rows = [locator for unit in unit_rows for locator in unit["locators"]]
    replacement_rows = [
        replacement for slot in slot_rows for replacement in slot["conditional_replacements"]
    ]
    reconstructed = {
        "candidate": [
            {
                "bank_id": bank["bank_id"],
                "candidate_id": candidate_id,
                "offset": offset,
            }
            for bank in bank_rows
            for offset, candidate_id in enumerate(bank["candidate_ids"])
        ],
        "provenance": [
            {
                "bank_id": bank["bank_id"],
                "candidate_id": candidate_id,
                "commitment": bank["candidate_provenance"][offset]["commitment"],
            }
            for bank in bank_rows
            for offset, candidate_id in enumerate(bank["candidate_ids"])
        ],
        "bank": [bank["bank_id"] for bank in bank_rows],
        "placement": [
            {
                "bank_id": bank["bank_id"],
                "slot_id": placement["slot_id"],
                "exclusive": placement["mutually_exclusive"],
            }
            for bank in bank_rows
            for placement in bank["eligible_placements"]
        ],
        "slot": [{"slot_id": slot["slot_id"], "phase": slot["phase"]} for slot in slot_rows],
        "unit": [unit["unit_id"] for unit in unit_rows],
        "claim": claim_rows,
        "locator": locator_rows,
        "plan": [{"unit_id": unit["unit_id"], "digest": unit["plan_digest"]} for unit in unit_rows],
        "reservation": [reservation for unit in unit_rows for reservation in unit["reservations"]],
        "allocation": [
            {
                "slot_id": slot["slot_id"],
                "requested_type": slot["requested_type"],
                "scheduled_type": slot["scheduled_type"],
                "substitution_reason": slot["substitution_reason"],
            }
            for slot in slot_rows
        ],
        "replacement": replacement_rows,
        "witness": _witness(allocation)["rows"],
    }
    return {
        domain: {"count": len(rows), "digest": sha256(rows)}
        for domain, rows in reconstructed.items()
    }


@dataclass(frozen=True)
class ReplayInputs:
    certificate_bytes: bytes
    anchor: RuntimeAnchor
    bundle: data.DataBundle
    repository: Path
    expected: ExpectedAuthority


def replay_certificate(inputs: ReplayInputs) -> ProofCertificate:
    """Independently rebuild and verify one certificate before trusting its rows."""
    # Authority is outside certificate control.  Inspect only this bounded
    # envelope before parsing declared membership/geometry, so a malformed
    # payload cannot mask a certificate-selected commit or baseline.
    try:
        repository_commit, manifest_digest, profile_digest = authority_envelope(
            inputs.certificate_bytes
        )
    except ProofSchemaError as exc:
        raise ProofReplayError("Certificate authority envelope is invalid.") from exc
    if repository_commit != inputs.expected.implementation_commit:
        raise ProofReplayError(
            "Certificate implementation authority does not match verifier policy."
        )
    if manifest_digest != CONTENT_MANIFEST_DIGEST:
        raise ProofReplayError("Certificate content baseline does not match verifier policy.")
    if profile_digest != lesson_profile_45().digest:
        raise ProofReplayError("Certificate profile authority has drifted.")
    try:
        certificate = ProofCertificate.from_bytes(inputs.certificate_bytes)
    except ProofSchemaError as exc:
        raise ProofReplayError("Certificate geometry is invalid.") from exc
    if certificate.to_bytes() != inputs.certificate_bytes:
        raise ProofReplayError("Proof certificate bytes are not canonical.")
    frozen_manifest_digest, frozen_data_digests = frozen_content_authority(inputs.repository)
    manifest = load_manifest()
    if manifest.sha256 != CONTENT_MANIFEST_DIGEST or manifest.sha256 != frozen_manifest_digest:
        raise ProofReplayError("Pinned qualification manifest does not match content baseline.")
    manifest_anchor = {row.id: row for row in manifest.anchors}.get(inputs.anchor.id)
    if (
        manifest_anchor is None
        or certificate.anchor_id != inputs.anchor.id
        or certificate.source_identity != inputs.anchor.source_identity
        or certificate.manifest_digest != manifest.sha256
    ):
        raise ProofReplayError("Certificate anchor authority does not match the pinned manifest.")
    if hashlib.sha256(inputs.anchor.text.encode("utf-8")).hexdigest() != manifest_anchor.sha256:
        raise ProofReplayError("Replay anchor bytes do not match the pinned manifest.")
    if certificate.profile_digest != lesson_profile_45().digest:
        raise ProofReplayError("Certificate profile authority has drifted.")
    execution_bundle = _replay_execution_bundle(inputs.bundle, frozen_data_digests)
    bundle_digests = _verify_frozen_bundle(execution_bundle, frozen_data_digests)
    if (
        dict(inputs.expected.data_digests) != bundle_digests
        or certificate.data_digests != bundle_digests
    ):
        raise ProofReplayError("Certificate data authority has drifted.")
    if certificate.implementation_inventory_digest != _replay_inventory_digest(
        inputs.repository, inputs.expected.implementation_commit
    ):
        raise ProofReplayError("Certificate repository authority has drifted.")
    slots = lesson_profile_45().slots
    with data.use_bundle(execution_bundle):
        inventory = inventory_from_anchor(
            inputs.anchor.text,
            scheduled_types=inventory_candidate_types_45(),
            replacement_types=inventory_replacement_types_45(),
            duration_minutes=45,
        )
        expected_banks = _expected_bank_rows(
            inventory,
            manifest_digest=manifest.sha256,
            source_identity=inputs.anchor.source_identity,
        )
        preflight = preflight_lesson(
            AnchorWindow((AnchorParagraph(inputs.anchor.id, inventory),), 0, 0),
            duration_minutes=45,
            slots=slots,
            builders=slot_builders_45(),
        )
    if preflight.allocation is None:
        raise ProofReplayError("Rebuilt anchor cannot produce a complete deterministic lesson.")
    try:
        validate_phase_pace(preflight.allocation.slots)
    except ValueError as exc:
        raise ProofReplayError("Rebuilt allocation does not meet the pinned phase pace.") from exc
    if certificate.inventory_commitment != _inventory_commitment(inventory):
        raise ProofReplayError("Certificate inventory membership does not match rebuilt authority.")
    if certificate.to_dict()["bank_receipts"] != expected_banks:
        raise ProofReplayError(
            "Certificate bank receipts do not match rebuilt creation-time banks."
        )
    certificate_payload = certificate.to_dict()
    if certificate_payload["allocation"] != _replay_allocation_projection(preflight.allocation):
        raise ProofReplayError("Certificate allocation does not match rebuilt lesson authority.")
    if certificate_payload["witness"] != _witness(preflight.allocation):
        raise ProofReplayError("Certificate witness does not match rebuilt lesson authority.")
    if certificate_payload["domain_commitments"] != _rebuilt_domain_commitments(
        expected_banks, preflight.allocation
    ):
        raise ProofReplayError("Certificate domain commitments do not match rebuilt authority.")
    return certificate


def replay_manifest_certificates(
    *,
    certificate_bytes: Mapping[str, bytes],
    anchors: Mapping[str, RuntimeAnchor],
    bundle: data.DataBundle,
    repository: Path,
    expected: ExpectedAuthority,
) -> tuple[ProofCertificate, ...]:
    """Replay exactly one certificate for every pinned manifest anchor."""
    frozen_manifest_digest, _frozen_data_digests = frozen_content_authority(repository)
    active_manifest = load_manifest()
    if (
        active_manifest.sha256 != CONTENT_MANIFEST_DIGEST
        or active_manifest.sha256 != frozen_manifest_digest
    ):
        raise ProofReplayError("Pinned qualification manifest does not match content baseline.")
    validated = active_manifest.validate_runtime_anchors(anchors)
    if set(certificate_bytes) != {anchor.id for anchor in validated}:
        raise ProofReplayError("Replay certificate set must match manifest anchors exactly.")
    return tuple(
        replay_certificate(
            ReplayInputs(certificate_bytes[anchor.id], anchor, bundle, repository, expected)
        )
        for anchor in validated
    )
