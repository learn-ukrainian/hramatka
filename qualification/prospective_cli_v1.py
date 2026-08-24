"""Explicit, durable offline entrypoint for the result-bearing #552 run."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hramatka.engine import data

from .prospective_capture_v1 import capture_source
from .prospective_evaluate_v1 import evaluate_population
from .prospective_manifest_v1 import build_manifest, canonical_bytes


class ProspectiveCliError(RuntimeError):
    pass


def _write_create_only(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--rights-receipt", required=True, type=Path)
    parser.add_argument("--engine-data-dir", required=True, type=Path)
    parser.add_argument("--engine-data-manifest", required=True, type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    output = arguments.output_dir
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ProspectiveCliError("Output directory must not exist under a real existing parent.")
    if os.environ.get("HRAMATKA_ALLOW_DATA_DRIFT"):
        raise ProspectiveCliError("HRAMATKA_ALLOW_DATA_DRIFT is forbidden for prospective proof.")
    bundle = data.resolve_bundle(
        data_dir=arguments.engine_data_dir,
        manifest=arguments.engine_data_manifest,
        verify=True,
        allow_drift=False,
    )
    manifest, source_texts = build_manifest(arguments.database, arguments.rights_receipt)
    try:
        os.mkdir(output, 0o700)
    except FileExistsError as exc:
        raise ProspectiveCliError("Output directory already exists.") from exc
    certificates = output / "certificates"
    os.mkdir(certificates, 0o700)
    _write_create_only(
        output / "run-started.json",
        canonical_bytes(
            {
                "version": "HramatkaProspectiveRunState.v1",
                "state": "incomplete",
                "manifest_digest": manifest.digest,
            }
        ),
    )
    _write_create_only(output / "manifest.json", canonical_bytes(manifest.to_dict()))
    for source in manifest.sources:
        certificate = capture_source(
            manifest,
            source,
            source_texts[source.sqlite_id],
            bundle=bundle,
            repository=arguments.repository,
        )
        _write_create_only(
            certificates / f"{source.selection_rank:03d}-{source.sqlite_id}.json",
            certificate.to_bytes(),
        )
    _fsync_directory(certificates)
    persisted_manifest = (output / "manifest.json").read_bytes()
    if persisted_manifest != canonical_bytes(manifest.to_dict()):
        raise ProspectiveCliError("Persisted manifest does not match capture authority.")
    persisted_certificates = {
        source.sqlite_id: (
            certificates / f"{source.selection_rank:03d}-{source.sqlite_id}.json"
        ).read_bytes()
        for source in manifest.sources
    }
    aggregate = evaluate_population(
        manifest,
        persisted_certificates,
        source_texts,
        bundle=bundle,
        repository=arguments.repository,
    )
    _write_create_only(output / "aggregate.json", canonical_bytes(aggregate.to_dict()))
    _write_create_only(
        output / "run-complete.json",
        canonical_bytes(
            {
                "version": "HramatkaProspectiveRunState.v1",
                "state": "complete",
                "manifest_digest": manifest.digest,
                "aggregate_digest": aggregate.digest,
                "certificate_count": len(persisted_certificates),
            }
        ),
    )
    _fsync_directory(output)
    _fsync_directory(output.parent)
    sys.stdout.buffer.write(canonical_bytes(aggregate.to_dict()))
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
