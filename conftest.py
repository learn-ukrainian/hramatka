"""Repo-root pytest bootstrap for Hramatka offline tests.

Installs the fixture data bundle so DB-free engine tests do not require a real
corpus release (`HRAMATKA_DATA_DIR`). Mirrors the pre-split private root conftest.
"""

from __future__ import annotations

import os

import pytest

from hramatka.engine import data
from hramatka.engine.fixtures import _build_fixture_bundle
from hramatka.engine.transport import METERED_PROVIDER_SPEND_ACK_ENV


@pytest.fixture(autouse=True)
def _enable_metered_provider_spend_for_mock_transports(monkeypatch):
    """Keep offline transport tests independent of an operator deployment flag."""
    monkeypatch.setenv(METERED_PROVIDER_SPEND_ACK_ENV, "1")


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
