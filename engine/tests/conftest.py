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
    _bundle_with_matchup_vocabulary,
    _seed,
    _sha_size,
)

# Compat alias for tests that import FIXTURES directly from conftest (e.g. drift test).
FIXTURES = Path(__file__).resolve().parent / "fixtures"




@pytest.fixture
def active_matchup_vocabulary_bundle(tmp_path):
    """Activate the bundle that carries GOOD_ACTIVITIES' right-side synonyms.

    The base offline bundle omits «книга»/«непевність» — real forms (#M-4) that
    production VESUM resolves — so the fixture match-up loses two pairs to
    vesum_token and salvages down to a 2-pair board. Tests that assert on that
    lesson need the production-faithful vocabulary; otherwise they measure a
    fixture gap rather than the engine. `EngineLessonBaker` takes `bundle=`
    directly; `pipeline.run`/`measure` read the active bundle, hence this.
    """
    bundle = _bundle_with_matchup_vocabulary(tmp_path / "matchup-data")
    previous = data._active
    data.set_active_bundle(bundle)
    try:
        yield bundle
    finally:
        data.set_active_bundle(previous)
