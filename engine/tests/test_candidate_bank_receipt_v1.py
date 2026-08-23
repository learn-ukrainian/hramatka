from __future__ import annotations

from dataclasses import replace

import pytest

from hramatka.api.baking.engine_adapter_v3 import (
    _inventory_candidate_types,
    _inventory_replacement_types,
    _lesson_slots,
)
from hramatka.engine import data
from hramatka.engine.anchor_inventory_v3 import inventory_from_anchor
from hramatka.engine.candidate_bank_receipt_v1 import BankReceiptContext, seal_complete_inventory
from hramatka.qualification.harness import (
    _qualification_fixture_bundle,
    deterministic_runtime_anchors,
)
from hramatka.qualification.manifest import load_manifest
from hramatka.qualification.proof_replay_v1 import ProofReplayError, _expected_bank_rows


def test_complete_creation_time_banks_are_receipted_once_with_shared_fallback(tmp_path) -> None:
    bundle = _qualification_fixture_bundle(tmp_path / "bundle")
    runtime = deterministic_runtime_anchors()["b1-narrative"]
    manifest = load_manifest()
    slots = _lesson_slots(45)
    captured = []

    with data.use_bundle(bundle):
        inventory = inventory_from_anchor(
            runtime.text,
            scheduled_types=_inventory_candidate_types(slots),
            replacement_types=_inventory_replacement_types(slots),
            receipt_context=BankReceiptContext(manifest.sha256, runtime.source_identity),
            receipt_sink=captured.append,
        )

    assert inventory.candidates
    assert len(captured) == 1
    receipts = captured[0]
    fallback = next(receipt for receipt in receipts if receipt.bank_id == "fill-in:1")
    assert fallback.eligible_placements == (("P2-A1", True), ("P2-A2", True))
    assert all("Теплим" not in receipt.digest for receipt in receipts)


def test_receipt_hook_rejects_partial_caller_capture() -> None:
    slots = _lesson_slots(45)
    try:
        inventory_from_anchor(
            "Короткий текст для перевірки.",
            scheduled_types=_inventory_candidate_types(slots),
            receipt_context=BankReceiptContext("0" * 64, "opaque-source"),
        )
    except ValueError as error:
        assert "context and sink" in str(error)
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("partial receipt capture unexpectedly succeeded")


def test_short_writing_only_inventory_cannot_be_sealed_or_replayed(tmp_path) -> None:
    bundle = _qualification_fixture_bundle(tmp_path / "bundle")
    runtime = deterministic_runtime_anchors()["b1-narrative"]
    with data.use_bundle(bundle):
        complete = inventory_from_anchor(
            runtime.text,
            scheduled_types=_inventory_candidate_types(_lesson_slots(45)),
            replacement_types=_inventory_replacement_types(_lesson_slots(45)),
            duration_minutes=45,
        )
    short_writing_only = replace(complete, candidates=(), atlas_pairs=())
    context = BankReceiptContext("0" * 64, runtime.source_identity)
    with pytest.raises(ValueError, match="shared fallback bank"):
        seal_complete_inventory(short_writing_only, context=context)
    with pytest.raises(ProofReplayError, match="shared fallback bank"):
        _expected_bank_rows(
            short_writing_only,
            manifest_digest="0" * 64,
            source_identity=runtime.source_identity,
        )
