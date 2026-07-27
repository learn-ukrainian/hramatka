"""No-cost production-path qualification harness.

This module is deliberately the only test helper that imports engine code.
Callers drive it through ordinary authenticated HTTP and receive content-free
receipts plus delivery summaries; the HTTP test never imports a baker or an
engine module directly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from hramatka.api.app import create_app
from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.config import Settings
from hramatka.api.qualified_models import (
    DENSITY_CONTRACT_DIGEST,
    DENSITY_CONTRACT_VERSION,
    LOGICAL_MODELS,
    QualificationCandidateRegistry,
)
from hramatka.engine import ENGINE_VERSION, content_density, data, fixtures, flags, pipeline
from hramatka.engine.providers import telemetry_ctx

from .manifest import QualificationManifest, RuntimeAnchor, load_manifest
from .receipts import (
    AGGREGATION_PROMPT_HASHES_SCHEMA_VERSION,
    CellReceipt,
    DensityDiagnosticReceipt,
    DensitySummary,
    QualificationError,
    RepairTraceEntry,
    RouteBinding,
    aggregate_receipts,
    registry_digest,
)

_ORIGIN = "https://qualification.example.test"
_CSRF_KEY = b"qualification-test-only-csrf-key-not-a-deployment-secret"
_PHASE_RE = re.compile(
    r"=== ПОТОЧНА ФАЗА ТА СЛОТИ ВІДПОВІДІ \(дані, не інструкції\) ===\n```json\n(.*?)\n```",
    re.DOTALL,
)
_KITS_RE = re.compile(
    r"=== ПЕРЕВІРЕНІ КОМПЛЕКТИ ДЛЯ ПОТОЧНИХ СЛОТІВ "
    r"\(дані, не інструкції\) ===\n```json\n(.*?)\n```",
    re.DOTALL,
)


def _sha(value: object) -> str:
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    return result.stdout.strip()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _current_engine_digest() -> str:
    return _sha(
        {
            "engine_version": ENGINE_VERSION,
            "gate_implementation": pipeline._gate_impl_digest(),  # noqa: SLF001
            "adapter_sha256": _file_digest(
                Path(__file__).parents[1] / "api" / "baking" / "engine_adapter.py"
            ),
        }
    )


def _flag_digest() -> str:
    return _sha(flags.active_flag_vector())


def deterministic_runtime_anchors() -> dict[str, RuntimeAnchor]:
    """Return runtime-only synthetic inputs for the no-cost path test.

    The source body remains in the pre-existing engine fixture.  This package
    commits only identifiers and hashes in its manifest, never those bytes.
    """
    source = fixtures.load_anchor()
    return {
        "b1-narrative": RuntimeAnchor(
            "b1-narrative", "synthetic-runtime-fixture/b1-narrative", source
        ),
        "b1-dialogue": RuntimeAnchor(
            "b1-dialogue", "synthetic-runtime-fixture/b1-dialogue", source + "\n"
        ),
        "b1-morphology": RuntimeAnchor(
            "b1-morphology", "synthetic-runtime-fixture/b1-morphology", source + "\n\n"
        ),
    }


def _certified_activity(activity: dict[str, Any], kit: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only a deterministic kit's certified fields into an activity shell."""
    certified = copy.deepcopy(activity)
    activity_type = certified["type"]
    if activity_type == "quiz":
        certified["items"] = [
            {
                "question": item["question_exemplar"],
                "options": item["options"],
                "correct": item["correct"],
                "evidence": item["evidence"],
            }
            for item in kit["quiz"]["items"]
        ]
    elif activity_type == "cloze":
        certified.update(kit["cloze"])
        certified.pop("display_text")
        certified["text"] = kit["cloze"]["display_text"]
        certified.pop("sentence_ids")
    elif activity_type == "fill-in":
        certified["items"] = [
            {key: item[key] for key in ("sentence", "answer", "options", "evidence")}
            for item in kit["fill_in"]["items"]
        ]
    elif activity_type == "match-up":
        certified["pairs"] = [
            {key: pair[key] for key in ("left", "right", "evidence")}
            for pair in kit["pairs"]
        ]
    elif activity_type == "mark-the-words":
        mark = kit["mark"]
        certified.update(
            {
                "instruction": mark["instruction"],
                "text": mark["text"],
                "criteria": mark["criterion"],
                "target_words": mark["expected_target_words"],
                "evidence": mark["text"],
            }
        )
    elif activity_type == "short-writing":
        short_writing = kit["short_writing"]
        certified.update(
            {
                "prompt": short_writing["prompt_exemplar"],
                "evidence": short_writing["evidence"],
                "word_count_guidance": short_writing["word_count_guidance"],
            }
        )
    return certified


def _citations(
    activities: list[dict[str, Any]], context: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    kits = context["type_kits"]
    for index, (activity, kit) in enumerate(zip(activities, kits, strict=True)):
        sentence_ids = kit.get("citation_plan")
        if sentence_ids is None:
            allowed = context["phase_request"]["requested_slots"][index]["allowed_sentence_ids"]
            activity_type = activity["type"]
            if activity_type in {"cloze", "mark-the-words", "short-writing"}:
                locators = ["text"]
            elif activity_type == "match-up":
                locators = [f"pairs[{item}]" for item in range(len(activity["pairs"]))]
            else:
                locators = [f"items[{item}]" for item in range(len(activity["items"]))]
            sentence_ids = {locator: [allowed[0]] for locator in locators}
        rows.append({"activity_index": index, "sentence_ids": sentence_ids})
    return rows


class _DeterministicRouteProvider:
    """Explicit no-network provider boundary with route-bound call telemetry."""

    def __init__(self, route: RouteBinding, *, force_initial_shortfall: bool) -> None:
        self._route = route
        self._model = route.model_id
        self._force_initial_shortfall = force_initial_shortfall
        self._shortfall_used = False
        self._counters: Counter[str] = Counter()
        self._lock = threading.Lock()
        self.calls: list[RepairTraceEntry] = []
        self.prompt_digests: list[str] = []

    def for_bake(self) -> _DeterministicRouteProvider:
        return self

    def __call__(self, prompt: str) -> str:
        phase_match = _PHASE_RE.search(prompt)
        kits_match = _KITS_RE.search(prompt)
        if phase_match is None or kits_match is None:
            raise AssertionError("Deterministic provider received no prompt-pack boundary.")
        phase_request = json.loads(phase_match.group(1))
        kits = json.loads(kits_match.group(1))
        mode = phase_request["mode"]
        phase = phase_request["phase"]
        with self._lock:
            raw_activities = fixtures.activities_for_prompt(prompt, self._counters)
            activities = [
                _certified_activity(activity, kit)
                for activity, kit in zip(raw_activities, kits, strict=True)
            ]
            if (
                self._force_initial_shortfall
                and not self._shortfall_used
                and mode == "initial"
                and phase == 3
            ):
                short_writing = next(
                    activity for activity in activities if activity["type"] == "short-writing"
                )
                # The prompt envelope remains valid, but the ordinary raw
                # contract rejects this one candidate.  The real selector then
                # invokes the generic slot-repair loop for the missing transfer.
                short_writing.pop("source_ref", None)
                self._shortfall_used = True
            binding = RepairTraceEntry(
                mode=mode,
                phase=phase,
                expected_route=self._route,
                observed_route=self._route,
            )
            self.calls.append(binding)
            self.prompt_digests.append(_sha(prompt))
        context = {
            "phase_request": phase_request,
            "type_kits": kits,
        }
        ctx = telemetry_ctx.get()
        if ctx is not None:
            ctx.record_provider_call(
                {
                    "event": "qualification_provider_call",
                    "mode": mode,
                    "phase": phase,
                    "provider_route": self._route.route_id,
                    "expected_host": self._route.host,
                    "expected_model": self._route.model_id,
                    "observed_host": self._route.host,
                    "observed_model": self._route.model_id,
                    "qualification_route_trace": binding.as_dict(),
                    "host": self._route.host,
                    "model": self._route.model_id,
                    "duration_ms": 0,
                    "attempts": 1,
                    "http_status_class": "2xx",
                    "activity_type": "qualification-cell",
                }
            )
        return json.dumps(
            {"activities": activities, "citations": _citations(activities, context)},
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class DeliverySummary:
    durable_job: bool
    block_count: int
    phase_counts: Mapping[str, int]
    response_units: int
    activity_types: frozenset[str]
    phase_three_transfer: bool
    provenance_continuous: bool


@dataclass(frozen=True)
class QualificationCellResult:
    receipt: CellReceipt
    delivery: DeliverySummary
    density_trace: tuple[dict[str, object], ...]
    repair_invocation_trace: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class QualificationRun:
    cells: tuple[QualificationCellResult, ...]
    receipt_paths: tuple[Path, ...]

    @property
    def receipts(self) -> tuple[CellReceipt, ...]:
        return tuple(cell.receipt for cell in self.cells)


@dataclass(frozen=True)
class QualificationDiagnosticRun:
    """One content-free, instrumented live cell and its external receipt."""

    cell: QualificationCellResult
    receipt_path: Path


ProviderFactory = Callable[[RuntimeAnchor, str, RouteBinding], Any]
RunnerStopWaiter = Callable[[Any, float], bool]


class QualificationRunnerStillActiveError(QualificationError):
    """A live scratch directory cannot be safely deleted yet."""


class ProductionQualificationHarness:
    """Execute the exact 3 anchors × 4 configured route cells without spend."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        manifest: QualificationManifest | None = None,
        source_commit: str | None = None,
    ) -> None:
        self._root = runtime_root
        self._manifest = manifest or load_manifest()
        self._source_commit = source_commit or _source_commit()
        self._last_anchor_hashes: dict[str, str] | None = None
        self._last_prompt_hashes: dict[tuple[str, str, str], str] | None = None

    def run(self, anchors: Mapping[str, RuntimeAnchor]) -> QualificationRun:
        if os.environ.get("HRAMATKA_SLOT_REPAIR") != "1":
            raise RuntimeError("The qualification harness requires explicit generic slot repair.")
        if os.environ.get("HRAMATKA_PROMPT_PACK") != "1":
            raise RuntimeError("The qualification harness requires explicit prompt-pack routing.")
        self._root.mkdir(parents=True, exist_ok=True)
        runtime_anchors = self._manifest.validate_runtime_anchors(anchors)
        self._last_anchor_hashes = {anchor.id: _sha(anchor.text) for anchor in runtime_anchors}
        self._last_prompt_hashes = {}
        bundle = fixtures._bundle_with_matchup_vocabulary(self._root / "fixture-data")
        return self._run_cells(
            runtime_anchors,
            bundle=bundle,
            provider_factory=lambda anchor, _logical_model_id, route: _DeterministicRouteProvider(
                route,
                force_initial_shortfall=(
                    anchor.id == "b1-morphology" and route.route_id == "gemma-openrouter"
                ),
            ),
            scratch_root=self._root,
            temporary_cells=False,
            bake_hard_timeout_seconds=600,
            readiness_timeout_seconds=20,
            runner_stop_timeout_seconds=1,
            runner_stop_waiter=lambda runner, timeout: runner.wait_until_stopped(timeout),
        )

    def run_with_provider_factory(
        self,
        anchors: Mapping[str, RuntimeAnchor],
        *,
        bundle: data.DataBundle,
        provider_factory: ProviderFactory,
        scratch_root: Path,
        bake_hard_timeout_seconds: int,
        readiness_timeout_seconds: int,
        runner_stop_timeout_seconds: float,
        runner_stop_waiter: RunnerStopWaiter | None = None,
    ) -> QualificationRun:
        """Run the same HTTP production path with an already-preflighted provider.

        The caller owns all live-mode authorization and provenance checks.  This
        method intentionally has no provider configuration or environment
        policy: each supplied factory receives one immutable matrix route.
        """
        runtime_anchors = self._manifest.validate_runtime_anchors(anchors)
        self._root.mkdir(parents=True, exist_ok=True)
        scratch_root.mkdir(parents=True, exist_ok=True)
        return self._run_cells(
            runtime_anchors,
            bundle=bundle,
            provider_factory=provider_factory,
            scratch_root=scratch_root,
            temporary_cells=True,
            bake_hard_timeout_seconds=bake_hard_timeout_seconds,
            readiness_timeout_seconds=readiness_timeout_seconds,
            runner_stop_timeout_seconds=runner_stop_timeout_seconds,
            runner_stop_waiter=runner_stop_waiter
            or (lambda runner, timeout: runner.wait_until_stopped(timeout)),
        )

    def run_diagnostic_cell(
        self,
        anchor: RuntimeAnchor,
        logical_model_id: str,
        route: RouteBinding,
        *,
        bundle: data.DataBundle,
        provider: Any,
        scratch_root: Path,
        bake_hard_timeout_seconds: int,
        readiness_timeout_seconds: int,
        runner_stop_timeout_seconds: float,
        runner_stop_waiter: RunnerStopWaiter | None = None,
    ) -> QualificationDiagnosticRun:
        """Run one already-authorized production cell with durable count traces.

        The caller must have validated the full runtime anchor pack, source
        identity, configured route, credentials, and explicit spend
        acknowledgement.  This narrow method deliberately does not aggregate
        or qualify a model.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        scratch_root.mkdir(parents=True, exist_ok=True)
        raw_cell_root = Path(
            tempfile.mkdtemp(dir=scratch_root, prefix=f"{anchor.id}-{route.route_id}-")
        )
        previous_bundle = data._active  # noqa: SLF001 - mirror normal worker scope.
        data.set_active_bundle(bundle)
        try:
            cell = self._run_cell(
                anchor,
                logical_model_id,
                route,
                bundle,
                provider=provider,
                cell_root=raw_cell_root,
                bake_hard_timeout_seconds=bake_hard_timeout_seconds,
                readiness_timeout_seconds=readiness_timeout_seconds,
                runner_stop_timeout_seconds=runner_stop_timeout_seconds,
                runner_stop_waiter=runner_stop_waiter
                or (lambda runner, timeout: runner.wait_until_stopped(timeout)),
                allow_failed_diagnostic=True,
            )
            receipt_path = self._persist_diagnostic_receipt(cell)
        except QualificationRunnerStillActiveError:
            raise
        except Exception:
            try:
                shutil.rmtree(raw_cell_root)
            except OSError:
                pass
            raise
        else:
            try:
                shutil.rmtree(raw_cell_root)
            except OSError as error:
                raise QualificationError("Qualification scratch cleanup failed.") from error
            return QualificationDiagnosticRun(cell=cell, receipt_path=receipt_path)
        finally:
            data.set_active_bundle(previous_bundle)

    def _run_cells(
        self,
        runtime_anchors: tuple[RuntimeAnchor, ...],
        *,
        bundle: data.DataBundle,
        provider_factory: ProviderFactory,
        scratch_root: Path,
        temporary_cells: bool,
        bake_hard_timeout_seconds: int,
        readiness_timeout_seconds: int,
        runner_stop_timeout_seconds: float,
        runner_stop_waiter: RunnerStopWaiter,
    ) -> QualificationRun:
        self._last_anchor_hashes = {anchor.id: _sha(anchor.text) for anchor in runtime_anchors}
        self._last_prompt_hashes = {}
        cells: list[QualificationCellResult] = []
        receipt_paths: list[Path] = []
        previous_bundle = data._active  # noqa: SLF001 - worker threads need this explicit scope.
        data.set_active_bundle(bundle)
        try:
            for runtime_anchor in runtime_anchors:
                for model in LOGICAL_MODELS:
                    for configured_route in model.provider_routes:
                        route = RouteBinding(
                            configured_route.id, configured_route.host, configured_route.model_id
                        )
                        provider = provider_factory(runtime_anchor, model.id, route)
                        if temporary_cells:
                            raw_cell_root = Path(
                                tempfile.mkdtemp(
                                    dir=scratch_root,
                                    prefix=f"{runtime_anchor.id}-{route.route_id}-",
                                )
                            )
                            try:
                                cell = self._run_cell(
                                    runtime_anchor,
                                    model.id,
                                    route,
                                    bundle,
                                    provider=provider,
                                    cell_root=Path(raw_cell_root),
                                    bake_hard_timeout_seconds=bake_hard_timeout_seconds,
                                    readiness_timeout_seconds=readiness_timeout_seconds,
                                    runner_stop_timeout_seconds=runner_stop_timeout_seconds,
                                    runner_stop_waiter=runner_stop_waiter,
                                )
                            except QualificationRunnerStillActiveError:
                                # This directory can contain durable job/cache data.
                                # Preserve it until the process has actually exited.
                                raise
                            except Exception:
                                try:
                                    shutil.rmtree(raw_cell_root)
                                except OSError:
                                    # Preserve the original qualification failure.
                                    pass
                                raise
                            else:
                                try:
                                    shutil.rmtree(raw_cell_root)
                                except OSError as error:
                                    raise QualificationError(
                                        "Qualification scratch cleanup failed."
                                    ) from error
                        else:
                            cell = self._run_cell(
                                runtime_anchor,
                                model.id,
                                route,
                                bundle,
                                provider=provider,
                                cell_root=scratch_root / f"{runtime_anchor.id}-{route.route_id}",
                                bake_hard_timeout_seconds=bake_hard_timeout_seconds,
                                readiness_timeout_seconds=readiness_timeout_seconds,
                                runner_stop_timeout_seconds=runner_stop_timeout_seconds,
                                runner_stop_waiter=runner_stop_waiter,
                            )
                        cells.append(cell)
                        receipt_paths.append(self._persist_cell_receipt(cell.receipt))
                        self._last_prompt_hashes[(model.id, route.route_id, runtime_anchor.id)] = (
                            cell.receipt.prompt_sha256
                        )
        finally:
            data.set_active_bundle(previous_bundle)
        self._persist_aggregation_prompt_hashes()
        return QualificationRun(tuple(cells), tuple(receipt_paths))

    def aggregates(self, run: QualificationRun):
        return self.aggregate_cells(run.receipts)

    def aggregate_cells(self, receipts: tuple[CellReceipt, ...]):
        if self._last_anchor_hashes is None or self._last_prompt_hashes is None:
            raise RuntimeError("Run the qualification cells before aggregating receipts.")
        return aggregate_receipts(
            receipts,
            source_commit=self._source_commit,
            harness_sha256=_file_digest(Path(__file__)),
            manifest_sha256=self._manifest.sha256,
            anchor_hashes=self._last_anchor_hashes,
            prompt_hashes=self._last_prompt_hashes,
            engine_sha256=_current_engine_digest(),
            flag_sha256=_flag_digest(),
        )

    def _persist_cell_receipt(self, receipt: CellReceipt) -> Path:
        """Persist one strictly validated, content-free receipt outside source control."""
        parsed = CellReceipt.from_dict(receipt.as_dict())
        directory = self._root / "receipts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{parsed.anchor_id}-{parsed.expected_route.route_id}.json"
        path.write_text(
            json.dumps(parsed.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return path

    def _persist_aggregation_prompt_hashes(self) -> Path:
        if self._last_prompt_hashes is None:
            raise RuntimeError("Run the qualification cells before persisting aggregation inputs.")
        rows = [
            {
                "logical_model_id": model_id,
                "route_id": route_id,
                "anchor_id": anchor_id,
                "sha256": digest,
            }
            for (model_id, route_id, anchor_id), digest in sorted(self._last_prompt_hashes.items())
        ]
        path = self._root / "aggregation-prompt-hashes.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": AGGREGATION_PROMPT_HASHES_SCHEMA_VERSION,
                    "prompt_hashes": rows,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return path

    def _persist_diagnostic_receipt(self, cell: QualificationCellResult) -> Path:
        """Persist exactly the count-only trace needed for one root-cause cell."""
        parsed = DensityDiagnosticReceipt.from_dict(
            DensityDiagnosticReceipt(
                cell_receipt=cell.receipt,
                density_trace=cell.density_trace,
                repair_invocation_trace=cell.repair_invocation_trace,
            ).as_dict()
        )
        directory = self._root / "diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (
            f"{parsed.cell_receipt.anchor_id}-"
            f"{parsed.cell_receipt.expected_route.route_id}.json"
        )
        path.write_text(
            json.dumps(parsed.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return path

    def _run_cell(
        self,
        anchor: RuntimeAnchor,
        logical_model_id: str,
        route: RouteBinding,
        bundle: data.DataBundle,
        *,
        provider: Any,
        cell_root: Path,
        bake_hard_timeout_seconds: int,
        readiness_timeout_seconds: int,
        runner_stop_timeout_seconds: float,
        runner_stop_waiter: RunnerStopWaiter,
        allow_failed_diagnostic: bool = False,
    ) -> QualificationCellResult:
        def logical_generator_factory(requested_logical_model_id: str):
            if requested_logical_model_id != logical_model_id:
                raise ValueError("Qualification provider received the wrong logical model.")
            return provider

        baker = EngineLessonBaker(
            generator=lambda _prompt: (_ for _ in ()).throw(
                AssertionError("legacy generator used")
            ),
            bundle=bundle,
            cache_dir=cell_root / "cache",
            engine_out_dir=cell_root / "engine-out",
            logical_generator_factory=logical_generator_factory,
        )
        app = create_app(
            settings=Settings(
                database_path=cell_root / "jobs.sqlite3",
                pilot_origin=_ORIGIN,
                csrf_hmac_key=_CSRF_KEY,
                bake_hard_timeout_seconds=bake_hard_timeout_seconds,
                bake_workers=1,
            ),
            baker=baker,
            model_registry=QualificationCandidateRegistry(
                logical_model_id=logical_model_id,
                provider_route=route.route_id,
                provider_host=route.host,
                provider_model_id=route.model_id,
            ),
        )
        baker.store = app.state.store
        try:
            with TestClient(app, base_url=_ORIGIN) as client:
                teacher = app.state.store.create_teacher(display_name="Qualification teacher")
                _, invite = app.state.store.create_invite(teacher.id)
                redeemed = client.post(
                    "/api/session/redeem", headers={"Origin": _ORIGIN}, json={"token": invite}
                )
                if redeemed.status_code != 200:
                    raise AssertionError("Qualification session could not be redeemed.")
                csrf = redeemed.json()["csrf_token"]
                lesson_id = str(uuid.uuid4())
                created = client.post(
                    "/api/lessons",
                    headers={"Origin": _ORIGIN, "X-CSRF-Token": csrf},
                    json={
                        "id": lesson_id,
                        "anchor": {"text": anchor.text, "source": "teacher-paste"},
                        "level": "B1",
                        "duration": 45,
                        "focus": None,
                        "logical_model_id": logical_model_id,
                    },
                )
                if created.status_code != 202:
                    raise AssertionError("Qualification lesson was not accepted through HTTP.")
                deadline = time.monotonic() + readiness_timeout_seconds
                status_payload: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    status = client.get(f"/api/lessons/{lesson_id}/status")
                    status_payload = status.json()
                    if status_payload.get("status") in {"ready", "failed"}:
                        break
                    time.sleep(0.01)
                terminal_status = status_payload.get("status")
                if terminal_status not in {"ready", "failed"}:
                    raise QualificationError(
                        "Qualification durable job did not reach a terminal state before "
                        "readiness timeout."
                    )
                if terminal_status != "ready" and not allow_failed_diagnostic:
                    raise AssertionError("Qualification durable job did not become ready.")
                durable_job = app.state.store.get(teacher.id, lesson_id)
                if durable_job is None or durable_job.status != terminal_status:
                    raise AssertionError("Qualification did not persist a durable terminal job.")
                resource = None
                if terminal_status == "ready":
                    resource = client.get(f"/api/lessons/{lesson_id}")
                    if resource.status_code != 200:
                        raise AssertionError("Qualification lesson resource was unavailable.")
        finally:
            if not runner_stop_waiter(app.state.runner, runner_stop_timeout_seconds):
                active_error = sys.exc_info()[1]
                raise QualificationRunnerStillActiveError(
                    "Qualification runner is still active; scratch was preserved."
                ) from active_error
        durable_trace = self._durable_route_trace(durable_job)
        density_trace, repair_invocation_trace = self._durable_diagnostic_traces(durable_job)
        if resource is not None:
            delivery = self._delivery_summary(
                resource.json(), logical_model_id, route, durable_job, durable_trace
            )
            density = self._durable_density_summary(durable_job)
        else:
            delivery, density = self._failed_diagnostic_delivery(density_trace)
        expected_trace = durable_trace
        receipt = CellReceipt(
            source_commit=self._source_commit,
            harness_sha256=_file_digest(Path(__file__)),
            manifest_sha256=self._manifest.sha256,
            anchor_id=anchor.id,
            anchor_sha256=_sha(anchor.text),
            logical_model_id=logical_model_id,
            expected_route=route,
            observed_route=route,
            prompt_sha256=_sha(sorted(provider.prompt_digests)),
            density_contract_version=DENSITY_CONTRACT_VERSION,
            density_contract_sha256=DENSITY_CONTRACT_DIGEST,
            registry_sha256=registry_digest(),
            engine_sha256=_current_engine_digest(),
            flag_sha256=_flag_digest(),
            density=density,
            repair_trace=expected_trace,
            semantic_gate="not_run",
            outcome="passed" if self._delivery_ready(delivery, density) else "failed",
        )
        return QualificationCellResult(
            receipt=receipt,
            delivery=delivery,
            density_trace=density_trace,
            repair_invocation_trace=repair_invocation_trace,
        )

    @staticmethod
    def _durable_route_trace(job: Any) -> tuple[RepairTraceEntry, ...]:
        progress = job.progress
        if not isinstance(progress, dict):
            raise AssertionError("Qualification durable job has no progress telemetry.")
        traces = progress.get("qualification_route_traces")
        if not isinstance(traces, list):
            raise AssertionError("Qualification durable job has no route trace telemetry.")
        return tuple(RepairTraceEntry.from_dict(entry) for entry in traces)

    @staticmethod
    def _durable_diagnostic_traces(
        job: Any,
    ) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
        progress = job.progress
        if not isinstance(progress, dict):
            raise AssertionError("Qualification durable job has no progress telemetry.")
        density = progress.get("qualification_density_traces")
        repair = progress.get("qualification_repair_traces", [])
        if (
            not isinstance(density, list)
            or not density
            or not all(isinstance(entry, dict) for entry in density)
            or not isinstance(repair, list)
            or not all(isinstance(entry, dict) for entry in repair)
        ):
            raise AssertionError("Qualification durable job has no density diagnostic telemetry.")
        return (
            tuple(copy.deepcopy(entry) for entry in density),
            tuple(copy.deepcopy(entry) for entry in repair),
        )

    @staticmethod
    def _failed_diagnostic_delivery(
        density_trace: tuple[dict[str, object], ...],
    ) -> tuple[DeliverySummary, DensitySummary]:
        """Project a failed diagnostic's last count-only snapshot, never lesson content."""
        last = density_trace[-1]
        phase_density = last["phase_density"]
        assert isinstance(phase_density, dict)  # validated by diagnostic receipt persistence
        phase_counts = {
            phase: int(phase_density[phase]["visible_blocks"])
            for phase in ("1", "2", "3")
        }
        response_units = sum(
            int(phase_density[phase]["response_units"]) for phase in ("1", "2", "3")
        )
        density = DensitySummary(
            delivered_blocks=sum(phase_counts.values()),
            ready_blocks=sum(phase_counts.values()),
            tray_blocks=0,
            floor_blocks=sum(phase_counts.values()),
            phase_counts=phase_counts,
            ready_phase_counts=phase_counts,
            tray_phase_counts={phase: 0 for phase in phase_counts},
            response_units=response_units,
            ready_response_units=response_units,
            tray_response_units=0,
            disposition="recoverable_draft",
        )
        return (
            DeliverySummary(
                durable_job=False,
                block_count=density.delivered_blocks,
                phase_counts=phase_counts,
                response_units=response_units,
                activity_types=frozenset(),
                phase_three_transfer=False,
                provenance_continuous=False,
            ),
            density,
        )

    @staticmethod
    def _durable_density_summary(job: Any) -> DensitySummary:
        progress = job.progress
        if not isinstance(progress, dict):
            raise AssertionError("Qualification durable job has no progress telemetry.")
        density = progress.get("teacher_ready_density")
        try:
            return DensitySummary.from_dict(density)
        except QualificationError as error:
            raise AssertionError("Qualification durable job has no density telemetry.") from error

    @staticmethod
    def _delivery_summary(
        resource: Mapping[str, Any],
        logical_model_id: str,
        route: RouteBinding,
        durable_job: Any,
        trace: tuple[RepairTraceEntry, ...],
    ) -> DeliverySummary:
        lesson = resource["lesson"]
        blocks = lesson["blocks"]
        phase_counts = Counter(str(block["phase"]) for block in blocks)
        payloads = [block["activity"]["payload"] for block in blocks]
        activity_types = frozenset(str(payload["type"]) for payload in payloads)
        response_units = sum(content_density.response_units(payload) for payload in payloads)
        phase_three_transfer = any(
            block["phase"] == 3 and block["type"] in content_density.PRODUCTIVE_TYPES
            for block in blocks
        )
        provenance_continuous = (
            resource.get("logical_model_id") == logical_model_id
            and durable_job.logical_model_id == logical_model_id
            and durable_job.progress.get("provider_routes")
            == [{"host": route.host, "model": route.model_id}]
            and all(
                entry.expected_route == route and entry.observed_route == route
                for entry in trace
            )
        )
        return DeliverySummary(
            durable_job=True,
            block_count=len(blocks),
            phase_counts=dict(phase_counts),
            response_units=response_units,
            activity_types=activity_types,
            phase_three_transfer=phase_three_transfer,
            provenance_continuous=provenance_continuous,
        )

    @staticmethod
    def _delivery_ready(delivery: DeliverySummary, density: DensitySummary) -> bool:
        return (
            delivery.durable_job
            and density.disposition == "teacher_ready"
            and density.floor_blocks >= 8
            and all(
                density.phase_counts[phase] >= expected
                for phase, expected in {"1": 3, "2": 4, "3": 1}.items()
            )
            and density.response_units >= 28
            and delivery.block_count == density.ready_blocks
            and delivery.phase_counts == density.ready_phase_counts
            and delivery.response_units == density.ready_response_units
            and delivery.provenance_continuous
        )
