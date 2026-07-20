"""Explicit, fail-closed real-provider production qualification command.

Importing this module never reaches a provider.  The command is intentionally
separate from tests and from the teacher API: an operator must supply both the
execution switch and a source/manifest-bound spend acknowledgement before a
single generator is constructed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from hramatka.engine import data
from hramatka.engine.providers import (
    make_qualification_pinned_generator,
    qualification_route_credential_present,
    telemetry_ctx,
    validate_qualification_route_runtime,
)
from hramatka.engine.transport import GEMMA_TIMEOUT_S

from .harness import (
    _PHASE_RE,
    ProductionQualificationHarness,
    QualificationRunnerStillActiveError,
    _sha,
)
from .manifest import QualificationManifest, RuntimeAnchor, load_manifest
from .receipts import RepairTraceEntry, RouteBinding

_ACK_PREFIX = "HRAMATKA-QUALIFICATION-SPEND"
_MATRIX_LABEL = "B1-45M-3x4"
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


def spend_acknowledgement(*, source_commit: str, manifest_sha256: str) -> str:
    """Return the exact acknowledgement an operator must pass to execute."""
    return f"{_ACK_PREFIX}:{source_commit}:{manifest_sha256}:{_MATRIX_LABEL}"


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
        raise LiveQualificationError(
            "Git repository state could not be verified."
        ) from error
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


def _matrix() -> tuple[tuple[str, RouteBinding], ...]:
    """Return the fixed qualification matrix; callers cannot choose a subset."""
    from hramatka.api.qualified_models import LOGICAL_MODELS

    return tuple(
        (model.id, RouteBinding(route.id, route.host, route.model_id))
        for model in LOGICAL_MODELS
        for route in model.provider_routes
    )


def preflight_live_qualification(
    request: LiveQualificationRequest,
    *,
    manifest: QualificationManifest | None = None,
    repository_state: Callable[[Path], tuple[str, bool]] = _repository_state,
    credential_present: Callable[..., bool] = qualification_route_credential_present,
    runtime_route_valid: Callable[..., None] = validate_qualification_route_runtime,
) -> QualificationManifest:
    """Reject every deterministic defect before any provider is constructed."""
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
    expected_ack = spend_acknowledgement(
        source_commit=current_head, manifest_sha256=active_manifest.sha256
    )
    if request.spend_acknowledgement != expected_ack:
        raise LiveQualificationError(
            "Real provider spend acknowledgement is missing or does not match."
        )
    if os.environ.get("HRAMATKA_SLOT_REPAIR") != "1":
        raise LiveQualificationError("Qualification requires explicit generic slot repair.")
    if os.environ.get("HRAMATKA_PROMPT_PACK") != "1":
        raise LiveQualificationError("Qualification requires explicit prompt-pack routing.")
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
    matrix = _matrix()
    if len(matrix) != 4 or len({route.route_id for _, route in matrix}) != 4:
        raise LiveQualificationError("Qualification matrix is not exactly four unique routes.")
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


class _PinnedRouteProvider:
    """One exact provider route plus content-free generation/repair provenance."""

    def __init__(self, route: RouteBinding, port: Callable[[str], str]) -> None:
        self._route = route
        self._port = port
        self.prompt_digests: list[str] = []

    def for_bake(self) -> _PinnedRouteProvider:
        return self

    def __call__(self, prompt: str) -> str:
        phase_match = _PHASE_RE.search(prompt)
        if phase_match is None:
            raise LiveQualificationError("Pinned route received no prompt-pack phase boundary.")
        try:
            phase_request = json.loads(phase_match.group(1))
            mode = phase_request["mode"]
            phase = phase_request["phase"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise LiveQualificationError(
                "Pinned route received an invalid phase boundary."
            ) from error
        if mode not in {"initial", "repair"} or type(phase) is not int or phase not in {1, 2, 3}:
            raise LiveQualificationError("Pinned route received an invalid generation mode.")
        result = self._port(prompt)
        self.prompt_digests.append(_sha(prompt))
        context = telemetry_ctx.get()
        if context is not None:
            context.record_qualification_route_trace(
                RepairTraceEntry(
                    mode=mode,
                    phase=phase,
                    expected_route=self._route,
                    observed_route=self._route,
                ).as_dict()
            )
        return result


PinnedPortFactory = Callable[[str, RouteBinding], Callable[[str], str]]


def _real_pinned_port(logical_model_id: str, route: RouteBinding) -> Callable[[str], str]:
    return make_qualification_pinned_generator(
        route_id=route.route_id,
        logical_model_id=logical_model_id,
        host=route.host,
        model_id=route.model_id,
    )


def _validate_complete_live_run(run: object, request: LiveQualificationRequest) -> None:
    """Require the full path matrix before the CLI can report real-run success."""
    cells = getattr(run, "cells", ())
    expected = {
        (anchor_id, route.route_id)
        for anchor_id in request.anchors
        for _logical_model_id, route in _matrix()
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
            or getattr(receipt, "semantic_gate", None) != "not_run"
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
    """Run the preflighted 12-cell matrix through the ordinary production API path."""
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
    harness = ProductionQualificationHarness(
        request.receipt_root,
        manifest=active_manifest,
        source_commit=request.expected_source_commit,
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
    _validate_complete_live_run(run, request)
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
        )
        run = execute_live_qualification(request)
    except LiveQualificationError as error:
        parser.exit(1, f"Qualification refused: {error}\n")
    except Exception:
        parser.exit(1, "Qualification failed unexpectedly.\n")
    print(f"Qualification completed: {len(run.cells)} content-free receipts written.")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator command
    raise SystemExit(main())
