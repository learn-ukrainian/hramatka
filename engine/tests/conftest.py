"""Offline engine-test harness: build a tiny fixture data bundle and inject it.

The real corpus DBs (≈2.8 GB, protected) are never committed. Instead the suite
builds a small SQLite `vesum.db` + `atlas.db` in a tmp dir from committed JSON
fixtures (`fixtures/vesum_forms.json`, `fixtures/atlas_rows.json` — extracted
from the real DBs for exactly the forms/lemmas these tests touch) and installs
it as the engine's active, digest-verified data bundle. No network, no checkout.

Extraction / integration mode: set `HRAMATKA_TEST_DATA_DIR` to a real corpus
release dir and the suite runs against it (drift allowed), using the committed
production `data-manifest.json`. `fixtures/_build_fixtures.py` uses this mode to
regenerate the JSON fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from hramatka.engine import data

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _sha_size(path: Path) -> tuple[str, int]:
    b = path.read_bytes()
    return hashlib.sha256(b).hexdigest(), len(b)


def _seed() -> dict:
    """Extra rows NOT extracted from the real corpus (e.g. the seeded
    russianism for the defect-5 e2e), kept in their own file so a
    `_build_fixtures` regeneration of the extracted JSON never clobbers them."""
    path = FIXTURES / "seeded_russianism.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _build_vesum_db(path: Path) -> None:
    rows = json.loads((FIXTURES / "vesum_forms.json").read_text(encoding="utf-8"))
    rows = [*rows, *_seed().get("vesum_forms", [])]
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE forms (word_form TEXT NOT NULL, lemma TEXT NOT NULL, "
            "tags TEXT NOT NULL, pos TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO forms (word_form, lemma, tags, pos) VALUES (?, ?, ?, ?)",
            [(r["word_form"], r["lemma"], r["tags"], r["pos"]) for r in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _build_atlas_db(path: Path) -> None:
    payloads = json.loads((FIXTURES / "atlas_rows.json").read_text(encoding="utf-8"))
    payloads = [*payloads, *_seed().get("atlas_rows", [])]
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE article_payloads (slug TEXT PRIMARY KEY, "
            "route_order INTEGER NOT NULL, payload_json TEXT NOT NULL, "
            "is_public_route INTEGER NOT NULL CHECK (is_public_route IN (0, 1)))"
        )
        conn.executemany(
            "INSERT INTO article_payloads (slug, route_order, payload_json, is_public_route) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    p.get("slug") or p["lemma"],
                    i,
                    json.dumps(p, ensure_ascii=False),
                    1,
                )
                for i, p in enumerate(payloads)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _build_fixture_bundle(root: Path) -> data.DataBundle:
    vesum = root / "vesum.db"
    atlas = root / "atlas.db"
    _build_vesum_db(vesum)
    _build_atlas_db(atlas)
    v_sha, v_size = _sha_size(vesum)
    a_sha, a_size = _sha_size(atlas)
    manifest = {
        "bundle": "lu-runtime-data-fixture",
        "version": "test",
        "inputs": {
            "vesum.db": {"path": "vesum.db", "sha256": v_sha, "size": v_size, "required": True},
            "atlas.db": {"path": "atlas.db", "sha256": a_sha, "size": a_size, "required": True},
            # sources.db is not opened by slice-1; absent + optional, but its
            # pinned digest still contributes to the bake fingerprint identity.
            "sources.db": {
                "path": "sources.db",
                "sha256": "0" * 64,
                "size": 0,
                "required": False,
            },
        },
    }
    return data.resolve_bundle(data_dir=root, manifest=manifest, verify=True)


@pytest.fixture(scope="session", autouse=True)
def _active_data_bundle(tmp_path_factory):
    """Install the offline fixture bundle (or a real one in extraction mode)."""
    real_dir = os.environ.get("HRAMATKA_TEST_DATA_DIR")
    if real_dir:
        bundle = data.resolve_bundle(data_dir=real_dir, verify=True, allow_drift=True)
    else:
        root = tmp_path_factory.mktemp("lu-data-fixture")
        bundle = _build_fixture_bundle(root)
    previous = data._active
    data.set_active_bundle(bundle)
    try:
        yield bundle
    finally:
        data.set_active_bundle(previous)
