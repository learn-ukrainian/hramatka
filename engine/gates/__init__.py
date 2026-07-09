"""Deterministic gate-OUT checks for the Hramatka engine (slice 1: numeral
case-government only — see `.agent/tmp/hramatka/slice-1-build-plan.md` §6c).
"""

from __future__ import annotations

import sys

from .. import paths


def _bootstrap_sys_path() -> None:
    """Ensure the repo root is importable as `scripts.*` regardless of cwd.

    `scripts/verification/vesum.py` self-inserts the project root onto
    `sys.path` when imported directly, but that only happens once the
    import machinery can find the `scripts` package in the first place.
    Since this engine lives outside the `scripts` package tree
    (`.agent/tmp/hramatka/engine/`), we insert `paths.PROJECT_ROOT`
    ourselves before importing anything under `scripts.*`.
    """
    root = str(paths.PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _vesum_db_path():
    """Explicit VESUM db path (never cwd-relative) — see paths.py."""
    return paths.VESUM_DB


def _atlas_db_path():
    """Explicit atlas db path (never cwd-relative) — see paths.py."""
    return paths.ATLAS_DB
