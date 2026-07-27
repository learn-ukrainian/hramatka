"""Source-blind no-cost proof of the ordinary production qualification path."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from hramatka.qualification import (
    CellReceipt,
    ProductionQualificationHarness,
    QualificationError,
    deterministic_runtime_anchors,
    load_manifest,
)
from hramatka.qualification.receipts import main as receipt_main


def test_b1_qualification_harness_drives_all_cells_through_http_and_durable_jobs(
    tmp_path, monkeypatch
) -> None:
    """Path proof only: semantic/key adjudication is deliberately not claimed here."""
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
    for aggregate in aggregates:
        with pytest.raises(QualificationError, match="Path proof alone"):
            aggregate.as_model_receipt()

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

    failed_cell = (*run.receipts[:-1], replace(run.receipts[-1], outcome="failed"))
    with pytest.raises(QualificationError, match="failed"):
        harness.aggregate_cells(failed_cell)

    sparse_density = (*run.receipts[:-1], replace(
        run.receipts[-1],
        density=replace(
            run.receipts[-1].density,
            delivered_blocks=0,
            ready_blocks=0,
            tray_blocks=0,
            floor_blocks=0,
            phase_counts={"1": 0, "2": 0, "3": 0},
            ready_phase_counts={"1": 0, "2": 0, "3": 0},
            tray_phase_counts={"1": 0, "2": 0, "3": 0},
            response_units=0,
            ready_response_units=0,
            tray_response_units=0,
        ),
    ))
    with pytest.raises(QualificationError, match="density counts"):
        harness.aggregate_cells(sparse_density)


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
