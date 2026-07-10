"""Tests for building local, digest-pinned corpus releases without real corpus DBs."""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from hramatka.engine.tools import make_data_release


def _make_fixture_db(path: Path, marker: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE fixture (marker TEXT NOT NULL)")
        connection.execute("INSERT INTO fixture (marker) VALUES (?)", (marker,))
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def source_bundle(tmp_path: Path) -> Path:
    source = tmp_path / "private-source"
    source.mkdir()
    for name in make_data_release.REQUIRED_INPUTS:
        _make_fixture_db(source / name, name)
    return source


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_create_data_release_copies_all_dbs_and_writes_fresh_manifest(
    source_bundle: Path, tmp_path: Path
):
    releases_root = tmp_path / "releases"
    release = make_data_release.create_data_release(
        source_bundle,
        releases_root=releases_root,
        release_date=date(2026, 7, 10),
    )

    assert release.root.parent == releases_root
    assert release.root.name == f"2026-07-10-{release.digest[:12]}"
    assert release.manifest_path == release.root / "data-manifest.json"

    written_manifest = json.loads(release.manifest_path.read_text(encoding="utf-8"))
    assert written_manifest == release.manifest
    assert written_manifest["release"] == {
        "date": "2026-07-10",
        "content_sha256": release.digest,
        "created_by": "hramatka.engine.tools.make_data_release",
    }
    assert set(written_manifest["inputs"]) == set(make_data_release.REQUIRED_INPUTS)

    for name in make_data_release.REQUIRED_INPUTS:
        copied = release.root / name
        original = source_bundle / name
        assert copied.read_bytes() == original.read_bytes()
        assert copied.stat().st_ino != original.stat().st_ino
        assert written_manifest["inputs"][name]["path"] == name
        assert written_manifest["inputs"][name]["sha256"] == _sha256(copied)
        assert written_manifest["inputs"][name]["size"] == copied.stat().st_size


def test_release_manifest_pins_copied_bytes_not_mutable_source(
    source_bundle: Path, tmp_path: Path
):
    release = make_data_release.create_data_release(
        source_bundle,
        releases_root=tmp_path / "releases",
        release_date=date(2026, 7, 10),
    )
    source_bundle.joinpath("vesum.db").write_bytes(b"changed after release")

    manifest = json.loads(release.manifest_path.read_text(encoding="utf-8"))
    assert _sha256(release.root / "vesum.db") == manifest["inputs"]["vesum.db"]["sha256"]
    assert _sha256(source_bundle / "vesum.db") != manifest["inputs"]["vesum.db"]["sha256"]


def test_cli_prints_runtime_data_dir_export(
    source_bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    releases_root = tmp_path / "releases root"
    status = make_data_release.main(
        [
            str(source_bundle),
            "--output-root",
            str(releases_root),
            "--date",
            "2026-07-10",
        ]
    )

    assert status == 0
    output = capsys.readouterr().out.splitlines()
    release_root = next(releases_root.iterdir())
    assert output[-1] == f"export HRAMATKA_DATA_DIR={shlex.quote(str(release_root))}"


def test_release_refuses_missing_required_input(tmp_path: Path):
    source = tmp_path / "incomplete-source"
    source.mkdir()
    _make_fixture_db(source / "vesum.db", "vesum")

    with pytest.raises(make_data_release.DataReleaseError, match="atlas.db, sources.db"):
        make_data_release.create_data_release(source, releases_root=tmp_path / "releases")
