"""Generation (slice-1 §5) — Gemma 4, toolless, $0 AIS. Generator INJECTABLE.

The real generator is `call_gemma`, an `AISGeneratorPort` over the locked route
(`google-ais/gemma-4-31b-it`, toolless) wired to the real HTTP client (step-4a
swap) whose AIS key comes from the `HRAMATKA_AIS_API_KEY` env var — no
`scripts.*` import and no dev-home key path. `generate()` takes any
`generator: Callable[[str], str]`, so unit tests inject a MOCK returning fixture
JSON — NO real Gemma in pytest.

Robustness: a `SystemExit` from a transport guard becomes a typed
`GeneratorUnavailable` (never crashes the pipeline); unparseable output is
retried with a bounded budget, then raised as `GenerationUnparseable`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from . import prompt_pack
from .providers import make_generator
from .transport import (
    GEMMA_MODEL,
    GenerationUnparseable,
    GeneratorUnavailable,
    activity_model_registry,
    generator_model_id,
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
    "generate_prompt_pack",
]

LEGACY_GENERATOR_VERSION = "extractive-v1"
RAW_PARSE_FAILURE_MAX_BYTES = 64 * 1024
GENERATION_PARSE_ATTEMPTS = 3
_ENVELOPE_SHAPE_ERROR = "Prompt-pack response requires activities and citations arrays."

# The default real generator: the locked Gemma AIS route over the real HTTP
# transport (env key, toolless). Constructing it performs no network I/O and
# needs no key — only an actual generation resolves the key and calls out.
call_gemma = make_generator("gemma-ais")


def _extract_json(text: str) -> dict | list | None:
    """Extract the best activity-shaped JSON object or array from `text`.

    Tolerates leading prose / a stripped <thought> block / trailing text.
    Prefers a dict with usable ``activities``, then any dict, then an
    all-dicts list. Returns None when no candidate has one of those shapes.
    """
    if not text:
        return None

    decoder = json.JSONDecoder()
    first_dict: dict | None = None
    first_dict_list: list | None = None
    search_from = 0
    while search_from < len(text):
        starts = [text.find(char, search_from) for char in "[{"]
        starts = [start for start in starts if start >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            search_from = start + 1
            continue
        # Nested containers belong to this candidate; do not bypass the
        # documented one-level wrapper boundary by treating them as new roots.
        search_from = end
        if isinstance(obj, dict):
            if "activities" in obj and _activities_from_parsed(obj) is not None:
                return obj
            if first_dict is None:
                first_dict = obj
        elif isinstance(obj, list) and all(isinstance(item, dict) for item in obj):
            if first_dict_list is None:
                first_dict_list = obj
    return first_dict if first_dict is not None else first_dict_list


def _repair_trailing_commas(text: str) -> str:
    """Remove only JSON trailing commas outside strings, preserving all content."""
    repaired: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            repaired.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            repaired.append(char)
            continue
        if char == ",":
            next_index = index + 1
            while next_index < len(text) and text[next_index].isspace():
                next_index += 1
            if next_index < len(text) and text[next_index] in "}]":
                continue
        repaired.append(char)
    return "".join(repaired)


def extract_json(text: str) -> dict | list | None:
    """Extract JSON, then apply the narrow trailing-comma repair if needed."""
    extracted = _extract_json(text)
    if extracted is not None:
        return extracted
    repaired = _repair_trailing_commas(text)
    return _extract_json(repaired) if repaired != text else None


def _repair_split_envelope(raw: str) -> tuple[dict, int] | None:
    """Merge adjacent, disjoint top-level objects from a split pack envelope.

    The pack response contract is one object, but the known host failure closes
    the activities object before emitting a comma and a citations object.  Keep
    this deliberately narrow: only a contiguous comma-separated object region
    that can be parsed as a JSON array, has disjoint keys, and supplies both
    envelope keys can be repaired.
    """
    decoder = json.JSONDecoder()
    search_from = 0
    while search_from < len(raw):
        starts = [raw.find(char, search_from) for char in "[{"]
        starts = [start for start in starts if start >= 0]
        if not starts:
            return None
        start = min(starts)
        try:
            first, first_end = decoder.raw_decode(raw, start)
        except ValueError:
            search_from = start + 1
            continue
        if not isinstance(first, dict):
            search_from = first_end
            continue

        region_end = first_end
        while True:
            next_start = region_end
            while next_start < len(raw) and raw[next_start].isspace():
                next_start += 1
            if next_start >= len(raw) or raw[next_start] != ",":
                break
            next_start += 1
            while next_start < len(raw) and raw[next_start].isspace():
                next_start += 1
            try:
                next_object, next_end = decoder.raw_decode(raw, next_start)
            except ValueError:
                break
            if not isinstance(next_object, dict):
                break
            region_end = next_end

        if region_end != first_end:
            try:
                objects = json.loads(f"[{raw[start:region_end]}]")
            except json.JSONDecodeError:
                objects = None
            if isinstance(objects, list) and all(isinstance(item, dict) for item in objects):
                merged: dict = {}
                for item in objects:
                    if not set(merged).isdisjoint(item):
                        break
                    merged.update(item)
                else:
                    if {"activities", "citations"} <= set(merged):
                        return merged, len(objects)
        search_from = region_end
    return None


def _record_envelope_repaired(context: dict, object_count: int) -> None:
    """Record the accepted structural repair through the durable trace path."""
    from .providers import telemetry_ctx

    telemetry = telemetry_ctx.get()
    if telemetry is not None:
        telemetry.record_event(
            {
                "event": "envelope_repaired",
                "phase": context["phase_request"]["phase"],
                "object_count": object_count,
            }
        )


def _persist_raw_parse_failure(raw: str, out_dir: str | Path | None, attempt: int) -> None:
    """Keep failed model output in the private phase artifact directory only."""
    if out_dir is None:
        return
    raw_path = Path(out_dir) / f"generation-raw-attempt{attempt}.txt"
    capped = raw.encode("utf-8")[:RAW_PARSE_FAILURE_MAX_BYTES]
    raw_path.write_text(capped.decode("utf-8", errors="ignore"), encoding="utf-8")


def _activities_from_parsed(obj: dict | list) -> list[object] | None:
    """Accept documented one-level host shape variants without validating content."""
    if isinstance(obj, list):
        return obj if all(isinstance(activity, dict) for activity in obj) else None

    activities = obj.get("activities")
    if isinstance(activities, list):
        return activities
    if isinstance(activities, dict):
        values = list(activities.values())
        return values if all(isinstance(activity, dict) for activity in values) else None
    if obj.get("type"):
        return [obj]

    # Some hosts add one response-envelope object. Deliberately do not recurse:
    # only the sole wrapper level is a tolerated transport shape variation.
    if len(obj) == 1:
        wrapped = next(iter(obj.values()))
        if isinstance(wrapped, (dict, list)):
            return _activities_from_parsed_without_wrapper(wrapped)
    return None


def _activities_from_parsed_without_wrapper(obj: dict | list) -> list[object] | None:
    """Apply ordinary activity rules after exactly one wrapper descent."""
    if isinstance(obj, list):
        return obj if all(isinstance(activity, dict) for activity in obj) else None
    activities = obj.get("activities")
    if isinstance(activities, list):
        return activities
    if isinstance(activities, dict):
        values = list(activities.values())
        return values if all(isinstance(activity, dict) for activity in values) else None
    return [obj] if obj.get("type") else None


def generate_baseline_v1(
    anchor: str,
    level: str = "B1",
    types: list[str] | None = None,
    *,
    counts: dict[str, int] | None = None,
    generator: Callable[[str], str] = call_gemma,
    grounding_pack: str = "",
    prompt_builder: Callable[[str, str, list[str], str], str] | None = None,
    out_dir: str | Path | None = None,
    _raw_attempt_counter: list[int] | None = None,
    anchor_snapshot: dict | None = None,
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
    del anchor_snapshot  # baseline accepts snapshot for call-shape parity only
    if counts is not None and any(count != 1 for count in counts.values()):
        raise ValueError("generate_baseline_v1 only supports one candidate per type")
    types = types or ["true-false", "cloze", "match-up"]
    from .providers import telemetry_ctx

    ctx = telemetry_ctx.get()
    if ctx is not None:
        ctx.activity_types = list(types)
    build = prompt_builder or _default_prompt_builder
    prompt = build(anchor, level, types, grounding_pack)
    return _generate_from_prompt(
        prompt,
        generator,
        out_dir=out_dir,
        raw_attempt_counter=_raw_attempt_counter,
    )


def _generate_from_prompt(
    prompt: str,
    generator: Callable[[str], str],
    *,
    retain_all: bool = False,
    out_dir: str | Path | None = None,
    raw_attempt_counter: list[int] | None = None,
) -> list[object]:
    """Legacy JSON parsing/retry semantics shared by baseline and registry."""

    attempts = raw_attempt_counter if raw_attempt_counter is not None else [0]
    parsed_but_rejected = False
    for _ in range(GENERATION_PARSE_ATTEMPTS):
        attempts[0] += 1
        raw = generator(prompt)
        obj = extract_json(raw)
        if obj is not None:
            activities = _activities_from_parsed(obj)
            if activities is not None:
                actual_model = generator_model_id.get()
                if actual_model:
                    registry = activity_model_registry.get()
                    if registry is not None:
                        for act in activities:
                            registry[id(act)] = actual_model
                if retain_all:
                    return activities
                return [activity for activity in activities if isinstance(activity, dict)]
            parsed_but_rejected = True
        _persist_raw_parse_failure(raw, out_dir, attempts[0])

    if parsed_but_rejected:
        raise GenerationUnparseable(
            "Parsed JSON has no 'activities' array and is not a single activity."
        )
    raise GenerationUnparseable("Gemma output was not parseable JSON after bounded retries.")


def generate_prompt_pack(
    context: dict,
    *,
    generator: Callable[[str], str] = call_gemma,
    out_dir: str | Path | None = None,
    _raw_attempt_counter: list[int] | None = None,
) -> list[object]:
    """Generate one engineered phase envelope and validate citations first.

    The returned value deliberately contains *only* raw activities.  Citation
    data is a private response-envelope concern and must never leak into an
    activity passed to the public raw contracts or projectors.
    """
    prompt = prompt_pack.render_phase_prompt(context)
    attempts = _raw_attempt_counter if _raw_attempt_counter is not None else [0]
    parse_error: str | None = None
    last_failure_was_envelope_validation = False
    for _ in range(GENERATION_PARSE_ATTEMPTS):
        attempts[0] += 1
        raw = generator(prompt)
        parsed = extract_json(raw)
        try:
            activities = prompt_pack.validate_response_envelope(parsed, context)
            actual_model = generator_model_id.get()
            if actual_model:
                registry = activity_model_registry.get()
                if registry is not None:
                    for act in activities:
                        registry[id(act)] = actual_model
            return activities
        except prompt_pack.PromptPackError as exc:
            parse_error = str(exc)
            # ``parsed is None`` is genuinely malformed/raw-unparseable output.
            # Any parsed response that violates the private pack contract stays
            # a PromptPackError so the adapter can take its legacy fallback.
            last_failure_was_envelope_validation = parsed is not None
            if parse_error == _ENVELOPE_SHAPE_ERROR:
                repaired = _repair_split_envelope(raw)
                if repaired is not None:
                    payload, object_count = repaired
                    try:
                        activities = prompt_pack.validate_response_envelope(payload, context)
                        actual_model = generator_model_id.get()
                        if actual_model:
                            registry = activity_model_registry.get()
                            if registry is not None:
                                for act in activities:
                                    registry[id(act)] = actual_model
                    except prompt_pack.PromptPackError as repair_exc:
                        parse_error = str(repair_exc)
                        last_failure_was_envelope_validation = True
                    else:
                        _record_envelope_repaired(context, object_count)
                        return activities
            _persist_raw_parse_failure(raw, out_dir, attempts[0])
    if last_failure_was_envelope_validation:
        raise prompt_pack.PromptPackError(
            "Prompt-pack response failed its citation envelope after bounded retries: "
            + (parse_error or "invalid response envelope")
        )
    raise GenerationUnparseable(
        "Prompt-pack response was not parseable JSON after bounded retries: "
        + (parse_error or "unparseable JSON")
    )


def generate(
    anchor: str,
    level: str = "B1",
    types: list[str] | None = None,
    *,
    counts: dict[str, int] | None = None,
    generator: Callable[[str], str] = call_gemma,
    grounding_pack: str = "",
    prompt_builder: Callable[..., str] | None = None,
    out_dir: str | Path | None = None,
    _raw_attempt_counter: list[int] | None = None,
    anchor_snapshot: dict | None = None,
    prompt_pack_context: dict | None = None,
) -> list[object]:
    """Registry-planned typed candidate generation.

    ``counts`` requests the exact number of candidates per type.  The default
    remains one candidate for every requested type.  Wave 0 entries deliberately
    resolve to the same count-aware extractive prompt, so equal prompt/count
    plans coalesce into one model call.  Future entries may use different
    prompts without changing callers.
    """
    if prompt_pack_context is not None:
        # The context is precomputed once by the adapter.  Do not fall back to
        # a type-oriented prompt when a pack preflight rejects the phase.
        return generate_prompt_pack(
            prompt_pack_context,
            generator=generator,
            out_dir=out_dir,
            _raw_attempt_counter=_raw_attempt_counter,
        )

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
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 1
        for count in requested_counts.values()
    ):
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
        prompt = builder(
            anchor,
            level,
            requested,
            grounding_pack,
            counts=requested_counts,
            anchor_snapshot=anchor_snapshot,
        )
        prompt_groups.setdefault((prompt, count_signature), []).append(entry.activity_type)

    candidates: list[object] = []
    raw_attempt_counter = _raw_attempt_counter if _raw_attempt_counter is not None else [0]
    from .providers import telemetry_ctx

    for prompt, _count_signature in prompt_groups:
        active_types = prompt_groups[(prompt, _count_signature)]
        ctx = telemetry_ctx.get()
        if ctx is not None:
            ctx.activity_types = list(active_types)
        # Preserve every emitted member, including primitives and types outside
        # the target bank.  The pipeline turns each into a visible rejection
        # when appropriate; generation must never silently discard evidence.
        candidates.extend(
            _generate_from_prompt(
                prompt,
                generator,
                retain_all=True,
                out_dir=out_dir,
                raw_attempt_counter=raw_attempt_counter,
            )
        )
    return candidates


def _default_prompt_builder(anchor: str, level: str, types: list[str], grounding_pack: str) -> str:
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
