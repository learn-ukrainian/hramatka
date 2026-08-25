"""Explicit, fail-closed real-provider production qualification command.

Importing this module never reaches a provider.  The command is intentionally
separate from tests and from the teacher API: an operator must supply both the
execution switch and a source/manifest-bound spend acknowledgement before a
single generator is constructed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from hramatka.api.qualified_models import QUALIFICATION_ANCHORS
from hramatka.engine import data
from hramatka.engine.providers import (
    make_qualification_pinned_generator,
    qualification_route_credential_present,
    telemetry_ctx,
    validate_qualification_route_runtime,
)
from hramatka.engine.transport import GEMMA_TIMEOUT_S, GeneratorPort

from .harness import (
    _PHASE_RE,
    _V3_KITS_RE,
    _V3_PROBE_RE,
    _V3_REPAIR_RE,
    ProductionQualificationHarness,
    QualificationRunnerStillActiveError,
    _sha,
)
from .manifest import QualificationManifest, RuntimeAnchor, load_manifest
from .receipts import (
    QualificationError,
    RepairTraceEntry,
    RouteBinding,
    qualification_matrix,
    qualification_target_model_ids,
)

_ACK_PREFIX = "HRAMATKA-QUALIFICATION-SPEND"
_DIAGNOSTIC_LABEL = "B1-45M-density-diagnostic"
_LIVE_BAKE_HARD_TIMEOUT_SECONDS = 1800
_LIVE_READINESS_TIMEOUT_SECONDS = 1830
_LIVE_RUNNER_STOP_TIMEOUT_SECONDS = GEMMA_TIMEOUT_S + 30


class LiveQualificationError(RuntimeError):
    """A deterministic live-qualification preflight refusal."""


@dataclass(frozen=True)
class LiveQualificationRequest:
    anchors: Mapping[str, RuntimeAnchor]
    receipt_root: Path
    scratch_root: Path
    expected_source_commit: str
    expected_manifest_sha256: str
    execute_real_provider: bool
    spend_acknowledgement: str | None
    repository_root: Path
    logical_model_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class LiveDiagnosticRequest:
    """One explicitly acknowledged route × anchor measurement cell."""

    qualification: LiveQualificationRequest
    anchor_id: str
    route_id: str


def spend_acknowledgement(
    *,
    source_commit: str,
    manifest_sha256: str,
    logical_model_ids: tuple[str, ...] | None = None,
) -> str:
    """Return the exact acknowledgement an operator must pass to execute."""
    matrix = _matrix(logical_model_ids)
    label = f"B1-45M-{len(QUALIFICATION_ANCHORS)}x{len(matrix)}"
    if logical_model_ids is not None:
        routes = ",".join(
            f"{logical_model_id}/{route.route_id}" for logical_model_id, route in matrix
        )
        label = f"{label}:{routes}"
    return f"{_ACK_PREFIX}:{source_commit}:{manifest_sha256}:{label}"


def diagnostic_spend_acknowledgement(
    *, source_commit: str, manifest_sha256: str, anchor_id: str, route_id: str
) -> str:
    """Bind diagnostic spend to exactly one configured anchor and route."""
    return (
        f"{_ACK_PREFIX}:{source_commit}:{manifest_sha256}:{_DIAGNOSTIC_LABEL}:"
        f"{anchor_id}:{route_id}"
    )


def _repository_state(repository_root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            cwd=repository_root,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            cwd=repository_root,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise LiveQualificationError("Git repository state could not be verified.") from error
    return head, not bool(dirty.strip())


def _outside_repository(path: Path, repository_root: Path) -> bool:
    try:
        path.resolve().relative_to(repository_root.resolve())
    except ValueError:
        return True
    return False


def _paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve()
    right_resolved = right.resolve()
    try:
        left_resolved.relative_to(right_resolved)
    except ValueError:
        try:
            right_resolved.relative_to(left_resolved)
        except ValueError:
            return False
    return True


def _configured_matrix() -> tuple[tuple[str, RouteBinding], ...]:
    """Derive the configured route matrix from the teacher routing allowlist."""
    return qualification_matrix()


def _matrix(
    logical_model_ids: tuple[str, ...] | None = None,
) -> tuple[tuple[str, RouteBinding], ...]:
    """Return whole configured routes for the selected logical models."""
    return qualification_matrix(logical_model_ids)


def _matrix_cells(matrix: tuple[tuple[str, RouteBinding], ...]) -> frozenset[tuple[str, str, str]]:
    """Return the complete model × route × anchor execution contract."""
    return frozenset(
        (logical_model_id, route.route_id, anchor_id)
        for logical_model_id, route in matrix
        for anchor_id in QUALIFICATION_ANCHORS
    )


def _preflight_live_cells(
    request: LiveQualificationRequest,
    *,
    matrix: tuple[tuple[str, RouteBinding], ...],
    acknowledgement: Callable[[str, str], str],
    manifest: QualificationManifest | None = None,
    repository_state: Callable[[Path], tuple[str, bool]] = _repository_state,
    credential_present: Callable[..., bool] = qualification_route_credential_present,
    runtime_route_valid: Callable[..., None] = validate_qualification_route_runtime,
) -> QualificationManifest:
    """Reject every deterministic defect before a selected provider is built."""
    if not request.execute_real_provider:
        raise LiveQualificationError("Real provider execution requires --execute-real-provider.")
    current_head, clean = repository_state(request.repository_root)
    if current_head != request.expected_source_commit:
        raise LiveQualificationError(
            "Current source commit does not match the operator acknowledgement."
        )
    if not clean:
        raise LiveQualificationError("Real qualification requires a clean source worktree.")
    active_manifest = manifest or load_manifest()
    if active_manifest.sha256 != request.expected_manifest_sha256:
        raise LiveQualificationError("Manifest digest does not match the operator acknowledgement.")
    expected_ack = acknowledgement(current_head, active_manifest.sha256)
    if request.spend_acknowledgement != expected_ack:
        raise LiveQualificationError(
            "Real provider spend acknowledgement is missing or does not match."
        )
    active_manifest.validate_runtime_anchors(request.anchors)
    if not _outside_repository(request.receipt_root, request.repository_root):
        raise LiveQualificationError("Receipt storage must be outside the repository.")
    if not _outside_repository(request.scratch_root, request.repository_root):
        raise LiveQualificationError("Scratch storage must be outside the repository.")
    if _paths_overlap(request.receipt_root, request.scratch_root):
        raise LiveQualificationError("Receipt and scratch storage must not overlap.")
    if request.receipt_root.exists():
        if not request.receipt_root.is_dir():
            raise LiveQualificationError("Receipt storage path is not a directory.")
        if any(request.receipt_root.iterdir()):
            raise LiveQualificationError(
                "Receipt storage must be empty for a new qualification run."
            )
    if request.scratch_root.exists() and not request.scratch_root.is_dir():
        raise LiveQualificationError("Scratch storage path is not a directory.")
    for logical_model_id, route in matrix:
        try:
            runtime_route_valid(
                route_id=route.route_id,
                logical_model_id=logical_model_id,
                host=route.host,
                model_id=route.model_id,
            )
        except ValueError as error:
            raise LiveQualificationError(
                "Qualification route runtime configuration is invalid."
            ) from error
        if not credential_present(
            route_id=route.route_id,
            logical_model_id=logical_model_id,
            host=route.host,
            model_id=route.model_id,
        ):
            raise LiveQualificationError(
                "A configured qualification route has no credential source."
            )
    return active_manifest


def preflight_live_qualification(
    request: LiveQualificationRequest,
    *,
    manifest: QualificationManifest | None = None,
    repository_state: Callable[[Path], tuple[str, bool]] = _repository_state,
    credential_present: Callable[..., bool] = qualification_route_credential_present,
    runtime_route_valid: Callable[..., None] = validate_qualification_route_runtime,
) -> QualificationManifest:
    """Reject every deterministic defect before any matrix provider is constructed."""
    try:
        target_ids = qualification_target_model_ids(request.logical_model_ids)
        matrix = _matrix(target_ids)
        expected_matrix = qualification_matrix(target_ids)
    except QualificationError as error:
        raise LiveQualificationError("Qualification target is invalid.") from error
    route_keys = {(logical_model_id, route.route_id) for logical_model_id, route in matrix}
    if not matrix or len(route_keys) != len(matrix) or matrix != expected_matrix:
        raise LiveQualificationError(
            "Qualification matrix does not match current logical-model routes."
        )
    return _preflight_live_cells(
        request,
        matrix=matrix,
        acknowledgement=lambda source_commit, manifest_sha256: spend_acknowledgement(
            source_commit=source_commit,
            manifest_sha256=manifest_sha256,
            logical_model_ids=target_ids if request.logical_model_ids is not None else None,
        ),
        manifest=manifest,
        repository_state=repository_state,
        credential_present=credential_present,
        runtime_route_valid=runtime_route_valid,
    )


def preflight_live_diagnostic(
    request: LiveDiagnosticRequest,
    *,
    manifest: QualificationManifest | None = None,
    repository_state: Callable[[Path], tuple[str, bool]] = _repository_state,
    credential_present: Callable[..., bool] = qualification_route_credential_present,
    runtime_route_valid: Callable[..., None] = validate_qualification_route_runtime,
) -> tuple[QualificationManifest, str, RouteBinding]:
    """Fail closed while authorizing only the named current matrix cell."""
    qualification = request.qualification
    if request.anchor_id not in qualification.anchors:
        raise LiveQualificationError(
            "Diagnostic anchor is not in the immutable qualification pack."
        )
    try:
        matrix = _matrix(qualification.logical_model_ids)
    except QualificationError as error:
        raise LiveQualificationError("Qualification target is invalid.") from error
    matches = [
        (logical_model_id, route)
        for logical_model_id, route in matrix
        if route.route_id == request.route_id
    ]
    if len(matches) != 1:
        raise LiveQualificationError(
            "Diagnostic route is not a unique configured qualification route."
        )
    logical_model_id, route = matches[0]
    active_manifest = _preflight_live_cells(
        qualification,
        matrix=((logical_model_id, route),),
        acknowledgement=lambda source_commit, manifest_sha256: diagnostic_spend_acknowledgement(
            source_commit=source_commit,
            manifest_sha256=manifest_sha256,
            anchor_id=request.anchor_id,
            route_id=request.route_id,
        ),
        manifest=manifest,
        repository_state=repository_state,
        credential_present=credential_present,
        runtime_route_valid=runtime_route_valid,
    )
    return active_manifest, logical_model_id, route


class _PinnedRouteProvider:
    """One exact provider route plus content-free generation/repair provenance."""

    def __init__(self, route: RouteBinding, port: GeneratorPort) -> None:
        self._route = route
        self._port = port
        self.prompt_digests: list[str] = []
        self.initial_prompt_digests: list[str] = []

    def for_bake(self) -> _PinnedRouteProvider:
        return self

    def receipt_provenance(self) -> dict[str, object]:
        """Expose only the port's content-free qualification evidence."""
        provenance = getattr(self._port, "receipt_provenance", None)
        if not callable(provenance):
            raise LiveQualificationError("Pinned qualification port lacks receipt provenance.")
        return provenance()

    def __call__(self, prompt: str) -> str:
        phase_match = _PHASE_RE.search(prompt)
        v3_kits_match = _V3_KITS_RE.search(prompt)
        v3_probe_match = _V3_PROBE_RE.search(prompt)
        v3_repair_match = _V3_REPAIR_RE.search(prompt)
        semantic_review = "BEGIN_HOST_REVIEW_REQUEST\n" in prompt
        try:
            if semantic_review:
                mode = "semantic_review"
                phase = 3
            elif v3_kits_match is not None:
                kits = json.loads(v3_kits_match.group(1))
                if not isinstance(kits, list) or not kits:
                    raise ValueError("missing v3 type-kits")
                phase = kits[0]["phase"]
                repair_metadata = (
                    json.loads(v3_repair_match.group(1)) if v3_repair_match is not None else None
                )
                mode = "initial" if repair_metadata is None else repair_metadata["mode"]
            elif phase_match is not None:
                phase_request = json.loads(phase_match.group(1))
                mode = phase_request["mode"]
                phase = phase_request["phase"]
            elif v3_probe_match is not None:
                probe = json.loads(v3_probe_match.group(1))
                mode = probe["mode"]
                phase = probe["phase"]
            else:
                raise ValueError("missing prompt boundary")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LiveQualificationError(
                "Pinned route received an invalid qualification prompt boundary."
            ) from error
        if (
            mode not in {"initial", "repair", "replacement", "semantic_review"}
            or type(phase) is not int
            or phase not in {1, 2, 3}
        ):
            raise LiveQualificationError("Pinned route received an invalid generation mode.")
        result = self._port(prompt)
        prompt_digest = _sha(prompt)
        self.prompt_digests.append(prompt_digest)
        if mode == "initial":
            self.initial_prompt_digests.append(prompt_digest)
        context = telemetry_ctx.get()
        if context is not None:
            context.record_qualification_route_trace(
                RepairTraceEntry(
                    mode=(
                        "semantic_review"
                        if mode == "semantic_review"
                        else "initial"
                        if mode == "initial"
                        else "repair"
                    ),
                    phase=phase,
                    expected_route=self._route,
                    observed_route=self._route,
                ).as_dict()
            )
        return result


PinnedPortFactory = Callable[[str, RouteBinding], GeneratorPort]


def _real_pinned_port(logical_model_id: str, route: RouteBinding) -> GeneratorPort:
    return make_qualification_pinned_generator(
        route_id=route.route_id,
        logical_model_id=logical_model_id,
        host=route.host,
        model_id=route.model_id,
    )


def _validate_complete_live_run(
    run: object,
    request: LiveQualificationRequest,
    matrix: tuple[tuple[str, RouteBinding], ...],
) -> None:
    """Require every selected model route and immutable anchor before success."""
    cells = getattr(run, "cells", ())
    expected = {
        (anchor_id, route.route_id)
        for anchor_id in request.anchors
        for _logical_model_id, route in matrix
    }
    observed: list[tuple[str, str]] = []
    invalid: set[tuple[str, str]] = set()
    for cell in cells:
        receipt = getattr(cell, "receipt", None)
        anchor_id = getattr(receipt, "anchor_id", None)
        route = getattr(getattr(receipt, "expected_route", None), "route_id", None)
        if not isinstance(anchor_id, str) or not isinstance(route, str):
            continue
        pair = (anchor_id, route)
        observed.append(pair)
        if (
            getattr(receipt, "outcome", None) != "passed"
            or getattr(receipt, "semantic_gate", None) != "passed"
        ):
            invalid.add(pair)
    observed_set = set(observed)
    if (
        len(cells) == len(expected)
        and observed_set == expected
        and not invalid
        and len(observed) == len(observed_set)
    ):
        return
    invalid.update(expected - observed_set)
    invalid.update(pair for pair in observed_set if pair not in expected)
    for pair in observed_set:
        if observed.count(pair) != 1:
            invalid.add(pair)
    identifiers = ", ".join(f"{anchor_id}/{route_id}" for anchor_id, route_id in sorted(invalid))
    if not identifiers:
        identifiers = "unknown/unknown"
    raise LiveQualificationError(f"Live qualification is incomplete or failed: {identifiers}")


def execute_live_qualification(
    request: LiveQualificationRequest,
    *,
    bundle: data.DataBundle | None = None,
    manifest: QualificationManifest | None = None,
    repository_state: Callable[[Path], tuple[str, bool]] = _repository_state,
    credential_present: Callable[..., bool] = qualification_route_credential_present,
    runtime_route_valid: Callable[..., None] = validate_qualification_route_runtime,
    pinned_port_factory: PinnedPortFactory = _real_pinned_port,
    bake_hard_timeout_seconds: int = _LIVE_BAKE_HARD_TIMEOUT_SECONDS,
    readiness_timeout_seconds: int = _LIVE_READINESS_TIMEOUT_SECONDS,
    runner_stop_timeout_seconds: float = _LIVE_RUNNER_STOP_TIMEOUT_SECONDS,
    runner_stop_waiter: Callable[[object, float], bool] | None = None,
):
    """Run a complete selected-model matrix through the ordinary production API path."""
    active_manifest = preflight_live_qualification(
        request,
        manifest=manifest,
        repository_state=repository_state,
        credential_present=credential_present,
        runtime_route_valid=runtime_route_valid,
    )
    active_bundle = bundle or data.resolve_bundle()
    if readiness_timeout_seconds < bake_hard_timeout_seconds:
        raise LiveQualificationError("Live readiness timeout must cover the bake hard timeout.")
    if runner_stop_timeout_seconds < _LIVE_RUNNER_STOP_TIMEOUT_SECONDS:
        raise LiveQualificationError(
            "Live runner stop wait must cover one provider timeout plus its safety margin."
        )
    target_ids = qualification_target_model_ids(request.logical_model_ids)
    matrix = _matrix(target_ids)
    harness = ProductionQualificationHarness(
        request.receipt_root,
        manifest=active_manifest,
        source_commit=request.expected_source_commit,
        logical_model_ids=target_ids,
    )

    def provider_factory(
        _anchor: RuntimeAnchor, logical_model_id: str, route: RouteBinding
    ) -> _PinnedRouteProvider:
        return _PinnedRouteProvider(route, pinned_port_factory(logical_model_id, route))

    try:
        run = harness.run_with_provider_factory(
            request.anchors,
            bundle=active_bundle,
            provider_factory=provider_factory,
            scratch_root=request.scratch_root,
            bake_hard_timeout_seconds=bake_hard_timeout_seconds,
            readiness_timeout_seconds=readiness_timeout_seconds,
            runner_stop_timeout_seconds=runner_stop_timeout_seconds,
            runner_stop_waiter=runner_stop_waiter,
        )
    except QualificationRunnerStillActiveError as error:
        raise LiveQualificationError(
            "Qualification runner remained active; scratch was preserved."
        ) from error
    _validate_complete_live_run(run, request, matrix)
    try:
        aggregates = harness.aggregates(run)
        # This is intentionally validation only.  Constructing the candidate
        # receipts proves each route has a complete current v3 aggregate, but
        # slice 7 alone may transcribe those receipts into the production
        # registry or expose v3 generation to teachers.
        if len(aggregates) != len(matrix):
            raise QualificationError("Live qualification did not produce every route aggregate.")
        for aggregate in aggregates:
            aggregate.as_model_receipt()
    except (QualificationError, RuntimeError) as error:
        raise LiveQualificationError(
            "Live qualification did not produce passing current v3 aggregates."
        ) from error
    return run


def execute_live_diagnostic(
    request: LiveDiagnosticRequest,
    *,
    bundle: data.DataBundle | None = None,
    manifest: QualificationManifest | None = None,
    repository_state: Callable[[Path], tuple[str, bool]] = _repository_state,
    credential_present: Callable[..., bool] = qualification_route_credential_present,
    runtime_route_valid: Callable[..., None] = validate_qualification_route_runtime,
    pinned_port_factory: PinnedPortFactory = _real_pinned_port,
    bake_hard_timeout_seconds: int = _LIVE_BAKE_HARD_TIMEOUT_SECONDS,
    readiness_timeout_seconds: int = _LIVE_READINESS_TIMEOUT_SECONDS,
    runner_stop_timeout_seconds: float = _LIVE_RUNNER_STOP_TIMEOUT_SECONDS,
    runner_stop_waiter: Callable[[object, float], bool] | None = None,
):
    """Run one pinned, content-free density diagnostic through the normal API path."""
    active_manifest, logical_model_id, route = preflight_live_diagnostic(
        request,
        manifest=manifest,
        repository_state=repository_state,
        credential_present=credential_present,
        runtime_route_valid=runtime_route_valid,
    )
    if readiness_timeout_seconds < bake_hard_timeout_seconds:
        raise LiveQualificationError("Live readiness timeout must cover the bake hard timeout.")
    if runner_stop_timeout_seconds < _LIVE_RUNNER_STOP_TIMEOUT_SECONDS:
        raise LiveQualificationError(
            "Live runner stop wait must cover one provider timeout plus its safety margin."
        )
    qualification = request.qualification
    harness = ProductionQualificationHarness(
        qualification.receipt_root,
        manifest=active_manifest,
        source_commit=qualification.expected_source_commit,
        logical_model_ids=qualification.logical_model_ids,
    )
    provider = _PinnedRouteProvider(route, pinned_port_factory(logical_model_id, route))
    try:
        run = harness.run_diagnostic_cell(
            qualification.anchors[request.anchor_id],
            logical_model_id,
            route,
            bundle=bundle or data.resolve_bundle(),
            provider=provider,
            scratch_root=qualification.scratch_root,
            bake_hard_timeout_seconds=bake_hard_timeout_seconds,
            readiness_timeout_seconds=readiness_timeout_seconds,
            runner_stop_timeout_seconds=runner_stop_timeout_seconds,
            runner_stop_waiter=runner_stop_waiter,
        )
    except QualificationRunnerStillActiveError as error:
        raise LiveQualificationError(
            "Qualification runner remained active; scratch was preserved."
        ) from error
    receipt = run.cell.receipt
    if (
        receipt.anchor_id != request.anchor_id
        or receipt.logical_model_id != logical_model_id
        or receipt.expected_route != route
        or receipt.observed_route != route
        or (
            receipt.semantic_gate != "passed"
            if receipt.outcome == "passed"
            else receipt.semantic_gate not in {"not_run", "failed"}
        )
    ):
        raise LiveQualificationError("Diagnostic cell did not retain its configured route binding.")
    return run


def load_anchor_pack(path: Path) -> dict[str, RuntimeAnchor]:
    """Load operator-supplied anchors without logging their text."""
    if not path.is_file():
        raise LiveQualificationError("Anchor pack does not exist or is not a file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LiveQualificationError("Anchor pack could not be parsed.") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"anchors"}
        or not isinstance(value["anchors"], list)
    ):
        raise LiveQualificationError("Anchor pack has an invalid schema.")
    anchors: dict[str, RuntimeAnchor] = {}
    for row in value["anchors"]:
        if not isinstance(row, dict) or set(row) != {"id", "source_identity", "text"}:
            raise LiveQualificationError("Anchor pack has an invalid anchor schema.")
        anchor_id = row["id"]
        source_identity = row["source_identity"]
        text = row["text"]
        if not all(isinstance(item, str) and item for item in (anchor_id, source_identity, text)):
            raise LiveQualificationError("Anchor pack contains an invalid anchor.")
        if anchor_id in anchors:
            raise LiveQualificationError("Anchor pack contains a duplicate anchor ID.")
        anchors[anchor_id] = RuntimeAnchor(anchor_id, source_identity, text)
    return anchors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchors-json", type=Path, required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--execute-real-provider", action="store_true")
    parser.add_argument("--acknowledge-provider-spend")
    parser.add_argument(
        "--logical-model-id",
        action="append",
        help="qualify every configured route for this logical model; repeat to target more models",
    )
    parser.add_argument("--diagnostic-anchor-id")
    parser.add_argument("--diagnostic-route-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[2]
    try:
        if not _outside_repository(args.anchors_json, repository_root):
            raise LiveQualificationError("Anchor pack must be outside the repository.")
        request = LiveQualificationRequest(
            anchors=load_anchor_pack(args.anchors_json),
            receipt_root=args.receipt_root,
            scratch_root=args.scratch_root,
            expected_source_commit=args.source_commit,
            expected_manifest_sha256=args.manifest_sha256,
            execute_real_provider=args.execute_real_provider,
            spend_acknowledgement=args.acknowledge_provider_spend,
            repository_root=repository_root,
            logical_model_ids=(tuple(args.logical_model_id) if args.logical_model_id else None),
        )
        diagnostic_requested = bool(args.diagnostic_anchor_id or args.diagnostic_route_id)
        if diagnostic_requested and not (args.diagnostic_anchor_id and args.diagnostic_route_id):
            raise LiveQualificationError(
                "Diagnostic execution requires both a configured anchor and route ID."
            )
        if diagnostic_requested:
            run = execute_live_diagnostic(
                LiveDiagnosticRequest(
                    qualification=request,
                    anchor_id=args.diagnostic_anchor_id,
                    route_id=args.diagnostic_route_id,
                )
            )
        else:
            run = execute_live_qualification(request)
    except LiveQualificationError as error:
        parser.exit(1, f"Qualification refused: {error}\n")
    except Exception:
        parser.exit(1, "Qualification failed unexpectedly.\n")
    if diagnostic_requested:
        print(f"Qualification diagnostic completed: {run.receipt_path.name}.")
    else:
        print(f"Qualification completed: {len(run.cells)} content-free receipts written.")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator command
    raise SystemExit(main())
