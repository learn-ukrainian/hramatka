"""Generation (slice-1 §5) — Gemma 4, toolless, $0 AIS. Generator INJECTABLE.

The real generator is `call_gemma`, an `AISGeneratorPort` over the locked route
(`google-ais/gemma-4-31b-it`, toolless) wired to the real HTTP client (step-4a
swap) whose AIS key comes from the `HRAMATKA_AIS_API_KEY` env var — no
`scripts.*` import and no dev-home key path. `generate()` takes any
`generator: Callable[[str], str]`, so unit tests inject a MOCK returning fixture
JSON — NO real Gemma in pytest.

Robustness: a `SystemExit` from a transport guard becomes a typed
`GeneratorUnavailable` (never crashes the pipeline); unparseable output is
retried once, then raised as `GenerationUnparseable`.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .providers import make_generator
from .transport import (
    GEMMA_MODEL,
    GenerationUnparseable,
    GeneratorUnavailable,
)

__all__ = [
    "GEMMA_MODEL",
    "GenerationUnparseable",
    "GeneratorUnavailable",
    "call_gemma",
    "extract_json",
    "generate",
    "generate_baseline_v1",
    "make_generator",
]

LEGACY_GENERATOR_VERSION = "extractive-v1"

# The default real generator: the locked Gemma AIS route over the real HTTP
# transport (env key, toolless). Constructing it performs no network I/O and
# needs no key — only an actual generation resolves the key and calls out.
call_gemma = make_generator("gemma-ais")


def extract_json(text: str) -> dict | None:
    """Extract the first balanced top-level {...} JSON object from `text`.

    Tolerates leading prose / a stripped <thought> block / trailing text.
    Ignores braces inside strings. Returns the parsed dict, or None.
    """
    if not text:
        return None
    stripped = text.strip()
    try:  # fast path: the whole thing is already a JSON object
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        break  # try the next '{'
        start = text.find("{", start + 1)
    return None


def generate_baseline_v1(
    anchor: str,
    level: str = "B1",
    types: list[str] | None = None,
    *,
    counts: dict[str, int] | None = None,
    generator: Callable[[str], str] = call_gemma,
    grounding_pack: str = "",
    prompt_builder: Callable[[str, str, list[str], str], str] | None = None,
) -> list[dict]:
    """Frozen pre-Wave-0 one-shot extractive path for measurement.

    This preserves the original whole-prompt content, JSON extraction, and
    retry semantics.  The public orchestrator below plans this same call via
    the typed registry, rather than splitting the three legacy types into
    content-drifting model calls.
    """
    # The measurement baseline retains its original one-of-each prompt.  It
    # accepts the production call shape solely so `_run` can forward its
    # normalized plan without special casing the orchestration path, but must
    # reject a quota that would make its frozen prompt misleading.
    if counts is not None and any(count != 1 for count in counts.values()):
        raise ValueError("generate_baseline_v1 only supports one candidate per type")
    types = types or ["true-false", "cloze", "match-up"]
    build = prompt_builder or _default_prompt_builder
    prompt = build(anchor, level, types, grounding_pack)
    return _generate_from_prompt(prompt, generator)


def _generate_from_prompt(
    prompt: str, generator: Callable[[str], str], *, retain_all: bool = False
) -> list[object]:
    """Legacy JSON parsing/retry semantics shared by baseline and registry."""

    raw = generator(prompt)
    obj = extract_json(raw)
    if obj is None:
        raw = generator(prompt)  # retry once
        obj = extract_json(raw)
    if obj is None:
        raise GenerationUnparseable(
            "Gemma output was not parseable JSON after one retry."
        )

    activities = obj.get("activities")
    if not isinstance(activities, list):
        # A bare single activity object is tolerated.
        if obj.get("type"):
            return [obj]
        raise GenerationUnparseable(
            "Parsed JSON has no 'activities' array and is not a single activity."
        )
    if retain_all:
        return activities
    return [activity for activity in activities if isinstance(activity, dict)]


def generate(
    anchor: str,
    level: str = "B1",
    types: list[str] | None = None,
    *,
    counts: dict[str, int] | None = None,
    generator: Callable[[str], str] = call_gemma,
    grounding_pack: str = "",
    prompt_builder: Callable[..., str] | None = None,
) -> list[object]:
    """Registry-planned typed candidate generation.

    ``counts`` requests the exact number of candidates per type.  The default
    remains one candidate for every requested type.  Wave 0 entries deliberately
    resolve to the same count-aware extractive prompt, so equal prompt/count
    plans coalesce into one model call.  Future entries may use different
    prompts without changing callers.
    """
    from .registry import ACTIVITY_REGISTRY, entries_for

    requested = list(types if types is not None else (counts or ACTIVITY_REGISTRY))
    entries = entries_for(requested)
    unknown_counts = sorted(set(counts or {}) - set(requested))
    if unknown_counts:
        unsupported = ", ".join(unknown_counts)
        raise ValueError(f"counts include unsupported requested type(s): {unsupported}")
    requested_counts = {
        activity_type: (counts or {}).get(activity_type, 1) for activity_type in requested
    }
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 1
           for count in requested_counts.values()):
        raise ValueError("counts values must be positive integers")

    # Include the normalized plan in the key as well as the rendered text.  A
    # custom builder that happens to emit equal text must not cause candidate
    # requests with distinct quotas to share a model response.
    count_signature = tuple(
        (activity_type, requested_counts[activity_type]) for activity_type in requested
    )
    prompt_groups: dict[tuple[str, tuple[tuple[str, int], ...]], list[str]] = {}
    for entry in entries:
        builder = prompt_builder or entry.prompt_builder
        prompt = builder(anchor, level, requested, grounding_pack, counts=requested_counts)
        prompt_groups.setdefault((prompt, count_signature), []).append(entry.activity_type)

    candidates: list[object] = []
    for prompt, _count_signature in prompt_groups:
        # Preserve every emitted member, including primitives and types outside
        # the target bank.  The pipeline turns each into a visible rejection
        # when appropriate; generation must never silently discard evidence.
        candidates.extend(_generate_from_prompt(prompt, generator, retain_all=True))
    return candidates


def _default_prompt_builder(
    anchor: str, level: str, types: list[str], grounding_pack: str
) -> str:
    """Assemble the pinned pre-count-plumbing prompt for measurement only.

    This deliberately preserves the v4 baseline's original literal request
    token and must not be used by the production candidate-bank generator.
    """
    from .prompts import load_legacy_extractive_v4_template

    template = load_legacy_extractive_v4_template()
    return (
        f"{template}\n\n"
        f"=== GROUNDING PACK ===\n{grounding_pack}\n\n"
        f"=== ТЕКСТ-ОПОРА (anchor, рівень {level}) ===\n{anchor}\n"
    )
