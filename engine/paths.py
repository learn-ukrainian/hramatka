"""Path + preflight helpers for the private Hramatka engine.

Everything the engine reads is resolved through a pinned indirection, never a
checkout walk or a dev-home path:
  - public code/schemas  -> `engine.vendoring` (digest-verified `hramatka/vendor/`)
  - corpus DB inputs     -> `engine.data` (digest-pinned data manifest)
  - the AIS secret       -> the `HRAMATKA_AIS_API_KEY` env var (see `engine.transport`)

`ATLAS_DB` / `VESUM_DB` are functions (not module constants) so nothing binds a
DB path at import time before the data bundle is configured/injected.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import data, vendoring

ENGINE_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = ENGINE_DIR.parent  # the `hramatka` package

# Runtime scratch (raw-generation cache, per-anchor output). Package-relative and
# cwd-independent; gitignored. Callers/tests override with explicit dirs.
CACHE_DIR = ENGINE_DIR / ".cache"
DEFAULT_OUT_DIR = ENGINE_DIR / ".out"

AIS_API_KEY_ENV = "HRAMATKA_AIS_API_KEY"


def vesum_db() -> Path:
    """Absolute VESUM DB path from the active, digest-verified data bundle."""
    return data.active_bundle().vesum_db


def atlas_db() -> Path:
    """Absolute atlas DB path from the active, digest-verified data bundle."""
    return data.active_bundle().atlas_db


class PreflightError(RuntimeError):
    """Raised by `preflight()` when a required engine dependency is missing.

    Lists every failing check (not just the first) with an actionable fix.
    """


def preflight(*, require_generator: bool = False) -> None:
    """Assert the engine's pinned dependencies are present and intact.

    Checks (fail fast, all failures reported at once):
      - every vendored artifact's digests verify (`vendoring.verify_all`);
      - the data bundle resolves and its input digests match the manifest;
      - the AIS key env var is set — ONLY when `require_generator=True` (offline
        runs with an injected fake generator do not need it).
    """
    errors: list[str] = []
    try:
        vendoring.verify_all()
    except vendoring.VendorIntegrityError as exc:
        errors.append(f"vendored artifact integrity: {exc}")
    try:
        data.active_bundle().verify()
    except (data.DataConfigError, data.DataDriftError) as exc:
        errors.append(f"data bundle: {exc}")
    if require_generator and not os.environ.get(AIS_API_KEY_ENV):
        errors.append(
            f"{AIS_API_KEY_ENV} is not set — Gemma generation "
            "(google-ais/gemma-4-31b-it, toolless) requires it."
        )
    if errors:
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        raise PreflightError(f"Hramatka engine preflight failed:\n{bullet_list}")
