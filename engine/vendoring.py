"""Digest-verifying loader for the pinned public artifacts under `hramatka/vendor/`.

Direction-of-flow rule (Sol separation review §Item 1): the private engine reads
public inputs ONLY from `hramatka/vendor/<artifact>@<version>/`, and every file's
sha256 is verified against its `MANIFEST.json` *before* the bytes are used. A
mismatch (tamper, truncation, wrong version) raises `VendorIntegrityError` — the
engine never silently runs on an unpinned or altered artifact.

Nothing here touches a public checkout, `sys.path`, or a network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

VENDOR_ROOT = Path(__file__).resolve().parents[1] / "vendor"

# Logical artifact -> pinned `<artifact>@<version>` directory name. Bumping a
# version is a one-line change here plus a manifest refresh + review.
LU_ACTIVITY = "lu.activity.v1@1.0.0"
LU_LESSON = "lu.lesson.v1@1.0.0"
PILOT_LU_ACTIVITY = "lu.activity.v1@1.0.0-ffd54054"
# 1.3.0 adds the required engine quality flag on every block plus the
# contentless flagged-notice rejected-tray shape (#402 flag-don't-drop).  It is
# a private-first adapted pin derived from the verbatim 1.2.0 schema, pending
# upstream publication.  A 1.2.0-pinned validator must reject a 1.3.0 document
# (additive-but-not-silently-compatible).  Older pins remain on disk as
# immutable provenance for recorded bake fingerprints.
PILOT_LU_LESSON = "lu.lesson.v1@1.3.0"
LINGUISTICS = "learn_ukrainian_linguistics@1.0.0"
TRAILSPEC_V1 = "trailspec.v1@1.0.0-1356e7c4"
STEP_RECEIPT_V1 = "step-receipt.v1@1.0.0-1356e7c4"
V4_RUNTIME = "learn_ukrainian_v4_runtime@v1-8d5a24c06bf397f6ea7faaa7ec5a03e822909098"

REGISTERED = (
    LU_ACTIVITY,
    LU_LESSON,
    PILOT_LU_ACTIVITY,
    PILOT_LU_LESSON,
    LINGUISTICS,
    TRAILSPEC_V1,
    STEP_RECEIPT_V1,
    V4_RUNTIME,
)

_module_cache: dict[str, tuple[str, ModuleType]] = {}


class VendorIntegrityError(RuntimeError):
    """A vendored file is missing, or its sha256 does not match MANIFEST.json."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_dir(artifact: str) -> Path:
    d = VENDOR_ROOT / artifact
    if not d.is_dir():
        raise VendorIntegrityError(f"Vendored artifact directory missing: {d}")
    return d


def verify_artifact(artifact: str) -> dict:
    """Verify every file recorded in an artifact's manifest. Returns the manifest.

    Raises `VendorIntegrityError` on a missing file or a sha256/size mismatch.
    Always recomputes — never trusts a cache — so it is a real integrity gate.
    """
    if artifact == V4_RUNTIME:
        from .v4_runtime_vendor import verify_vendor

        return verify_vendor()
    d = _artifact_dir(artifact)
    manifest_path = d / "MANIFEST.json"
    if not manifest_path.is_file():
        raise VendorIntegrityError(f"MANIFEST.json missing for artifact {artifact}.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise VendorIntegrityError(f"{artifact} MANIFEST.json records no files.")
    for name, meta in files.items():
        fp = d / name
        if not fp.is_file():
            raise VendorIntegrityError(f"{artifact}: pinned file '{name}' is missing.")
        data = fp.read_bytes()
        actual = _sha256(data)
        if actual != meta.get("sha256"):
            raise VendorIntegrityError(
                f"{artifact}: '{name}' sha256 mismatch — pinned "
                f"{meta.get('sha256')}, found {actual}. Refusing to use an "
                "unpinned/altered artifact."
            )
        if "size" in meta and len(data) != meta["size"]:
            raise VendorIntegrityError(
                f"{artifact}: '{name}' size mismatch — pinned {meta['size']}, "
                f"found {len(data)}."
            )
    return manifest


def verify_all() -> dict[str, dict]:
    """Verify every registered artifact (startup / preflight / CI hook)."""
    return {artifact: verify_artifact(artifact) for artifact in REGISTERED}


def verified_path(artifact: str, filename: str) -> Path:
    """Path to a vendored file, only after its artifact digests verify."""
    verify_artifact(artifact)
    return _artifact_dir(artifact) / filename


def read_json(artifact: str, filename: str) -> Any:
    """Parse a vendored JSON file after digest verification."""
    return json.loads(verified_path(artifact, filename).read_text(encoding="utf-8"))


def load_module(artifact: str, filename: str, module_name: str) -> ModuleType:
    """Import a vendored Python module by path, after verifying its digest.

    Cached by the file's current sha256, so a re-verified (unchanged) module is
    not re-executed, but any digest change both raises in `verify_artifact` and
    would bust the cache.
    """
    manifest = verify_artifact(artifact)
    sha = manifest["files"][filename]["sha256"]
    cached = _module_cache.get(artifact)
    if cached is not None and cached[0] == sha:
        return cached[1]
    fp = _artifact_dir(artifact) / filename
    spec = importlib.util.spec_from_file_location(module_name, fp)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery
        raise VendorIntegrityError(f"Cannot load vendored module {fp}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _module_cache[artifact] = (sha, module)
    return module


def artifact_versions() -> dict[str, dict]:
    """Compact {artifact: {version, files:{name: sha256}}} for the bake fingerprint."""
    out: dict[str, dict] = {}
    for artifact in REGISTERED:
        manifest = verify_artifact(artifact)
        out[artifact] = {
            "version": manifest.get("version"),
            "files": {n: m["sha256"] for n, m in manifest["files"].items()},
        }
    return out
