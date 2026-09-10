"""Prompt templates for the Hramatka engine.

E4 composition (grounding-mode v1 §7 / slice 5):
- ``writer_prompt_v2`` ∧ ``grounding_mode_v1`` → mode-split authoring text
- ``writer_prompt_v2`` alone → force extractive-v5 / current extractive copy
- flags off → extractive template (byte-identical production path)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hramatka.engine import flags

_PROMPTS_DIR = Path(__file__).resolve().parent

# ``sentence-builder`` is intentionally specified for authoring before it is
# registered in the production pilot. Registered types resolve through the
# registry-owned effective-mode helper below.
_UNREGISTERED_AUTHORING_MODES: dict[str, str] = {"sentence-builder": "derived"}


def load_extractive_template() -> str:
    """The UA-first extractive-generation instruction block (SSOT)."""
    return (_PROMPTS_DIR / "extractive.md").read_text(encoding="utf-8")


def load_legacy_extractive_v4_template() -> str:
    """The immutable pre-count-plumbing prompt used only for measurement."""
    return (_PROMPTS_DIR / "extractive-v4.md").read_text(encoding="utf-8")


def load_writer_prompt_v2_template() -> str:
    """Mode-split authoring text (slice 5); only used under E4 both-on."""
    return (_PROMPTS_DIR / "writer-prompt-v2.md").read_text(encoding="utf-8")


def mode_split_authoring_active() -> bool:
    """True only when both composition flags that own mode-split text are on."""
    return flags.writer_prompt_v2_enabled() and flags.grounding_mode_v1_enabled()


def load_active_writer_template() -> str:
    """Select the writer instruction block under E4 composition semantics.

    Choice: force extractive-v5/current extractive copy when ``writer_prompt_v2``
    is on without ``grounding_mode_v1`` (spec §7 table — not a boot error).
    """
    if mode_split_authoring_active():
        return load_writer_prompt_v2_template()
    return load_extractive_template()


def content_digest(text: str, *, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def active_writer_prompt_version(*, extractive_fallback: str) -> str:
    """Honest identity for the template actually used.

    Flags-off / E4 partial-on keep *extractive_fallback* (historical
    ``extractive-v5:…`` constant) so bake identity stays byte-stable. Mode-split
    on uses a content digest of ``writer-prompt-v2.md``.
    """
    if mode_split_authoring_active():
        body = load_writer_prompt_v2_template()
        return f"writer-prompt-v2:{content_digest(body)}"
    return extractive_fallback


def format_request_lines(
    types: list[str],
    requested_counts: dict[str, int],
    *,
    with_modes: bool | None = None,
) -> str:
    """Render the count list; include ``[quoting|derived]`` under mode-split."""
    include_modes = mode_split_authoring_active() if with_modes is None else with_modes
    lines: list[str] = []
    for activity_type in types:
        count = requested_counts[activity_type]
        if include_modes:
            mode = grounding_mode_for_type(activity_type)
            lines.append(f"- {activity_type} [{mode}]: {count}")
        else:
            lines.append(f"- {activity_type}: {count}")
    return "\n".join(lines)


def grounding_mode_for_type(activity_type: str) -> str:
    """Return the effective authoring mode for *activity_type*.

    The import is local because ``registry`` imports prompt helpers to build
    prompts; this function is called only after registry construction.
    """
    from hramatka.engine import registry

    if activity_type in registry.ACTIVITY_REGISTRY:
        return registry.effective_grounding_mode(activity_type)
    return _UNREGISTERED_AUTHORING_MODES.get(activity_type, "quoting")
