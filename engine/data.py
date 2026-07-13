"""Digest-pinned data inputs (the protected corpus bundle).

The real corpus DBs (`vesum.db`, `atlas.db`, `sources.db` — ≈2.8 GB, private
and non-redistributable) are NEVER committed. They are mounted read-only from a
release dir and pinned by a `data-manifest.json` recording path + sha256 + size
per input. The engine resolves inputs ONLY from that release dir and verifies
their content digests before use; a mismatch is refused unless
`HRAMATKA_ALLOW_DATA_DRIFT=1` (an explicit, logged escape hatch — never silent).

This replaces the old `PROJECT_ROOT/data/*.db` discovery, which walked into the
public checkout and identified DBs by size/mtime (Sol defect #4).

Config:
  HRAMATKA_DATA_DIR       release dir holding the DB files (required at runtime)
  HRAMATKA_DATA_MANIFEST  deployed canonical form: the release-local manifest at
                          $HRAMATKA_DATA_DIR/data-manifest.json. Without it,
                          development/tests use the committed fixture fallback.
  HRAMATKA_ALLOW_DATA_DRIFT=1   accept a digest mismatch (drift) instead of refusing
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR_ENV = "HRAMATKA_DATA_DIR"
MANIFEST_ENV = "HRAMATKA_DATA_MANIFEST"
ALLOW_DRIFT_ENV = "HRAMATKA_ALLOW_DATA_DRIFT"

DEFAULT_MANIFEST = Path(__file__).with_name("data-manifest.json")

_active: DataBundle | None = None  # noqa: F821 - forward ref, defined below


class DataConfigError(RuntimeError):
    """The data bundle is not configured (no release dir / manifest)."""


class DataDriftError(RuntimeError):
    """A data input's content digest does not match the pinned manifest."""


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


@dataclass(frozen=True)
class DataBundle:
    """A resolved, digest-pinned set of corpus inputs under one release dir."""

    root: Path
    manifest: dict

    def _input(self, name: str) -> dict:
        try:
            return self.manifest["inputs"][name]
        except KeyError as exc:
            raise DataConfigError(
                f"Data input {name!r} is not declared in the data manifest."
            ) from exc

    def path(self, name: str) -> Path:
        rec = self._input(name)
        return self.root / (rec.get("path") or name)

    @property
    def vesum_db(self) -> Path:
        return self.path("vesum.db")

    @property
    def atlas_db(self) -> Path:
        return self.path("atlas.db")

    def digests(self) -> dict[str, str]:
        """Pinned content digest per input — the identity used in the fingerprint."""
        return {n: rec["sha256"] for n, rec in self.manifest["inputs"].items()}

    def verify(self, *, allow_drift: bool | None = None) -> None:
        """Verify every present input's content digest against the manifest.

        Required inputs must exist; optional inputs (e.g. `sources.db`, which the
        slice-1 engine does not open) are skipped when absent but still pin the
        bundle identity via `digests()`. Raises `DataDriftError` on mismatch
        unless drift is explicitly allowed.
        """
        if allow_drift is None:
            allow_drift = os.environ.get(ALLOW_DRIFT_ENV) == "1"
        problems: list[str] = []
        for name, rec in self.manifest["inputs"].items():
            fp = self.path(name)
            required = rec.get("required", True)
            if not fp.exists():
                if required:
                    problems.append(f"{name}: missing at {fp} (required input).")
                continue
            actual, size = _sha256_file(fp)
            if actual != rec.get("sha256"):
                problems.append(
                    f"{name}: sha256 mismatch — pinned {rec.get('sha256')}, "
                    f"found {actual}."
                )
            elif "size" in rec and size != rec["size"]:
                problems.append(
                    f"{name}: size mismatch — pinned {rec['size']}, found {size}."
                )
        if problems:
            detail = "\n".join(f"  - {p}" for p in problems)
            if allow_drift:
                # explicit escape hatch — accepted drift is LOUD, never silent
                # (review-p46 nit 3: the docstring promises a logged hatch)
                logging.getLogger(__name__).warning(
                    "Data drift ACCEPTED via %s=1:\n%s", ALLOW_DRIFT_ENV, detail
                )
                return
            raise DataDriftError(
                "Data bundle digest verification failed:\n" + detail + "\n"
                f"Set {ALLOW_DRIFT_ENV}=1 to run on drifted data deliberately."
            )


def load_manifest(path: str | Path | None = None) -> dict:
    manifest_path = Path(path) if path else Path(
        os.environ.get(MANIFEST_ENV, DEFAULT_MANIFEST)
    )
    if not manifest_path.is_file():
        raise DataConfigError(f"Data manifest not found at {manifest_path}.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest.get("inputs"), dict):
        raise DataConfigError(f"{manifest_path} has no 'inputs' mapping.")
    return manifest


def resolve_bundle(
    *,
    data_dir: str | Path | None = None,
    manifest: str | Path | dict | None = None,
    verify: bool = True,
    allow_drift: bool | None = None,
) -> DataBundle:
    """Build a DataBundle from a release dir + manifest, verifying digests."""
    root = Path(data_dir) if data_dir else None
    if root is None:
        env_dir = os.environ.get(DATA_DIR_ENV)
        if not env_dir:
            raise DataConfigError(
                f"No data release dir configured. Set {DATA_DIR_ENV} to the "
                "read-only corpus release dir (never a public checkout)."
            )
        root = Path(env_dir)
    manifest_dict = manifest if isinstance(manifest, dict) else load_manifest(manifest)
    bundle = DataBundle(root=root, manifest=manifest_dict)
    if verify:
        bundle.verify(allow_drift=allow_drift)
    return bundle


def active_bundle() -> DataBundle:
    """The process-wide active bundle (resolved from env on first use)."""
    global _active
    if _active is None:
        _active = resolve_bundle()
    return _active


def set_active_bundle(bundle: DataBundle | None) -> None:
    """Install (or clear) the active bundle — used by tests and by the API adapter."""
    global _active
    _active = bundle


@contextlib.contextmanager
def use_bundle(bundle: DataBundle):
    """Temporarily install `bundle` as active (restores the prior one on exit)."""
    global _active
    previous = _active
    _active = bundle
    try:
        yield bundle
    finally:
        _active = previous
