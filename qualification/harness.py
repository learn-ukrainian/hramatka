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
import subprocess
import threading
import time
import uuid
from collections import Counter
from collections.abc import Mapping
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
    CellReceipt,
    DensitySummary,
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


@dataclass(frozen=True)
class QualificationRun:
    cells: tuple[QualificationCellResult, ...]
    receipt_paths: tuple[Path, ...]

    @property
    def receipts(self) -> tuple[CellReceipt, ...]:
        return tuple(cell.receipt for cell in self.cells)


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
        cells: list[QualificationCellResult] = []
        receipt_paths: list[Path] = []
        previous_bundle = data._active  # noqa: SLF001 - restore the explicit test boundary.
        data.set_active_bundle(bundle)
        try:
            for runtime_anchor in runtime_anchors:
                for model in LOGICAL_MODELS:
                    for route in model.provider_routes:
                        cell = self._run_cell(
                            runtime_anchor,
                            model.id,
                            RouteBinding(route.id, route.host, route.model_id),
                            bundle,
                            force_initial_shortfall=(
                                runtime_anchor.id == "b1-morphology"
                                and route.id == "gemma-openrouter"
                            ),
                        )
                        cells.append(cell)
                        receipt_paths.append(self._persist_cell_receipt(cell.receipt))
                        self._last_prompt_hashes[
                            (
                                cell.receipt.logical_model_id,
                                cell.receipt.expected_route.route_id,
                                cell.receipt.anchor_id,
                            )
                        ] = cell.receipt.prompt_sha256
        finally:
            data.set_active_bundle(previous_bundle)
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

    def _run_cell(
        self,
        anchor: RuntimeAnchor,
        logical_model_id: str,
        route: RouteBinding,
        bundle: data.DataBundle,
        *,
        force_initial_shortfall: bool,
    ) -> QualificationCellResult:
        cell_root = self._root / f"{anchor.id}-{route.route_id}"
        provider = _DeterministicRouteProvider(
            route, force_initial_shortfall=force_initial_shortfall
        )

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
            logical_generator_factory=logical_generator_factory,
        )
        app = create_app(
            settings=Settings(
                database_path=cell_root / "jobs.sqlite3",
                pilot_origin=_ORIGIN,
                csrf_hmac_key=_CSRF_KEY,
                bake_hard_timeout_seconds=600,
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
            deadline = time.monotonic() + 20
            status_payload: dict[str, Any] = {}
            while time.monotonic() < deadline:
                status = client.get(f"/api/lessons/{lesson_id}/status")
                status_payload = status.json()
                if status_payload.get("status") in {"ready", "failed"}:
                    break
                time.sleep(0.01)
            if status_payload.get("status") != "ready":
                raise AssertionError("Qualification durable job did not become ready.")
            resource = client.get(f"/api/lessons/{lesson_id}")
            if resource.status_code != 200:
                raise AssertionError("Qualification lesson resource was unavailable.")
            durable_job = app.state.store.get(teacher.id, lesson_id)
            if durable_job is None or durable_job.status != "ready":
                raise AssertionError("Qualification did not persist a durable ready job.")
        durable_trace = self._durable_route_trace(durable_job)
        delivery = self._delivery_summary(
            resource.json(), logical_model_id, route, durable_job, durable_trace
        )
        density = DensitySummary(
            delivered_blocks=delivery.block_count,
            phase_counts=delivery.phase_counts,
            response_units=delivery.response_units,
            disposition="teacher_ready" if self._delivery_ready(delivery) else "recoverable_draft",
        )
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
            outcome="passed" if density.disposition == "teacher_ready" else "failed",
        )
        return QualificationCellResult(receipt=receipt, delivery=delivery)

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
    def _delivery_ready(delivery: DeliverySummary) -> bool:
        return (
            delivery.durable_job
            and delivery.block_count == 8
            and delivery.phase_counts == {"1": 3, "2": 4, "3": 1}
            and delivery.response_units >= 28
            and len(delivery.activity_types) >= 4
            and delivery.phase_three_transfer
            and delivery.provenance_continuous
        )
