"""No-network structural tests for the isolated #552 prospective proof path."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import hramatka.qualification.prospective_capture_v1 as prospective_capture
import hramatka.qualification.prospective_cli_v1 as prospective_cli
import hramatka.qualification.prospective_replay_v1 as prospective_replay
from hramatka.engine import data
from hramatka.engine.lesson_profile_45_v1 import lesson_profile_45
from hramatka.engine.teacher_ready_density_v3 import density_floor_fingerprint
from hramatka.qualification.proof_schema_v1 import ProofCertificate, ProofSchemaError
from hramatka.qualification.prospective_evaluate_v1 import (
    CLAIM_CEILING,
    ProspectiveAggregate,
    ProspectiveEvaluationError,
    evaluate_population,
)
from hramatka.qualification.prospective_manifest_v1 import (
    DATABASE_DIGEST,
    EXPECTED_SELECTED_IDS,
    POPULATION_PACKET_DIGEST,
    RIGHTS_RECEIPT_DIGEST,
    SELECTED_ID_DIGEST,
    ProspectiveManifest,
    ProspectiveManifestError,
    ProspectiveSource,
    build_manifest,
)
from hramatka.qualification.prospective_proof_schema_v1 import (
    COMPOSITE_RULE,
    COMPOSITE_RULE_DIGEST,
    DENSITY_FLOOR_DIGEST,
    PROFILE_DIGEST,
    ProspectiveCertificate,
    ProspectiveProofSchemaError,
    sha256,
)


def _source(rank: int = 1) -> dict[str, object]:
    sqlite_id = EXPECTED_SELECTED_IDS[rank - 1]
    return {
        "sqlite_id": sqlite_id,
        "title": f"Article {rank}",
        "url": f"https://uk.wikipedia.org/wiki/{sqlite_id}",
        "fetched_at": "2026-01-01T00:00:00Z",
        "database_text_sha256": "a" * 64,
        "extracted_text_sha256": "b" * 64,
        "extracted_token_count": 370 if rank == 1 else (500 if rank <= 8 else 600),
        "selection_rank": rank,
    }


def _payload(
    *, manifest_digest: str = "1" * 64, source_id: int = EXPECTED_SELECTED_IDS[0]
) -> dict[str, object]:
    placements = {
        "quiz:1": (("P1-A1", False),),
        "cloze:1": (("P1-A2", False),),
        "match-up:1": (("P2-A1", False),),
        "error-correction:1": (("P2-A2", False),),
        "text-questions:1": (("P2-A3", False),),
        "short-writing:1": (("P3-A1", False),),
        "fill-in:1": (("P2-A1", True), ("P2-A2", True)),
        "fill-in:2": (("P2-A2", False),),
    }
    tiers = {
        "quiz": "selected-response",
        "cloze": "bounded-production",
        "match-up": "selected-response",
        "error-correction": "bounded-production",
        "text-questions": "source-grounded-open-response",
        "short-writing": "extended-writing",
        "fill-in": "bounded-production",
    }
    full_receipts = []
    for bank in sorted(
        placements,
        key=lambda bank_id: (bank_id.rsplit(":", 1)[0], int(bank_id.rsplit(":", 1)[1])),
    ):
        activity = bank.rsplit(":", 1)[0]
        ids = [f"{bank}:{number}" for number in range(1, 9)]
        full_receipts.append(
            {
                "version": "HramatkaCandidateBankReceipt.v1",
                "bank_id": bank,
                "activity_type": activity,
                "response_demand_tier": tiers[activity],
                "candidate_ids": ids,
                "count": 8,
                "candidate_offsets": [
                    {"candidate_id": item, "unit_offset": offset}
                    for offset, item in enumerate(ids)
                ],
                "candidate_provenance": [
                    {
                        "candidate_id": item,
                        "commitment": sha256(
                            {
                                "bank_id": bank,
                                "candidate_id": item,
                                "unit_offset": offset,
                                "source_identity": str(source_id),
                            }
                        ),
                    }
                    for offset, item in enumerate(ids)
                ],
                "eligible_placements": [
                    {"slot_id": slot_id, "mutually_exclusive": exclusive}
                    for slot_id, exclusive in placements[bank]
                ],
                "origin_kind": "deterministic-local-inventory",
                "origin_commitment": sha256(
                    {
                        "origin_kind": "deterministic-local-inventory",
                        "source_identity": str(source_id),
                        "bank_id": bank,
                        "candidate_ids": ids,
                        "placements": [list(item) for item in placements[bank]],
                        "authority_digest": PROFILE_DIGEST,
                    }
                ),
                "authority_digest": PROFILE_DIGEST,
                "manifest_digest": manifest_digest,
                "source_identity": str(source_id),
                "inventory_seal": "pending",
            }
        )
    seal = sha256(
        {
            "manifest_digest": manifest_digest,
            "source_identity": str(source_id),
            "profile_digest": PROFILE_DIGEST,
            "banks": [
                {"bank_id": row["bank_id"], "candidate_ids": row["candidate_ids"]}
                for row in full_receipts
            ],
        }
    )
    for receipt in full_receipts:
        receipt["inventory_seal"] = seal
    by_id = {receipt["bank_id"]: receipt for receipt in full_receipts}
    banks = [
        {"bank_id": bank_id, "receipt": by_id[bank_id]}
        for bank_id in ("quiz:1", "cloze:1")
    ]

    slot_specs = (
        ("P1-A1", 1, "quiz", 8),
        ("P1-A2", 1, "cloze", 8),
        ("P2-A1", 2, "match-up", 3),
        ("P2-A2", 2, "error-correction", 3),
        ("P2-A3", 2, "text-questions", 3),
        ("P3-A1", 3, "short-writing", 2),
    )
    slots = []
    for slot_id, phase, activity, count in slot_specs:
        units = []
        for number in range(count):
            unit_id = f"{activity}:unit:{number}"
            claims = [{"kind": "source", "resource_id": f"resource:{slot_id}:{number}"}]
            locators = [f"locator:{slot_id}:{number}"]
            units.append(
                {
                    "unit_id": unit_id,
                    "claims": claims,
                    "reservations": list(claims),
                    "locators": locators,
                    "plan_digest": sha256(
                        {"unit_id": unit_id, "claims": claims, "locators": locators}
                    ),
                }
            )
        slots.append(
            {
                "slot_id": slot_id,
                "phase": phase,
                "requested_type": activity,
                "scheduled_type": activity,
                "substitution_reason": None,
                "units": units,
                "conditional_replacements": [],
            }
        )
    allocation = {
        "paragraph_ids": [str(source_id)],
        "slots": slots,
        "canonical_allocation_digest": "c" * 64,
    }
    witness = {
        "rows": [
            {
                "slot_id": slot["slot_id"],
                "phase": slot["phase"],
                "activity_type": slot["scheduled_type"],
                "units": slot["units"],
            }
            for slot in slots
        ],
        "eligible_replacement_edges": [
            {"slot_id": slot["slot_id"], "types": []} for slot in slots
        ],
        "canonical_allocation_digest": "c" * 64,
    }
    units = [unit for slot in slots for unit in slot["units"]]
    claims = [claim for unit in units for claim in unit["claims"]]
    locators = [locator for unit in units for locator in unit["locators"]]
    domains = {
        "candidate": [
            {"bank_id": receipt["bank_id"], "candidate_id": candidate, "offset": offset}
            for receipt in full_receipts
            for offset, candidate in enumerate(receipt["candidate_ids"])
        ],
        "provenance": [
            {
                "bank_id": receipt["bank_id"],
                "candidate_id": candidate,
                "commitment": receipt["candidate_provenance"][offset]["commitment"],
            }
            for receipt in full_receipts
            for offset, candidate in enumerate(receipt["candidate_ids"])
        ],
        "bank": [receipt["bank_id"] for receipt in full_receipts],
        "placement": [
            {
                "bank_id": receipt["bank_id"],
                "slot_id": placement["slot_id"],
                "exclusive": placement["mutually_exclusive"],
            }
            for receipt in full_receipts
            for placement in receipt["eligible_placements"]
        ],
        "slot": [{"slot_id": slot["slot_id"], "phase": slot["phase"]} for slot in slots],
        "unit": [unit["unit_id"] for unit in units],
        "claim": claims,
        "locator": locators,
        "plan": [{"unit_id": unit["unit_id"], "digest": unit["plan_digest"]} for unit in units],
        "reservation": [item for unit in units for item in unit["reservations"]],
        "allocation": [
            {
                "slot_id": slot["slot_id"],
                "requested_type": slot["requested_type"],
                "scheduled_type": slot["scheduled_type"],
                "substitution_reason": slot["substitution_reason"],
            }
            for slot in slots
        ],
        "replacement": [],
        "witness": witness["rows"],
    }
    domain_commitments = {
        name: {"count": len(rows), "digest": sha256(rows)} for name, rows in domains.items()
    }
    cells = []
    for cloze in banks[1]["receipt"]["candidate_ids"]:
        for quiz in banks[0]["receipt"]["candidate_ids"]:
            cells.append(
                {
                    "identity": {"cloze_candidate_id": cloze, "quiz_candidate_id": quiz},
                    "outcome": "passed",
                    "allocation": copy.deepcopy(allocation),
                    "witness": copy.deepcopy(witness),
                    "domain_commitments": copy.deepcopy(domain_commitments),
                    "failure_reason": None,
                }
            )
    return {
        "version": "HramatkaProspectiveCertificate.v1",
        "manifest_digest": "1" * 64,
        "source": _source(),
        "composite_rule_digest": COMPOSITE_RULE_DIGEST,
        "profile_digest": PROFILE_DIGEST,
        "density_floor_digest": DENSITY_FLOOR_DIGEST,
        "status": "passed",
        "failure_reason": None,
        "bank_receipts": full_receipts,
        "banks": banks,
        "cells": cells,
    }


def _manifest(*, first_text: str | None = None) -> ProspectiveManifest:
    rows = []
    for rank, sqlite_id in enumerate(EXPECTED_SELECTED_IDS, 1):
        extracted_digest = (
            hashlib.sha256(first_text.encode()).hexdigest()
            if rank == 1 and first_text is not None
            else "b" * 64
        )
        rows.append(
            ProspectiveSource(
                sqlite_id,
                f"Article {rank}",
                f"https://uk.wikipedia.org/wiki/{sqlite_id}",
                "2026-01-01T00:00:00Z",
                "a" * 64,
                extracted_digest,
                370 if rank == 1 else (500 if rank <= 8 else 600),
                rank,
            )
        )
    return ProspectiveManifest(
        tuple(rows),
        DATABASE_DIGEST,
        RIGHTS_RECEIPT_DIGEST,
        SELECTED_ID_DIGEST,
        POPULATION_PACKET_DIGEST,
    )


def test_composite_rule_and_existing_profile_are_byte_pinned() -> None:
    assert sha256(COMPOSITE_RULE) == COMPOSITE_RULE_DIGEST
    assert lesson_profile_45().digest == PROFILE_DIGEST
    assert density_floor_fingerprint() == DENSITY_FLOOR_DIGEST
    assert len(lesson_profile_45().slots) == 6


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["cells"].append(copy.deepcopy(value["cells"][0])),
        lambda value: value["cells"].pop(),
        lambda value: value["cells"].__setitem__(
            0,
            {
                **value["cells"][0],
                "identity": {"cloze_candidate_id": "extra", "quiz_candidate_id": "quiz:1:1"},
            },
        ),
        lambda value: value["banks"][0]["receipt"]["candidate_ids"].__setitem__(1, "quiz:1:1"),
        lambda value: value["banks"][0]["receipt"]["candidate_provenance"].__setitem__(
            0, {"candidate_id": "other", "commitment": "a" * 64}
        ),
        lambda value: value.__setitem__("composite_rule_digest", "0" * 64),
    ],
)
def test_certificate_rejects_duplicate_omitted_extra_reused_and_provenance_cells(mutate) -> None:
    value = _payload()
    mutate(value)
    with pytest.raises(ProspectiveProofSchemaError):
        ProspectiveCertificate(value)


def test_production_and_prospective_parsers_reject_one_another() -> None:
    prospective = ProspectiveCertificate(_payload()).to_bytes()
    with pytest.raises(ProofSchemaError):
        ProofCertificate.from_bytes(prospective)
    production = Path("hramatka/qualification/assets/b1-45m.manifest.json").read_bytes()
    with pytest.raises(ProspectiveProofSchemaError):
        ProspectiveCertificate.from_bytes(production)


def test_failed_source_is_retained_as_an_explicit_no_witness_failure() -> None:
    value = _payload()
    value.update({"status": "failed", "failure_reason": "loss-grid:1-no-witness-cells"})
    value["cells"][0]["outcome"] = "failed"
    value["cells"][0]["failure_reason"] = "insufficient_anchor_capacity"
    failure_digest = sha256(
        {
            "identity": value["cells"][0]["identity"],
            "failure": value["cells"][0]["failure_reason"],
        }
    )
    for field in ("allocation", "witness", "domain_commitments"):
        value["cells"][0][field] = {"failure_digest": failure_digest}
    assert ProspectiveCertificate(value).payload["status"] == "failed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["source"].__setitem__("extracted_token_count", 9999),
        lambda value: value["source"].__setitem__("selection_rank", -5),
        lambda value: value["cells"][0].__setitem__("allocation", {"lol": "not-geometry"}),
        lambda value: value["cells"][0].__setitem__("witness", {"lol": "not-witness"}),
        lambda value: value["cells"][0].__setitem__(
            "domain_commitments", {"lol": "not-domains"}
        ),
        lambda value: value["bank_receipts"][1].__setitem__("inventory_seal", "0" * 64),
    ],
)
def test_parser_rejects_weak_geometry_source_and_receipt_seal_canaries(mutate) -> None:
    value = _payload()
    mutate(value)
    with pytest.raises(ProspectiveProofSchemaError):
        ProspectiveCertificate(value)


def test_detached_engine_bundle_is_reverified_with_all_inputs_required(
    monkeypatch, tmp_path
) -> None:
    policy = {
        name: {"path": name, "sha256": character * 64, "size": 1}
        for name, character in (
            ("atlas.db", "a"),
            ("sources.db", "b"),
            ("vesum.db", "c"),
        )
    }
    caller = data.DataBundle(
        root=tmp_path,
        manifest={
            "inputs": {
                name: {**row, "required": False} for name, row in policy.items()
            }
        },
    )
    monkeypatch.setattr(
        prospective_capture, "frozen_content_authority", lambda _repository: ("d" * 64, policy)
    )
    monkeypatch.setattr(
        prospective_replay, "frozen_content_authority", lambda _repository: ("d" * 64, policy)
    )
    with pytest.raises(prospective_capture.ProspectiveCaptureError, match="drifted"):
        prospective_capture._verified_engine_bundle(caller, Path.cwd())
    with pytest.raises(prospective_replay.ProspectiveReplayError, match="drifted"):
        prospective_replay._verified_engine_bundle(caller, Path.cwd())


@pytest.mark.parametrize(
    "field",
    [
        "candidate_provenance",
        "allocation",
        "witness",
        "domain_commitments",
    ],
)
def test_parser_and_independent_replay_reject_bank_and_geometry_corruption(
    monkeypatch, field
) -> None:
    """Replay comparison catches semantic digests that are syntactically valid."""
    text = "controlled source"
    manifest = _manifest(first_text=text)
    source = manifest.sources[0]
    payload = _payload(manifest_digest=manifest.digest, source_id=source.sqlite_id)
    payload["source"] = source.to_dict()
    payload["manifest_digest"] = manifest.digest

    class FakeReceipt:
        def __init__(self, value):
            self.value = value

        def to_dict(self):
            return self.value

    def fake_inventory(*_args, **kwargs):
        kwargs["receipt_sink"](tuple(FakeReceipt(item) for item in payload["bank_receipts"]))
        return object()

    monkeypatch.setattr(prospective_replay, "inventory_from_anchor", fake_inventory)
    monkeypatch.setattr(
        prospective_replay, "_banks", lambda *_args: copy.deepcopy(payload["banks"])
    )
    monkeypatch.setattr(prospective_replay, "_verified_engine_bundle", lambda *_args: object())
    expected_cells = copy.deepcopy(payload["cells"])
    monkeypatch.setattr(
        prospective_replay,
        "_replay_cell",
        lambda *_args: expected_cells.pop(0),
    )
    certificate = ProspectiveCertificate(payload).to_bytes()
    assert prospective_replay.replay_source(
        certificate, manifest, source, text, bundle=object(), repository=Path.cwd()
    ).digest

    corrupted = _payload(manifest_digest=manifest.digest, source_id=source.sqlite_id)
    corrupted["source"] = source.to_dict()
    corrupted["manifest_digest"] = manifest.digest
    if field == "candidate_provenance":
        corrupted["banks"][0]["receipt"][field][0]["commitment"] = "0" * 64
    elif field == "allocation":
        corrupted["cells"][0][field]["slots"][0]["units"][0]["plan_digest"] = "0" * 64
    elif field == "witness":
        corrupted["cells"][0][field]["canonical_allocation_digest"] = "0" * 64
    else:
        corrupted["cells"][0][field] = {"corrupt": "0" * 64}
    with pytest.raises(ProspectiveProofSchemaError):
        ProspectiveCertificate(corrupted)

    expected_cells = copy.deepcopy(payload["cells"])
    expected_cells[0]["allocation"]["canonical_allocation_digest"] = "0" * 64
    monkeypatch.setattr(
        prospective_replay,
        "_replay_cell",
        lambda *_args: expected_cells.pop(0),
    )
    with pytest.raises(prospective_replay.ProspectiveReplayError, match="does not replay"):
        prospective_replay.replay_source(
            certificate,
            manifest,
            source,
            text,
            bundle=object(),
            repository=Path.cwd(),
        )


def test_manifest_database_and_receipt_digests_fail_before_inventory(tmp_path) -> None:
    database, receipt = tmp_path / "sources.db", tmp_path / "rights.json"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE wikipedia (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
        "url TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', "
        "char_count INTEGER DEFAULT 0, fetched_at TEXT NOT NULL DEFAULT '')"
    )
    connection.execute(
        "INSERT INTO wikipedia VALUES "
        "(1, 'Title', 'https://uk.wikipedia.org/wiki/X', 'text', 4, 'now')"
    )
    connection.commit()
    connection.close()
    receipt.write_text("receipt", encoding="utf-8")
    with pytest.raises(ProspectiveManifestError, match="digest"):
        build_manifest(database, receipt)
    # URL/text/row-order/schema mutations are all rejected by the frozen DB digest gate,
    # before a source inventory or preflight can be constructed.
    for statement in (
        "UPDATE wikipedia SET url='https://uk.wikipedia.org/wiki/Y'",
        "UPDATE wikipedia SET text='mutated'",
        "INSERT INTO wikipedia VALUES "
        "(2, 'Other', 'https://uk.wikipedia.org/wiki/Z', 'more', 4, 'now')",
        "ALTER TABLE wikipedia ADD COLUMN unexpected TEXT",
    ):
        connection = sqlite3.connect(database)
        connection.execute(statement)
        connection.commit()
        connection.close()
        with pytest.raises(ProspectiveManifestError, match="digest"):
            build_manifest(database, receipt)


def test_aggregate_requires_exact_source_set_before_replay() -> None:
    manifest = _manifest()
    with pytest.raises(ProspectiveEvaluationError, match="exact 94-source"):
        evaluate_population(manifest, {}, {}, bundle=object(), repository=Path.cwd())
    assert "823 unselected eligible" in CLAIM_CEILING
    assert "all <2600 rows are unevaluated" in CLAIM_CEILING


@pytest.mark.parametrize("emissions", [0, 2])
def test_capture_and_replay_fail_closed_on_non_exact_receipt_emission(
    monkeypatch, emissions
) -> None:
    text = "controlled source"
    manifest = _manifest(first_text=text)
    source = manifest.sources[0]

    def fake_inventory(*_args, **kwargs):
        for _ in range(emissions):
            kwargs["receipt_sink"]((object(),))
        return object()

    monkeypatch.setattr(prospective_capture, "inventory_from_anchor", fake_inventory)
    monkeypatch.setattr(prospective_capture, "_verified_engine_bundle", lambda *_args: object())
    certificate = prospective_capture.capture_source(
        manifest, source, text, bundle=object(), repository=Path.cwd()
    )
    assert certificate.payload["status"] == "failed"
    assert certificate.payload["banks"] == []
    assert certificate.payload["cells"] == []

    monkeypatch.setattr(prospective_replay, "inventory_from_anchor", fake_inventory)
    monkeypatch.setattr(prospective_replay, "_verified_engine_bundle", lambda *_args: object())
    replayed = prospective_replay.replay_source(
        certificate.to_bytes(),
        manifest,
        source,
        text,
        bundle=object(),
        repository=Path.cwd(),
    )
    assert replayed.digest == certificate.digest


def test_cli_persists_exact_denominator_and_replays_persisted_bytes(monkeypatch, tmp_path) -> None:
    manifest = _manifest()
    source_texts = {source.sqlite_id: f"text-{source.sqlite_id}" for source in manifest.sources}

    class DummyCertificate:
        def __init__(self, source_id: int) -> None:
            self.source_id = source_id

        def to_bytes(self) -> bytes:
            return f"certificate-{self.source_id}".encode()

    aggregate = ProspectiveAggregate(
        manifest.digest,
        COMPOSITE_RULE_DIGEST,
        94,
        0,
        94,
        917,
        823,
        CLAIM_CEILING,
        tuple((source.sqlite_id, "a" * 64) for source in manifest.sources),
    )

    monkeypatch.setattr(prospective_cli.data, "resolve_bundle", lambda **_kwargs: object())
    monkeypatch.setattr(prospective_cli, "build_manifest", lambda *_args: (manifest, source_texts))
    monkeypatch.setattr(
        prospective_cli,
        "capture_source",
        lambda _manifest, source, _text, **_kwargs: DummyCertificate(source.sqlite_id),
    )

    def fake_evaluate(_manifest, certificate_bytes, texts, **_kwargs):
        assert texts == source_texts
        assert set(certificate_bytes) == set(source_texts)
        assert all(
            certificate_bytes[source_id] == f"certificate-{source_id}".encode()
            for source_id in source_texts
        )
        return aggregate

    monkeypatch.setattr(prospective_cli, "evaluate_population", fake_evaluate)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
    output = tmp_path / "evidence"
    arguments = [
        "--database",
        str(tmp_path / "sources.db"),
        "--rights-receipt",
        str(tmp_path / "rights.json"),
        "--engine-data-dir",
        str(tmp_path),
        "--engine-data-manifest",
        str(tmp_path / "data-manifest.json"),
        "--repository",
        str(Path.cwd()),
        "--output-dir",
        str(output),
    ]
    assert prospective_cli.main(arguments) == 0
    expected_names = {
        f"{source.selection_rank:03d}-{source.sqlite_id}.json" for source in manifest.sources
    }
    assert {path.name for path in (output / "certificates").iterdir()} == expected_names
    complete = json.loads((output / "run-complete.json").read_bytes())
    assert complete == {
        "version": "HramatkaProspectiveRunState.v1",
        "state": "complete",
        "manifest_digest": manifest.digest,
        "aggregate_digest": aggregate.digest,
        "certificate_count": 94,
    }
    with pytest.raises(prospective_cli.ProspectiveCliError, match="already exists"):
        prospective_cli.main(arguments)


def test_prospective_modules_do_not_open_socket_http_provider_or_selector_on_import(
    tmp_path,
) -> None:
    program = (
        "import socket; "
        "socket.socket=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('socket')); "
        "import hramatka.qualification.prospective_manifest_v1; "
        "import hramatka.qualification.prospective_proof_schema_v1; "
        "import hramatka.qualification.prospective_capture_v1; "
        "import hramatka.qualification.prospective_replay_v1; "
        "import hramatka.qualification.prospective_evaluate_v1"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert socket.getdefaulttimeout() is None
