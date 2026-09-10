"""Build a content-pinned Hramatka corpus *release* from local DB inputs.

The source directory is an operator-local staging area, not an engine input. A
release is a copied snapshot of its three required DBs plus a freshly calculated
``data-manifest.json``. This prevents the engine from silently following a
mutable ``data/`` directory after an approved corpus release has been pinned.

Run with::

    python -m hramatka.engine.tools.make_data_release /private/corpus-inputs

By default this creates ``~/hramatka-data-releases/<date>-<shortdigest>/`` and
prints the exact ``HRAMATKA_DATA_DIR`` export needed to select it at runtime.
No corpus contents are written into the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

REQUIRED_INPUTS = ("vesum.db", "atlas.db", "sources.db")
DEFAULT_RELEASES_ROOT = Path("~/hramatka-data-releases").expanduser()

_INPUT_METADATA = {
    "vesum.db": {"role": "morphology (VESUM forms)", "required": True},
    "atlas.db": {
        "role": "grounding (Word Atlas article_payloads)",
        "required": True,
    },
    "sources.db": {
        "role": "protected private corpus (non-redistributable)",
        "required": False,
    },
}


class DataReleaseError(RuntimeError):
    """A corpus input cannot be safely turned into a release."""


@dataclass(frozen=True)
class DataRelease:
    """Location and content identity of one immutable corpus release."""

    root: Path
    manifest_path: Path
    digest: str
    manifest: dict


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _release_digest(inputs: dict[str, dict]) -> str:
    """Return a stable identity for the exact named inputs in a release."""
    digest = hashlib.sha256()
    for name in REQUIRED_INPUTS:
        record = inputs[name]
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _build_manifest(*, release_date: date, digest: str, inputs: dict[str, dict]) -> dict:
    return {
        "bundle": "lu-runtime-data",
        "version": f"{release_date.isoformat()}-{digest[:12]}",
        "description": (
            "Digest-pinned Hramatka corpus release. The DB files are local-only "
            "and never committed; this manifest identifies the copied release "
            "snapshot, not a mutable source data directory."
        ),
        "release": {
            "date": release_date.isoformat(),
            "content_sha256": digest,
            "created_by": "hramatka.engine.tools.make_data_release",
        },
        "inputs": inputs,
    }


def _source_inputs(source_dir: Path) -> dict[str, Path]:
    if not source_dir.is_dir():
        raise DataReleaseError(f"Corpus source directory does not exist: {source_dir}")
    paths = {name: source_dir / name for name in REQUIRED_INPUTS}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        names = ", ".join(missing)
        raise DataReleaseError(
            f"Corpus source directory must contain regular files {', '.join(REQUIRED_INPUTS)}; "
            f"missing: {names}."
        )
    return paths


def create_data_release(
    source_dir: str | Path,
    *,
    releases_root: str | Path | None = None,
    release_date: date | None = None,
) -> DataRelease:
    """Copy all corpus DBs into an immutable-named release and write its manifest.

    Inputs are copied rather than linked: subsequent writes to the operator's
    source directory therefore cannot change the bytes selected by this release.
    The target name uses the digest calculated from the copied bytes, not a
    pre-copy source hash.
    """
    source = Path(source_dir).expanduser().resolve()
    source_inputs = _source_inputs(source)
    root = Path(releases_root or DEFAULT_RELEASES_ROOT).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    today = release_date or datetime.now(UTC).date()

    stage = Path(tempfile.mkdtemp(prefix=f".{today.isoformat()}-", dir=root))
    try:
        inputs: dict[str, dict] = {}
        for name in REQUIRED_INPUTS:
            target = stage / name
            shutil.copy2(source_inputs[name], target)
            sha256, size = _sha256_file(target)
            inputs[name] = {
                "path": name,
                "sha256": sha256,
                "size": size,
                **_INPUT_METADATA[name],
            }

        digest = _release_digest(inputs)
        release_root = root / f"{today.isoformat()}-{digest[:12]}"
        if release_root.exists():
            raise DataReleaseError(
                f"Refusing to overwrite existing data release: {release_root}"
            )

        manifest = _build_manifest(release_date=today, digest=digest, inputs=inputs)
        manifest_path = stage / "data-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        try:
            os.rename(stage, release_root)
        except FileExistsError as exc:
            raise DataReleaseError(
                f"Refusing to overwrite existing data release: {release_root}"
            ) from exc
        return DataRelease(
            root=release_root,
            manifest_path=release_root / manifest_path.name,
            digest=digest,
            manifest=manifest,
        )
    finally:
        # ``rename`` moves the staging directory on success; otherwise remove
        # only our incomplete staging copy, never the source or another release.
        if stage.exists():
            shutil.rmtree(stage)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_dir",
        type=Path,
        help="private directory containing vesum.db, atlas.db, and sources.db",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_RELEASES_ROOT,
        help="release parent directory (default: ~/hramatka-data-releases)",
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help="release date in YYYY-MM-DD (test/reproducibility override)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        release = create_data_release(
            args.source_dir,
            releases_root=args.output_root,
            release_date=args.date,
        )
    except DataReleaseError as exc:
        print(f"Data release failed: {exc}", file=os.sys.stderr)
        return 1

    print(f"Created pinned data release: {release.root}")
    print(f"Manifest: {release.manifest_path}")
    print(f"export HRAMATKA_DATA_DIR={shlex.quote(str(release.root))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
