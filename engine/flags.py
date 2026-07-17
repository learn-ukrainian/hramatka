"""Composable engine feature flags (grounding-mode v1 §7).

Three independent flags — never one mega-flag. Active vector is part of
bake identity via ``fingerprint_inputs``.

``teacher_review_tray_v1`` is a hard publish dependency (G4), not part of the
§7 composition flag vector.
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

# G4 publish dependency (not a §7 composition flag — not in FLAG_NAMES).
TRAY_ENV: Final = "HRAMATKA_TEACHER_REVIEW_TRAY_V1"
TEACHER_REVIEW_TRAY_V1: Final = "teacher_review_tray_v1"


class DerivedPublishDependencyError(RuntimeError):
    """Raised when derived-mode content is refused for a hard dependency."""

    def __init__(self, dependency: str, detail: str) -> None:
        self.dependency = dependency
        self.detail = detail
        super().__init__(detail)


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
    """Registry mode + dual-path validators + gate bifurcation."""
    return is_enabled(FLAG_GROUNDING_MODE_V1)


def kit_enrichment_v1_enabled() -> bool:
    return is_enabled(FLAG_KIT_ENRICHMENT_V1)


def writer_prompt_v2_enabled() -> bool:
    return is_enabled(FLAG_WRITER_PROMPT_V2)


def teacher_review_tray_v1_operational() -> bool:
    """Hard ordering constraint before derived-mode content publishes (G4)."""
    return os.environ.get(TRAY_ENV) == "1"


def derived_publish_allowed() -> bool:
    """Derived-mode lesson publish requires mode flag + operational tray (G4).

    Fail-closed derived gates still run under ``grounding_mode_v1`` even when
    this is False; publish/selection readiness is refused with an explicit
    dependency error until the tray is operational.
    """
    return grounding_mode_v1_enabled() and teacher_review_tray_v1_operational()


def assert_derived_publish_allowed() -> None:
    """Raise ``DerivedPublishDependencyError`` when derived publish is blocked."""
    if not grounding_mode_v1_enabled():
        raise DerivedPublishDependencyError(
            FLAG_GROUNDING_MODE_V1,
            "dependency error: grounding_mode_v1 is off; derived-mode content "
            "cannot publish",
        )
    if not teacher_review_tray_v1_operational():
        raise DerivedPublishDependencyError(
            TEACHER_REVIEW_TRAY_V1,
            "dependency error: teacher_review_tray_v1 is not operational; "
            "derived-mode content cannot publish (grounding-mode v1 G4)",
        )
