from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
import release_artifact as ra

LOCKFILE = b'version = 1\nrequires-python = ">=3.12"\n'


@pytest.fixture
def release_inputs(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "hramatka-repo"
    (root / "hramatka" / "app" / "dist" / "assets").mkdir(parents=True)
    (root / "hramatka" / "engine").mkdir(parents=True)
    (root / "hramatka" / "api").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='hramatka'\n", encoding="utf-8")
    (root / "uv.lock").write_bytes(LOCKFILE)
    bundle = root / "hramatka" / "app" / "dist" / "assets" / "index-deadbeef.js"
    bundle.write_text("console.log('hi')\n", encoding="utf-8")
    (root / "hramatka" / "engine" / ".keep").write_text("x", encoding="utf-8")
    (root / "hramatka" / "api" / ".keep").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "artifact"], cwd=root, check=True)
    wheel = tmp_path / "hramatka-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel content")
    return root, wheel


def test_pack_and_verify(tmp_path: Path, release_inputs: tuple[Path, Path]) -> None:
    root, wheel = release_inputs
    (root / "uv.lock").write_bytes(b"uncommitted lock edit\n")
    out = tmp_path / "out"
    tarball = ra.pack(root, out, wheel=wheel)
    assert tarball.is_file()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == ra.SCHEMA
    sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    assert manifest["public_sha"] == sha
    with tarfile.open(tarball, "r:gz") as tar:
        assert tar.extractfile("uv.lock").read() == LOCKFILE
        assert tar.extractfile(f"wheel/{wheel.name}").read() == wheel.read_bytes()
    assert manifest["uv_lock_sha256"] == hashlib.sha256(LOCKFILE).hexdigest()
    assert manifest["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert len(bytes.fromhex(manifest["runtime_tree_sha256"])) == hashlib.sha256().digest_size
    verified = ra.verify_tarball(tarball, expected_sha=sha)
    assert verified == manifest


def test_verify_rejects_wrong_expect_sha(
    tmp_path: Path, release_inputs: tuple[Path, Path]
) -> None:
    root, wheel = release_inputs
    tarball = ra.pack(root, tmp_path / "out", wheel=wheel)
    with pytest.raises(SystemExit, match="manifest public_sha"):
        ra.verify_tarball(tarball, expected_sha="0" * 40)


def test_verify_rejects_tampered_lockfile(tmp_path: Path, release_inputs: tuple[Path, Path]) -> None:
    root, wheel = release_inputs
    tarball = ra.pack(root, tmp_path / "out", wheel=wheel)
    extracted = tmp_path / "extracted"
    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(extracted, filter="data")

    (extracted / "uv.lock").write_bytes(LOCKFILE + b"tampered = true\n")
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as tar:
        for entry in extracted.iterdir():
            tar.add(entry, arcname=entry.name)

    with pytest.raises(SystemExit, match="uv.lock sha256 mismatch"):
        ra.verify_tarball(tampered)
