"""Strict, content-free per-cell qualification receipts and aggregation."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hramatka.api import qualified_models
from hramatka.api.qualified_models import (
    DENSITY_CONTRACT_DIGEST,
    DENSITY_CONTRACT_VERSION,
    LOGICAL_MODELS,
    QUALIFICATION_ANCHORS,
    QUALIFIED_MODEL_REGISTRY_VERSION,
    TEMPLATE_SHA256,
    TEMPLATE_VERSION,
    TYPE_KIT_IDENTITY,
    QualificationReceipt,
)
from hramatka.engine.density_evaluator_v3 import MAX_REPAIR_ROUNDS
from hramatka.engine.prompt_pack_v3 import PROMPT_PACK_VERSION, template_digest
from hramatka.engine.prompt_pack_v3 import (
    TEMPLATE_VERSION as LIVE_TEMPLATE_VERSION,
)
from hramatka.engine.prompt_pack_v3 import TYPE_KIT_IDENTITY as LIVE_TYPE_KIT_IDENTITY
from hramatka.engine.serializer_policy import serializer_temperature
from hramatka.engine.teacher_ready_density_v3 import (
    FLOOR_TABLE,
    TEACHER_READY_DENSITY_VERSION,
    density_floor_fingerprint,
)

CELL_RECEIPT_SCHEMA_VERSION = "ProductionQualificationCellReceipt.v6"
DIAGNOSTIC_RECEIPT_SCHEMA_VERSION = "ProductionQualificationDensityDiagnostic.v2"
AGGREGATION_PROMPT_HASHES_SCHEMA_VERSION = "ProductionQualificationPromptHashes.v2"
AGGREGATION_TARGETS_SCHEMA_VERSION = "ProductionQualificationTargets.v1"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_RE = re.compile(r"^[a-f0-9]{40,64}$")
_OUTCOMES = frozenset({"passed", "failed"})
_SEMANTIC_GATES = frozenset({"not_run", "passed", "failed"})
_V3_SLOT_DISPOSITIONS = frozenset({"ready", "tray", "density_shortfall", "dropped"})
_SLOT_ID_RE = re.compile(r"^P([1-3])-A([1-9][0-9]*)$")
_PROVENANCE_TIERS = frozenset({"api_observed", "cli_self_reported"})
# The 45-minute qualification matrix has the same immutable 3/4/1 phase
# shape as the v3 allocation contract.  A passing cell must account for each
# scheduled position separately; it may not repeat a dense slot to compensate
# for a sparse or absent one.
_QUALIFICATION_SLOT_PHASES = {
    "P1-A1": 1,
    "P1-A2": 1,
    "P1-A3": 1,
    "P2-A1": 2,
    "P2-A2": 2,
    "P2-A3": 2,
    "P2-A4": 2,
    "P3-A1": 3,
}


class QualificationError(ValueError):
    """A receipt is incomplete, stale, duplicate, or otherwise untrustworthy."""


def assert_live_v3_authorities() -> None:
    """Reject qualification work when selector literals drift from live v3 code.

    The production registry intentionally contains literals so an empty
    qualification registry can remain fail-closed.  A transcription must still
    prove every one of those literals names the currently importable v3
    authority; otherwise a self-consistent but stale receipt run could be
    copied into the selector.
    """
    if (
        PROMPT_PACK_VERSION != qualified_models.PROMPT_PACK_VERSION
        or LIVE_TEMPLATE_VERSION != qualified_models.TEMPLATE_VERSION
        or template_digest() != qualified_models.TEMPLATE_SHA256
        or TEACHER_READY_DENSITY_VERSION != qualified_models.DENSITY_CONTRACT_VERSION
        or density_floor_fingerprint() != qualified_models.DENSITY_CONTRACT_DIGEST
        or LIVE_TYPE_KIT_IDENTITY != qualified_models.TYPE_KIT_IDENTITY
    ):
        raise QualificationError(
            "Production qualification literals do not match the live v3 authorities."
        )


def _canonical_digest(value: object) -> str:
    import hashlib

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise QualificationError(f"{field} must be a SHA-256 digest.")
    return value


def _require_exact_keys(value: object, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise QualificationError(f"{label} has an invalid schema.")
    return value


@dataclass(frozen=True)
class RouteBinding:
    route_id: str
    host: str
    model_id: str

    @classmethod
    def from_dict(cls, value: object, *, label: str) -> RouteBinding:
        row = _require_exact_keys(value, {"host", "model_id", "route_id"}, label)
        route_id = row["route_id"]
        host = row["host"]
        model_id = row["model_id"]
        if not all(isinstance(item, str) and item for item in (route_id, host, model_id)):
            raise QualificationError(f"{label} contains invalid route metadata.")
        return cls(route_id=route_id, host=host, model_id=model_id)

    def as_dict(self) -> dict[str, str]:
        return {"route_id": self.route_id, "host": self.host, "model_id": self.model_id}


def qualification_matrix(
    logical_model_ids: tuple[str, ...] | None = None,
) -> tuple[tuple[str, RouteBinding], ...]:
    """Return whole configured routes for an explicit logical-model target set.

    A target is deliberately a set of *logical models*, never individual
    routes.  Selecting a model therefore continues to require every route it
    ships, while allowing an independently complete three-anchor matrix to
    qualify one model without waiting for unrelated models' credentials.
    ``None`` retains the full catalog matrix used by the legacy command.
    """
    model_ids = tuple(model.id for model in LOGICAL_MODELS)
    if logical_model_ids is None:
        selected = frozenset(model_ids)
    else:
        requested = tuple(logical_model_ids)
        if not requested:
            raise QualificationError("Qualification target must name at least one logical model.")
        if len(requested) != len(set(requested)):
            raise QualificationError("Qualification target contains a duplicate logical model.")
        unknown = sorted(set(requested) - set(model_ids))
        if unknown:
            raise QualificationError("Qualification target contains an unknown logical model.")
        selected = frozenset(requested)
    return tuple(
        (model.id, RouteBinding(route.id, route.host, route.model_id))
        for model in LOGICAL_MODELS
        if model.id in selected
        for route in model.provider_routes
    )


def qualification_target_model_ids(
    logical_model_ids: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Return target identities in the catalog's stable order after validation."""
    ordered: list[str] = []
    for model_id, _route in qualification_matrix(logical_model_ids):
        if model_id not in ordered:
            ordered.append(model_id)
    return tuple(ordered)


@dataclass(frozen=True)
class ProviderProvenance:
    """Content-free evidence tier for one route's raw generator outputs."""

    tier: str
    client_version: str | None = None
    requested_model: str | None = None
    raw_output_sha256: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> ProviderProvenance:
        row = _require_exact_keys(
            value,
            {"tier", "client_version", "requested_model", "raw_output_sha256"},
            "provider provenance",
        )
        tier = row["tier"]
        if tier not in _PROVENANCE_TIERS:
            raise QualificationError("Provider provenance tier is unsupported.")
        hashes = row["raw_output_sha256"]
        if not isinstance(hashes, (list, tuple)) or not all(
            isinstance(item, str) and _SHA256_RE.fullmatch(item) is not None for item in hashes
        ):
            raise QualificationError("Provider provenance raw-output hashes are invalid.")
        if tier == "api_observed":
            if row["client_version"] is not None:
                raise QualificationError(
                    "API-observed provenance must not claim a CLI client version."
                )
            if row["requested_model"] is not None and (
                not isinstance(row["requested_model"], str) or not row["requested_model"]
            ):
                raise QualificationError("API-observed provenance has an invalid requested model.")
        elif (
            not isinstance(row["client_version"], str)
            or not row["client_version"]
            or not isinstance(row["requested_model"], str)
            or not row["requested_model"]
            or not hashes
        ):
            raise QualificationError("CLI provenance must record client, model, and output hashes.")
        return cls(
            tier=tier,
            client_version=row["client_version"],
            requested_model=row["requested_model"],
            raw_output_sha256=tuple(hashes),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "client_version": self.client_version,
            "requested_model": self.requested_model,
            "raw_output_sha256": list(self.raw_output_sha256),
        }


@dataclass(frozen=True)
class RepairTraceEntry:
    mode: str
    phase: int
    expected_route: RouteBinding
    observed_route: RouteBinding

    @classmethod
    def from_dict(cls, value: object) -> RepairTraceEntry:
        row = _require_exact_keys(
            value, {"expected_route", "mode", "observed_route", "phase"}, "repair trace entry"
        )
        mode = row["mode"]
        phase = row["phase"]
        if mode not in {"initial", "repair"} or type(phase) is not int or phase not in {1, 2, 3}:
            raise QualificationError("Repair trace entry has invalid mode or phase.")
        return cls(
            mode=mode,
            phase=phase,
            expected_route=RouteBinding.from_dict(row["expected_route"], label="expected route"),
            observed_route=RouteBinding.from_dict(row["observed_route"], label="observed route"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "phase": self.phase,
            "expected_route": self.expected_route.as_dict(),
            "observed_route": self.observed_route.as_dict(),
        }


@dataclass(frozen=True)
class DensitySummary:
    """Secondary content-free totals, derived from v3 block receipts."""

    lesson_units: int
    ready_units: int
    tray_units: int
    floor_units: int
    phase_units: Mapping[str, int]
    ready_phase_units: Mapping[str, int]
    tray_phase_units: Mapping[str, int]
    slot_count: int
    ready_slots: int
    tray_slots: int
    disposition: str

    @classmethod
    def from_dict(cls, value: object) -> DensitySummary:
        row = _require_exact_keys(
            value,
            {
                "lesson_units",
                "ready_units",
                "tray_units",
                "floor_units",
                "phase_units",
                "ready_phase_units",
                "tray_phase_units",
                "slot_count",
                "ready_slots",
                "tray_slots",
                "disposition",
            },
            "density",
        )
        count_fields = (
            "lesson_units",
            "ready_units",
            "tray_units",
            "floor_units",
            "slot_count",
            "ready_slots",
            "tray_slots",
        )
        phase_count_fields = ("phase_units", "ready_phase_units", "tray_phase_units")
        if (
            any(type(row[field]) is not int or row[field] < 0 for field in count_fields)
            or row["disposition"] not in {"teacher_ready", "recoverable_draft"}
            or any(
                not isinstance(row[field], dict)
                or set(row[field]) != {"1", "2", "3"}
                or any(type(count) is not int or count < 0 for count in row[field].values())
                for field in phase_count_fields
            )
            or row["floor_units"] != row["ready_units"] + row["tray_units"]
            or row["lesson_units"] != row["floor_units"]
            or row["lesson_units"] != sum(row["phase_units"].values())
            or row["ready_units"] != sum(row["ready_phase_units"].values())
            or row["tray_units"] != sum(row["tray_phase_units"].values())
            or row["slot_count"] != row["ready_slots"] + row["tray_slots"]
            or any(
                row["phase_units"][phase]
                != row["ready_phase_units"][phase] + row["tray_phase_units"][phase]
                for phase in ("1", "2", "3")
            )
        ):
            raise QualificationError("Density summary has invalid values.")
        return cls(
            lesson_units=row["lesson_units"],
            ready_units=row["ready_units"],
            tray_units=row["tray_units"],
            floor_units=row["floor_units"],
            phase_units=dict(row["phase_units"]),
            ready_phase_units=dict(row["ready_phase_units"]),
            tray_phase_units=dict(row["tray_phase_units"]),
            slot_count=row["slot_count"],
            ready_slots=row["ready_slots"],
            tray_slots=row["tray_slots"],
            disposition=row["disposition"],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "lesson_units": self.lesson_units,
            "ready_units": self.ready_units,
            "tray_units": self.tray_units,
            "floor_units": self.floor_units,
            "phase_units": dict(self.phase_units),
            "ready_phase_units": dict(self.ready_phase_units),
            "tray_phase_units": dict(self.tray_phase_units),
            "slot_count": self.slot_count,
            "ready_slots": self.ready_slots,
            "tray_slots": self.tray_slots,
            "disposition": self.disposition,
        }


@dataclass(frozen=True)
class SlotTelemetry:
    """One content-free v3 evaluation attempt for a scheduled slot."""

    slot_id: str
    phase: int
    activity_type: str
    disposition: str
    units: int
    floor_met: bool
    contract_version: str = TEACHER_READY_DENSITY_VERSION
    repair_rounds: int = 0
    replacement_used: bool = False
    unassigned_errors_count: int = 0

    @classmethod
    def from_dict(cls, value: object) -> SlotTelemetry:
        row = _require_exact_keys(
            value,
            {
                "slot_id",
                "phase",
                "type",
                "disposition",
                "units",
                "floor_met",
                "contract_version",
                "repair_rounds",
                "replacement_used",
                "unassigned_errors_count",
            },
            "v3 slot telemetry",
        )
        slot_id = row["slot_id"]
        activity_type = row["type"]
        slot_match = _SLOT_ID_RE.fullmatch(slot_id) if isinstance(slot_id, str) else None
        if (
            slot_match is None
            or type(row["phase"]) is not int
            or row["phase"] not in {1, 2, 3}
            or int(slot_match.group(1)) != row["phase"]
            or not isinstance(activity_type, str)
            or activity_type not in FLOOR_TABLE
            or row["disposition"] not in _V3_SLOT_DISPOSITIONS
            or type(row["units"]) is not int
            or row["units"] < 0
            or type(row["floor_met"]) is not bool
            or not isinstance(row["contract_version"], str)
            or not row["contract_version"]
            or type(row["repair_rounds"]) is not int
            or row["repair_rounds"] < 0
            or row["repair_rounds"] > MAX_REPAIR_ROUNDS
            or type(row["replacement_used"]) is not bool
            or type(row["unassigned_errors_count"]) is not int
            or row["unassigned_errors_count"] < 0
        ):
            raise QualificationError("v3 slot telemetry has invalid values.")
        floor_met = row["units"] >= FLOOR_TABLE[activity_type].minimum_units
        if row["floor_met"] != floor_met:
            raise QualificationError("v3 slot telemetry floor status must derive from its units.")
        if row["disposition"] in {"ready", "tray"} and not floor_met:
            raise QualificationError("Ready/tray v3 telemetry must meet its own floor.")
        if row["disposition"] == "density_shortfall" and floor_met:
            raise QualificationError("Density shortfall telemetry must be below its own floor.")
        return cls(
            slot_id=slot_id,
            phase=row["phase"],
            activity_type=activity_type,
            disposition=row["disposition"],
            units=row["units"],
            floor_met=row["floor_met"],
            contract_version=row["contract_version"],
            repair_rounds=row["repair_rounds"],
            replacement_used=row["replacement_used"],
            unassigned_errors_count=row["unassigned_errors_count"],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "slot_id": self.slot_id,
            "phase": self.phase,
            "type": self.activity_type,
            "disposition": self.disposition,
            "units": self.units,
            "floor_met": self.floor_met,
            "contract_version": self.contract_version,
            "repair_rounds": self.repair_rounds,
            "replacement_used": self.replacement_used,
            "unassigned_errors_count": self.unassigned_errors_count,
        }


@dataclass(frozen=True)
class CellReceipt:
    """A content-free proof for one anchor × configured provider route cell."""

    source_commit: str
    harness_sha256: str
    manifest_sha256: str
    anchor_id: str
    anchor_sha256: str
    logical_model_id: str
    expected_route: RouteBinding
    observed_route: RouteBinding
    prompt_sha256: str
    prompt_pack_version: str
    template_version: str
    template_sha256: str
    density_contract_version: str
    density_contract_sha256: str
    type_kit_identity: str
    serializer_temperature: float
    registry_sha256: str
    engine_sha256: str
    flag_sha256: str
    density: DensitySummary
    slot_telemetry: tuple[SlotTelemetry, ...]
    repair_trace: tuple[RepairTraceEntry, ...]
    semantic_gate: str
    outcome: str
    provider_provenance: ProviderProvenance = ProviderProvenance("api_observed")
    schema_version: str = CELL_RECEIPT_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: object) -> CellReceipt:
        row = _require_exact_keys(
            value,
            {
                "anchor_id",
                "anchor_sha256",
                "density",
                "density_contract_sha256",
                "density_contract_version",
                "engine_sha256",
                "expected_route",
                "flag_sha256",
                "harness_sha256",
                "logical_model_id",
                "manifest_sha256",
                "observed_route",
                "outcome",
                "provider_provenance",
                "prompt_sha256",
                "prompt_pack_version",
                "registry_sha256",
                "repair_trace",
                "schema_version",
                "semantic_gate",
                "serializer_temperature",
                "source_commit",
                "slot_telemetry",
                "template_sha256",
                "template_version",
                "type_kit_identity",
            },
            "cell receipt",
        )
        if row["schema_version"] != CELL_RECEIPT_SCHEMA_VERSION:
            raise QualificationError("Cell receipt schema version is unsupported.")
        source_commit = row["source_commit"]
        if not isinstance(source_commit, str) or _COMMIT_RE.fullmatch(source_commit) is None:
            raise QualificationError("Cell receipt source_commit is invalid.")
        for field in (
            "harness_sha256",
            "manifest_sha256",
            "anchor_sha256",
            "prompt_sha256",
            "template_sha256",
            "density_contract_sha256",
            "registry_sha256",
            "engine_sha256",
            "flag_sha256",
        ):
            _require_sha256(row[field], field)
        if (
            not isinstance(row["anchor_id"], str)
            or not row["anchor_id"]
            or not isinstance(row["logical_model_id"], str)
            or not row["logical_model_id"]
            or not isinstance(row["density_contract_version"], str)
            or not row["density_contract_version"]
            or not isinstance(row["prompt_pack_version"], str)
            or not row["prompt_pack_version"]
            or not isinstance(row["template_version"], str)
            or not row["template_version"]
            or not isinstance(row["type_kit_identity"], str)
            or not row["type_kit_identity"]
            or type(row["serializer_temperature"]) not in {int, float}
            or float(row["serializer_temperature"]) != serializer_temperature()
            or row["outcome"] not in _OUTCOMES
            or row["semantic_gate"] not in _SEMANTIC_GATES
            or not isinstance(row["repair_trace"], list)
            or not isinstance(row["slot_telemetry"], list)
        ):
            raise QualificationError("Cell receipt has invalid required values.")
        trace = tuple(RepairTraceEntry.from_dict(entry) for entry in row["repair_trace"])
        if not trace:
            raise QualificationError("Cell receipt must contain a route-bound generation trace.")
        slot_telemetry = tuple(SlotTelemetry.from_dict(entry) for entry in row["slot_telemetry"])
        if (
            not slot_telemetry
            or tuple(sorted(slot_telemetry, key=lambda entry: (entry.phase, entry.slot_id)))
            != slot_telemetry
        ):
            raise QualificationError(
                "v3 slot telemetry must be non-empty and deterministically ordered."
            )
        accepted = tuple(
            entry for entry in slot_telemetry if entry.disposition in {"ready", "tray"}
        )
        if len({entry.slot_id for entry in accepted}) != len(accepted):
            raise QualificationError("Accepted v3 slot telemetry may not repeat a scheduled slot.")
        density = DensitySummary.from_dict(row["density"])
        if (
            density.lesson_units != sum(entry.units for entry in accepted)
            or density.slot_count != len(accepted)
            or density.ready_slots != sum(entry.disposition == "ready" for entry in accepted)
            or density.tray_slots != sum(entry.disposition == "tray" for entry in accepted)
            or any(
                density.phase_units[phase]
                != sum(entry.units for entry in accepted if str(entry.phase) == phase)
                or density.ready_phase_units[phase]
                != sum(
                    entry.units
                    for entry in accepted
                    if str(entry.phase) == phase and entry.disposition == "ready"
                )
                or density.tray_phase_units[phase]
                != sum(
                    entry.units
                    for entry in accepted
                    if str(entry.phase) == phase and entry.disposition == "tray"
                )
                for phase in ("1", "2", "3")
            )
        ):
            raise QualificationError("v3 density totals must derive from accepted slot receipts.")
        if (
            density.disposition == "teacher_ready"
            and {entry.slot_id: entry.phase for entry in accepted} != _QUALIFICATION_SLOT_PHASES
        ):
            raise QualificationError(
                "Teacher-ready v3 density requires every scheduled qualification slot exactly once."
            )
        return cls(
            source_commit=source_commit,
            harness_sha256=row["harness_sha256"],
            manifest_sha256=row["manifest_sha256"],
            anchor_id=row["anchor_id"],
            anchor_sha256=row["anchor_sha256"],
            logical_model_id=row["logical_model_id"],
            expected_route=RouteBinding.from_dict(row["expected_route"], label="expected route"),
            observed_route=RouteBinding.from_dict(row["observed_route"], label="observed route"),
            prompt_sha256=row["prompt_sha256"],
            prompt_pack_version=row["prompt_pack_version"],
            template_version=row["template_version"],
            template_sha256=row["template_sha256"],
            density_contract_version=row["density_contract_version"],
            density_contract_sha256=row["density_contract_sha256"],
            type_kit_identity=row["type_kit_identity"],
            serializer_temperature=float(row["serializer_temperature"]),
            registry_sha256=row["registry_sha256"],
            engine_sha256=row["engine_sha256"],
            flag_sha256=row["flag_sha256"],
            density=density,
            slot_telemetry=slot_telemetry,
            repair_trace=trace,
            semantic_gate=row["semantic_gate"],
            outcome=row["outcome"],
            provider_provenance=ProviderProvenance.from_dict(row["provider_provenance"]),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_commit": self.source_commit,
            "harness_sha256": self.harness_sha256,
            "manifest_sha256": self.manifest_sha256,
            "anchor_id": self.anchor_id,
            "anchor_sha256": self.anchor_sha256,
            "logical_model_id": self.logical_model_id,
            "expected_route": self.expected_route.as_dict(),
            "observed_route": self.observed_route.as_dict(),
            "prompt_sha256": self.prompt_sha256,
            "prompt_pack_version": self.prompt_pack_version,
            "template_version": self.template_version,
            "template_sha256": self.template_sha256,
            "density_contract_version": self.density_contract_version,
            "density_contract_sha256": self.density_contract_sha256,
            "type_kit_identity": self.type_kit_identity,
            "serializer_temperature": self.serializer_temperature,
            "registry_sha256": self.registry_sha256,
            "engine_sha256": self.engine_sha256,
            "flag_sha256": self.flag_sha256,
            "density": self.density.as_dict(),
            "slot_telemetry": [entry.as_dict() for entry in self.slot_telemetry],
            "repair_trace": [entry.as_dict() for entry in self.repair_trace],
            "semantic_gate": self.semantic_gate,
            "outcome": self.outcome,
            "provider_provenance": self.provider_provenance.as_dict(),
        }


def _diagnostic_density_trace(value: object) -> dict[str, object]:
    row = _require_exact_keys(
        value,
        {
            "density_error_codes",
            "gate_outcomes_by_phase",
            "phase_density",
            "repair_invocations",
            "stage",
        },
        "diagnostic density trace",
    )
    if row["stage"] not in {"initial", "repair"} or (
        type(row["repair_invocations"]) is not int or row["repair_invocations"] < 0
    ):
        raise QualificationError("Diagnostic density trace has invalid stage or repair count.")
    errors = row["density_error_codes"]
    phases = row["phase_density"]
    outcomes = row["gate_outcomes_by_phase"]
    if (
        not isinstance(errors, list)
        or not all(isinstance(error, str) for error in errors)
        or not isinstance(phases, dict)
        or not isinstance(outcomes, dict)
        or set(phases) != {"1", "2", "3"}
        or set(outcomes) != {"1", "2", "3"}
    ):
        raise QualificationError("Diagnostic density trace has invalid phase evidence.")
    parsed_phases: dict[str, dict[str, int]] = {}
    parsed_outcomes: dict[str, dict[str, int]] = {}
    for phase in ("1", "2", "3"):
        density = _require_exact_keys(
            phases[phase], {"response_units", "visible_blocks"}, "diagnostic phase density"
        )
        gate = _require_exact_keys(
            outcomes[phase],
            {"dropped", "generated", "ready", "requested", "review"},
            "diagnostic gate outcomes",
        )
        if not all(type(count) is int and count >= 0 for count in density.values()) or not all(
            type(count) is int and count >= 0 for count in gate.values()
        ):
            raise QualificationError("Diagnostic density trace has invalid counts.")
        if gate["generated"] != gate["ready"] + gate["review"] + gate["dropped"]:
            raise QualificationError("Diagnostic gate counts do not reconcile.")
        parsed_phases[phase] = dict(density)
        parsed_outcomes[phase] = dict(gate)
    return {
        "stage": row["stage"],
        "phase_density": parsed_phases,
        "gate_outcomes_by_phase": parsed_outcomes,
        "density_error_codes": list(errors),
        "repair_invocations": row["repair_invocations"],
    }


def _diagnostic_repair_trace(value: object) -> dict[str, object]:
    row = _require_exact_keys(
        value,
        {
            "gate_drops",
            "outcome",
            "phase",
            "response_units_after",
            "response_units_before",
            "round",
            "visible_blocks_after",
            "visible_blocks_before",
        },
        "diagnostic repair trace",
    )
    if (
        row["outcome"] not in {"amended", "provider_failure"}
        or type(row["phase"]) is not int
        or row["phase"] not in {1, 2, 3}
        or type(row["round"]) is not int
        or row["round"] < 1
        or not all(
            type(row[field]) is int and row[field] >= 0
            for field in {
                "gate_drops",
                "response_units_after",
                "response_units_before",
                "visible_blocks_after",
                "visible_blocks_before",
            }
        )
    ):
        raise QualificationError("Diagnostic repair trace has invalid values.")
    return dict(row)


@dataclass(frozen=True)
class DensityDiagnosticReceipt:
    """Strict content-free instrumentation for one explicitly authorized cell."""

    cell_receipt: CellReceipt
    density_trace: tuple[dict[str, object], ...]
    repair_invocation_trace: tuple[dict[str, object], ...]
    schema_version: str = DIAGNOSTIC_RECEIPT_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: object) -> DensityDiagnosticReceipt:
        row = _require_exact_keys(
            value,
            {"cell_receipt", "density_trace", "repair_invocation_trace", "schema_version"},
            "diagnostic receipt",
        )
        if row["schema_version"] != DIAGNOSTIC_RECEIPT_SCHEMA_VERSION:
            raise QualificationError("Diagnostic receipt schema version is unsupported.")
        if not isinstance(row["density_trace"], list) or not isinstance(
            row["repair_invocation_trace"], list
        ):
            raise QualificationError("Diagnostic receipt traces must be lists.")
        density_trace = tuple(_diagnostic_density_trace(entry) for entry in row["density_trace"])
        if not density_trace or density_trace[0]["stage"] != "initial":
            raise QualificationError("Diagnostic receipt lacks an initial density snapshot.")
        repair_trace = tuple(
            _diagnostic_repair_trace(entry) for entry in row["repair_invocation_trace"]
        )
        if density_trace[-1]["repair_invocations"] != len(repair_trace):
            raise QualificationError("Diagnostic repair count does not match its invocation trace.")
        return cls(
            cell_receipt=CellReceipt.from_dict(row["cell_receipt"]),
            density_trace=density_trace,
            repair_invocation_trace=repair_trace,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "cell_receipt": self.cell_receipt.as_dict(),
            "density_trace": [dict(entry) for entry in self.density_trace],
            "repair_invocation_trace": [dict(entry) for entry in self.repair_invocation_trace],
        }


@dataclass(frozen=True)
class RouteAggregate:
    """Three-anchor route aggregation; semantic proof stays intentionally separate."""

    logical_model_id: str
    route: RouteBinding
    cells: tuple[CellReceipt, ...]

    @property
    def passed_anchors(self) -> frozenset[str]:
        return frozenset(cell.anchor_id for cell in self.cells if cell.outcome == "passed")

    @property
    def semantic_gate_passed(self) -> bool:
        # The shadow-tier semantic gate remains advisory by the locked #305
        # ruling.  A reported semantic failure is still not transcribable.
        return all(cell.semantic_gate in {"not_run", "passed"} for cell in self.cells)

    @property
    def current_v3(self) -> bool:
        """Return whether this is a complete current v3 aggregate, not path-only data."""
        return (
            len(self.cells) == len(QUALIFICATION_ANCHORS)
            and self.passed_anchors == QUALIFICATION_ANCHORS
            and all(
                cell.outcome == "passed"
                and cell.density.disposition == "teacher_ready"
                and cell.density.slot_count >= 8
                and cell.density.lesson_units >= 57
                and cell.prompt_pack_version == PROMPT_PACK_VERSION
                and cell.template_version == TEMPLATE_VERSION
                and cell.template_sha256 == template_digest()
                and cell.density_contract_version == DENSITY_CONTRACT_VERSION
                and cell.density_contract_sha256 == density_floor_fingerprint()
                and cell.type_kit_identity == TYPE_KIT_IDENTITY
                and cell.serializer_temperature == serializer_temperature()
                and cell.provider_provenance.tier in _PROVENANCE_TIERS
                for cell in self.cells
            )
            and len({cell.provider_provenance.tier for cell in self.cells}) == 1
        )

    def as_model_receipt(self) -> QualificationReceipt:
        """Produce a selector receipt only from a passing current v3 aggregate."""
        if not self.current_v3 or not self.semantic_gate_passed:
            raise QualificationError("Only a passing current v3 aggregate may qualify a route.")
        cells = tuple(sorted(self.cells, key=lambda cell: cell.anchor_id))
        return QualificationReceipt(
            logical_model_id=self.logical_model_id,
            provider_route=self.route.route_id,
            provider_host=self.route.host,
            provider_model_id=self.route.model_id,
            registry_version=QUALIFIED_MODEL_REGISTRY_VERSION,
            prompt_pack_version=PROMPT_PACK_VERSION,
            prompt_sha256=_canonical_digest(
                [
                    {"anchor_id": cell.anchor_id, "prompt_sha256": cell.prompt_sha256}
                    for cell in cells
                ]
            ),
            template_version=TEMPLATE_VERSION,
            template_sha256=TEMPLATE_SHA256,
            density_contract_version=DENSITY_CONTRACT_VERSION,
            density_contract_digest=DENSITY_CONTRACT_DIGEST,
            type_kit_identity=TYPE_KIT_IDENTITY,
            serializer_temperature=self.cells[0].serializer_temperature,
            passed_anchors=self.passed_anchors,
            passed=True,
            provenance_tier=cells[0].provider_provenance.tier,
        )


def registry_digest() -> str:
    """Digest only public routing identities, never credentials or endpoints."""
    return _canonical_digest(
        {
            "version": QUALIFIED_MODEL_REGISTRY_VERSION,
            "models": [
                {
                    "id": model.id,
                    "routes": [
                        {"id": route.id, "host": route.host, "model_id": route.model_id}
                        for route in model.provider_routes
                    ],
                }
                for model in LOGICAL_MODELS
            ],
        }
    )


def aggregate_receipts(
    receipts: Iterable[CellReceipt | Mapping[str, object]],
    *,
    source_commit: str,
    harness_sha256: str,
    manifest_sha256: str,
    anchor_hashes: Mapping[str, str],
    prompt_hashes: Mapping[tuple[str, str, str], str],
    engine_sha256: str,
    flag_sha256: str,
    logical_model_ids: tuple[str, ...] | None = None,
) -> tuple[RouteAggregate, ...]:
    """Strictly aggregate every targeted configured route × anchor cell.

    The caller supplies current runtime identity values.  Every cell must bind
    them exactly; missing, stale, duplicate, route-mismatched, or failed cells
    are rejected before any selector receipt can exist.  A narrowed target is
    permitted only at a logical-model boundary: each selected model must still
    provide every one of its configured routes and all immutable anchors.
    """
    assert_live_v3_authorities()
    # Re-parse in-memory dataclasses too.  Callers and tests can use
    # ``dataclasses.replace``; qualification must never trust those objects
    # without applying the same strict receipt invariants as persisted JSON.
    parsed = tuple(
        CellReceipt.from_dict(receipt.as_dict())
        if isinstance(receipt, CellReceipt)
        else CellReceipt.from_dict(receipt)
        for receipt in receipts
    )
    matrix = qualification_matrix(logical_model_ids)
    expected_routes = {(model_id, route.route_id): route for model_id, route in matrix}
    expected_cells = {
        (model_id, route_id, anchor_id)
        for model_id, route_id in expected_routes
        for anchor_id in QUALIFICATION_ANCHORS
    }
    if set(anchor_hashes) != set(QUALIFICATION_ANCHORS):
        raise QualificationError("Anchor hashes must cover the immutable three-anchor manifest.")
    if set(prompt_hashes) != expected_cells:
        raise QualificationError("Prompt hashes must cover every configured qualification cell.")
    seen: set[tuple[str, str, str]] = set()
    grouped: dict[tuple[str, str], list[CellReceipt]] = defaultdict(list)
    for receipt in parsed:
        key = (receipt.logical_model_id, receipt.expected_route.route_id, receipt.anchor_id)
        if key in seen:
            raise QualificationError("Duplicate qualification cell receipt.")
        seen.add(key)
        expected_route = expected_routes.get(
            (receipt.logical_model_id, receipt.expected_route.route_id)
        )
        if expected_route is None or receipt.expected_route != expected_route:
            raise QualificationError("Receipt expected route does not match configured routing.")
        if receipt.observed_route != expected_route:
            raise QualificationError("Receipt observed route does not match its expected route.")
        provenance = receipt.provider_provenance
        if expected_route.host == "antigravity-cli":
            if (
                provenance.tier != "cli_self_reported"
                or provenance.requested_model != expected_route.model_id
            ):
                raise QualificationError(
                    "Subscription receipt must retain matching CLI self-reported provenance."
                )
        elif provenance.tier != "api_observed":
            raise QualificationError("API receipt must retain API-observed provenance.")
        if receipt.anchor_sha256 != anchor_hashes.get(receipt.anchor_id):
            raise QualificationError("Receipt anchor hash is stale or mismatched.")
        if receipt.prompt_sha256 != prompt_hashes.get(key):
            raise QualificationError("Receipt prompt hash is stale or mismatched.")
        if (
            receipt.source_commit != source_commit
            or receipt.harness_sha256 != harness_sha256
            or receipt.manifest_sha256 != manifest_sha256
            or receipt.registry_sha256 != registry_digest()
            or receipt.engine_sha256 != engine_sha256
            or receipt.flag_sha256 != flag_sha256
            or receipt.prompt_pack_version != PROMPT_PACK_VERSION
            or receipt.template_version != TEMPLATE_VERSION
            or receipt.template_sha256 != template_digest()
            or receipt.density_contract_version != DENSITY_CONTRACT_VERSION
            or receipt.density_contract_sha256 != DENSITY_CONTRACT_DIGEST
            or receipt.density_contract_sha256 != density_floor_fingerprint()
            or receipt.type_kit_identity != TYPE_KIT_IDENTITY
        ):
            raise QualificationError("Receipt is stale for the current qualification contract.")
        if receipt.outcome != "passed" or receipt.density.disposition != "teacher_ready":
            raise QualificationError(
                "A failed or non-deliverable qualification cell cannot aggregate."
            )
        expected_phase_slots = {"1": 3, "2": 4, "3": 1}
        if (
            receipt.density.slot_count < 8
            or any(
                receipt.density.ready_slots + receipt.density.tray_slots < 8
                or sum(
                    entry.disposition in {"ready", "tray"} and str(entry.phase) == phase
                    for entry in receipt.slot_telemetry
                )
                < expected
                for phase, expected in expected_phase_slots.items()
            )
            or receipt.density.lesson_units < 57
        ):
            raise QualificationError(
                "Receipt v3 unit totals do not meet the B1 45-minute contract."
            )
        for trace in receipt.repair_trace:
            if trace.expected_route != expected_route or trace.observed_route != expected_route:
                raise QualificationError("Generation or repair trace lost route continuity.")
        if not any(trace.mode == "initial" for trace in receipt.repair_trace):
            raise QualificationError("Receipt has no route-bound initial generation trace.")
        grouped[(receipt.logical_model_id, receipt.expected_route.route_id)].append(receipt)
    if seen != expected_cells:
        raise QualificationError(
            "Qualification aggregation requires every configured route × anchor cell."
        )
    return tuple(
        RouteAggregate(
            logical_model_id=model_id,
            route=expected_routes[(model_id, route_id)],
            cells=tuple(sorted(grouped[(model_id, route_id)], key=lambda cell: cell.anchor_id)),
        )
        for model_id, route_id in sorted(grouped)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed aggregation check for persisted qualification cell receipts."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    aggregate = subcommands.add_parser(
        "aggregate", help="validate every persisted cell receipt against the current contract"
    )
    aggregate.add_argument(
        "--receipt-dir",
        type=Path,
        required=True,
        help="operator-local directory containing the persisted cell receipt JSON files",
    )
    aggregate.add_argument(
        "--source-commit",
        help="expected source commit; defaults to the current checkout HEAD",
    )
    return parser


def _load_receipts(receipt_dir: Path) -> tuple[CellReceipt, ...]:
    if not receipt_dir.is_dir():
        raise QualificationError("Receipt directory does not exist.")
    paths = tuple(sorted(receipt_dir.glob("*.json")))
    if not paths:
        raise QualificationError("Receipt directory contains no cell receipt JSON files.")
    try:
        return tuple(
            CellReceipt.from_dict(json.loads(path.read_text(encoding="utf-8"))) for path in paths
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QualificationError("A persisted cell receipt could not be parsed.") from error


def _load_prompt_hashes(receipt_dir: Path) -> dict[tuple[str, str, str], str]:
    path = receipt_dir.parent / "aggregation-prompt-hashes.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QualificationError("Current aggregation prompt hashes are unavailable.") from error
    if not isinstance(value, dict) or set(value) != {"prompt_hashes", "schema_version"}:
        raise QualificationError("Aggregation prompt hashes have an invalid schema.")
    if value["schema_version"] != AGGREGATION_PROMPT_HASHES_SCHEMA_VERSION:
        raise QualificationError("Aggregation prompt hashes have an unsupported schema version.")
    rows = value["prompt_hashes"]
    if not isinstance(rows, list):
        raise QualificationError("Aggregation prompt hashes have an invalid schema.")
    prompt_hashes: dict[tuple[str, str, str], str] = {}
    for row in rows:
        parsed = _require_exact_keys(
            row, {"anchor_id", "logical_model_id", "route_id", "sha256"}, "prompt hash"
        )
        key = (parsed["logical_model_id"], parsed["route_id"], parsed["anchor_id"])
        if not all(isinstance(part, str) and part for part in key):
            raise QualificationError("Aggregation prompt hashes have invalid route identity.")
        if key in prompt_hashes:
            raise QualificationError("Aggregation prompt hashes contain a duplicate cell.")
        prompt_hashes[key] = _require_sha256(parsed["sha256"], "prompt hash")
    return prompt_hashes


def _load_qualification_target_model_ids(receipt_dir: Path) -> tuple[str, ...]:
    """Load the explicit model-level target recorded beside a matrix run."""
    path = receipt_dir.parent / "aggregation-targets.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QualificationError("Qualification target metadata is unavailable.") from error
    if not isinstance(value, dict) or set(value) != {"logical_model_ids", "schema_version"}:
        raise QualificationError("Qualification target metadata has an invalid schema.")
    if value["schema_version"] != AGGREGATION_TARGETS_SCHEMA_VERSION:
        raise QualificationError("Qualification target metadata has an unsupported schema version.")
    rows = value["logical_model_ids"]
    if not isinstance(rows, list) or not all(isinstance(item, str) and item for item in rows):
        raise QualificationError("Qualification target metadata has invalid logical model IDs.")
    requested = tuple(rows)
    canonical = qualification_target_model_ids(requested)
    if requested != canonical:
        raise QualificationError("Qualification target metadata is not in canonical model order.")
    return canonical


def main(argv: list[str] | None = None) -> int:
    """Aggregate persisted receipts against the current checkout's contract."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        # Import lazily: harness imports this module, while this operator command
        # needs the same current-code digests that the harness writes into cells.
        from .harness import _current_engine_digest, _file_digest, _flag_digest, _source_commit
        from .manifest import load_manifest

        receipts = _load_receipts(args.receipt_dir)
        manifest = load_manifest()
        aggregates = aggregate_receipts(
            receipts,
            source_commit=args.source_commit or _source_commit(),
            harness_sha256=_file_digest(Path(__file__).with_name("harness.py")),
            manifest_sha256=manifest.sha256,
            anchor_hashes={anchor.id: anchor.sha256 for anchor in manifest.anchors},
            prompt_hashes=_load_prompt_hashes(args.receipt_dir),
            engine_sha256=_current_engine_digest(),
            flag_sha256=_flag_digest(),
            logical_model_ids=_load_qualification_target_model_ids(args.receipt_dir),
        )
    except (OSError, QualificationError, ValueError) as error:
        parser.exit(1, f"Qualification receipt aggregation refused: {error}\n")

    print(f"Qualification receipts aggregated: {len(aggregates)} routes")
    for aggregate in aggregates:
        print(f"{aggregate.logical_model_id} {aggregate.route.route_id} anchors=3/3")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator command
    raise SystemExit(main())
