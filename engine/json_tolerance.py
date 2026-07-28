"""Narrow, reusable tolerance for fenced or slightly malformed JSON output."""

from __future__ import annotations

import json
from collections.abc import Callable


def _extract_json(
    text: str,
    *,
    preferred_keys: frozenset[str] = frozenset(),
    preferred_object: Callable[[dict], bool] | None = None,
) -> dict | list | None:
    """Scan embedded JSON while preserving top-level container boundaries."""
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
        # Nested containers belong to this candidate; do not treat them as
        # roots after a successfully parsed top-level object or array.
        search_from = end
        if isinstance(obj, dict):
            if preferred_object is not None and preferred_object(obj):
                return obj
            if preferred_keys and preferred_keys <= set(obj):
                return obj
            if first_dict is None:
                first_dict = obj
        elif isinstance(obj, list) and all(isinstance(item, dict) for item in obj):
            if first_dict_list is None:
                first_dict_list = obj
    return first_dict if first_dict is not None else first_dict_list


def repair_trailing_commas(text: str) -> str:
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


def extract_json(
    text: str,
    *,
    preferred_keys: frozenset[str] = frozenset(),
    preferred_object: Callable[[dict], bool] | None = None,
) -> dict | list | None:
    """Extract embedded JSON, then apply the narrow trailing-comma repair."""
    extracted = _extract_json(
        text,
        preferred_keys=preferred_keys,
        preferred_object=preferred_object,
    )
    if extracted is not None:
        return extracted
    repaired = repair_trailing_commas(text)
    if repaired == text:
        return None
    return _extract_json(
        repaired,
        preferred_keys=preferred_keys,
        preferred_object=preferred_object,
    )


def repair_split_envelope(
    raw: str, *, required_keys: frozenset[str]
) -> tuple[dict, int] | None:
    """Merge adjacent, disjoint top-level objects only when the envelope closes."""
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
                    if required_keys <= set(merged):
                        return merged, len(objects)
        search_from = region_end
    return None
