"""Pack / verify immutable Hramatka release artifacts (public CI).

Manifest schema: hramatka-release-artifact.v1
Artifact is content bound to an exact public git SHA. Host secrets never belong here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

SCHEMA = "hramatka-release-artifact.v1"
MANIFEST_NAME = "manifest.json"
RELEASE_MEMBERS = (MANIFEST_NAME, "PUBLIC_SHA", "pyproject.toml", "uv.lock", "wheel", "hramatka")
WHEEL_PATTERN = "hramatka-*.whl"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(root: Path) -> str:
    """Bind relative paths, file contents, and whether each file is executable by its owner."""
    if root.is_symlink() or not root.is_dir():
        raise SystemExit("release runtime tree is missing or not a directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SystemExit(f"symbolic link in release runtime tree: {relative!r}")
        if path.is_dir():
            entry = (relative, None)
        elif path.is_file():
            entry = (relative, sha256_file(path), bool(path.stat().st_mode & 0o100))
        else:
            raise SystemExit(f"unsupported file type in release runtime tree: {relative!r}")
        digest.update(json.dumps(entry).encode("ascii") + b"\n")
    return digest.hexdigest()


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


def pack(repo: Path, out_dir: Path, *, wheel: Path) -> Path:
    pkg = repo / "hramatka"
    dist_assets = pkg / "app" / "dist" / "assets"
    if not dist_assets.is_dir():
        raise SystemExit("missing hramatka/app/dist/assets — build the teacher app first")
    bundle = find_bundle(dist_assets)
    if not wheel.match(WHEEL_PATTERN) or not wheel.is_file():
        raise SystemExit("expected a built Hramatka wheel")
    public_sha = git_sha(repo)
    try:
        lockfile = subprocess.check_output(
            [
                "git", "-C", str(repo), "--no-replace-objects",
                "cat-file", "blob", f"{public_sha}:uv.lock",
            ],
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit("timed out reading uv.lock from the release commit") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit("cannot read uv.lock from the release commit") from exc

    manifest = {
        "schema": SCHEMA,
        "public_repo": "https://github.com/learn-ukrainian/hramatka.git",
        "public_sha": public_sha,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bundle_path": "hramatka/app/dist/assets/" + bundle.name,
        "bundle_sha256": sha256_file(bundle),
        "pyproject_sha256": sha256_file(repo / "pyproject.toml"),
        "uv_lock_sha256": hashlib.sha256(lockfile).hexdigest(),
        "wheel_sha256": sha256_file(wheel),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = out_dir / f"staging-{manifest['public_sha']}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    (staging / "PUBLIC_SHA").write_text(manifest["public_sha"] + "\n", encoding="utf-8")
    shutil.copy2(repo / "pyproject.toml", staging / "pyproject.toml")
    (staging / "uv.lock").write_bytes(lockfile)
    staged_wheel = staging / "wheel" / wheel.name
    staged_wheel.parent.mkdir()
    shutil.copy2(wheel, staged_wheel)
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
    if sha256_file(staged_wheel) != manifest["wheel_sha256"]:
        raise SystemExit("staged wheel hash mismatch")
    manifest["runtime_tree_sha256"] = sha256_tree(staging / "hramatka")
    (staging / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    tarball = out_dir / f"hramatka-release-{manifest['public_sha']}.tar.gz"
    if tarball.exists():
        tarball.unlink()
    with tarfile.open(tarball, "w:gz") as tar:
        for name in RELEASE_MEMBERS:
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
    with TemporaryDirectory(prefix="hramatka-release-verify-") as temporary:
        staging = Path(temporary)
        try:
            with tarfile.open(tarball, "r:gz") as tar:
                tar.extractall(staging, filter="data")
        except (EOFError, OSError, tarfile.TarError) as exc:
            raise SystemExit("cannot extract a valid release archive") from exc
        if any(path.name not in RELEASE_MEMBERS for path in staging.iterdir()):
            raise SystemExit("unexpected file or directory at release archive root")
        manifest_path = staging / MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SystemExit("manifest.json missing or not a regular file at the archive root")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit("invalid release manifest JSON") from exc
        if not isinstance(manifest, dict):
            raise SystemExit("release manifest must be a JSON object")
        if manifest.get("schema") != SCHEMA:
            raise SystemExit(f"unsupported schema: {manifest.get('schema')!r}")
        for field, length in (
            ("public_sha", 40),
            ("bundle_sha256", 64),
            ("pyproject_sha256", 64),
            ("uv_lock_sha256", 64),
            ("wheel_sha256", 64),
            ("runtime_tree_sha256", 64),
        ):
            value = manifest.get(field)
            if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
                raise SystemExit(f"invalid {field} in release manifest")
        public_sha = manifest["public_sha"]
        if expected_sha is not None and public_sha != expected_sha:
            raise SystemExit(f"manifest public_sha {public_sha} != expected {expected_sha!r}")
        public_sha_file = staging / "PUBLIC_SHA"
        if public_sha_file.is_symlink() or not public_sha_file.is_file():
            raise SystemExit("PUBLIC_SHA missing or not a regular file")
        if public_sha_file.read_bytes().strip() != public_sha.encode("ascii"):
            raise SystemExit("PUBLIC_SHA file mismatch")
        bundle_value = manifest.get("bundle_path")
        if not isinstance(bundle_value, str):
            raise SystemExit("invalid bundle_path in release manifest")
        bundle_path = Path(bundle_value)
        if bundle_path.parent != Path("hramatka/app/dist/assets") or bundle_path.suffix != ".js":
            raise SystemExit("bundle_path must name a JavaScript file in hramatka/app/dist/assets")
        runtime_tree = staging / "hramatka"
        if sha256_tree(runtime_tree) != manifest["runtime_tree_sha256"]:
            raise SystemExit("release runtime tree sha256 mismatch")
        if not (runtime_tree / "api").is_dir() or not (runtime_tree / "engine").is_dir():
            raise SystemExit("artifact missing hramatka/{api,engine}")
        bundle = staging / bundle_path
        if not bundle.is_file():
            raise SystemExit(f"bundle missing: {bundle_value!r}")
        if sha256_file(bundle) != manifest["bundle_sha256"]:
            raise SystemExit("bundle sha256 mismatch")
        for name, field in (("pyproject.toml", "pyproject_sha256"), ("uv.lock", "uv_lock_sha256")):
            path = staging / name
            if path.is_symlink() or not path.is_file():
                raise SystemExit(f"{name} missing or not a regular file")
            if sha256_file(path) != manifest[field]:
                raise SystemExit(f"{name} sha256 mismatch")
        wheel_dir = staging / "wheel"
        wheels = list(wheel_dir.iterdir()) if not wheel_dir.is_symlink() and wheel_dir.is_dir() else []
        if len(wheels) != 1 or not wheels[0].match(WHEEL_PATTERN):
            raise SystemExit("artifact must contain exactly one Hramatka wheel")
        wheel = wheels[0]
        if wheel.is_symlink() or not wheel.is_file():
            raise SystemExit("Hramatka wheel is not a regular file")
        if sha256_file(wheel) != manifest["wheel_sha256"]:
            raise SystemExit("Hramatka wheel sha256 mismatch")
    print(f"verified public_sha={manifest['public_sha']}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack", help="pack release tarball from a built checkout")
    p_pack.add_argument("--repo", type=Path, default=Path.cwd())
    p_pack.add_argument("--out-dir", type=Path, required=True)
    p_pack.add_argument("--wheel", type=Path, required=True, help="built Hramatka wheel")

    p_verify = sub.add_parser("verify", help="verify a release tarball")
    p_verify.add_argument("tarball", type=Path)
    p_verify.add_argument("--expect-sha", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "pack":
        pack(args.repo.resolve(), args.out_dir.resolve(), wheel=args.wheel)
        return 0
    if args.cmd == "verify":
        verify_tarball(args.tarball.resolve(), expected_sha=args.expect_sha)
        return 0
    raise SystemExit(f"unknown cmd {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
