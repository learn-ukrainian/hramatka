from __future__ import annotations

import json
import subprocess
from pathlib import Path

import release_artifact as ra


def _repo(tmp: Path) -> Path:
    root = tmp / "hramatka-repo"
    (root / "hramatka" / "app" / "dist" / "assets").mkdir(parents=True)
    (root / "hramatka" / "engine").mkdir(parents=True)
    (root / "hramatka" / "api").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='hramatka'\n", encoding="utf-8")
    bundle = root / "hramatka" / "app" / "dist" / "assets" / "index-deadbeef.js"
    bundle.write_text("console.log('hi')\n", encoding="utf-8")
    (root / "hramatka" / "engine" / ".keep").write_text("x", encoding="utf-8")
    (root / "hramatka" / "api" / ".keep").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "artifact"], cwd=root, check=True)
    return root


def test_pack_and_verify(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "out"
    tarball = ra.pack(root, out)
    assert tarball.is_file()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == ra.SCHEMA
    sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    assert manifest["public_sha"] == sha
    verified = ra.verify_tarball(tarball, expected_sha=sha)
    assert verified["bundle_sha256"] == manifest["bundle_sha256"]


def test_verify_rejects_wrong_expect_sha(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    tarball = ra.pack(root, tmp_path / "out")
    try:
        ra.verify_tarball(tarball, expected_sha="0" * 40)
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass
