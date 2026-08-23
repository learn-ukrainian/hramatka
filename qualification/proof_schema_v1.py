"""Strict shared, content-free schema primitives for offline proof replay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

SCHEMA_VERSION: Final = "HramatkaProofCertificate.v1"
CONTENT_BASELINE_COMMIT: Final = "feccaa9083fdc824023284cf097d543737060f93"
CONTENT_MANIFEST_DIGEST: Final = "da0ace9338c09c3bca68f3d08921e97d958182472151e3f03f5f852a1be182cc"
CONTENT_MANIFEST_PATH: Final = "hramatka/qualification/assets/b1-45m.manifest.json"
CONTENT_ANCHORS_PATH: Final = "hramatka/qualification/assets/b1-45m.anchors.json"
CONTENT_LINGUISTICS_PATH: Final = "hramatka/qualification/assets/b1-45m.linguistics.json"
CONTENT_DATA_MANIFEST_PATH: Final = "hramatka/engine/data-manifest.json"
REPOSITORY_IDENTITY: Final = "learn-ukrainian-infra-private"
IMPLEMENTATION_PATHS: Final = (
    "hramatka/__init__.py",
    "hramatka/engine",
    "hramatka/contracts",
    "hramatka/vendor",
    "hramatka/api",
    "hramatka/sizing_policy.py",
    "hramatka/qualification/__init__.py",
    "hramatka/qualification/manifest.py",
    "hramatka/qualification/proof_schema_v1.py",
    "hramatka/qualification/proof_capture_v1.py",
    "hramatka/qualification/proof_replay_v1.py",
)
_REQUIRED_IMPLEMENTATION_FILES: Final = frozenset(
    {
        "hramatka/__init__.py",
        "hramatka/sizing_policy.py",
        "hramatka/qualification/__init__.py",
        "hramatka/qualification/manifest.py",
        "hramatka/qualification/proof_schema_v1.py",
        "hramatka/qualification/proof_capture_v1.py",
        "hramatka/qualification/proof_replay_v1.py",
    }
)
_SHA256_RE: Final = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_RE: Final = re.compile(r"^[a-f0-9]{40}$")
_GIT_OBJECT_RE: Final = re.compile(r"^[a-f0-9]{40,64}$")
_OPAQUE_RE: Final = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
_FORBIDDEN_OPAQUE_TOKENS: Final = (
    "forbidden",
    "sentinel",
    "secret",
    "prompt",
    "answer",
    "password",
    "teacher-key",
    "source-text",
    "credential",
)
_FROZEN_DATA_NAMES: Final = frozenset({"vesum.db", "atlas.db", "sources.db"})
_ACTIVITY_TYPES: Final = frozenset(
    {
        "quiz",
        "match-up",
        "cloze",
        "fill-in",
        "error-correction",
        "text-questions",
        "short-writing",
    }
)
_SLOT_ID_RE: Final = re.compile(r"^P[1-3]-A[1-9][0-9]*$")
_MAX_CERTIFICATE_BYTES: Final = 1_000_000
_APPROVED_ORIGIN_RE: Final = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"learn-ukrainian/learn-ukrainian-infra-private(?:\.git)?$"
)


class ProofSchemaError(ValueError):
    """A proof record crossed a strict content-free schema boundary."""


@dataclass(frozen=True)
class ExpectedAuthority:
    """Verifier-supplied policy; certificates never select either axis."""

    implementation_commit: str
    data_digests: Mapping[str, str]

    def __post_init__(self) -> None:
        require_commit(self.implementation_commit)
        if set(self.data_digests) != _FROZEN_DATA_NAMES:
            raise ProofSchemaError("Expected authority must name the exact frozen data inputs.")
        for name, digest in self.data_digests.items():
            if not isinstance(name, str) or _OPAQUE_RE.fullmatch(name) is None:
                raise ProofSchemaError("Expected authority has an invalid data input name.")
            require_sha256(digest, "expected data digest")
        object.__setattr__(self, "data_digests", MappingProxyType(dict(self.data_digests)))


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProofSchemaError(f"{label} must be a SHA-256 digest.")
    return value


def require_commit(value: object) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ProofSchemaError("Proof repository authority must be a full commit digest.")
    return value


def _approved_repository_origin(value: object) -> bool:
    """Accept only canonical HTTPS or SSH spellings of this exact GitHub repo."""
    return isinstance(value, str) and _APPROVED_ORIGIN_RE.fullmatch(value) is not None


def _git(repository: Path, *arguments: str) -> bytes:
    environment = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "core.hooksPath",
        "GIT_CONFIG_VALUE_1": "/dev/null",
        "GIT_CONFIG_KEY_2": "fetch.fsckObjects",
        "GIT_CONFIG_VALUE_2": "true",
        "GIT_CONFIG_KEY_3": "transfer.fsckObjects",
        "GIT_CONFIG_VALUE_3": "true",
    }
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ProofSchemaError("Pinned implementation authority cannot be read locally.")
    return result.stdout


def implementation_inventory_digest(repository: Path, commit: str) -> str:
    """Return a content-addressed complete engine/API/contracts implementation inventory."""
    require_commit(commit)
    root = Path(_git(repository, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if root != repository.resolve():
        raise ProofSchemaError("Implementation authority must use the repository root.")
    common_dir = Path(
        _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
    ).resolve()
    if not common_dir.is_dir():
        raise ProofSchemaError(
            "Implementation authority has an invalid repository common directory."
        )
    if _git(repository, "status", "--porcelain", "--untracked-files=all"):
        raise ProofSchemaError("Implementation authority requires a clean checkout.")
    if _git(repository, "rev-parse", "HEAD").decode().strip() != commit:
        raise ProofSchemaError("Implementation authority commit is not the executing checkout.")
    origin = _git(repository, "remote", "get-url", "origin").decode().strip()
    if not _approved_repository_origin(origin):
        raise ProofSchemaError(
            "Implementation authority repository identity does not match policy."
        )
    raw = _git(repository, "ls-tree", "-r", "-l", "-z", commit, "--", *IMPLEMENTATION_PATHS)
    records: list[dict[str, object]] = []
    for row in raw.split(b"\0"):
        if not row:
            continue
        try:
            header, encoded_path = row.split(b"\t", 1)
            mode, object_type, blob, size = header.decode("ascii").split()
            path = encoded_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProofSchemaError("Implementation authority inventory is malformed.") from exc
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise ProofSchemaError("Implementation authority inventory has an unauthorized entry.")
        if _GIT_OBJECT_RE.fullmatch(blob) is None:
            raise ProofSchemaError("Implementation authority inventory has an invalid blob ID.")
        if not size.isdigit() or int(size) < 0:
            raise ProofSchemaError("Implementation authority inventory has an invalid blob size.")
        if path.startswith("/") or ".." in Path(path).parts or path != Path(path).as_posix():
            raise ProofSchemaError("Implementation authority inventory has a non-canonical path.")
        content = _git(repository, "cat-file", "blob", f"{commit}:{path}")
        if len(content) != int(size):
            raise ProofSchemaError("Implementation authority blob size has drifted.")
        records.append(
            {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": int(size)}
        )
    if not records:
        raise ProofSchemaError("Implementation authority inventory is empty.")
    record_by_path = {str(record["path"]): record for record in records}
    if len(record_by_path) != len(records) or not _REQUIRED_IMPLEMENTATION_FILES <= set(
        record_by_path
    ):
        raise ProofSchemaError(
            "Implementation authority inventory has missing or duplicate surfaces."
        )
    for name, module in tuple(sys.modules.items()):
        if not name.startswith("hramatka"):
            continue
        source = getattr(module, "__file__", None)
        if source is None:
            continue
        module_path = Path(source).resolve()
        try:
            relative = module_path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ProofSchemaError(
                "Loaded Hramatka module is outside the executing checkout."
            ) from exc
        if relative.endswith(".pyc"):
            relative = relative[:-1]
        record = record_by_path.get(relative)
        # The proof authority closes only the explicitly inventoried execution
        # surface; unrelated qualification harness modules may be loaded by a
        # test runner without becoming implementation authority.
        if record is None:
            continue
        if not module_path.is_file():
            raise ProofSchemaError("Loaded Hramatka module is outside the authority inventory.")
        if hashlib.sha256(module_path.read_bytes()).hexdigest() != record["sha256"]:
            raise ProofSchemaError("Loaded Hramatka module bytes do not match Git authority.")
    return sha256({"repository": REPOSITORY_IDENTITY, "commit": commit, "files": records})


def _pinned_json_blob(repository: Path, path: str) -> Mapping[str, object]:
    raw = _git(repository, "cat-file", "blob", f"{CONTENT_BASELINE_COMMIT}:{path}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProofSchemaError("Frozen content authority object is malformed.") from exc
    if not isinstance(value, dict):
        raise ProofSchemaError("Frozen content authority object must be a mapping.")
    return value


def frozen_content_authority(repository: Path) -> tuple[str, Mapping[str, Mapping[str, object]]]:
    """Read the closed content/data authority from the immutable base commit.

    The resulting digest map comes from Git objects, never a caller's bundle
    or environment-selected manifest.
    """
    manifest_raw = _git(
        repository, "cat-file", "blob", f"{CONTENT_BASELINE_COMMIT}:{CONTENT_MANIFEST_PATH}"
    )
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    if manifest_digest != CONTENT_MANIFEST_DIGEST:
        raise ProofSchemaError("Frozen qualification manifest object has drifted.")
    # Every source/linguistic/data policy object is read from the frozen Git
    # tree.  Live import resources are compared later, never treated as policy.
    anchors_value = _pinned_json_blob(repository, CONTENT_ANCHORS_PATH)
    linguistics_value = _pinned_json_blob(repository, CONTENT_LINGUISTICS_PATH)
    if set(anchors_value) != {"anchors"} or not isinstance(anchors_value["anchors"], list):
        raise ProofSchemaError("Frozen anchor authority has an invalid key set.")
    if set(linguistics_value) != {"atlas_rows", "schema_version", "source_bundle", "vesum_forms"}:
        raise ProofSchemaError("Frozen linguistics authority has an invalid key set.")
    if (
        not isinstance(linguistics_value["atlas_rows"], list)
        or not isinstance(linguistics_value["vesum_forms"], list)
        or not isinstance(linguistics_value["schema_version"], str)
        or not isinstance(linguistics_value["source_bundle"], dict)
    ):
        raise ProofSchemaError("Frozen linguistics authority has invalid rows.")
    source_bundle = linguistics_value["source_bundle"]
    if set(source_bundle) != {
        "version",
        "content_sha256",
        "manifest_sha256",
        "vesum_sha256",
        "atlas_sha256",
    }:
        raise ProofSchemaError("Frozen linguistics source bundle has an invalid key set.")
    if not isinstance(source_bundle["version"], str):
        raise ProofSchemaError("Frozen linguistics source bundle version is invalid.")
    for key in ("content_sha256", "manifest_sha256", "vesum_sha256", "atlas_sha256"):
        require_sha256(source_bundle[key], "frozen linguistics source digest")
    data_value = _pinned_json_blob(repository, CONTENT_DATA_MANIFEST_PATH)
    try:
        inputs = data_value["inputs"]
    except (TypeError, KeyError) as exc:
        raise ProofSchemaError("Frozen data authority manifest is malformed.") from exc
    if set(data_value) != {"bundle", "version", "description", "release", "inputs"}:
        raise ProofSchemaError("Frozen data authority manifest has an invalid key set.")
    if not isinstance(inputs, dict) or set(inputs) != {"vesum.db", "atlas.db", "sources.db"}:
        raise ProofSchemaError("Frozen data authority manifest has no inputs.")
    policy: dict[str, Mapping[str, object]] = {}
    for name, row in inputs.items():
        if not isinstance(name, str) or not isinstance(row, dict):
            raise ProofSchemaError("Frozen data authority manifest has invalid input rows.")
        if set(row) != {"path", "sha256", "size", "role", "required"}:
            raise ProofSchemaError("Frozen data authority manifest has an invalid input key set.")
        digest = require_sha256(row.get("sha256"), "frozen data digest")
        path = row.get("path")
        size = row.get("size")
        if (
            path != name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(row.get("role"), str)
            or not isinstance(row.get("required"), bool)
        ):
            raise ProofSchemaError("Frozen data authority manifest has invalid input metadata.")
        policy[name] = MappingProxyType({"path": path, "sha256": digest, "size": size})
    return manifest_digest, MappingProxyType(dict(sorted(policy.items())))


def _require_structural(value: object, label: str) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int) and value >= 0:
        return
    if isinstance(value, str) and _OPAQUE_RE.fullmatch(value) is not None:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _OPAQUE_RE.fullmatch(key) is None:
                raise ProofSchemaError(f"{label} has an invalid structural key.")
            _require_structural(item, label)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for item in value:
            _require_structural(item, label)
        return
    raise ProofSchemaError(f"{label} contains a non-structural value.")


def _mapping(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProofSchemaError(f"{label} has an invalid key set.")
    return value


def _opaque_field(value: object, label: str, *, field: str) -> str:
    """Validate a structural reference without accepting content-like payloads."""
    if (
        not isinstance(value, str)
        or _OPAQUE_RE.fullmatch(value) is None
        or len(value) < 3
        or len(set(value.replace("-", "").replace("_", ""))) < 2
        or any(token in value.casefold() for token in _FORBIDDEN_OPAQUE_TOKENS)
    ):
        raise ProofSchemaError(f"{label} must be an opaque {field} reference.")
    return value


def _bank_id(value: object, label: str) -> str:
    result = _opaque_field(value, label, field="bank")
    activity, separator, group = result.rpartition(":")
    if separator != ":" or activity not in _ACTIVITY_TYPES or not group.isdigit() or int(group) < 1:
        raise ProofSchemaError(f"{label} must identify an authorized bank.")
    return result


def _activity_type(value: object, label: str) -> str:
    result = _opaque_field(value, label, field="activity")
    if result not in _ACTIVITY_TYPES:
        raise ProofSchemaError(f"{label} must identify an authorized activity.")
    return result


def _slot_id(value: object, label: str) -> str:
    result = _opaque_field(value, label, field="slot")
    if _SLOT_ID_RE.fullmatch(result) is None:
        raise ProofSchemaError(f"{label} must identify an authorized slot.")
    return result


def _resource_id(value: object, label: str) -> str:
    return _opaque_field(value, label, field="resource")


def _locator(value: object, label: str) -> str:
    return _opaque_field(value, label, field="locator")


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        raise ProofSchemaError(f"{label} must be a list.")
    return list(value)


def _validate_bank_receipts(rows: object) -> None:
    seen_banks: set[str] = set()
    seals: set[str] = set()
    for row in _list(rows, "bank receipts"):
        receipt = _mapping(
            row,
            {
                "version",
                "bank_id",
                "activity_type",
                "response_demand_tier",
                "candidate_ids",
                "count",
                "candidate_offsets",
                "candidate_provenance",
                "eligible_placements",
                "origin_kind",
                "origin_commitment",
                "authority_digest",
                "manifest_digest",
                "source_identity",
                "inventory_seal",
            },
            "bank receipt",
        )
        if receipt["version"] != "HramatkaCandidateBankReceipt.v1":
            raise ProofSchemaError("Bank receipt has an unsupported version.")
        bank_id = _bank_id(receipt["bank_id"], "bank receipt id")
        if bank_id in seen_banks:
            raise ProofSchemaError("Bank receipts must have unique bank identities.")
        seen_banks.add(bank_id)
        _activity_type(receipt["activity_type"], "bank receipt activity")
        if receipt["response_demand_tier"] not in {
            "selected-response",
            "bounded-production",
            "source-grounded-open-response",
            "extended-writing",
        }:
            raise ProofSchemaError("Bank receipt has an unknown response-demand tier.")
        candidate_ids = [
            _resource_id(item, "bank candidate id")
            for item in _list(receipt["candidate_ids"], "bank candidate ids")
        ]
        if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
            raise ProofSchemaError("Bank receipt must have unique candidate membership.")
        if (
            not isinstance(receipt["count"], int)
            or isinstance(receipt["count"], bool)
            or receipt["count"] != len(candidate_ids)
        ):
            raise ProofSchemaError("Bank receipt count does not cover candidate membership.")
        offsets = _list(receipt["candidate_offsets"], "bank candidate offsets")
        if len(offsets) != len(candidate_ids):
            raise ProofSchemaError("Bank receipt offsets do not cover candidate membership.")
        for offset, item in enumerate(offsets):
            entry = _mapping(item, {"candidate_id", "unit_offset"}, "bank candidate offset")
            if entry["candidate_id"] != candidate_ids[offset] or entry["unit_offset"] != offset:
                raise ProofSchemaError("Bank receipt offsets are not canonical.")
        provenance = _list(receipt["candidate_provenance"], "bank candidate provenance")
        if len(provenance) != len(candidate_ids):
            raise ProofSchemaError("Bank receipt provenance does not cover candidate membership.")
        for offset, item in enumerate(provenance):
            entry = _mapping(item, {"candidate_id", "commitment"}, "bank candidate provenance")
            if entry["candidate_id"] != candidate_ids[offset]:
                raise ProofSchemaError("Bank receipt provenance is not canonical.")
            require_sha256(entry["commitment"], "bank candidate provenance")
        placements = _list(receipt["eligible_placements"], "bank placements")
        if not placements:
            raise ProofSchemaError("Bank receipt needs eligible placements.")
        for item in placements:
            entry = _mapping(item, {"slot_id", "mutually_exclusive"}, "bank placement")
            _slot_id(entry["slot_id"], "bank placement slot")
            if not isinstance(entry["mutually_exclusive"], bool):
                raise ProofSchemaError("Bank placement exclusivity must be boolean.")
        if receipt["origin_kind"] != "deterministic-local-inventory":
            raise ProofSchemaError("Bank receipt has an unauthorized origin.")
        for label in ("origin_commitment", "authority_digest", "manifest_digest"):
            require_sha256(receipt[label], f"bank receipt {label}")
        require_sha256(receipt["inventory_seal"], "bank receipt inventory seal")
        seals.add(receipt["inventory_seal"])
        _resource_id(receipt["source_identity"], "bank receipt source identity")
    if not seen_banks:
        raise ProofSchemaError("Proof certificate requires complete bank receipts.")
    if len(seals) != 1:
        raise ProofSchemaError("Proof bank receipts must share one complete-inventory seal.")


def _validate_units(value: object, label: str) -> None:
    units = _list(value, label)
    if not units:
        raise ProofSchemaError(f"{label} must not be empty.")
    seen: set[str] = set()
    for row in units:
        unit = _mapping(
            row,
            {"unit_id", "claims", "reservations", "locators", "plan_digest"},
            "proof unit",
        )
        unit_id = _resource_id(unit["unit_id"], "proof unit id")
        if unit_id in seen:
            raise ProofSchemaError("Proof units must be unique within a slot.")
        seen.add(unit_id)
        claims = _list(unit["claims"], "proof unit claims")
        locators = _list(unit["locators"], "proof unit locators")
        if not claims or not locators:
            raise ProofSchemaError("Proof units need claims and locators.")
        for claim in claims:
            entry = _mapping(claim, {"kind", "resource_id"}, "proof claim")
            _resource_id(entry["kind"], "proof claim kind")
            _resource_id(entry["resource_id"], "proof claim resource")
        reservations = _list(unit["reservations"], "proof unit reservations")
        if reservations != claims:
            raise ProofSchemaError("Proof unit reservations must exactly mirror claims.")
        for locator in locators:
            _locator(locator, "proof locator")
        if unit["plan_digest"] != sha256(
            {"unit_id": unit_id, "claims": claims, "locators": locators}
        ):
            raise ProofSchemaError("Proof unit plan digest does not bind its geometry.")


def _validate_geometry(allocation: object, witness: object) -> None:
    allocation_map = _mapping(
        allocation, {"paragraph_ids", "slots", "canonical_allocation_digest"}, "allocation"
    )
    paragraphs = _list(allocation_map["paragraph_ids"], "allocation paragraph ids")
    if len(paragraphs) != 1:
        raise ProofSchemaError("Allocation must witness exactly one anchor paragraph.")
    _resource_id(paragraphs[0], "allocation paragraph id")
    slots = _list(allocation_map["slots"], "allocation slots")
    if len(slots) != 6:
        raise ProofSchemaError("Allocation must witness the complete six-slot lesson.")
    slot_ids: list[str] = []
    for row in slots:
        slot = _mapping(
            row,
            {
                "slot_id",
                "phase",
                "requested_type",
                "scheduled_type",
                "substitution_reason",
                "units",
                "conditional_replacements",
            },
            "allocation slot",
        )
        slot_ids.append(_slot_id(slot["slot_id"], "allocation slot id"))
        if slot["phase"] not in {1, 2, 3}:
            raise ProofSchemaError("Allocation slot has an invalid phase.")
        _activity_type(slot["requested_type"], "allocation requested type")
        _activity_type(slot["scheduled_type"], "allocation scheduled type")
        if slot["substitution_reason"] is not None:
            _resource_id(slot["substitution_reason"], "allocation substitution reason")
        _validate_units(slot["units"], "allocation slot units")
        for replacement in _list(slot["conditional_replacements"], "conditional replacements"):
            replacement_map = _mapping(
                replacement, {"activity_type", "units"}, "conditional replacement"
            )
            _activity_type(replacement_map["activity_type"], "replacement activity type")
            _validate_units(replacement_map["units"], "conditional replacement units")
    if len(slot_ids) != len(set(slot_ids)):
        raise ProofSchemaError("Allocation slots must be unique.")
    require_sha256(allocation_map["canonical_allocation_digest"], "allocation digest")
    witness_map = _mapping(
        witness, {"rows", "eligible_replacement_edges", "canonical_allocation_digest"}, "witness"
    )
    rows = _list(witness_map["rows"], "witness rows")
    if len(rows) != len(slot_ids):
        raise ProofSchemaError("Witness must cover every allocation slot.")
    if witness_map["canonical_allocation_digest"] != allocation_map["canonical_allocation_digest"]:
        raise ProofSchemaError("Witness allocation digest does not match allocation.")
    witness_ids: list[str] = []
    for index, row in enumerate(rows):
        witness_row = _mapping(row, {"slot_id", "phase", "activity_type", "units"}, "witness row")
        witness_ids.append(_slot_id(witness_row["slot_id"], "witness slot id"))
        if witness_row["phase"] not in {1, 2, 3}:
            raise ProofSchemaError("Witness row has an invalid phase.")
        _activity_type(witness_row["activity_type"], "witness activity type")
        _validate_units(witness_row["units"], "witness units")
        allocation_slot = _mapping(
            slots[index],
            {
                "slot_id",
                "phase",
                "requested_type",
                "scheduled_type",
                "substitution_reason",
                "units",
                "conditional_replacements",
            },
            "allocation slot",
        )
        if (
            witness_row["phase"] != allocation_slot["phase"]
            or witness_row["activity_type"] != allocation_slot["scheduled_type"]
            or witness_row["units"] != allocation_slot["units"]
        ):
            raise ProofSchemaError("Witness rows must exactly mirror allocation geometry.")
    if witness_ids != slot_ids:
        raise ProofSchemaError("Witness slots must exactly match allocation slots.")
    edges = _list(witness_map["eligible_replacement_edges"], "witness replacement edges")
    if len(edges) != len(slot_ids):
        raise ProofSchemaError("Witness replacement edges must cover every slot.")
    if [
        _slot_id(
            _mapping(edge, {"slot_id", "types"}, "replacement edge")["slot_id"],
            "replacement edge slot",
        )
        for edge in edges
    ] != slot_ids:
        raise ProofSchemaError("Witness replacement edges must match allocation slots.")
    for index, edge in enumerate(edges):
        for activity_type in _list(
            _mapping(edge, {"slot_id", "types"}, "replacement edge")["types"],
            "replacement edge types",
        ):
            _activity_type(activity_type, "replacement edge type")
        allocation_slot = _mapping(
            slots[index],
            {
                "slot_id",
                "phase",
                "requested_type",
                "scheduled_type",
                "substitution_reason",
                "units",
                "conditional_replacements",
            },
            "allocation slot",
        )
        expected_types = [
            _mapping(item, {"activity_type", "units"}, "conditional replacement")["activity_type"]
            for item in _list(
                allocation_slot["conditional_replacements"], "conditional replacements"
            )
        ]
        actual_types = _list(
            _mapping(edge, {"slot_id", "types"}, "replacement edge")["types"],
            "replacement edge types",
        )
        if actual_types != expected_types:
            raise ProofSchemaError(
                "Witness replacement edges must mirror conditional replacements."
            )


_COMMITMENT_DOMAINS: Final = frozenset(
    {
        "candidate",
        "provenance",
        "bank",
        "placement",
        "slot",
        "unit",
        "claim",
        "locator",
        "plan",
        "reservation",
        "allocation",
        "replacement",
        "witness",
    }
)


def _declared_domain_commitments(
    receipts: Sequence[Mapping[str, object]],
    allocation: Mapping[str, object],
    witness: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    slots = _list(allocation["slots"], "allocation slots")
    units = [
        unit
        for slot in slots
        for unit in _list(
            _mapping(
                slot,
                {
                    "slot_id",
                    "phase",
                    "requested_type",
                    "scheduled_type",
                    "substitution_reason",
                    "units",
                    "conditional_replacements",
                },
                "allocation slot",
            )["units"],
            "allocation slot units",
        )
    ]
    claims = [
        claim
        for unit in units
        for claim in _list(
            _mapping(
                unit,
                {"unit_id", "claims", "reservations", "locators", "plan_digest"},
                "proof unit",
            )["claims"],
            "proof unit claims",
        )
    ]
    locators = [
        locator
        for unit in units
        for locator in _list(
            _mapping(
                unit,
                {"unit_id", "claims", "reservations", "locators", "plan_digest"},
                "proof unit",
            )["locators"],
            "proof unit locators",
        )
    ]
    replacements = [
        replacement
        for slot in slots
        for replacement in _list(
            _mapping(
                slot,
                {
                    "slot_id",
                    "phase",
                    "requested_type",
                    "scheduled_type",
                    "substitution_reason",
                    "units",
                    "conditional_replacements",
                },
                "allocation slot",
            )["conditional_replacements"],
            "conditional replacements",
        )
    ]
    domains = {
        "candidate": [
            {
                "bank_id": receipt["bank_id"],
                "candidate_id": candidate,
                "offset": offset,
            }
            for receipt in receipts
            for offset, candidate in enumerate(
                _list(receipt["candidate_ids"], "bank candidate ids")
            )
        ],
        "provenance": [
            {
                "bank_id": receipt["bank_id"],
                "candidate_id": candidate,
                "commitment": _mapping(
                    _list(receipt["candidate_provenance"], "bank candidate provenance")[offset],
                    {"candidate_id", "commitment"},
                    "bank candidate provenance",
                )["commitment"],
            }
            for receipt in receipts
            for offset, candidate in enumerate(
                _list(receipt["candidate_ids"], "bank candidate ids")
            )
        ],
        "bank": [receipt["bank_id"] for receipt in receipts],
        "placement": [
            {
                "bank_id": receipt["bank_id"],
                "slot_id": placement["slot_id"],
                "exclusive": placement["mutually_exclusive"],
            }
            for receipt in receipts
            for placement in _list(receipt["eligible_placements"], "bank placements")
        ],
        "slot": [{"slot_id": slot["slot_id"], "phase": slot["phase"]} for slot in slots],
        "unit": [unit["unit_id"] for unit in units],
        "claim": claims,
        "locator": locators,
        "plan": [
            {"unit_id": unit["unit_id"], "digest": unit["plan_digest"]} for unit in units
        ],
        "reservation": [
            reservation
            for unit in units
            for reservation in _list(
                _mapping(
                    unit,
                    {"unit_id", "claims", "reservations", "locators", "plan_digest"},
                    "proof unit",
                )["reservations"],
                "proof unit reservations",
            )
        ],
        "allocation": [
            {
                "slot_id": slot["slot_id"],
                "requested_type": slot["requested_type"],
                "scheduled_type": slot["scheduled_type"],
                "substitution_reason": slot["substitution_reason"],
            }
            for slot in slots
        ],
        "replacement": replacements,
        "witness": _list(witness["rows"], "witness rows"),
    }
    return {name: {"count": len(rows), "digest": sha256(rows)} for name, rows in domains.items()}


def _validate_domain_commitments(
    value: object,
    receipts: Sequence[Mapping[str, object]],
    allocation: Mapping[str, object],
    witness: Mapping[str, object],
) -> None:
    rows = _mapping(value, set(_COMMITMENT_DOMAINS), "domain commitments")
    for domain in _COMMITMENT_DOMAINS:
        row = _mapping(rows[domain], {"count", "digest"}, "domain commitment")
        if not isinstance(row["count"], int) or isinstance(row["count"], bool) or row["count"] < 0:
            raise ProofSchemaError("Domain commitment count is invalid.")
        require_sha256(row["digest"], "domain commitment digest")
    if _thaw(rows) != _declared_domain_commitments(receipts, allocation, witness):
        raise ProofSchemaError("Domain commitments do not cover declared proof geometry.")


@dataclass(frozen=True)
class ProofCertificate:
    """One exact-anchor proof record; it carries no rendered lesson content."""

    anchor_id: str
    manifest_digest: str
    source_identity: str
    repository_commit: str
    profile_digest: str
    data_digests: Mapping[str, str]
    implementation_inventory_digest: str
    inventory_commitment: str
    bank_receipts: tuple[Mapping[str, object], ...]
    allocation: Mapping[str, object]
    witness: Mapping[str, object]
    domain_commitments: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.anchor_id not in {"b1-narrative", "b1-dialogue", "b1-informational"}:
            raise ProofSchemaError("Proof certificate has an unknown manifest anchor.")
        for label, value in (
            ("manifest digest", self.manifest_digest),
            ("profile digest", self.profile_digest),
            ("inventory commitment", self.inventory_commitment),
        ):
            require_sha256(value, label)
        require_commit(self.repository_commit)
        _resource_id(self.source_identity, "proof source identity")
        if not self.data_digests:
            raise ProofSchemaError("Proof certificate needs pinned data digests.")
        for name, digest in self.data_digests.items():
            _resource_id(name, "proof data name")
            require_sha256(digest, "data digest")
        require_sha256(self.implementation_inventory_digest, "implementation inventory digest")
        _validate_bank_receipts(self.bank_receipts)
        _validate_geometry(self.allocation, self.witness)
        _validate_domain_commitments(
            self.domain_commitments, self.bank_receipts, self.allocation, self.witness
        )
        object.__setattr__(self, "data_digests", _freeze(self.data_digests))
        object.__setattr__(self, "bank_receipts", _freeze(self.bank_receipts))
        object.__setattr__(self, "allocation", _freeze(self.allocation))
        object.__setattr__(self, "witness", _freeze(self.witness))
        object.__setattr__(self, "domain_commitments", _freeze(self.domain_commitments))

    def to_dict(self) -> dict[str, object]:
        return {
            "version": SCHEMA_VERSION,
            "anchor_id": self.anchor_id,
            "manifest_digest": self.manifest_digest,
            "source_identity": self.source_identity,
            "repository_commit": self.repository_commit,
            "profile_digest": self.profile_digest,
            "data_digests": _thaw(self.data_digests),
            "implementation_inventory_digest": self.implementation_inventory_digest,
            "inventory_commitment": self.inventory_commitment,
            "bank_receipts": _thaw(self.bank_receipts),
            "allocation": _thaw(self.allocation),
            "witness": _thaw(self.witness),
            "domain_commitments": _thaw(self.domain_commitments),
        }

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return sha256(self.to_dict())

    @classmethod
    def from_bytes(cls, raw: bytes) -> ProofCertificate:
        if not isinstance(raw, bytes) or len(raw) > _MAX_CERTIFICATE_BYTES:
            raise ProofSchemaError("Proof certificate has an invalid byte envelope.")
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProofSchemaError("Proof certificate is not canonical JSON.") from exc
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "version",
                "anchor_id",
                "manifest_digest",
                "source_identity",
                "repository_commit",
                "profile_digest",
                "data_digests",
                "implementation_inventory_digest",
                "inventory_commitment",
                "bank_receipts",
                "allocation",
                "witness",
                "domain_commitments",
            }
            or value.get("version") != SCHEMA_VERSION
        ):
            raise ProofSchemaError("Proof certificate has an invalid schema.")
        for key in ("data_digests", "allocation", "witness"):
            if not isinstance(value[key], dict):
                raise ProofSchemaError("Proof certificate has a malformed mapping.")
        if not isinstance(value["bank_receipts"], list):
            raise ProofSchemaError("Proof certificate has malformed bank receipts.")
        return cls(
            anchor_id=value["anchor_id"],
            manifest_digest=value["manifest_digest"],
            source_identity=value["source_identity"],
            repository_commit=value["repository_commit"],
            profile_digest=value["profile_digest"],
            data_digests=value["data_digests"],
            implementation_inventory_digest=value["implementation_inventory_digest"],
            inventory_commitment=value["inventory_commitment"],
            bank_receipts=tuple(value["bank_receipts"]),
            allocation=value["allocation"],
            witness=value["witness"],
            domain_commitments=value["domain_commitments"],
        )


def authority_envelope(raw: bytes) -> tuple[str, str, str]:
    """Parse only the authority fields needed before expensive geometry parsing.

    This intentionally does not construct a certificate: replay can reject a
    certificate-selected commit/baseline without trusting its declared rows.
    """
    if not isinstance(raw, bytes) or len(raw) > _MAX_CERTIFICATE_BYTES:
        raise ProofSchemaError("Proof certificate has an invalid byte envelope.")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProofSchemaError("Proof certificate is not canonical JSON.") from exc
    if not isinstance(value, dict) or value.get("version") != SCHEMA_VERSION:
        raise ProofSchemaError("Proof certificate has an invalid authority envelope.")
    commit = require_commit(value.get("repository_commit"))
    manifest = require_sha256(value.get("manifest_digest"), "manifest digest")
    profile = require_sha256(value.get("profile_digest"), "profile digest")
    return commit, manifest, profile


def _freeze(value: object) -> object:
    """Detach and recursively freeze validated certificate state."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
