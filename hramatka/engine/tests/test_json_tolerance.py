"""Bounded JSON scan — garbage openers must not stall a bake worker (#451)."""

from __future__ import annotations

import json
import time

import pytest

from hramatka.api.baking.engine_adapter_v3 import _parse_payload
from hramatka.engine import generate as G
from hramatka.engine.json_tolerance import extract_json, repair_split_envelope
from hramatka.engine.transport import GenerationUnparseable

_SLOTS = frozenset({"slots"})


def test_extract_json_still_finds_object_after_leading_braces() -> None:
    raw = "{" * 8 + '{"slots": [{"id": "P1-A1"}]}'
    assert extract_json(raw, preferred_keys=_SLOTS) == {"slots": [{"id": "P1-A1"}]}


def test_extract_json_bounds_brace_bomb() -> None:
    bomb = "{" * 250_000
    started = time.perf_counter()
    assert extract_json(bomb, preferred_keys=_SLOTS) is None
    assert time.perf_counter() - started < 0.5


def test_repair_split_envelope_bounds_brace_bomb() -> None:
    bomb = "{" * 250_000
    started = time.perf_counter()
    assert repair_split_envelope(bomb, required_keys=_SLOTS) is None
    assert time.perf_counter() - started < 0.5


def test_extract_json_bounds_bracket_bomb() -> None:
    bomb = "[" * 250_000
    started = time.perf_counter()
    assert extract_json(bomb, preferred_keys=_SLOTS) is None
    assert time.perf_counter() - started < 0.5


def test_repair_split_envelope_bounds_bracket_bomb() -> None:
    bomb = "[" * 250_000
    started = time.perf_counter()
    assert repair_split_envelope(bomb, required_keys=_SLOTS) is None
    assert time.perf_counter() - started < 0.5


def test_repair_split_envelope_adjacent_decode_recursion_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adjacent-object raw_decode RecursionError must not escape (#451/#463)."""
    calls = {"n": 0}
    real_raw_decode = json.JSONDecoder.raw_decode

    def boom(self: json.JSONDecoder, s: str, idx: int = 0) -> tuple[object, int]:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RecursionError("adjacent opener recursion")
        return real_raw_decode(self, s, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", boom)
    assert repair_split_envelope('{"a": 1}, {"slots": []}', required_keys=_SLOTS) is None


def test_repair_split_envelope_loads_recursion_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merged-region json.loads RecursionError must not escape (#451/#463)."""

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("merged envelope recursion")

    monkeypatch.setattr(json, "loads", boom)
    assert repair_split_envelope('{"a": 1}, {"slots": []}', required_keys=_SLOTS) is None


def test_v3_parse_payload_adjacent_recursion_is_typed_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v3 bake path turns adjacent RecursionError into GenerationUnparseable."""
    calls = {"n": 0}
    real_raw_decode = json.JSONDecoder.raw_decode

    def boom(self: json.JSONDecoder, s: str, idx: int = 0) -> tuple[object, int]:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RecursionError("adjacent opener recursion")
        return real_raw_decode(self, s, idx)

    # Drive the adjacent-object repair path; extract has already failed closed.
    monkeypatch.setattr(
        "hramatka.api.baking.engine_adapter_v3.extract_json",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(json.JSONDecoder, "raw_decode", boom)
    with pytest.raises(GenerationUnparseable, match="invalid JSON"):
        _parse_payload('{"a": 1}, {"slots": []}')
    assert calls["n"] >= 2

def test_legacy_generate_adjacent_recursion_is_typed_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy generate path turns adjacent RecursionError into GenerationUnparseable."""
    calls = {"n": 0}
    real_raw_decode = json.JSONDecoder.raw_decode

    def boom(self: json.JSONDecoder, s: str, idx: int = 0) -> tuple[object, int]:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RecursionError("adjacent opener recursion")
        return real_raw_decode(self, s, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", boom)
    raw = '{"preamble": true}, {"activities": [{"type": "cloze"}]}'

    with pytest.raises(G.GenerationUnparseable):
        G.generate("anchor", generator=lambda _p: raw, prompt_builder=lambda *a, **k: "p")
