"""External release anchor; verify bytes before importing the public runtime.

The pin belongs to the reviewed private release, not to a supplied wheel or
installed package. No runtime resource, request or environment selects it.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import json
import sys
import zipfile
from pathlib import Path

from . import vendoring

PACKAGE = "learn_ukrainian_v4_runtime"
PUBLIC_COMMIT = "8184cb848d4e5d6cad659232854f019af7175ea9"
ARTIFACT = f"{PACKAGE}@v1-{PUBLIC_COMMIT}"
MANIFEST_SHA256 = "344db32d8bfecf351034fecc208aedda5901b36405ce558d9cd337c5d55dca23"
_verified_root: Path | None = None


def _refuse() -> None:
    raise vendoring.VendorIntegrityError("v4_release_integrity_failed")


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def verify_vendor() -> dict:
    """Verify the immutable manifest, entire wheel and complete member set."""
    try:
        directory = vendoring.VENDOR_ROOT / ARTIFACT
        manifest_path = directory / "MANIFEST.json"
        if directory.is_symlink() or manifest_path.is_symlink():
            _refuse()
        raw = manifest_path.read_bytes()
        if _hash(raw) != MANIFEST_SHA256:
            _refuse()
        manifest = json.loads(raw)
        if {p.name for p in directory.iterdir()} != {"MANIFEST.json", *manifest["files"]}:
            _refuse()
        for name, metadata in manifest["files"].items():
            wheel = directory / name
            if wheel.is_symlink() or not wheel.is_file():
                _refuse()
            raw = wheel.read_bytes()
            if (
                len(raw) != metadata["size"]
                or _hash(raw) != metadata["sha256"]
            ):
                _refuse()
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)) or set(names) != set(manifest["wheel_files"]):
                    _refuse()
                for member, expected in manifest["wheel_files"].items():
                    if _hash(archive.read(member)) != expected:
                        _refuse()
        return manifest
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        _refuse()


def verify_installed() -> dict:
    """Discover without importing, then verify every installed product byte.

    Install with --no-compile and run with PYTHONDONTWRITEBYTECODE=1. Refusing
    bytecode prevents an unchecked timestamp-valid cache from replacing source.
    Pip-owned dist-info bookkeeping is not executable product code; immutable
    METADATA/WHEEL and every other non-RECORD wheel member still have to match.
    """
    global _verified_root
    manifest = verify_vendor()
    try:
        spec = importlib.machinery.PathFinder.find_spec(PACKAGE)
        if (
            spec is None
            or spec.origin is None
            or not isinstance(spec.loader, importlib.machinery.SourceFileLoader)
        ):
            _refuse()
        root = Path(spec.origin).parent
        if root.is_symlink():
            _refuse()
        root = root.resolve()
        loaded = {
            n: m for n, m in sys.modules.items() if n == PACKAGE or n.startswith(PACKAGE + ".")
        }
        if loaded and _verified_root != root:
            _refuse()
        for module in loaded.values():
            location = getattr(module, "__file__", None)
            if location is None or not Path(location).resolve().is_relative_to(root):
                _refuse()
        actual = {}
        for path in root.rglob("*"):
            if path.is_symlink():
                _refuse()
            if path.is_file():
                actual[path.relative_to(root).as_posix()] = _hash(path.read_bytes())
        if actual != manifest["runtime_identity"]["installed_files"]:
            _refuse()
        for name, expected in manifest["wheel_files"].items():
            if name.endswith(".dist-info/RECORD"):
                continue  # pip rewrites its own installation inventory.
            path = root.parent / name
            if path.is_symlink() or _hash(path.read_bytes()) != expected:
                _refuse()
        _verified_root = root
        return manifest
    except (OSError, ValueError, KeyError):
        _refuse()


class VerifiedV4Release:
    """Public VerifiedReleaseProvider, always backed by the private release pin."""

    def verify(self, installed_identity: dict) -> dict:
        manifest = verify_installed()
        if installed_identity != manifest["runtime_identity"]:
            _refuse()
        return {
            **manifest["runtime_identity"],
            "wheel_sha256": next(iter(manifest["files"].values()))["sha256"],
            "wheel_files": manifest["wheel_files"],
        }


if __name__ == "__main__":
    verify_installed()
    print("V4 vendor and installed release verified")
