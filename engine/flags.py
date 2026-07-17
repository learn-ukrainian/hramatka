"""Composable engine feature flags (grounding-mode v1 §7).

Three independent flags — never one mega-flag. Active vector is part of
bake identity via ``fingerprint_inputs``.
"""

from __future__ import annotations

import os
from typing import Final

# Canonical flag names (spec §7). Env vars are upper-snake with HRAMATKA_ prefix.
FLAG_KIT_ENRICHMENT_V1: Final = "kit_enrichment_v1"
FLAG_WRITER_PROMPT_V2: Final = "writer_prompt_v2"
FLAG_GROUNDING_MODE_V1: Final = "grounding_mode_v1"

FLAG_NAMES: Final[tuple[str, ...]] = (
    FLAG_KIT_ENRICHMENT_V1,
    FLAG_WRITER_PROMPT_V2,
    FLAG_GROUNDING_MODE_V1,
)

_ENV_BY_FLAG: Final[dict[str, str]] = {
    FLAG_KIT_ENRICHMENT_V1: "HRAMATKA_KIT_ENRICHMENT_V1",
    FLAG_WRITER_PROMPT_V2: "HRAMATKA_WRITER_PROMPT_V2",
    FLAG_GROUNDING_MODE_V1: "HRAMATKA_GROUNDING_MODE_V1",
}


def is_enabled(flag: str) -> bool:
    """Return whether *flag* is explicitly enabled (env value ``\"1\"``)."""
    env_name = _ENV_BY_FLAG.get(flag)
    if env_name is None:
        raise ValueError(f"Unknown engine flag {flag!r}; expected one of {FLAG_NAMES}")
    return os.environ.get(env_name) == "1"


def active_flag_vector() -> dict[str, bool]:
    """Stable flag vector for fingerprint / bake-id identity (spec §7)."""
    return {name: is_enabled(name) for name in FLAG_NAMES}


def grounding_mode_v1_enabled() -> bool:
    """Registry mode + dual-path validators + (later) gate bifurcation."""
    return is_enabled(FLAG_GROUNDING_MODE_V1)


def kit_enrichment_v1_enabled() -> bool:
    return is_enabled(FLAG_KIT_ENRICHMENT_V1)


def writer_prompt_v2_enabled() -> bool:
    return is_enabled(FLAG_WRITER_PROMPT_V2)


def derived_publish_allowed() -> bool:
    """Slice 1: derived-mode survivors are never lesson-ready for publish.

    Slice 3 enables publish only with operational ``teacher_review_tray_v1``
    and fail-closed derived gates. Until then this is hard-False.
    """
    return False
