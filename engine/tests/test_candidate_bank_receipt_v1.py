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


def _qualified_source() -> tuple[str, str]:
    anchors = deterministic_runtime_anchors()
    return (
        "\n\n".join((anchors["b1-narrative"].text, anchors["b1-informational"].text)),
        "+".join(
            (
                anchors["b1-narrative"].source_identity,
                anchors["b1-informational"].source_identity,
            )
        ),
    )


def test_complete_creation_time_banks_are_receipted_once_for_every_profile_slot(tmp_path) -> None:
    bundle = _qualification_fixture_bundle(tmp_path / "bundle")
    source, source_identity = _qualified_source()
    manifest = load_manifest()
    slots = _lesson_slots(45)
    captured = []

    with data.use_bundle(bundle):
        inventory = inventory_from_anchor(
            source,
            scheduled_types=_inventory_candidate_types(slots),
            replacement_types=_inventory_replacement_types(slots),
            receipt_context=BankReceiptContext(manifest.sha256, source_identity),
            receipt_sink=captured.append,
        )

    assert inventory.candidates
    assert len(captured) == 1
    receipts = captured[0]
    assert {receipt.bank_id for receipt in receipts} == {
        "match-up:1",
        "quiz:1",
        "fill-in:1",
        "error-correction:1",
        "mark-the-words:1",
        "cloze:1",
    }
    assert all(len(receipt.eligible_placements) == 1 for receipt in receipts)
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


def test_partial_inventory_cannot_be_sealed_or_replayed(tmp_path) -> None:
    bundle = _qualification_fixture_bundle(tmp_path / "bundle")
    source, source_identity = _qualified_source()
    with data.use_bundle(bundle):
        complete = inventory_from_anchor(
            source,
            scheduled_types=_inventory_candidate_types(_lesson_slots(45)),
            replacement_types=_inventory_replacement_types(_lesson_slots(45)),
            duration_minutes=45,
        )
    partial = replace(complete, candidates=(), atlas_pairs=(), mark_requests=())
    context = BankReceiptContext("0" * 64, source_identity)
    with pytest.raises(ValueError, match="complete authorized bank set"):
        seal_complete_inventory(partial, context=context)
    with pytest.raises(ProofReplayError, match="complete authorized bank set"):
        _expected_bank_rows(
            partial,
            manifest_digest="0" * 64,
            source_identity=source_identity,
        )
