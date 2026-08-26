from __future__ import annotations

import copy
import hashlib
import json
import socket
import subprocess
import sys
import types
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import hramatka.qualification.proof_capture_v1 as proof_capture
import hramatka.qualification.proof_replay_v1 as proof_replay
import hramatka.qualification.proof_schema_v1 as proof_schema
from hramatka.engine import data
from hramatka.qualification.harness import (
    _qualification_fixture_bundle,
    deterministic_runtime_anchors,
)
from hramatka.qualification.manifest import load_manifest
from hramatka.qualification.proof_capture_v1 import (
    ProofCaptureError,
    capture_manifest_certificates,
    persist_certificate_create_only,
)
from hramatka.qualification.proof_capture_v1 import _verify_frozen_bundle as capture_bundle_guard
from hramatka.qualification.proof_replay_v1 import (
    ProofReplayError,
    replay_manifest_certificates,
)
from hramatka.qualification.proof_replay_v1 import (
    _verify_frozen_bundle as replay_bundle_guard,
)
from hramatka.qualification.proof_schema_v1 import (
    CONTENT_MANIFEST_DIGEST,
    ExpectedAuthority,
    ProofCertificate,
    ProofSchemaError,
    _approved_repository_origin,
    frozen_content_authority,
    implementation_inventory_digest,
)


def _checked_out_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _strict_fixture_bundle(root: Path) -> data.DataBundle:
    """Supply all three deterministic files to the strict mounted-data gate."""
    bundle = _qualification_fixture_bundle(root)
    sources = root / "sources.db"
    sources.write_bytes(b"hramatka-proof-fixture-sources-v1\n")
    bundle.manifest["inputs"]["sources.db"].update(
        {
            "path": "sources.db",
            "sha256": hashlib.sha256(sources.read_bytes()).hexdigest(),
            "size": sources.stat().st_size,
            "required": True,
        }
    )
    return bundle


@pytest.fixture(autouse=True)
def _fixture_frozen_data_authority(monkeypatch, tmp_path) -> None:
    """Inject a test-owned immutable three-file policy; production has no test mode."""
    bundle = _strict_fixture_bundle(tmp_path / "frozen-policy")
    policy = {
        name: {
            "path": name,
            "sha256": digest,
            "size": bundle.path(name).stat().st_size,
        }
        for name, digest in bundle.digests().items()
    }

    def frozen_fixture_policy(_repository: Path):
        return CONTENT_MANIFEST_DIGEST, policy

    monkeypatch.setattr(proof_capture, "frozen_content_authority", frozen_fixture_policy)
    monkeypatch.setattr(proof_replay, "frozen_content_authority", frozen_fixture_policy)

    # The compact test DBs exercise legacy synthetic choice banks.  Production
    # execution bundles remain policy/root-only; this local harness marker is
    # injected only after the detached bundle is made, never copied from a
    # caller-owned manifest.
    capture_execution_bundle = proof_capture._capture_execution_bundle
    replay_execution_bundle = proof_replay._replay_execution_bundle

    def test_capture_execution_bundle(bundle, frozen_policy):
        detached = capture_execution_bundle(bundle, frozen_policy)
        detached.manifest["version"] = "test"
        return detached

    def test_replay_execution_bundle(bundle, frozen_policy):
        detached = replay_execution_bundle(bundle, frozen_policy)
        detached.manifest["version"] = "test"
        return detached

    monkeypatch.setattr(proof_capture, "_capture_execution_bundle", test_capture_execution_bundle)
    monkeypatch.setattr(proof_replay, "_replay_execution_bundle", test_replay_execution_bundle)


def _certificates(tmp_path):
    bundle = _strict_fixture_bundle(tmp_path / "bundle")
    anchors = deterministic_runtime_anchors()
    expected = ExpectedAuthority(
        implementation_commit=_checked_out_commit(),
        data_digests=bundle.digests(),
    )
    certificates = capture_manifest_certificates(
        anchors=anchors,
        bundle=bundle,
        repository=Path.cwd(),
        expected=expected,
    )
    return bundle, anchors, expected, certificates


def _mutate_after_validation_and_observe_execution_bundle(monkeypatch, module, bundle):
    """Force a caller manifest redirect after validation, before engine use."""
    original_verify = module._verify_frozen_bundle
    original_use_bundle = data.use_bundle
    observed_paths: list[Path] = []

    def mutate_after_validation(active_bundle, policy):
        result = original_verify(active_bundle, policy)
        bundle.manifest["inputs"]["atlas.db"]["path"] = "data.py"
        return result

    @contextmanager
    def observe_execution_bundle(active_bundle):
        observed_paths.append(active_bundle.path("atlas.db"))
        with original_use_bundle(active_bundle):
            yield active_bundle

    monkeypatch.setattr(module, "_verify_frozen_bundle", mutate_after_validation)
    monkeypatch.setattr(module.data, "use_bundle", observe_execution_bundle)
    return observed_paths


def test_capture_and_independent_replay_cover_all_manifest_anchors(tmp_path) -> None:
    bundle, anchors, expected, certificates = _certificates(tmp_path)
    replayed = replay_manifest_certificates(
        certificate_bytes={
            certificate.anchor_id: certificate.to_bytes() for certificate in certificates
        },
        anchors=anchors,
        bundle=bundle,
        repository=Path.cwd(),
        expected=expected,
    )

    assert [certificate.anchor_id for certificate in replayed] == [
        "b1-narrative",
        "b1-dialogue",
        "b1-informational",
    ]
    assert [
        sum(len(slot["units"]) for slot in certificate.allocation["slots"])
        for certificate in certificates
    ] == [64, 60, 69]
    assert {
        certificate.anchor_id: (
            certificate.profile_digest,
            certificate.allocation["canonical_allocation_digest"],
            certificate.witness["canonical_allocation_digest"],
        )
        for certificate in certificates
    } == {
        "b1-narrative": (
            "32d0aff3e7c0ffe89620a6416cc13b75c333a24ddaa332800a0b52f8fd3f3eb6",
            "d4df26bc2dfc4ae64ceccfd74eda4f9412fc7b034d83b334900fdeb1187e7771",
            "d4df26bc2dfc4ae64ceccfd74eda4f9412fc7b034d83b334900fdeb1187e7771",
        ),
        "b1-dialogue": (
            "32d0aff3e7c0ffe89620a6416cc13b75c333a24ddaa332800a0b52f8fd3f3eb6",
            "18085a954bbfa72d9ed4bf6d5aec2e947edaba6c34b921f32a1a6aafe9cc00bb",
            "18085a954bbfa72d9ed4bf6d5aec2e947edaba6c34b921f32a1a6aafe9cc00bb",
        ),
        "b1-informational": (
            "32d0aff3e7c0ffe89620a6416cc13b75c333a24ddaa332800a0b52f8fd3f3eb6",
            "96a4e2f3381826089f2faf550a5100f28a3874a50df0489c626ee7e20bb1b39b",
            "96a4e2f3381826089f2faf550a5100f28a3874a50df0489c626ee7e20bb1b39b",
        ),
    }
    assert all(
        anchor.text.encode("utf-8") not in certificate.to_bytes()
        for anchor in anchors.values()
        for certificate in certificates
    )
    assert all(
        anchor.sha256.encode("ascii") not in certificate.to_bytes()
        for anchor in load_manifest().anchors
        for certificate in certificates
    )


def test_direct_replay_rejects_whitespace_mutated_runtime_anchor(tmp_path) -> None:
    bundle, anchors, expected, certificates = _certificates(tmp_path)
    certificate = certificates[0]
    anchor = anchors[certificate.anchor_id]
    whitespace_mutated = replace(anchor, text=f"{anchor.text} ")
    with pytest.raises(ProofReplayError, match="Replay anchor bytes"):
        proof_replay.replay_certificate(
            proof_replay.ReplayInputs(
                certificate_bytes=certificate.to_bytes(),
                anchor=whitespace_mutated,
                bundle=bundle,
                repository=Path.cwd(),
                expected=expected,
            )
        )


def test_expected_authority_snapshots_caller_owned_digest_mapping(tmp_path) -> None:
    bundle = _strict_fixture_bundle(tmp_path / "bundle")
    caller_digests = bundle.digests()
    expected = ExpectedAuthority(_checked_out_commit(), caller_digests)
    caller_digests["atlas.db"] = "0" * 64
    assert expected.data_digests["atlas.db"] != caller_digests["atlas.db"]
    with pytest.raises(TypeError):
        expected.data_digests["atlas.db"] = "0" * 64


def test_capture_executes_only_detached_frozen_bundle_after_validation_mutation(
    monkeypatch, tmp_path
) -> None:
    bundle = _strict_fixture_bundle(tmp_path / "bundle")
    expected = ExpectedAuthority(_checked_out_commit(), bundle.digests())
    observed = _mutate_after_validation_and_observe_execution_bundle(
        monkeypatch, proof_capture, bundle
    )
    proof_capture.capture_certificate(
        proof_capture.CaptureInputs(
            anchor=deterministic_runtime_anchors()["b1-narrative"],
            bundle=bundle,
            repository=Path.cwd(),
            expected=expected,
        )
    )
    assert observed == [bundle.root / "atlas.db"]


def test_replay_executes_only_detached_frozen_bundle_after_validation_mutation(
    monkeypatch, tmp_path
) -> None:
    bundle, anchors, expected, certificates = _certificates(tmp_path)
    certificate = certificates[0]
    observed = _mutate_after_validation_and_observe_execution_bundle(
        monkeypatch, proof_replay, bundle
    )
    proof_replay.replay_certificate(
        proof_replay.ReplayInputs(
            certificate_bytes=certificate.to_bytes(),
            anchor=anchors[certificate.anchor_id],
            bundle=bundle,
            repository=Path.cwd(),
            expected=expected,
        )
    )
    assert observed == [bundle.root / "atlas.db"]


def test_capture_rejects_synthetic_non_test_data_authority(tmp_path) -> None:
    bundle = _strict_fixture_bundle(tmp_path / "bundle")
    anchors = deterministic_runtime_anchors()
    expected = ExpectedAuthority(
        implementation_commit=_checked_out_commit(), data_digests=bundle.digests()
    )
    with (bundle.root / "atlas.db").open("ab") as handle:
        handle.write(b"drift")
    with pytest.raises(ProofCaptureError, match="frozen Git policy"):
        capture_manifest_certificates(
            anchors=anchors,
            bundle=bundle,
            repository=Path.cwd(),
            expected=expected,
        )


def test_certificate_writer_is_atomic_create_only_and_cleans_failed_output(
    monkeypatch, tmp_path
) -> None:
    """Persistence publishes only complete canonical bytes and never overwrites."""
    _bundle, _anchors, _expected, certificates = _certificates(tmp_path)
    target = tmp_path / "proof" / "certificate.json"
    target.parent.mkdir()

    persist_certificate_create_only(target, certificates[0])
    assert target.read_bytes() == certificates[0].to_bytes()
    with pytest.raises(ProofCaptureError, match="already exists"):
        persist_certificate_create_only(target, certificates[0])
    assert target.read_bytes() == certificates[0].to_bytes()

    failed_target = target.parent / "failed.json"

    def link_failure(*_args: object, **_kwargs: object) -> None:
        raise OSError("test-only link failure")

    monkeypatch.setattr(proof_capture.os, "link", link_failure)
    with pytest.raises(ProofCaptureError, match="could not be persisted"):
        persist_certificate_create_only(failed_target, certificates[0])
    assert not failed_target.exists()
    assert list(target.parent.glob(".*.tmp")) == []


def test_capture_replay_are_repeatable_and_cannot_read_environment_or_network(
    monkeypatch, tmp_path
) -> None:
    """The proof path is deterministic and has no ambient provider authority."""
    sentinel = "R2F7_ENVIRONMENT_SENTINEL"

    def network_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("proof capture/replay must not open a network connection")

    monkeypatch.setenv("HRAMATKA_PROOF_CANARY", sentinel)
    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    bundle = _strict_fixture_bundle(tmp_path / "bundle")
    anchors = deterministic_runtime_anchors()
    expected = ExpectedAuthority(
        implementation_commit=_checked_out_commit(), data_digests=bundle.digests()
    )
    first = capture_manifest_certificates(
        anchors=anchors, bundle=bundle, repository=Path.cwd(), expected=expected
    )
    second = capture_manifest_certificates(
        anchors=anchors, bundle=bundle, repository=Path.cwd(), expected=expected
    )
    first_bytes = {certificate.anchor_id: certificate.to_bytes() for certificate in first}
    assert first_bytes == {certificate.anchor_id: certificate.to_bytes() for certificate in second}
    replayed = replay_manifest_certificates(
        certificate_bytes=first_bytes,
        anchors=anchors,
        bundle=bundle,
        repository=Path.cwd(),
        expected=expected,
    )
    assert first_bytes == {
        certificate.anchor_id: certificate.to_bytes() for certificate in replayed
    }
    assert all(sentinel.encode("ascii") not in certificate for certificate in first_bytes.values())


def test_frozen_content_policy_is_read_from_exact_baseline_git_objects() -> None:
    manifest_digest, policy = frozen_content_authority(Path.cwd())
    assert manifest_digest == CONTENT_MANIFEST_DIGEST
    assert set(policy) == {"vesum.db", "atlas.db", "sources.db"}
    assert all(
        record["path"] == name and isinstance(record["size"], int)
        for name, record in policy.items()
    )


def test_implementation_inventory_rejects_wrong_head_and_foreign_loaded_module(
    monkeypatch, tmp_path
) -> None:
    repository = Path.cwd()
    with pytest.raises(ProofSchemaError, match="commit is not the executing checkout"):
        implementation_inventory_digest(repository, "0" * 40)

    foreign = types.ModuleType("hramatka.engine.foreign_fixture")
    foreign.__file__ = str(tmp_path / "foreign.py")
    monkeypatch.setitem(sys.modules, foreign.__name__, foreign)
    with pytest.raises(ProofSchemaError, match="outside the executing checkout"):
        implementation_inventory_digest(repository, _checked_out_commit())


def test_implementation_inventory_rejects_untracked_checkout_state() -> None:
    repository = Path.cwd()
    untracked = repository / ".r2f2-untracked-canary"
    try:
        untracked.write_text("canary", encoding="utf-8")
        with pytest.raises(ProofSchemaError, match="clean checkout"):
            implementation_inventory_digest(repository, _checked_out_commit())
    finally:
        untracked.unlink(missing_ok=True)


def test_implementation_inventory_ignores_environment_git_substitution(monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/nonexistent/forged-gitconfig")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/nonexistent/alternate")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:https:ssh")
    digest = implementation_inventory_digest(Path.cwd(), _checked_out_commit())
    assert len(digest) == 64


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/learn-ukrainian/learn-ukrainian-infra-private",
        "https://github.com/learn-ukrainian/learn-ukrainian-infra-private.git",
        "git@github.com:learn-ukrainian/learn-ukrainian-infra-private.git",
        "ssh://git@github.com/learn-ukrainian/learn-ukrainian-infra-private.git",
    ],
)
def test_repository_identity_accepts_only_canonical_actions_and_ssh_origins(origin) -> None:
    assert _approved_repository_origin(origin)


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/learn-ukrainian/learn-ukrainian-infra-private.git.evil",
        "https://github.com/learn-ukrainian/learn-ukrainian-infra-private.git/extra",
        "https://github.com/learn-ukrainian/learn-ukrainian-infra-private.git?ref=main",
        "https://github.com/learn-ukrainian/learn-ukrainian-infra-private.git#fragment",
        "https://token@github.com/learn-ukrainian/learn-ukrainian-infra-private.git",
        "https://github.com/learn-ukrainian-lookalike/learn-ukrainian-infra-private.git",
        "https://github.com/learn-ukrainian/learn-ukrainian-infra-private-lookalike.git",
        "git@github.com:learn-ukrainian/learn-ukrainian-infra-private.git.evil",
        "ssh://git@github.com:22/learn-ukrainian/learn-ukrainian-infra-private.git",
        "ssh://other@github.com/learn-ukrainian/learn-ukrainian-infra-private.git",
    ],
)
def test_repository_identity_rejects_forged_or_substring_origins(origin) -> None:
    assert not _approved_repository_origin(origin)


@pytest.mark.parametrize(
    "guard,error",
    [(capture_bundle_guard, ProofCaptureError), (replay_bundle_guard, ProofReplayError)],
)
@pytest.mark.parametrize("missing", [True, False])
def test_mounted_bundle_guard_requires_every_frozen_db_and_exact_bytes(
    tmp_path, guard, error, missing
) -> None:
    bundle = _strict_fixture_bundle(tmp_path / "bundle")
    bundle.manifest["version"] = "synthetic-production"
    policy = {
        name: {"path": name, "sha256": digest, "size": (bundle.root / name).stat().st_size}
        for name, digest in bundle.digests().items()
        if (bundle.root / name).is_file()
    }
    policy["sources.db"] = {
        "path": "sources.db",
        "sha256": "0" * 64,
        "size": 1,
    }
    if not missing:
        policy["atlas.db"] = {**policy["atlas.db"], "sha256": "0" * 64}
    with pytest.raises(error, match="frozen Git policy"):
        guard(bundle, policy)


@pytest.mark.parametrize(
    "guard,error",
    [(capture_bundle_guard, ProofCaptureError), (replay_bundle_guard, ProofReplayError)],
)
def test_test_version_and_caller_selected_single_db_cannot_bypass_authority(
    tmp_path, guard, error
) -> None:
    (tmp_path / "caller-selected.db").write_bytes(b"fixture")
    bundle = data.DataBundle(
        root=tmp_path,
        manifest={
            "version": "test",
            "inputs": {
                "caller-selected.db": {
                    "path": "caller-selected.db",
                    "sha256": hashlib.sha256(b"fixture").hexdigest(),
                    "size": len(b"fixture"),
                }
            },
        },
    )
    policy = {
        name: {"path": name, "sha256": "0" * 64, "size": 1}
        for name in ("vesum.db", "atlas.db", "sources.db")
    }
    with pytest.raises(error, match="data manifest"):
        guard(bundle, policy)


@pytest.mark.parametrize(
    "guard,error",
    [(capture_bundle_guard, ProofCaptureError), (replay_bundle_guard, ProofReplayError)],
)
@pytest.mark.parametrize(
    ("name", "redirect"),
    [("atlas.db", "vesum.db"), ("sources.db", "atlas.db")],
)
def test_mounted_bundle_guard_rejects_exact_key_path_redirection(
    tmp_path, guard, error, name, redirect
) -> None:
    bundle = _strict_fixture_bundle(tmp_path / "bundle")
    policy = {
        input_name: {
            "path": input_name,
            "sha256": digest,
            "size": bundle.path(input_name).stat().st_size,
        }
        for input_name, digest in bundle.digests().items()
    }
    bundle.manifest["inputs"][name]["path"] = redirect
    with pytest.raises(error, match="data manifest"):
        guard(bundle, policy)


def test_replay_rejects_self_consistent_certificate_bank_tampering(tmp_path) -> None:
    bundle, anchors, expected, certificates = _certificates(tmp_path)
    certificate = certificates[0]
    tampered = json.loads(certificate.to_bytes())
    tampered["bank_receipts"] = tampered["bank_receipts"][1:]
    raw = json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(ProofReplayError, match="geometry"):
        replay_manifest_certificates(
            certificate_bytes={
                certificate.anchor_id: raw,
                **{item.anchor_id: item.to_bytes() for item in certificates[1:]},
            },
            anchors=anchors,
            bundle=bundle,
            repository=Path.cwd(),
            expected=expected,
        )


def test_replay_rejects_certificate_selected_implementation_authority(tmp_path) -> None:
    bundle, anchors, expected, certificates = _certificates(tmp_path)
    tampered = json.loads(certificates[0].to_bytes())
    tampered["repository_commit"] = "0" * 40
    raw = json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(ProofReplayError, match="implementation authority"):
        replay_manifest_certificates(
            certificate_bytes={
                certificates[0].anchor_id: raw,
                **{item.anchor_id: item.to_bytes() for item in certificates[1:]},
            },
            anchors=anchors,
            bundle=bundle,
            repository=Path.cwd(),
            expected=expected,
        )


def test_replay_rejects_authority_before_malformed_geometry(tmp_path) -> None:
    bundle, anchors, expected, certificates = _certificates(tmp_path)
    tampered = json.loads(certificates[0].to_bytes())
    tampered["repository_commit"] = "0" * 40
    tampered["allocation"]["slots"] = []
    raw = json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(ProofReplayError, match="implementation authority"):
        replay_manifest_certificates(
            certificate_bytes={
                certificates[0].anchor_id: raw,
                **{item.anchor_id: item.to_bytes() for item in certificates[1:]},
            },
            anchors=anchors,
            bundle=bundle,
            repository=Path.cwd(),
            expected=expected,
        )


def test_replay_dissents_from_deliberately_corrupted_capture_projection(
    monkeypatch, tmp_path
) -> None:
    bundle = _strict_fixture_bundle(tmp_path / "bundle")
    anchors = deterministic_runtime_anchors()
    expected = ExpectedAuthority(
        implementation_commit=_checked_out_commit(), data_digests=bundle.digests()
    )
    monkeypatch.setattr(proof_capture, "_inventory_commitment", lambda _inventory: "0" * 64)
    certificates = capture_manifest_certificates(
        anchors=anchors,
        bundle=bundle,
        repository=Path.cwd(),
        expected=expected,
    )
    with pytest.raises(ProofReplayError, match="inventory membership"):
        replay_manifest_certificates(
            certificate_bytes={item.anchor_id: item.to_bytes() for item in certificates},
            anchors=anchors,
            bundle=bundle,
            repository=Path.cwd(),
            expected=expected,
        )


@pytest.mark.parametrize(
    ("attribute", "message"),
    [
        ("_replay_allocation_projection", "allocation"),
        ("_witness", "witness"),
        ("_rebuilt_domain_commitments", "domain commitments"),
    ],
)
def test_replay_has_independent_allocation_witness_and_domain_dissent_canaries(
    monkeypatch, tmp_path, attribute, message
) -> None:
    """A defective replay derivation cannot silently accept capture's certificate."""
    bundle, anchors, expected, certificates = _certificates(tmp_path)
    original = getattr(proof_replay, attribute)

    def dissenting(*args):
        value = copy.deepcopy(original(*args))
        if attribute == "_replay_allocation_projection":
            value["slots"][0]["scheduled_type"] = "cloze"
        elif attribute == "_witness":
            value["rows"][0]["activity_type"] = "cloze"
        else:
            value["unit"]["digest"] = "0" * 64
        return value

    monkeypatch.setattr(proof_replay, attribute, dissenting)
    with pytest.raises(ProofReplayError, match=message):
        replay_manifest_certificates(
            certificate_bytes={item.anchor_id: item.to_bytes() for item in certificates},
            anchors=anchors,
            bundle=bundle,
            repository=Path.cwd(),
            expected=expected,
        )


def test_schema_rejects_secret_low_entropy_and_content_like_structural_fields(tmp_path) -> None:
    _bundle, _anchors, _expected, certificates = _certificates(tmp_path)
    original = json.loads(certificates[0].to_bytes())
    mutations = [
        (("bank_receipts", 0, "bank_id"), "password"),
        (("bank_receipts", 0, "candidate_ids", 0), "aaaa"),
        (("allocation", "slots", 0, "units", 0, "claims", 0, "resource_id"), "teacher-key"),
        (("allocation", "slots", 0, "units", 0, "locators", 0), "source-text"),
        (("allocation", "slots", 0, "slot_id"), "credential"),
    ]
    for path, value in mutations:
        payload = copy.deepcopy(original)
        target = payload
        for segment in path[:-1]:
            target = target[segment]
        target[path[-1]] = value
        with pytest.raises(ProofSchemaError):
            ProofCertificate.from_bytes(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            )


def test_replay_rejects_self_consistent_reordered_or_reused_proof_domains(tmp_path) -> None:
    """Replay owns ordering/membership truth, not a certificate rehash."""
    bundle, anchors, expected, certificates = _certificates(tmp_path)
    original = json.loads(certificates[0].to_bytes())

    def raw(payload: dict) -> bytes:
        payload["domain_commitments"] = proof_schema._declared_domain_commitments(
            payload["bank_receipts"], payload["allocation"], payload["witness"]
        )
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

    def replay_rejects(payload: dict) -> None:
        certificate_bytes = {
            certificates[0].anchor_id: raw(payload),
            **{item.anchor_id: item.to_bytes() for item in certificates[1:]},
        }
        # A syntactically self-consistent attacker rehash must get past schema
        # parsing, then fail the independently derived replay geometry.
        ProofCertificate.from_bytes(certificate_bytes[certificates[0].anchor_id])
        with pytest.raises(ProofReplayError):
            replay_manifest_certificates(
                certificate_bytes=certificate_bytes,
                anchors=anchors,
                bundle=bundle,
                repository=Path.cwd(),
                expected=expected,
            )

    reordered_banks = copy.deepcopy(original)
    reordered_banks["bank_receipts"].reverse()
    replay_rejects(reordered_banks)

    reordered_candidates = copy.deepcopy(original)
    receipt = next(
        item for item in reordered_candidates["bank_receipts"] if len(item["candidate_ids"]) > 1
    )
    receipt["candidate_ids"].reverse()
    commitments = {
        item["candidate_id"]: item["commitment"] for item in receipt["candidate_provenance"]
    }
    receipt["candidate_offsets"] = [
        {"candidate_id": candidate_id, "unit_offset": offset}
        for offset, candidate_id in enumerate(receipt["candidate_ids"])
    ]
    receipt["candidate_provenance"] = [
        {"candidate_id": candidate_id, "commitment": commitments[candidate_id]}
        for candidate_id in receipt["candidate_ids"]
    ]
    replay_rejects(reordered_candidates)

    missing_candidate = copy.deepcopy(original)
    receipt = next(
        item for item in missing_candidate["bank_receipts"] if len(item["candidate_ids"]) > 1
    )
    removed_id = receipt["candidate_ids"].pop()
    receipt["count"] -= 1
    receipt["candidate_offsets"] = [
        {"candidate_id": candidate_id, "unit_offset": offset}
        for offset, candidate_id in enumerate(receipt["candidate_ids"])
    ]
    receipt["candidate_provenance"] = [
        item for item in receipt["candidate_provenance"] if item["candidate_id"] != removed_id
    ]
    replay_rejects(missing_candidate)

    duplicated_placement = copy.deepcopy(original)
    receipt = duplicated_placement["bank_receipts"][0]
    receipt["eligible_placements"].append(copy.deepcopy(receipt["eligible_placements"][0]))
    replay_rejects(duplicated_placement)

    reordered_slots = copy.deepcopy(original)
    reordered_slots["allocation"]["slots"].reverse()
    reordered_slots["witness"]["rows"].reverse()
    reordered_slots["witness"]["eligible_replacement_edges"].reverse()
    replay_rejects(reordered_slots)

    reused_claim = copy.deepcopy(original)
    unit = reused_claim["allocation"]["slots"][0]["units"][0]
    unit["claims"].append(copy.deepcopy(unit["claims"][0]))
    unit["reservations"] = copy.deepcopy(unit["claims"])
    unit["plan_digest"] = proof_schema.sha256(
        {"unit_id": unit["unit_id"], "claims": unit["claims"], "locators": unit["locators"]}
    )
    reused_claim["witness"]["rows"][0]["units"][0] = copy.deepcopy(unit)
    replay_rejects(reused_claim)

    reused_locator = copy.deepcopy(original)
    unit = reused_locator["allocation"]["slots"][0]["units"][0]
    unit["locators"].append(unit["locators"][0])
    unit["plan_digest"] = proof_schema.sha256(
        {"unit_id": unit["unit_id"], "claims": unit["claims"], "locators": unit["locators"]}
    )
    reused_locator["witness"]["rows"][0]["units"][0] = copy.deepcopy(unit)
    replay_rejects(reused_locator)

    reordered_units = copy.deepcopy(original)
    reordered_units["allocation"]["slots"][0]["units"].reverse()
    reordered_units["witness"]["rows"][0]["units"].reverse()
    replay_rejects(reordered_units)


def test_wire_schema_rejects_duplicate_and_reordered_witness_geometry(tmp_path) -> None:
    """Closed slot/witness joins fail before replay when direct geometry breaks."""
    _bundle, _anchors, _expected, certificates = _certificates(tmp_path)
    duplicate = json.loads(certificates[0].to_bytes())
    duplicate["allocation"]["slots"].append(copy.deepcopy(duplicate["allocation"]["slots"][0]))
    with pytest.raises(ProofSchemaError, match="six-slot"):
        ProofCertificate.from_bytes(json.dumps(duplicate, sort_keys=True).encode())

    reordered_witness = json.loads(certificates[0].to_bytes())
    reordered_witness["witness"]["rows"].reverse()
    with pytest.raises(ProofSchemaError, match="mirror allocation geometry"):
        ProofCertificate.from_bytes(json.dumps(reordered_witness, sort_keys=True).encode())


def test_frozen_content_authority_rejects_baseline_manifest_digest_drift(monkeypatch) -> None:
    """Every mounted policy class remains subordinate to the pinned manifest blob."""
    monkeypatch.setattr(proof_schema, "CONTENT_MANIFEST_DIGEST", "0" * 64)
    with pytest.raises(ProofSchemaError, match="manifest object has drifted"):
        frozen_content_authority(Path.cwd())


@pytest.mark.parametrize(
    "path",
    [
        proof_schema.CONTENT_ANCHORS_PATH,
        proof_schema.CONTENT_LINGUISTICS_PATH,
        proof_schema.CONTENT_DATA_MANIFEST_PATH,
    ],
)
def test_frozen_content_authority_rejects_every_policy_blob_class_drift(monkeypatch, path) -> None:
    """Anchor, linguistic, and data-manifest Git blobs all have closed schemas."""
    original = proof_schema._pinned_json_blob

    def drifted_blob(repository, candidate_path):
        if candidate_path == path:
            return {"unexpected": "drift"}
        return original(repository, candidate_path)

    monkeypatch.setattr(proof_schema, "_pinned_json_blob", drifted_blob)
    with pytest.raises(ProofSchemaError):
        frozen_content_authority(Path.cwd())


def test_certificate_nested_records_are_deeply_immutable(tmp_path) -> None:
    _bundle, _anchors, _expected, certificates = _certificates(tmp_path)
    certificate = certificates[0]
    with pytest.raises(TypeError):
        certificate.allocation["slots"][0]["slot_id"] = "FORBIDDEN_SENTINEL"  # type: ignore[index]
    with pytest.raises(AttributeError):
        certificate.allocation["slots"].append("FORBIDDEN_SENTINEL")  # type: ignore[union-attr]


def test_wire_schema_rejects_reservation_and_conditional_geometry_tampering(tmp_path) -> None:
    _bundle, _anchors, _expected, certificates = _certificates(tmp_path)
    payload = json.loads(certificates[0].to_bytes())
    payload["allocation"]["slots"][0]["units"][0]["reservations"] = []
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ProofSchemaError, match="reservations"):
        ProofCertificate.from_bytes(raw)

    conditional_payload = json.loads(certificates[0].to_bytes())
    conditional_payload["allocation"]["slots"][0]["conditional_replacements"].append(
        {"activity_type": "fill-in", "units": []}
    )
    conditional_raw = json.dumps(
        conditional_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    with pytest.raises(ProofSchemaError, match="must not be empty"):
        ProofCertificate.from_bytes(conditional_raw)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("candidate_offsets", 0, "unit_offset"), 8, "offsets"),
        (("candidate_provenance", 0, "commitment"), "0" * 64, "Domain commitments"),
        (("inventory_seal",), "0" * 64, "seal"),
    ],
)
def test_wire_schema_rejects_forged_bank_seal_and_member_metadata(
    tmp_path, path, value, message
) -> None:
    _bundle, _anchors, _expected, certificates = _certificates(tmp_path)
    payload = json.loads(certificates[0].to_bytes())
    target = payload["bank_receipts"][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ProofSchemaError, match=message):
        ProofCertificate.from_bytes(raw)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("bank_receipts", 0, "origin_kind"), "arbitrary-origin"),
        (("allocation", "slots", 0, "unexpected"), "forbidden sentinel"),
    ],
)
def test_wire_schema_rejects_unapproved_origin_and_nested_sentinel(tmp_path, path, value) -> None:
    _bundle, _anchors, _expected, certificates = _certificates(tmp_path)
    payload = json.loads(certificates[0].to_bytes())
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(ProofSchemaError):
        ProofCertificate.from_bytes(raw)


def test_proof_modules_do_not_import_api_or_provider_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import hramatka.qualification.proof_capture_v1; "
                "assert not any(name.startswith('hramatka.api') or name.startswith("
                "'hramatka.engine.providers') for name in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
