"""Source-blind no-cost proof of the ordinary production qualification path."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from hramatka.api.qualified_models import PROMPT_SHA256
from hramatka.engine import fixtures
from hramatka.qualification import (
    CellReceipt,
    ProductionQualificationHarness,
    QualificationError,
    deterministic_runtime_anchors,
    load_manifest,
)
from hramatka.qualification.harness import _DeterministicRouteProvider
from hramatka.qualification.receipts import SlotTelemetry
from hramatka.qualification.receipts import main as receipt_main


def test_b1_qualification_harness_drives_all_cells_through_http_and_durable_jobs(
    tmp_path, monkeypatch
) -> None:
    """Every cell retains v3 slot receipts; aggregates remain secondary evidence."""
    # These control the unchanged v2 HTTP delivery diagnostic only.  The v3
    # probe below has no environment switch and independently decides receipt
    # eligibility.
    monkeypatch.setenv("HRAMATKA_SLOT_REPAIR", "1")
    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")
    harness = ProductionQualificationHarness(tmp_path / "qualification")
    anchors = deterministic_runtime_anchors()
    run = harness.run(anchors)

    assert len(run.cells) == 12
    assert {cell.receipt.anchor_id for cell in run.cells} == {
        "b1-narrative",
        "b1-dialogue",
        "b1-morphology",
    }
    assert {cell.receipt.expected_route.route_id for cell in run.cells} == {
        "gemini-flash-ais",
        "gemini-pro-ais",
        "gemma-ais",
        "gemma-openrouter",
    }
    forced_cell = next(
        cell
        for cell in run.cells
        if cell.receipt.anchor_id == "b1-morphology"
        and cell.receipt.expected_route.route_id == "gemma-openrouter"
    )
    assert any(trace.mode == "repair" for trace in forced_cell.receipt.repair_trace)
    assert len(run.receipt_paths) == 12
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
    assert len(aggregates) == 4
    assert all(aggregate.passed_anchors == frozenset({
        "b1-narrative", "b1-dialogue", "b1-morphology"
    }) for aggregate in aggregates)
    assert all(aggregate.as_model_receipt().passed for aggregate in aggregates)
    assert {aggregate.as_model_receipt().prompt_sha256 for aggregate in aggregates} == {
        PROMPT_SHA256
    }
    assert [entry.disposition for entry in forced_cell.receipt.slot_telemetry].count(
        "density_shortfall"
    ) == 3
    assert any(entry.replacement_used for entry in forced_cell.receipt.slot_telemetry)
    tray_cell = next(
        cell
        for cell in run.cells
        if cell.receipt.anchor_id == "b1-dialogue"
        and cell.receipt.expected_route.route_id == "gemini-pro-ais"
    )
    assert any(entry.disposition == "tray" for entry in tray_cell.receipt.slot_telemetry)

    duplicate = (*run.receipts, run.receipts[0])
    with pytest.raises(QualificationError, match="Duplicate"):
        harness.aggregate_cells(duplicate)

    with pytest.raises(QualificationError, match="requires every"):
        harness.aggregate_cells(run.receipts[:-1])

    route_mismatch = (*run.receipts[:-1], replace(
        run.receipts[-1],
        observed_route=replace(run.receipts[-1].observed_route, host="wrong.example.test"),
    ))
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

    sparse_density = (*run.receipts[:-1], replace(
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
    ))
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
        index
        for index, entry in enumerate(sparse_slots)
        if entry.disposition in {"ready", "tray"}
    )
    sparse_slots[first_accepted] = replace(
        sparse_slots[first_accepted], units=7, floor_met=True
    )
    forged_floor = (*run.receipts[:-1], replace(
        run.receipts[-1], slot_telemetry=tuple(sparse_slots)
    ))
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


def test_provider_must_pass_its_actual_v3_serialization_to_qualify(tmp_path, monkeypatch) -> None:
    """A legacy HTTP-ready job cannot be promoted over a failed v3 probe."""
    # The legacy v2 diagnostic succeeds with both historic flags set; failure
    # below can therefore only come from the always-on v3 receipt path.
    monkeypatch.setenv("HRAMATKA_SLOT_REPAIR", "1")
    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")

    class UnderfloorV3Provider:
        def __init__(self, route) -> None:
            self._v2 = _DeterministicRouteProvider(route, force_initial_shortfall=False)

        def for_bake(self):
            return self

        def __call__(self, prompt: str) -> str:
            if "QUALIFICATION V3 PROBE" in prompt:
                return '{"slots":[]}'
            return self._v2(prompt)

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

    assert all(cell.delivery.durable_job for cell in run.cells)
    assert all(cell.receipt.outcome == "failed" for cell in run.cells)
    assert all(
        {entry.disposition for entry in cell.receipt.slot_telemetry} == {"dropped"}
        for cell in run.cells
    )
    with pytest.raises(QualificationError, match="failed"):
        harness.aggregates(run)


def test_receipt_aggregation_cli_validates_persisted_matrix(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HRAMATKA_SLOT_REPAIR", "1")
    monkeypatch.setenv("HRAMATKA_PROMPT_PACK", "1")
    runtime_root = tmp_path / "qualification"
    harness = ProductionQualificationHarness(runtime_root)
    harness.run(deterministic_runtime_anchors())

    assert receipt_main(["aggregate", "--receipt-dir", str(runtime_root / "receipts")]) == 0
    output = capsys.readouterr().out
    assert "Qualification receipts aggregated: 4 routes" in output
    assert "gemini-3.5-flash gemini-flash-ais anchors=3/3" in output

    prompt_hashes_path = runtime_root / "aggregation-prompt-hashes.json"
    prompt_hashes = json.loads(prompt_hashes_path.read_text(encoding="utf-8"))
    prompt_hashes["prompt_hashes"][0]["sha256"] = "0" * 64
    prompt_hashes_path.write_text(json.dumps(prompt_hashes), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        receipt_main(["aggregate", "--receipt-dir", str(runtime_root / "receipts")])
    assert exit_info.value.code == 1
    assert "prompt hash is stale" in capsys.readouterr().err
