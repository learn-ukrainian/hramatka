"""Immutable, content-free B1 production-qualification manifest."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Final

_SHA256_RE: Final = re.compile(r"^[a-f0-9]{64}$")
_ANCHOR_IDS: Final = ("b1-narrative", "b1-dialogue", "b1-morphology")
_MANIFEST_NAME: Final = "b1-45m.manifest.json"


class ManifestError(ValueError):
    """The immutable qualification manifest or runtime input is invalid."""


@dataclass(frozen=True)
class ManifestAnchor:
    id: str
    source_identity: str
    sha256: str


@dataclass(frozen=True)
class RuntimeAnchor:
    """Runtime-only source input.  Its text is never included in a receipt."""

    id: str
    source_identity: str
    text: str


@dataclass(frozen=True)
class QualificationManifest:
    version: str
    level: str
    duration_minutes: int
    anchors: tuple[ManifestAnchor, ...]
    sha256: str

    def validate_runtime_anchors(
        self, anchors: Mapping[str, RuntimeAnchor]
    ) -> tuple[RuntimeAnchor, ...]:
        """Fail closed unless the exact manifest inputs hash-match at runtime."""
        if set(anchors) != set(_ANCHOR_IDS):
            raise ManifestError("Runtime anchors must match the manifest anchor IDs exactly.")
        validated: list[RuntimeAnchor] = []
        for expected in self.anchors:
            runtime = anchors.get(expected.id)
            if runtime is None or runtime.id != expected.id:
                raise ManifestError("Runtime anchor identity does not match the manifest.")
            if runtime.source_identity != expected.source_identity:
                raise ManifestError("Runtime anchor source identity does not match the manifest.")
            if not isinstance(runtime.text, str) or not runtime.text.strip():
                raise ManifestError("Runtime anchor text must be non-empty.")
            actual = hashlib.sha256(runtime.text.encode("utf-8")).hexdigest()
            if actual != expected.sha256:
                raise ManifestError("Runtime anchor SHA-256 does not match the manifest.")
            validated.append(runtime)
        return tuple(validated)


def _require_exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ManifestError(f"{label} has an invalid schema.")


def load_manifest() -> QualificationManifest:
    """Load and strictly validate the versioned B1 45-minute contract."""
    resource = files(__package__).joinpath("assets", _MANIFEST_NAME)
    raw = resource.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:  # pragma: no cover - committed asset
        raise ManifestError("Qualification manifest is not valid JSON.") from error
    if not isinstance(value, dict):
        raise ManifestError("Qualification manifest must be a JSON object.")
    _require_exact_keys(value, {"anchors", "duration_minutes", "level", "version"}, "manifest")
    if value["version"] != "ProductionQualificationManifest.v1":
        raise ManifestError("Qualification manifest version is unsupported.")
    if value["level"] != "B1" or value["duration_minutes"] != 45:
        raise ManifestError("Qualification manifest must describe B1 at 45 minutes.")
    rows = value["anchors"]
    if not isinstance(rows, list) or len(rows) != len(_ANCHOR_IDS):
        raise ManifestError("Qualification manifest must contain exactly three anchors.")
    anchors: list[ManifestAnchor] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ManifestError("Qualification manifest anchor has an invalid schema.")
        _require_exact_keys(row, {"id", "sha256", "source_identity"}, "manifest anchor")
        anchor_id = row["id"]
        source_identity = row["source_identity"]
        digest = row["sha256"]
        if (
            not isinstance(anchor_id, str)
            or not isinstance(source_identity, str)
            or not source_identity
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise ManifestError("Qualification manifest anchor has invalid metadata.")
        anchors.append(ManifestAnchor(anchor_id, source_identity, digest))
    if tuple(anchor.id for anchor in anchors) != _ANCHOR_IDS:
        raise ManifestError("Qualification manifest anchor ordering or IDs are invalid.")
    return QualificationManifest(
        version=value["version"],
        level=value["level"],
        duration_minutes=value["duration_minutes"],
        anchors=tuple(anchors),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
