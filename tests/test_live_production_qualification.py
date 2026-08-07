"""No-provider safety tests for the explicit real qualification mode."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hramatka.engine import data, fixtures
from hramatka.engine.providers import (
    SUBSCRIPTION_EXECUTABLE_ENV,
    SUBSCRIPTION_HOST,
    FailoverGeneratorPort,
    make_qualification_pinned_generator,
    qualification_route_credential_present,
    validate_qualification_route_runtime,
)
from hramatka.qualification import QualificationError, deterministic_runtime_anchors, load_manifest
from hramatka.qualification.harness import (
    ProductionQualificationHarness,
    _DeterministicRouteProvider,
)
from hramatka.qualification.live import (
    LiveDiagnosticRequest,
    LiveQualificationError,
    LiveQualificationRequest,
    _matrix,
    _PinnedRouteProvider,
    _repository_state,
    diagnostic_spend_acknowledgement,
    execute_live_diagnostic,
    execute_live_qualification,
    load_anchor_pack,
    main,
    preflight_live_diagnostic,
    preflight_live_qualification,
    spend_acknowledgement,
)
from hramatka.qualification.manifest import ManifestError
from hramatka.qualification.receipts import DensityDiagnosticReceipt, RouteBinding

_HEAD = "a" * 40
def _request(tmp_path: Path, **changes: object) -> LiveQualificationRequest:
    manifest = load_manifest()
    request = LiveQualificationRequest(
        anchors=deterministic_runtime_anchors(),
        receipt_root=tmp_path / "receipts",
        scratch_root=tmp_path / "scratch",
        expected_source_commit=_HEAD,
        expected_manifest_sha256=manifest.sha256,
        execute_real_provider=True,
        spend_acknowledgement=spend_acknowledgement(
            source_commit=_HEAD, manifest_sha256=manifest.sha256
        ),
        repository_root=tmp_path / "repository",
    )
    return replace(request, **changes)


def _clean_repository_state(_root: Path) -> tuple[str, bool]:
    return _HEAD, True


def _credential_present(**_kwargs: object) -> bool:
    return True


def _diagnostic_request(tmp_path: Path) -> LiveDiagnosticRequest:
    qualification = _request(tmp_path)
    return LiveDiagnosticRequest(
        qualification=replace(
            qualification,
            spend_acknowledgement=diagnostic_spend_acknowledgement(
                source_commit=_HEAD,
                manifest_sha256=qualification.expected_manifest_sha256,
                anchor_id="b1-narrative",
                route_id="gemini-flash-subscription",
            ),
        ),
        anchor_id="b1-narrative",
        route_id="gemini-flash-subscription",
    )


def _flash_request(tmp_path: Path) -> LiveQualificationRequest:
    target = ("gemini-3.6-flash",)
    request = _request(tmp_path)
    return replace(
        request,
        logical_model_ids=target,
        spend_acknowledgement=spend_acknowledgement(
            source_commit=_HEAD,
            manifest_sha256=request.expected_manifest_sha256,
            logical_model_ids=target,
        ),
    )


def _complete_fake_run(request: LiveQualificationRequest, *, failed: bool = False):
    cells = []
    for anchor_id in request.anchors:
        for _logical_model_id, route in _matrix():
            outcome = "failed" if failed and not cells else "passed"
            cells.append(
                SimpleNamespace(
                    receipt=SimpleNamespace(
                        anchor_id=anchor_id,
                        expected_route=route,
                        outcome=outcome,
                        semantic_gate="not_run",
                    )
                )
            )
    return SimpleNamespace(cells=tuple(cells))


@pytest.fixture
def v2_delivery_flags(monkeypatch):
    """Enable only the legacy HTTP diagnostic that slice 6 intentionally retains."""
    monkeypatch.setenv("HRAMATKA_SLOT_REPAIR", "1")
    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"execute_real_provider": False}, "--execute-real-provider"),
        ({"spend_acknowledgement": "wrong"}, "spend acknowledgement"),
        ({"expected_source_commit": "b" * 40}, "source commit"),
        ({"expected_manifest_sha256": "0" * 64}, "Manifest digest"),
    ],
)
def test_preflight_refuses_execution_before_provider_construction(
    tmp_path, v2_delivery_flags, changes, message
) -> None:
    request = _request(tmp_path, **changes)
    called = False

    def port_factory(*_args):
        nonlocal called
        called = True
        raise AssertionError("provider construction must not occur")

    with pytest.raises(LiveQualificationError, match=message):
        execute_live_qualification(
            request,
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
            pinned_port_factory=port_factory,
        )
    assert not called


def test_matrix_is_derived_from_three_logical_models_and_rebinds_to_9_cells() -> None:
    manifest = load_manifest()
    assert len(_matrix()) == 3
    assert len(_matrix()) * 3 == 9
    assert {route.route_id for _logical_model_id, route in _matrix()} == {
        "gemini-flash-subscription",
        "gemini-pro-subscription",
        "gemma-openrouter",
    }
    assert spend_acknowledgement(source_commit=_HEAD, manifest_sha256=manifest.sha256).endswith(
        ":B1-45M-3x3"
    )


def test_flash_target_requires_its_complete_three_anchor_route_matrix(tmp_path) -> None:
    manifest = load_manifest()
    target = ("gemini-3.6-flash",)
    matrix = _matrix(target)

    assert matrix == (
        (
            "gemini-3.6-flash",
            RouteBinding(
                "gemini-flash-subscription", "antigravity-cli", "gemini-3.6-flash-high"
            ),
        ),
    )
    assert len(matrix) * 3 == 3
    assert spend_acknowledgement(
        source_commit=_HEAD,
        manifest_sha256=manifest.sha256,
        logical_model_ids=target,
    ).endswith(":B1-45M-3x1:gemini-3.6-flash/gemini-flash-subscription")

    preflight_live_qualification(
        _flash_request(tmp_path),
        repository_state=_clean_repository_state,
        credential_present=_credential_present,
    )


def test_preflight_refuses_a_stale_matrix_count(tmp_path, v2_delivery_flags, monkeypatch) -> None:
    import hramatka.qualification.live as live_module

    configured = live_module._configured_matrix()
    monkeypatch.setattr(live_module, "_matrix", lambda _ids=None: configured[:-1])

    with pytest.raises(LiveQualificationError, match="does not match current logical-model routes"):
        preflight_live_qualification(
            _request(tmp_path),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )


def test_cli_refuses_anchor_pack_inside_repository_before_loading(
    tmp_path, monkeypatch, capsys
) -> None:
    import hramatka.qualification.live as live_module

    repository_root = tmp_path / "repository"
    module_path = repository_root / "hramatka" / "qualification" / "live.py"
    anchor_path = repository_root / "ignored-anchors.json"
    anchor_path.parent.mkdir(parents=True)
    anchor_path.write_text("not parsed", encoding="utf-8")
    monkeypatch.setattr(live_module, "__file__", str(module_path))

    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--anchors-json",
                str(anchor_path),
                "--receipt-root",
                str(tmp_path / "receipts"),
                "--scratch-root",
                str(tmp_path / "scratch"),
                "--source-commit",
                _HEAD,
                "--manifest-sha256",
                "0" * 64,
            ]
        )

    assert exit_info.value.code == 1
    assert "Anchor pack must be outside the repository" in capsys.readouterr().err


def test_preflight_refuses_dirty_tree_anchor_and_path_defects_before_provider(
    tmp_path, v2_delivery_flags
) -> None:
    request = _request(tmp_path)
    with pytest.raises(LiveQualificationError, match="clean source"):
        preflight_live_qualification(
            request,
            repository_state=lambda _root: (_HEAD, False),
            credential_present=_credential_present,
        )

    bad_anchors = dict(request.anchors)
    bad_anchors["b1-dialogue"] = replace(bad_anchors["b1-dialogue"], source_identity="wrong-source")
    with pytest.raises(ManifestError, match="source identity"):
        preflight_live_qualification(
            replace(request, anchors=bad_anchors),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )

    with pytest.raises(LiveQualificationError, match="outside the repository"):
        preflight_live_qualification(
            replace(request, receipt_root=request.repository_root / "receipts"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )

    receipt_parent = tmp_path / "receipt-parent"
    with pytest.raises(LiveQualificationError, match="must not overlap"):
        preflight_live_qualification(
            replace(request, receipt_root=receipt_parent, scratch_root=receipt_parent / "scratch"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )
    scratch_parent = tmp_path / "scratch-parent"
    with pytest.raises(LiveQualificationError, match="must not overlap"):
        preflight_live_qualification(
            replace(request, receipt_root=scratch_parent / "receipts", scratch_root=scratch_parent),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )

    with pytest.raises(LiveQualificationError, match="credential source"):
        preflight_live_qualification(
            request,
            repository_state=_clean_repository_state,
            credential_present=lambda **_kwargs: False,
        )

    receipt_file = tmp_path / "receipt-file"
    receipt_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(LiveQualificationError, match="Receipt storage path"):
        preflight_live_qualification(
            replace(request, receipt_root=receipt_file),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )

    scratch_file = tmp_path / "scratch-file"
    scratch_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(LiveQualificationError, match="Scratch storage path"):
        preflight_live_qualification(
            replace(request, scratch_root=scratch_file),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )


def test_live_density_diagnostic_runs_only_the_pinned_flash_cell_and_persists_counts(
    tmp_path, v2_delivery_flags
) -> None:
    request = _diagnostic_request(tmp_path)
    constructed: list[tuple[str, str]] = []
    credential_routes: list[str] = []

    def credential_present(**route: str) -> bool:
        credential_routes.append(route["route_id"])
        return route["route_id"] == "gemini-flash-subscription"

    def fake_port_factory(logical_model_id: str, route: RouteBinding):
        constructed.append((logical_model_id, route.route_id))
        return _DeterministicRouteProvider(route, force_initial_shortfall=False)

    run = execute_live_diagnostic(
        request,
        bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
        repository_state=_clean_repository_state,
        credential_present=credential_present,
        pinned_port_factory=fake_port_factory,
    )

    assert constructed == [("gemini-3.6-flash", "gemini-flash-subscription")]
    assert credential_routes == ["gemini-flash-subscription"]
    assert run.cell.receipt.outcome == "passed"
    parsed = DensityDiagnosticReceipt.from_dict(
        json.loads(run.receipt_path.read_text(encoding="utf-8"))
    )
    assert parsed.cell_receipt.expected_route.route_id == "gemini-flash-subscription"
    assert parsed.density_trace[0]["stage"] == "initial"
    assert set(parsed.density_trace[0]["phase_density"]) == {"1", "2", "3"}
    assert parsed.density_trace[-1]["repair_invocations"] == len(parsed.repair_invocation_trace)
    assert request.qualification.scratch_root.is_dir()
    assert list(request.qualification.scratch_root.iterdir()) == []


def test_failed_live_diagnostic_preserves_raw_parse_artifact_outside_receipts(
    tmp_path, v2_delivery_flags
) -> None:
    request = _diagnostic_request(tmp_path)
    raw_response = "PRIVATE RAW MODEL RESPONSE"

    class InvalidRawPort:
        def __call__(self, _prompt: str) -> str:
            return raw_response

        def receipt_provenance(self) -> dict[str, object]:
            return {
                "tier": "api_observed",
                "client_version": None,
                "requested_model": None,
                "raw_output_sha256": (),
            }

    run = execute_live_diagnostic(
        request,
        bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
        repository_state=_clean_repository_state,
        credential_present=_credential_present,
        pinned_port_factory=lambda _logical_model_id, _route: InvalidRawPort(),
    )

    assert run.cell.receipt.outcome == "failed"
    raw_paths = tuple(
        request.qualification.scratch_root.glob(
            "raw-parse-failures/*/engine-out/*/generation-raw-attempt1.txt"
        )
    )
    assert len(raw_paths) == 1
    assert raw_paths[0].read_text(encoding="utf-8") == raw_response
    assert raw_response not in run.receipt_path.read_text(encoding="utf-8")


def test_live_density_diagnostic_rejects_an_unconfigured_route_before_provider(
    tmp_path, v2_delivery_flags
) -> None:
    request = replace(_diagnostic_request(tmp_path), route_id="wrong-route")
    with pytest.raises(LiveQualificationError, match="unique configured"):
        preflight_live_diagnostic(
            request,
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )


def test_live_density_diagnostic_normalizes_an_invalid_logical_model_target(tmp_path) -> None:
    request = _diagnostic_request(tmp_path)
    request = replace(
        request,
        qualification=replace(request.qualification, logical_model_ids=("wrong-model",)),
    )

    with pytest.raises(LiveQualificationError, match="Qualification target is invalid"):
        preflight_live_diagnostic(
            request,
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )


def test_missing_anchor_pack_has_explicit_content_free_error(tmp_path) -> None:
    missing_path = tmp_path / "missing-anchors.json"
    with pytest.raises(LiveQualificationError, match="does not exist or is not a file"):
        load_anchor_pack(missing_path)


@pytest.mark.parametrize(
    "repository_error",
    [
        FileNotFoundError("sensitive executable path"),
        subprocess.CalledProcessError(128, ["git"], stderr="sensitive repository path"),
    ],
)
def test_repository_state_failure_is_content_free(tmp_path, monkeypatch, repository_error) -> None:
    def fail_git(*_args, **_kwargs):
        raise repository_error

    monkeypatch.setattr("hramatka.qualification.live.subprocess.run", fail_git)
    with pytest.raises(LiveQualificationError) as raised:
        _repository_state(tmp_path)

    assert str(raised.value) == "Git repository state could not be verified."
    assert "sensitive" not in str(raised.value)


def test_invalid_later_route_runtime_refuses_before_any_pinned_port_factory(
    tmp_path, v2_delivery_flags
) -> None:
    request = _request(tmp_path)
    constructed = False

    def port_factory(*_args):
        nonlocal constructed
        constructed = True
        raise AssertionError("a provider must not be constructed")

    def runtime_route_valid(**route: str) -> None:
        if route["route_id"] == "gemini-flash-subscription":
            raise ValueError("noncanonical runtime configuration")

    with pytest.raises(LiveQualificationError, match="runtime configuration"):
        execute_live_qualification(
            request,
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
            runtime_route_valid=runtime_route_valid,
            pinned_port_factory=port_factory,
        )
    assert not constructed

def test_route_validation_is_pure_config_and_never_probes_the_host_filesystem(
    monkeypatch,
) -> None:
    """Route canonicity must not depend on a locally installed executable.

    Executable presence is a credential-source question that belongs to
    ``qualification_route_credential_present``, which preflight calls through a
    separate injectable seam.  Probing it inside route validation made every
    offline caller — the whole unit suite, and any host without the client —
    fail canonicity for an environment reason.
    """
    monkeypatch.setenv(SUBSCRIPTION_EXECUTABLE_ENV, "/nonexistent/subscription-client")

    subscription_routes = [
        (logical_model_id, route)
        for logical_model_id, route in _matrix()
        if route.host == SUBSCRIPTION_HOST
    ]
    assert subscription_routes, "the matrix must pin one subscription route"

    for logical_model_id, route in subscription_routes:
        validate_qualification_route_runtime(
            route_id=route.route_id,
            logical_model_id=logical_model_id,
            host=route.host,
            model_id=route.model_id,
        )
        # The same absent executable must still be reported — as a missing
        # credential source, which is the layer that owns provider reachability.
        assert not qualification_route_credential_present(
            route_id=route.route_id,
            logical_model_id=logical_model_id,
            host=route.host,
            model_id=route.model_id,
        )


def test_live_passes_production_timeouts_to_shared_harness(
    tmp_path, v2_delivery_flags, monkeypatch
) -> None:
    request = _request(tmp_path)
    received: dict[str, int] = {}

    class PassingAggregate:
        def as_model_receipt(self) -> object:
            return object()

    def fake_run(_self, _anchors, **kwargs):
        received["hard"] = kwargs["bake_hard_timeout_seconds"]
        received["readiness"] = kwargs["readiness_timeout_seconds"]
        return _complete_fake_run(request)

    monkeypatch.setattr(ProductionQualificationHarness, "run_with_provider_factory", fake_run)
    monkeypatch.setattr(
        ProductionQualificationHarness,
        "aggregates",
        lambda _self, _run: tuple(PassingAggregate() for _ in _matrix()),
    )
    execute_live_qualification(
        request,
        bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
        repository_state=_clean_repository_state,
        credential_present=_credential_present,
    )
    assert received == {"hard": 1800, "readiness": 1830}


def test_live_rejects_a_failed_cell_from_the_shared_harness(
    tmp_path, v2_delivery_flags, monkeypatch
) -> None:
    request = _request(tmp_path)
    monkeypatch.setattr(
        ProductionQualificationHarness,
        "run_with_provider_factory",
        lambda _self, _anchors, **_kwargs: _complete_fake_run(request, failed=True),
    )
    with pytest.raises(LiveQualificationError, match="b1-narrative/gemini-flash-subscription"):
        execute_live_qualification(
            request,
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )


def test_live_refuses_cells_without_passing_current_v3_aggregates(
    tmp_path, v2_delivery_flags, monkeypatch
) -> None:
    request = _request(tmp_path)
    monkeypatch.setattr(
        ProductionQualificationHarness,
        "run_with_provider_factory",
        lambda _self, _anchors, **_kwargs: _complete_fake_run(request),
    )

    def refuse_aggregate(_self, _run):
        raise QualificationError("receipt is stale for the current qualification contract")

    monkeypatch.setattr(ProductionQualificationHarness, "aggregates", refuse_aggregate)
    with pytest.raises(LiveQualificationError, match="passing current v3 aggregates"):
        execute_live_qualification(
            request,
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )


def test_shared_runner_waits_for_injected_live_readiness_timeout(
    tmp_path, v2_delivery_flags
) -> None:
    class SlowProvider:
        def __init__(self, route: RouteBinding) -> None:
            self._inner = _DeterministicRouteProvider(route, force_initial_shortfall=False)

        @property
        def prompt_digests(self) -> list[str]:
            return self._inner.prompt_digests

        @property
        def initial_prompt_digests(self) -> list[str]:
            return self._inner.initial_prompt_digests

        def for_bake(self):
            return self

        def __call__(self, prompt: str) -> str:
            time.sleep(0.2)
            return self._inner(prompt)

    anchors = deterministic_runtime_anchors()
    anchor = anchors["b1-narrative"]
    route = RouteBinding("gemini-flash-subscription", "antigravity-cli", "gemini-3.6-flash-high")
    bundle = fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data")
    harness = ProductionQualificationHarness(tmp_path / "receipts", source_commit=_HEAD)
    (tmp_path / "short").mkdir()
    (tmp_path / "long").mkdir()
    previous_bundle = data._active  # noqa: SLF001 - mirror the shared runner's worker scope.
    data.set_active_bundle(bundle)
    try:
        with pytest.raises(QualificationError, match="terminal state"):
            harness._run_cell(
                anchor,
                "gemini-3.6-flash",
                route,
                bundle,
                provider=SlowProvider(route),
                cell_root=tmp_path / "short",
                bake_hard_timeout_seconds=100,
                readiness_timeout_seconds=0.05,
                runner_stop_timeout_seconds=1,
                runner_stop_waiter=lambda _runner, _timeout: True,
            )
        result = harness._run_cell(
            anchor,
            "gemini-3.6-flash",
            route,
            bundle,
            provider=SlowProvider(route),
            cell_root=tmp_path / "long",
            bake_hard_timeout_seconds=100,
            readiness_timeout_seconds=2,
            runner_stop_timeout_seconds=1,
            runner_stop_waiter=lambda _runner, _timeout: True,
        )
    finally:
        data.set_active_bundle(previous_bundle)
    assert result.receipt.outcome == "passed"


def test_pinned_factory_never_builds_failover_or_round_robin_ports(monkeypatch) -> None:
    from hramatka.qualification.live import _matrix

    for logical_model_id, route in _matrix():
        port = make_qualification_pinned_generator(
            route_id=route.route_id,
            logical_model_id=logical_model_id,
            host=route.host,
            model_id=route.model_id,
        )
        assert not isinstance(port, FailoverGeneratorPort)
        assert getattr(port, "_model", getattr(port, "model", None)) == route.model_id
        if route.host == "antigravity-cli":
            assert getattr(port, "host", None) == route.host
            continue
        assert port._transport.host == route.host
        assert port._transport.max_attempts == 1
        if route.model_id == "google-ais/gemini-3.6-flash":
            assert port._transport.retry_json_mode_on_400 is True
        else:
            assert port._transport.retry_json_mode_on_400 is False


def test_pinned_route_refuses_port_without_receipt_provenance() -> None:
    route = RouteBinding(
        route_id="gemini-flash-subscription",
        host="antigravity-cli",
        model_id="gemini-3.6-flash-high",
    )
    provider = _PinnedRouteProvider(route, lambda _prompt: '{"activities": []}')  # type: ignore[arg-type]

    with pytest.raises(LiveQualificationError, match="lacks receipt provenance"):
        provider.receipt_provenance()


def test_live_mode_uses_exact_routes_cleans_scratch_and_leaves_semantic_separate(
    tmp_path, v2_delivery_flags, monkeypatch
) -> None:
    request = _request(tmp_path)
    environment_root = tmp_path / "environment-engine-out"
    monkeypatch.setenv("HRAMATKA_ENGINE_OUT_DIR", str(environment_root))
    constructed: list[tuple[str, str, str, str]] = []

    def fake_port_factory(logical_model_id, route):
        constructed.append((logical_model_id, route.route_id, route.host, route.model_id))
        return _DeterministicRouteProvider(route, force_initial_shortfall=False)

    run = execute_live_qualification(
        request,
        bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
        repository_state=_clean_repository_state,
        credential_present=_credential_present,
        pinned_port_factory=fake_port_factory,
    )

    assert len(run.cells) == 9
    assert len(constructed) == 9
    assert set(constructed) == {
        (
            "gemini-3.6-flash",
            "gemini-flash-subscription",
            "antigravity-cli",
            "gemini-3.6-flash-high",
        ),
        ("gemini-3.1-pro", "gemini-pro-subscription", "antigravity-cli", "gemini-3.1-pro-high"),
        ("gemma-4-31b", "gemma-openrouter", "openrouter", "google/gemma-4-31b-it"),
    }
    assert request.scratch_root.is_dir()
    assert list(request.scratch_root.iterdir()) == []
    assert not environment_root.exists()
    assert all(cell.receipt.semantic_gate == "not_run" for cell in run.cells)
    assert all(path.is_file() for path in run.receipt_paths)
    assert all(
        all(
            anchor.text not in path.read_text(encoding="utf-8")
            for anchor in request.anchors.values()
        )
        for path in run.receipt_paths
    )
    from hramatka.qualification.receipts import RouteAggregate

    route_cells = tuple(
        cell.receipt
        for cell in run.cells
        if cell.receipt.expected_route.route_id == "gemini-flash-subscription"
    )
    # The locked shadow-tier ruling keeps ``semantic_gate=not_run`` advisory;
    # only the current v3 aggregate may create this candidate receipt.
    assert (
        RouteAggregate(
            logical_model_id="gemini-3.6-flash",
            route=route_cells[0].expected_route,
            cells=route_cells,
        )
        .as_model_receipt()
        .passed
    )


def test_live_mode_can_qualify_the_complete_flash_target_without_other_credentials(
    tmp_path, v2_delivery_flags
) -> None:
    request = _flash_request(tmp_path)
    constructed: list[tuple[str, str, str, str]] = []

    def fake_port_factory(logical_model_id, route):
        constructed.append((logical_model_id, route.route_id, route.host, route.model_id))
        return _DeterministicRouteProvider(route, force_initial_shortfall=False)

    run = execute_live_qualification(
        request,
        bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
        repository_state=_clean_repository_state,
        credential_present=_credential_present,
        pinned_port_factory=fake_port_factory,
    )

    assert len(run.cells) == 3
    assert set(constructed) == {
        (
            "gemini-3.6-flash",
            "gemini-flash-subscription",
            "antigravity-cli",
            "gemini-3.6-flash-high",
        )
    }
    assert json.loads((request.receipt_root / "aggregation-targets.json").read_text()) == {
        "schema_version": "ProductionQualificationTargets.v1",
        "logical_model_ids": ["gemini-3.6-flash"],
    }


def test_live_wait_false_preserves_scratch_and_prevents_receipt_completion(
    tmp_path, v2_delivery_flags
) -> None:
    request = _request(tmp_path)

    def fake_port_factory(_logical_model_id, route):
        return _DeterministicRouteProvider(route, force_initial_shortfall=False)

    with pytest.raises(LiveQualificationError, match="scratch was preserved"):
        execute_live_qualification(
            request,
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
            pinned_port_factory=fake_port_factory,
            runner_stop_waiter=lambda _runner, _timeout: False,
        )
    assert request.scratch_root.is_dir()
    assert len(tuple(request.scratch_root.iterdir())) == 1
    assert not (request.receipt_root / "receipts").exists()


def test_live_wait_failure_preserves_active_qualification_error_as_cause(
    tmp_path, v2_delivery_flags
) -> None:
    request = _request(tmp_path)

    def fake_port_factory(_logical_model_id, _route):
        def fail(_prompt: str) -> str:
            raise RuntimeError("provider failure detail")

        return fail

    with pytest.raises(LiveQualificationError, match="scratch was preserved") as raised:
        execute_live_qualification(
            request,
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
            pinned_port_factory=fake_port_factory,
            runner_stop_waiter=lambda _runner, _timeout: False,
        )

    runner_error = raised.value.__cause__
    assert runner_error is not None
    assert isinstance(runner_error.__cause__, AssertionError)


def test_cleanup_failure_does_not_mask_original_cell_failure(tmp_path, monkeypatch) -> None:
    class OriginalCellError(RuntimeError):
        pass

    harness = ProductionQualificationHarness(tmp_path / "receipts", source_commit=_HEAD)

    def fail_cell(*_args, **_kwargs):
        raise OriginalCellError("original cell failure")

    def fail_cleanup(_path):
        raise OSError("cleanup failure")

    monkeypatch.setattr(harness, "_run_cell", fail_cell)
    monkeypatch.setattr("hramatka.qualification.harness.shutil.rmtree", fail_cleanup)

    with pytest.raises(OriginalCellError, match="original cell failure"):
        harness.run_with_provider_factory(
            deterministic_runtime_anchors(),
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            provider_factory=lambda _anchor, _model, _route: object(),
            scratch_root=tmp_path / "scratch",
            bake_hard_timeout_seconds=1,
            readiness_timeout_seconds=1,
            runner_stop_timeout_seconds=1,
        )


def test_cleanup_failure_cannot_report_a_successful_cell(tmp_path, monkeypatch) -> None:
    harness = ProductionQualificationHarness(tmp_path / "receipts", source_commit=_HEAD)

    def fail_cleanup(_path):
        raise OSError("cleanup failure")

    monkeypatch.setattr(harness, "_run_cell", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("hramatka.qualification.harness.shutil.rmtree", fail_cleanup)

    with pytest.raises(QualificationError, match="scratch cleanup failed"):
        harness.run_with_provider_factory(
            deterministic_runtime_anchors(),
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            provider_factory=lambda _anchor, _model, _route: object(),
            scratch_root=tmp_path / "scratch",
            bake_hard_timeout_seconds=1,
            readiness_timeout_seconds=1,
            runner_stop_timeout_seconds=1,
        )


def test_cli_unexpected_failure_is_content_free(tmp_path, monkeypatch, capsys) -> None:
    import hramatka.qualification.live as live_module

    manifest = load_manifest()
    anchor_path = tmp_path / "anchors.json"
    anchor_path.write_text(
        json.dumps(
            {
                "anchors": [
                    {
                        "id": "b1-narrative",
                        "source_identity": "proof-only",
                        "text": "sensitive anchor text",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fail_unexpectedly(_request):
        raise RuntimeError("sensitive provider response")

    monkeypatch.setattr(live_module, "execute_live_qualification", fail_unexpectedly)
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--anchors-json",
                str(anchor_path),
                "--receipt-root",
                str(tmp_path / "receipts"),
                "--scratch-root",
                str(tmp_path / "scratch"),
                "--source-commit",
                _HEAD,
                "--manifest-sha256",
                manifest.sha256,
            ]
        )

    error_output = capsys.readouterr().err
    assert exit_info.value.code == 1
    assert error_output == "Qualification failed unexpectedly.\n"
    assert "sensitive" not in error_output
