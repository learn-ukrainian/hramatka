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

import os
from pathlib import Path

import pytest

from hramatka.engine import data

# Re-export the pytest-free helpers (relocated to hramatka/engine/fixtures.py per #97)
# so that test modules can continue `from hramatka.engine.tests.conftest import ...`
# for test-local use. The real definitions live in the non-test fixtures module
# so runtime entrypoints do not import any test module.
from hramatka.engine.fixtures import (  # noqa: F401
    _build_atlas_db,
    _build_fixture_bundle,
    _build_vesum_db,
    _seed,
    _sha_size,
)

# Compat alias for tests that import FIXTURES directly from conftest (e.g. drift test).
FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
