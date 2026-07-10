"""Deterministic gate-OUT checks for the Hramatka engine (slice 1: numeral
case-government only — see `hramatka/slice-1-build-plan.md` §6c).

VESUM lookups go through `engine.linguistics` (the digest-pinned vendored
adapter), and DB paths resolve from the digest-pinned data manifest
(`engine.data`). No `sys.path` bootstrap and no `scripts.*` import remain.
"""

from __future__ import annotations

from .. import data


def _vesum_db_path():
    """Explicit VESUM db path from the active, digest-verified data bundle."""
    return data.active_bundle().vesum_db


def _atlas_db_path():
    """Explicit atlas db path from the active, digest-verified data bundle."""
    return data.active_bundle().atlas_db
