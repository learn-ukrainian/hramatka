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
    "make_generator",
]

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


def generate(
    anchor: str,
    level: str = "B1",
    types: list[str] | None = None,
    *,
    generator: Callable[[str], str] = call_gemma,
    grounding_pack: str = "",
    prompt_builder: Callable[[str, str, list[str], str], str] | None = None,
) -> list[dict]:
    """Build the prompt, call the (injectable) generator, parse the first
    balanced JSON object, and return the raw `activities` list (evidence-
    carrying SUPERSET — projection/stripping happens in schema.py).

    Retries once on unparseable output; a 2nd failure raises
    GenerationUnparseable. Transport failure raises GeneratorUnavailable
    (from `call_gemma`). Both are caught by the pipeline as gate-fails.
    """
    types = types or ["true-false", "cloze", "match-up"]
    build = prompt_builder or _default_prompt_builder
    prompt = build(anchor, level, types, grounding_pack)

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
    return [a for a in activities if isinstance(a, dict)]


def _default_prompt_builder(
    anchor: str, level: str, types: list[str], grounding_pack: str
) -> str:
    """Assemble the runtime prompt from the extractive template + grounding
    pack + anchor. The template file is the SSOT for the instruction block.
    """
    from .prompts import load_extractive_template

    template = load_extractive_template()
    return (
        f"{template}\n\n"
        f"=== GROUNDING PACK ===\n{grounding_pack}\n\n"
        f"=== ТЕКСТ-ОПОРА (anchor, рівень {level}) ===\n{anchor}\n"
    )
