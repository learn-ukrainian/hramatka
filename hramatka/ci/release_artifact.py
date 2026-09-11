"""Pack / verify immutable Hramatka release artifacts (public CI).

Manifest schema: hramatka-release-artifact.v1
Artifact is content bound to an exact public git SHA. Host secrets never belong here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "hramatka-release-artifact.v1"
MANIFEST_NAME = "manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha(repo: Path) -> str:
    out = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if len(out) != 40:
        raise SystemExit(f"expected 40-char HEAD, got {out!r}")
    return out


def find_bundle(dist_assets: Path) -> Path:
    indexed = sorted(dist_assets.glob("index-*.js"))
    if indexed:
        return indexed[0]
    js = sorted(dist_assets.glob("*.js"))
    if not js:
        raise SystemExit(f"no JS bundle under {dist_assets}")
    return js[0]


def build_manifest(*, repo: Path, bundle: Path) -> dict:
    return {
        "schema": SCHEMA,
        "public_repo": "https://github.com/learn-ukrainian/hramatka.git",
        "public_sha": git_sha(repo),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bundle_path": "hramatka/app/dist/assets/" + bundle.name,
        "bundle_sha256": sha256_file(bundle),
        "pyproject_sha256": sha256_file(repo / "pyproject.toml"),
    }


def pack(repo: Path, out_dir: Path) -> Path:
    pkg = repo / "hramatka"
    dist_assets = pkg / "app" / "dist" / "assets"
    if not dist_assets.is_dir():
        raise SystemExit("missing hramatka/app/dist/assets — build the teacher app first")
    bundle = find_bundle(dist_assets)
    manifest = build_manifest(repo=repo, bundle=bundle)
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = out_dir / f"staging-{manifest['public_sha']}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    (staging / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (staging / "PUBLIC_SHA").write_text(manifest["public_sha"] + "\n", encoding="utf-8")
    shutil.copy2(repo / "pyproject.toml", staging / "pyproject.toml")
    shutil.copytree(
        pkg,
        staging / "hramatka",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            "node_modules",
            ".git",
            "app/node_modules",
        ),
    )
    bundle_staged = staging / manifest["bundle_path"]
    if sha256_file(bundle_staged) != manifest["bundle_sha256"]:
        raise SystemExit("staged bundle hash mismatch")
    if sha256_file(staging / "pyproject.toml") != manifest["pyproject_sha256"]:
        raise SystemExit("staged pyproject hash mismatch")

    tarball = out_dir / f"hramatka-release-{manifest['public_sha']}.tar.gz"
    if tarball.exists():
        tarball.unlink()
    with tarfile.open(tarball, "w:gz") as tar:
        for name in (MANIFEST_NAME, "PUBLIC_SHA", "pyproject.toml", "hramatka"):
            tar.add(staging / name, arcname=name)
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(staging)
    print(f"packed {tarball}")
    print(f"public_sha={manifest['public_sha']}")
    print(f"bundle_sha256={manifest['bundle_sha256']}")
    return tarball


def verify_tarball(tarball: Path, expected_sha: str | None = None) -> dict:
    staging = tarball.parent / f"verify-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with tarfile.open(tarball, "r:gz") as tar:
        # filter='data' is the Python 3.12+ safe default; fall back on older runtimes.
        try:
            tar.extractall(staging, filter="data")
        except TypeError:
            tar.extractall(staging)
    manifest_path = staging / MANIFEST_NAME
    if not manifest_path.is_file():
        candidates = list(staging.rglob(MANIFEST_NAME))
        if not candidates:
            raise SystemExit("manifest.json missing from tarball")
        manifest_path = candidates[0]
        staging = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise SystemExit(f"unsupported schema: {manifest.get('schema')!r}")
    if expected_sha and manifest["public_sha"] != expected_sha:
        raise SystemExit(
            f"manifest public_sha {manifest['public_sha']} != expected {expected_sha}"
        )
    public_sha_file = staging / "PUBLIC_SHA"
    if public_sha_file.is_file():
        stamped = public_sha_file.read_text(encoding="utf-8").strip()
        if stamped != manifest["public_sha"]:
            raise SystemExit("PUBLIC_SHA file mismatch")
    bundle = staging / manifest["bundle_path"]
    if not bundle.is_file():
        raise SystemExit(f"missing bundle: {bundle}")
    if sha256_file(bundle) != manifest["bundle_sha256"]:
        raise SystemExit("bundle sha256 mismatch")
    if sha256_file(staging / "pyproject.toml") != manifest["pyproject_sha256"]:
        raise SystemExit("pyproject sha256 mismatch")
    if not (staging / "hramatka" / "api").is_dir() or not (
        staging / "hramatka" / "engine"
    ).is_dir():
        raise SystemExit("artifact missing hramatka/{api,engine}")
    print(f"verified public_sha={manifest['public_sha']}")
    shutil.rmtree(staging)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack", help="pack release tarball from a built checkout")
    p_pack.add_argument("--repo", type=Path, default=Path.cwd())
    p_pack.add_argument("--out-dir", type=Path, required=True)

    p_verify = sub.add_parser("verify", help="verify a release tarball")
    p_verify.add_argument("tarball", type=Path)
    p_verify.add_argument("--expect-sha", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "pack":
        pack(args.repo.resolve(), args.out_dir.resolve())
        return 0
    if args.cmd == "verify":
        verify_tarball(args.tarball.resolve(), expected_sha=args.expect_sha)
        return 0
    raise SystemExit(f"unknown cmd {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
