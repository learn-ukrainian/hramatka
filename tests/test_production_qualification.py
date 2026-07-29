"""Source-blind no-cost proof of the ordinary production qualification path."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from hramatka.api import qualified_models
from hramatka.api.qualified_models import PROMPT_SHA256, QualificationReceipt
from hramatka.engine import fixtures
from hramatka.qualification import (
    CellReceipt,
    ProductionQualificationHarness,
    QualificationError,
    deterministic_runtime_anchors,
    load_manifest,
)
from hramatka.qualification.harness import _DeterministicRouteProvider
from hramatka.qualification.receipts import (
    SlotTelemetry,
)
from hramatka.qualification.receipts import (
    main as receipt_main,
)
from hramatka.qualification.transcribe import transcribe


def test_b1_qualification_harness_drives_all_cells_through_http_and_durable_jobs(tmp_path) -> None:
    """Every cell retains v3 slot receipts; aggregates remain secondary evidence."""
    harness = ProductionQualificationHarness(tmp_path / "qualification")
    anchors = deterministic_runtime_anchors()
    run = harness.run(anchors)

    assert len(run.cells) == 6
    assert {cell.receipt.anchor_id for cell in run.cells} == {
        "b1-narrative",
        "b1-dialogue",
        "b1-morphology",
    }
    assert {cell.receipt.expected_route.route_id for cell in run.cells} == {
        "gemini-flash-ais",
        "gemini-flash-vertex",
    }
    forced_cell = next(
        cell
        for cell in run.cells
        if cell.receipt.anchor_id == "b1-morphology"
        and cell.receipt.expected_route.route_id == "gemini-flash-vertex"
    )
    assert any(trace.mode == "repair" for trace in forced_cell.receipt.repair_trace)
    assert len(run.receipt_paths) == 6
    assert all(path.is_file() for path in run.receipt_paths)
    assert all(
        all(anchor.text not in path.read_text(encoding="utf-8") for anchor in anchors.values())
        for path in run.receipt_paths
    )
    for cell in run.cells:
        assert cell.delivery.durable_job
        assert cell.delivery.block_count == 8
        assert cell.delivery.phase_counts == {"1": 3, "2": 4, "3": 1}
        assert cell.delivery.response_units >= 28
        assert len(cell.delivery.activity_types) >= 4
        assert cell.delivery.phase_three_transfer
        assert cell.delivery.provenance_continuous
        assert cell.receipt.outcome == "passed"
        assert cell.receipt.semantic_gate == "not_run"
        assert cell.receipt.prompt_pack_version == "PromptPackInput.v3"
        assert cell.receipt.template_version == "gemma-phase-pack.v3.2"
        assert cell.receipt.density_contract_version == "TeacherReadyDensity.v3"
        assert cell.receipt.density.lesson_units == 57
        assert cell.receipt.density.slot_count == 8
        assert all(
            set(entry.as_dict())
            == {
                "slot_id",
                "phase",
                "type",
                "disposition",
                "units",
                "floor_met",
                "repair_rounds",
                "replacement_used",
                "unassigned_errors_count",
            }
            for entry in cell.receipt.slot_telemetry
        )
        assert any(trace.mode == "initial" for trace in cell.receipt.repair_trace)
        assert all(
            trace.expected_route == cell.receipt.expected_route
            and trace.observed_route == cell.receipt.observed_route
            for trace in cell.receipt.repair_trace
        )

    aggregates = harness.aggregates(run)
    assert len(aggregates) == 2
    assert all(
        aggregate.passed_anchors == frozenset({"b1-narrative", "b1-dialogue", "b1-morphology"})
        for aggregate in aggregates
    )
    assert all(aggregate.as_model_receipt().passed for aggregate in aggregates)
    assert {aggregate.as_model_receipt().prompt_sha256 for aggregate in aggregates} == {
        PROMPT_SHA256
    }
    assert any(
        entry.disposition == "density_shortfall" for entry in forced_cell.receipt.slot_telemetry
    )
    assert any(
        entry.disposition == "ready" and entry.repair_rounds == 1
        for entry in forced_cell.receipt.slot_telemetry
    )

    duplicate = (*run.receipts, run.receipts[0])
    with pytest.raises(QualificationError, match="Duplicate"):
        harness.aggregate_cells(duplicate)

    with pytest.raises(QualificationError, match="requires every"):
        harness.aggregate_cells(run.receipts[:-1])

    missing_vertex = tuple(
        receipt
        for receipt in run.receipts
        if receipt.expected_route.route_id != "gemini-flash-vertex"
    )
    with pytest.raises(QualificationError, match="requires every"):
        harness.aggregate_cells(missing_vertex)

    route_mismatch = (
        *run.receipts[:-1],
        replace(
            run.receipts[-1],
            observed_route=replace(run.receipts[-1].observed_route, host="wrong.example.test"),
        ),
    )
    with pytest.raises(QualificationError, match="observed route"):
        harness.aggregate_cells(route_mismatch)

    stale_prompt = (*run.receipts[:-1], replace(run.receipts[-1], prompt_sha256="0" * 64))
    with pytest.raises(QualificationError, match="prompt hash is stale"):
        harness.aggregate_cells(stale_prompt)

    stale_template = (*run.receipts[:-1], replace(run.receipts[-1], template_sha256="0" * 64))
    with pytest.raises(QualificationError, match="stale"):
        harness.aggregate_cells(stale_template)

    stale_kit = (*run.receipts[:-1], replace(run.receipts[-1], type_kit_identity="old-kit"))
    with pytest.raises(QualificationError, match="stale"):
        harness.aggregate_cells(stale_kit)

    failed_cell = (*run.receipts[:-1], replace(run.receipts[-1], outcome="failed"))
    with pytest.raises(QualificationError, match="failed"):
        harness.aggregate_cells(failed_cell)

    sparse_density = (
        *run.receipts[:-1],
        replace(
            run.receipts[-1],
            density=replace(
                run.receipts[-1].density,
                lesson_units=0,
                ready_units=0,
                tray_units=0,
                floor_units=0,
                phase_units={"1": 0, "2": 0, "3": 0},
                ready_phase_units={"1": 0, "2": 0, "3": 0},
                tray_phase_units={"1": 0, "2": 0, "3": 0},
                slot_count=0,
                ready_slots=0,
                tray_slots=0,
            ),
        ),
    )
    with pytest.raises(QualificationError, match="totals"):
        harness.aggregate_cells(sparse_density)

    # A model cannot borrow a dense slot to conceal a missing scheduled slot.
    # Re-parse in ``aggregate_cells`` is intentional: dataclasses.replace() is
    # an in-memory analogue of a tampered persisted receipt.
    dense_slots = list(run.receipts[-1].slot_telemetry)
    p1_a2 = next(index for index, entry in enumerate(dense_slots) if entry.slot_id == "P1-A2")
    dense_slots[p1_a2] = replace(dense_slots[p1_a2], slot_id="P1-A1")
    repeated_slot = (
        *run.receipts[:-1],
        replace(run.receipts[-1], slot_telemetry=tuple(dense_slots)),
    )
    with pytest.raises(QualificationError, match="repeat a scheduled slot"):
        harness.aggregate_cells(repeated_slot)

    sparse_slots = list(run.receipts[-1].slot_telemetry)
    first_accepted = next(
        index for index, entry in enumerate(sparse_slots) if entry.disposition in {"ready", "tray"}
    )
    sparse_slots[first_accepted] = replace(sparse_slots[first_accepted], units=7, floor_met=True)
    forged_floor = (
        *run.receipts[:-1],
        replace(run.receipts[-1], slot_telemetry=tuple(sparse_slots)),
    )
    with pytest.raises(QualificationError, match="floor status"):
        harness.aggregate_cells(forged_floor)


def test_manifest_and_receipt_schema_are_content_free_and_fail_closed() -> None:
    manifest = load_manifest()
    assert manifest.level == "B1"
    assert manifest.duration_minutes == 45
    assert [anchor.id for anchor in manifest.anchors] == [
        "b1-narrative",
        "b1-dialogue",
        "b1-morphology",
    ]
    assert all(anchor.source_identity and len(anchor.sha256) == 64 for anchor in manifest.anchors)

    with pytest.raises(QualificationError, match="invalid schema"):
        CellReceipt.from_dict({"outcome": "passed"})

    telemetry = {
        "slot_id": "anchor-text",
        "phase": 1,
        "type": "quiz",
        "disposition": "ready",
        "units": 8,
        "floor_met": True,
        "repair_rounds": 0,
        "replacement_used": False,
        "unassigned_errors_count": 0,
    }
    with pytest.raises(QualificationError, match="telemetry"):
        SlotTelemetry.from_dict(telemetry)


def test_provider_must_pass_its_actual_v3_serialization_to_qualify(tmp_path) -> None:
    """A failed live V3 serialization cannot produce a qualified receipt."""

    class UnderfloorV3Provider:
        def __init__(self, route) -> None:
            self._inner = _DeterministicRouteProvider(route, force_initial_shortfall=False)

        @property
        def initial_prompt_digests(self) -> list[str]:
            return self._inner.initial_prompt_digests

        def for_bake(self):
            return self

        def __call__(self, prompt: str) -> str:
            self._inner(prompt)
            if "IMMUTABLE TYPE-KITS" in prompt:
                return '{"slots":[]}'
            raise AssertionError("Expected a v3 serializer prompt.")

    harness = ProductionQualificationHarness(tmp_path / "qualification")
    run = harness.run_with_provider_factory(
        deterministic_runtime_anchors(),
        bundle=fixtures._bundle_with_matchup_vocabulary(tmp_path / "fixture-data"),
        provider_factory=lambda _anchor, _model, route: UnderfloorV3Provider(route),
        scratch_root=tmp_path / "scratch",
        bake_hard_timeout_seconds=30,
        readiness_timeout_seconds=20,
        runner_stop_timeout_seconds=1,
    )

    assert all(not cell.delivery.durable_job for cell in run.cells)
    assert all(cell.receipt.outcome == "failed" for cell in run.cells)
    assert all(
        {entry.disposition for entry in cell.receipt.slot_telemetry} == {"dropped"}
        for cell in run.cells
    )
    with pytest.raises(QualificationError, match="failed"):
        harness.aggregates(run)


def test_receipt_aggregation_cli_validates_persisted_matrix(tmp_path, capsys) -> None:
    runtime_root = tmp_path / "qualification"
    harness = ProductionQualificationHarness(runtime_root)
    harness.run(deterministic_runtime_anchors())

    assert receipt_main(["aggregate", "--receipt-dir", str(runtime_root / "receipts")]) == 0
    output = capsys.readouterr().out
    assert "Qualification receipts aggregated: 2 routes" in output
    assert "gemini-3.6-flash gemini-flash-ais anchors=3/3" in output

    prompt_hashes_path = runtime_root / "aggregation-prompt-hashes.json"
    prompt_hashes = json.loads(prompt_hashes_path.read_text(encoding="utf-8"))
    prompt_hashes["prompt_hashes"][0]["sha256"] = "0" * 64
    prompt_hashes_path.write_text(json.dumps(prompt_hashes), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        receipt_main(["aggregate", "--receipt-dir", str(runtime_root / "receipts")])
    assert exit_info.value.code == 1
    assert "prompt hash is stale" in capsys.readouterr().err


def test_transcription_prints_the_exact_registry_block_without_editing_it(
    tmp_path,
) -> None:
    runtime_root = tmp_path / "qualification"
    harness = ProductionQualificationHarness(runtime_root)
    run = harness.run(deterministic_runtime_anchors())

    registry = Path("hramatka/api/qualified_models.py")
    before = registry.read_bytes()
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    block = transcribe(receipt_dir=runtime_root / "receipts", source_commit=source_commit)

    assert registry.read_bytes() == before
    assert block.startswith(
        "PRODUCTION_QUALIFICATION_RECEIPTS: Final[tuple[QualificationReceipt, ...]] = (\n"
    )
    assert block.count("    QualificationReceipt(\n") == 2
    assert block.count("passed_anchors=frozenset({") == 2
    assert 'prompt_pack_version="PromptPackInput.v3"' in block
    assert 'template_version="gemma-phase-pack.v3.2"' in block
    assert 'density_contract_version="TeacherReadyDensity.v3"' in block
    assert "passed=True," in block
    emitted: dict[str, object] = {"Final": Final, "QualificationReceipt": QualificationReceipt}
    exec(block, emitted)  # noqa: S102 - verifies the review block is a paste-ready declaration.
    expected_receipts = tuple(
        aggregate.as_model_receipt()
        for aggregate in sorted(
            harness.aggregates(run),
            key=lambda aggregate: (aggregate.logical_model_id, aggregate.route.route_id),
        )
    )
    assert emitted["PRODUCTION_QUALIFICATION_RECEIPTS"] == expected_receipts
    field_order = (
        "logical_model_id",
        "provider_route",
        "provider_host",
        "provider_model_id",
        "registry_version",
        "prompt_pack_version",
        "prompt_sha256",
        "template_version",
        "template_sha256",
        "density_contract_version",
        "density_contract_digest",
        "type_kit_identity",
        "serializer_temperature",
        "passed_anchors",
        "passed",
    )
    for receipt_block in block.split("    QualificationReceipt(\n")[1:]:
        positions = [receipt_block.index(f"        {field}=") for field in field_order]
        assert positions == sorted(positions)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "hramatka.qualification.transcribe",
            "--receipt-dir",
            str(runtime_root / "receipts"),
            "--source-commit",
            source_commit,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == f"{block}\n"


def test_transcription_refuses_missing_failed_or_stale_aggregate_input(tmp_path) -> None:
    runtime_root = tmp_path / "qualification"
    harness = ProductionQualificationHarness(runtime_root)
    harness.run(deterministic_runtime_anchors())

    with pytest.raises(QualificationError, match="candidate pin"):
        transcribe(receipt_dir=runtime_root / "receipts", source_commit="0" * 40)

    receipt_path = next((runtime_root / "receipts").glob("*.json"))
    original_receipt = receipt_path.read_bytes()
    changed_receipt = json.loads(original_receipt)
    changed_receipt["source_commit"] = "0" * 40
    receipt_path.write_text(json.dumps(changed_receipt), encoding="utf-8")
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    with pytest.raises(QualificationError, match="stale"):
        transcribe(receipt_dir=runtime_root / "receipts", source_commit=source_commit)
    receipt_path.write_bytes(original_receipt)

    changed_receipt = json.loads(original_receipt)
    changed_receipt["outcome"] = "failed"
    receipt_path.write_text(json.dumps(changed_receipt), encoding="utf-8")
    with pytest.raises(QualificationError, match="failed"):
        transcribe(receipt_dir=runtime_root / "receipts", source_commit=source_commit)
    receipt_path.write_bytes(original_receipt)

    receipt_path.unlink()
    with pytest.raises(QualificationError, match="requires every"):
        transcribe(receipt_dir=runtime_root / "receipts", source_commit=source_commit)
    receipt_path.write_bytes(original_receipt)

    hashes_path = runtime_root / "aggregation-prompt-hashes.json"
    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    hashes["prompt_hashes"][0]["sha256"] = "0" * 64
    hashes_path.write_text(json.dumps(hashes), encoding="utf-8")
    with pytest.raises(QualificationError, match="prompt hash is stale"):
        transcribe(receipt_dir=runtime_root / "receipts", source_commit=source_commit)


def test_transcription_authority_guard_refuses_live_selector_literal_drift(
    tmp_path, monkeypatch
) -> None:
    runtime_root = tmp_path / "qualification"
    harness = ProductionQualificationHarness(runtime_root)
    harness.run(deterministic_runtime_anchors())
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setattr(qualified_models, "TYPE_KIT_IDENTITY", "retired-kit")

    with pytest.raises(QualificationError, match="literals do not match"):
        transcribe(receipt_dir=runtime_root / "receipts", source_commit=source_commit)


def test_transcription_refuses_aggregate_selector_literal_drift(tmp_path, monkeypatch) -> None:
    runtime_root = tmp_path / "qualification"
    harness = ProductionQualificationHarness(runtime_root)
    harness.run(deterministic_runtime_anchors())
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setattr(qualified_models, "PROMPT_SHA256", "0" * 64)

    with pytest.raises(QualificationError, match="Aggregate receipt literals"):
        transcribe(receipt_dir=runtime_root / "receipts", source_commit=source_commit)
