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
    DEFAULT_GEMMA_AIS_BASE_URL,
    DEFAULT_GEMMA_FALLBACK_BASE_URL,
    GEMMA_AIS_BASE_URL_ENV,
    GEMMA_FALLBACK_BASE_URL_ENV,
    GEMMA_FALLBACK_MODEL_ENV,
    FailoverGeneratorPort,
    make_qualification_pinned_generator,
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
                route_id="gemini-flash-ais",
            ),
        ),
        anchor_id="b1-narrative",
        route_id="gemini-flash-ais",
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
def qualification_flags(monkeypatch):
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
    tmp_path, qualification_flags, changes, message
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
    tmp_path, qualification_flags
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
    tmp_path, qualification_flags
) -> None:
    request = _diagnostic_request(tmp_path)
    constructed: list[tuple[str, str]] = []
    credential_routes: list[str] = []

    def credential_present(**route: str) -> bool:
        credential_routes.append(route["route_id"])
        return route["route_id"] == "gemini-flash-ais"

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

    assert constructed == [("gemini-3.5-flash", "gemini-flash-ais")]
    assert credential_routes == ["gemini-flash-ais"]
    assert run.cell.receipt.outcome == "passed"
    parsed = DensityDiagnosticReceipt.from_dict(
        json.loads(run.receipt_path.read_text(encoding="utf-8"))
    )
    assert parsed.cell_receipt.expected_route.route_id == "gemini-flash-ais"
    assert parsed.density_trace[0]["stage"] == "initial"
    assert set(parsed.density_trace[0]["phase_density"]) == {"1", "2", "3"}
    assert parsed.density_trace[-1]["repair_invocations"] == len(
        parsed.repair_invocation_trace
    )
    assert request.qualification.scratch_root.is_dir()
    assert list(request.qualification.scratch_root.iterdir()) == []


def test_live_density_diagnostic_rejects_an_unconfigured_route_before_provider(
    tmp_path, qualification_flags
) -> None:
    request = replace(_diagnostic_request(tmp_path), route_id="wrong-route")
    with pytest.raises(LiveQualificationError, match="unique configured"):
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
def test_repository_state_failure_is_content_free(
    tmp_path, monkeypatch, repository_error
) -> None:
    def fail_git(*_args, **_kwargs):
        raise repository_error

    monkeypatch.setattr("hramatka.qualification.live.subprocess.run", fail_git)
    with pytest.raises(LiveQualificationError) as raised:
        _repository_state(tmp_path)

    assert str(raised.value) == "Git repository state could not be verified."
    assert "sensitive" not in str(raised.value)


def test_invalid_later_route_runtime_refuses_before_any_pinned_port_factory(
    tmp_path, qualification_flags
) -> None:
    request = _request(tmp_path)
    constructed = False

    def port_factory(*_args):
        nonlocal constructed
        constructed = True
        raise AssertionError("a provider must not be constructed")

    def runtime_route_valid(**route: str) -> None:
        if route["route_id"] == "gemma-openrouter":
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


@pytest.mark.parametrize(
    ("environment", "value"),
    [
        (GEMMA_AIS_BASE_URL_ENV, "https://untrusted.example/v1"),
        (GEMMA_FALLBACK_MODEL_ENV, "other/model"),
        (GEMMA_FALLBACK_BASE_URL_ENV, "https://untrusted.example/v1"),
    ],
)
def test_noncanonical_later_route_override_refuses_before_any_pinned_port_factory(
    tmp_path, qualification_flags, monkeypatch, environment, value
) -> None:
    monkeypatch.setenv(environment, value)
    request = _request(tmp_path)
    constructed = False

    def port_factory(*_args):
        nonlocal constructed
        constructed = True
        raise AssertionError("a provider must not be constructed")

    with pytest.raises(LiveQualificationError, match="runtime configuration"):
        execute_live_qualification(
            request,
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
            pinned_port_factory=port_factory,
        )
    assert not constructed


@pytest.mark.parametrize(
    ("environment", "value"),
    [
        (GEMMA_AIS_BASE_URL_ENV, f"{DEFAULT_GEMMA_AIS_BASE_URL}/"),
        (GEMMA_FALLBACK_BASE_URL_ENV, f"{DEFAULT_GEMMA_FALLBACK_BASE_URL}/"),
    ],
)
def test_canonical_route_base_accepts_one_trailing_slash(
    monkeypatch, environment, value
) -> None:
    monkeypatch.setenv(environment, value)
    for logical_model_id, route in _matrix():
        validate_qualification_route_runtime(
            route_id=route.route_id,
            logical_model_id=logical_model_id,
            host=route.host,
            model_id=route.model_id,
        )


def test_live_passes_production_timeouts_to_shared_harness(
    tmp_path, qualification_flags, monkeypatch
) -> None:
    request = _request(tmp_path)
    received: dict[str, int] = {}

    def fake_run(_self, _anchors, **kwargs):
        received["hard"] = kwargs["bake_hard_timeout_seconds"]
        received["readiness"] = kwargs["readiness_timeout_seconds"]
        return _complete_fake_run(request)

    monkeypatch.setattr(ProductionQualificationHarness, "run_with_provider_factory", fake_run)
    execute_live_qualification(
        request,
        bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
        repository_state=_clean_repository_state,
        credential_present=_credential_present,
    )
    assert received == {"hard": 1800, "readiness": 1830}


def test_live_rejects_a_failed_cell_from_the_shared_harness(
    tmp_path, qualification_flags, monkeypatch
) -> None:
    request = _request(tmp_path)
    monkeypatch.setattr(
        ProductionQualificationHarness,
        "run_with_provider_factory",
        lambda _self, _anchors, **_kwargs: _complete_fake_run(request, failed=True),
    )
    with pytest.raises(LiveQualificationError, match="b1-narrative/gemini-flash-ais"):
        execute_live_qualification(
            request,
            bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
            repository_state=_clean_repository_state,
            credential_present=_credential_present,
        )


def test_shared_runner_waits_for_injected_live_readiness_timeout(
    tmp_path, qualification_flags
) -> None:
    class SlowProvider:
        def __init__(self, route: RouteBinding) -> None:
            self._inner = _DeterministicRouteProvider(route, force_initial_shortfall=False)

        @property
        def prompt_digests(self) -> list[str]:
            return self._inner.prompt_digests

        def for_bake(self):
            return self

        def __call__(self, prompt: str) -> str:
            time.sleep(0.2)
            return self._inner(prompt)

    anchors = deterministic_runtime_anchors()
    anchor = anchors["b1-narrative"]
    route = RouteBinding("gemini-flash-ais", "google-ais", "google-ais/gemini-3.5-flash")
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
                "gemini-3.5-flash",
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
            "gemini-3.5-flash",
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


def test_pinned_factory_never_builds_failover_or_round_robin_ports() -> None:
    from hramatka.qualification.live import _matrix

    for logical_model_id, route in _matrix():
        port = make_qualification_pinned_generator(
            route_id=route.route_id,
            logical_model_id=logical_model_id,
            host=route.host,
            model_id=route.model_id,
        )
        assert not isinstance(port, FailoverGeneratorPort)
        assert port._model == route.model_id
        assert port._transport.host == route.host
        assert port._transport.max_attempts == 1
        assert port._transport.retry_json_mode_on_400 is False


def test_live_mode_uses_exact_routes_cleans_scratch_and_leaves_semantic_separate(
    tmp_path, qualification_flags, monkeypatch
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

    assert len(run.cells) == 12
    assert len(constructed) == 12
    assert set(constructed) == {
        ("gemini-3.5-flash", "gemini-flash-ais", "google-ais", "google-ais/gemini-3.5-flash"),
        ("gemini-3.1-pro", "gemini-pro-ais", "google-ais", "google-ais/gemini-3.1-pro-preview"),
        ("gemma-4-31b", "gemma-ais", "google-ais", "google-ais/gemma-4-31b-it"),
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
        if cell.receipt.expected_route.route_id == "gemini-flash-ais"
    )
    with pytest.raises(QualificationError, match="Path proof alone"):
        # The semantic gate remains a separate command and cannot be implied by
        # a successful live transport/path run.
        RouteAggregate(
            logical_model_id="gemini-3.5-flash",
            route=route_cells[0].expected_route,
            cells=route_cells,
        ).as_model_receipt()


def test_live_wait_false_preserves_scratch_and_prevents_receipt_completion(
    tmp_path, qualification_flags
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
    tmp_path, qualification_flags
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


def test_cleanup_failure_does_not_mask_original_cell_failure(
    tmp_path, monkeypatch
) -> None:
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


def test_cleanup_failure_cannot_report_a_successful_cell(
    tmp_path, monkeypatch
) -> None:
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


def test_cli_unexpected_failure_is_content_free(
    tmp_path, monkeypatch, capsys
) -> None:
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
