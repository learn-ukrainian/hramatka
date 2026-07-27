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

from hramatka.api.qualified_models import (
    DENSITY_CONTRACT_DIGEST,
    DENSITY_CONTRACT_VERSION,
    LOGICAL_MODELS,
    QUALIFICATION_ANCHORS,
    QUALIFIED_MODEL_REGISTRY_VERSION,
    QualificationReceipt,
)
from hramatka.engine.prompt_pack import PROMPT_PACK_VERSION

CELL_RECEIPT_SCHEMA_VERSION = "ProductionQualificationCellReceipt.v2"
DIAGNOSTIC_RECEIPT_SCHEMA_VERSION = "ProductionQualificationDensityDiagnostic.v1"
AGGREGATION_PROMPT_HASHES_SCHEMA_VERSION = "ProductionQualificationPromptHashes.v1"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_RE = re.compile(r"^[a-f0-9]{40,64}$")
_OUTCOMES = frozenset({"passed", "failed"})
_SEMANTIC_GATES = frozenset({"not_run", "passed", "failed"})


class QualificationError(ValueError):
    """A receipt is incomplete, stale, duplicate, or otherwise untrustworthy."""


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
    """Content-free accounting of ready and teacher-tray floor capacity."""

    delivered_blocks: int
    ready_blocks: int
    tray_blocks: int
    floor_blocks: int
    phase_counts: Mapping[str, int]
    ready_phase_counts: Mapping[str, int]
    tray_phase_counts: Mapping[str, int]
    response_units: int
    ready_response_units: int
    tray_response_units: int
    disposition: str

    @classmethod
    def from_dict(cls, value: object) -> DensitySummary:
        row = _require_exact_keys(
            value,
            {
                "delivered_blocks",
                "ready_blocks",
                "tray_blocks",
                "floor_blocks",
                "phase_counts",
                "ready_phase_counts",
                "tray_phase_counts",
                "response_units",
                "ready_response_units",
                "tray_response_units",
                "disposition",
            },
            "density",
        )
        count_fields = (
            "delivered_blocks",
            "ready_blocks",
            "tray_blocks",
            "floor_blocks",
            "response_units",
            "ready_response_units",
            "tray_response_units",
        )
        phase_count_fields = ("phase_counts", "ready_phase_counts", "tray_phase_counts")
        if (
            any(type(row[field]) is not int or row[field] < 0 for field in count_fields)
            or row["disposition"] not in {"teacher_ready", "recoverable_draft"}
            or any(
                not isinstance(row[field], dict)
                or set(row[field]) != {"1", "2", "3"}
                or any(type(count) is not int or count < 0 for count in row[field].values())
                for field in phase_count_fields
            )
            or row["delivered_blocks"] != row["ready_blocks"]
            or row["floor_blocks"] != row["ready_blocks"] + row["tray_blocks"]
            or row["response_units"]
            != row["ready_response_units"] + row["tray_response_units"]
            or any(
                row["phase_counts"][phase]
                != row["ready_phase_counts"][phase] + row["tray_phase_counts"][phase]
                for phase in ("1", "2", "3")
            )
        ):
            raise QualificationError("Density summary has invalid values.")
        return cls(
            delivered_blocks=row["delivered_blocks"],
            ready_blocks=row["ready_blocks"],
            tray_blocks=row["tray_blocks"],
            floor_blocks=row["floor_blocks"],
            phase_counts=dict(row["phase_counts"]),
            ready_phase_counts=dict(row["ready_phase_counts"]),
            tray_phase_counts=dict(row["tray_phase_counts"]),
            response_units=row["response_units"],
            ready_response_units=row["ready_response_units"],
            tray_response_units=row["tray_response_units"],
            disposition=row["disposition"],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "delivered_blocks": self.delivered_blocks,
            "ready_blocks": self.ready_blocks,
            "tray_blocks": self.tray_blocks,
            "floor_blocks": self.floor_blocks,
            "phase_counts": dict(self.phase_counts),
            "ready_phase_counts": dict(self.ready_phase_counts),
            "tray_phase_counts": dict(self.tray_phase_counts),
            "response_units": self.response_units,
            "ready_response_units": self.ready_response_units,
            "tray_response_units": self.tray_response_units,
            "disposition": self.disposition,
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
    density_contract_version: str
    density_contract_sha256: str
    registry_sha256: str
    engine_sha256: str
    flag_sha256: str
    density: DensitySummary
    repair_trace: tuple[RepairTraceEntry, ...]
    semantic_gate: str
    outcome: str
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
                "prompt_sha256",
                "registry_sha256",
                "repair_trace",
                "schema_version",
                "semantic_gate",
                "source_commit",
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
            or row["outcome"] not in _OUTCOMES
            or row["semantic_gate"] not in _SEMANTIC_GATES
            or not isinstance(row["repair_trace"], list)
        ):
            raise QualificationError("Cell receipt has invalid required values.")
        trace = tuple(RepairTraceEntry.from_dict(entry) for entry in row["repair_trace"])
        if not trace:
            raise QualificationError("Cell receipt must contain a route-bound generation trace.")
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
            density_contract_version=row["density_contract_version"],
            density_contract_sha256=row["density_contract_sha256"],
            registry_sha256=row["registry_sha256"],
            engine_sha256=row["engine_sha256"],
            flag_sha256=row["flag_sha256"],
            density=DensitySummary.from_dict(row["density"]),
            repair_trace=trace,
            semantic_gate=row["semantic_gate"],
            outcome=row["outcome"],
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
            "density_contract_version": self.density_contract_version,
            "density_contract_sha256": self.density_contract_sha256,
            "registry_sha256": self.registry_sha256,
            "engine_sha256": self.engine_sha256,
            "flag_sha256": self.flag_sha256,
            "density": self.density.as_dict(),
            "repair_trace": [entry.as_dict() for entry in self.repair_trace],
            "semantic_gate": self.semantic_gate,
            "outcome": self.outcome,
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
            outcomes[phase], {"dropped", "generated", "ready", "requested", "review"},
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
        return all(cell.semantic_gate == "passed" for cell in self.cells)

    def as_model_receipt(self) -> QualificationReceipt:
        """Produce a selector receipt only after the separate semantic gate passes."""
        if self.passed_anchors != QUALIFICATION_ANCHORS or not self.semantic_gate_passed:
            raise QualificationError("Path proof alone cannot qualify a production provider route.")
        return QualificationReceipt(
            logical_model_id=self.logical_model_id,
            provider_route=self.route.route_id,
            provider_host=self.route.host,
            provider_model_id=self.route.model_id,
            registry_version=QUALIFIED_MODEL_REGISTRY_VERSION,
            prompt_pack_version=PROMPT_PACK_VERSION,
            density_contract_version=DENSITY_CONTRACT_VERSION,
            density_contract_digest=DENSITY_CONTRACT_DIGEST,
            passed_anchors=self.passed_anchors,
            passed=True,
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
) -> tuple[RouteAggregate, ...]:
    """Strictly aggregate all 12 configured cells or fail closed.

    The caller supplies current runtime identity values.  Every cell must bind
    them exactly; missing, stale, duplicate, route-mismatched, or failed cells
    are rejected before any selector receipt can exist.
    """
    parsed = tuple(
        receipt if isinstance(receipt, CellReceipt) else CellReceipt.from_dict(receipt)
        for receipt in receipts
    )
    expected_routes = {
        (model.id, route.id): RouteBinding(route.id, route.host, route.model_id)
        for model in LOGICAL_MODELS
        for route in model.provider_routes
    }
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
            or receipt.density_contract_version != DENSITY_CONTRACT_VERSION
            or receipt.density_contract_sha256 != DENSITY_CONTRACT_DIGEST
        ):
            raise QualificationError("Receipt is stale for the current qualification contract.")
        if receipt.outcome != "passed" or receipt.density.disposition != "teacher_ready":
            raise QualificationError(
                "A failed or non-deliverable qualification cell cannot aggregate."
            )
        expected_phase_counts = {"1": 3, "2": 4, "3": 1}
        if (
            receipt.density.floor_blocks < 8
            or any(
                receipt.density.phase_counts[phase] < expected
                for phase, expected in expected_phase_counts.items()
            )
            or receipt.density.response_units < 28
        ):
            raise QualificationError(
                "Receipt density counts do not meet the B1 45-minute contract."
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
        )
    except (OSError, QualificationError, ValueError) as error:
        parser.exit(1, f"Qualification receipt aggregation refused: {error}\n")

    print(f"Qualification receipts aggregated: {len(aggregates)} routes")
    for aggregate in aggregates:
        print(f"{aggregate.logical_model_id} {aggregate.route.route_id} anchors=3/3")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator command
    raise SystemExit(main())
