"""Tests for generate.py — injectable generator, JSON extraction, retry,
typed failures. NO real Gemma (mock generators only).
"""

from __future__ import annotations

import json

import pytest

from engine import generate as G


def _pb(a, l, t, g):
    return "PROMPT"


# --- extract_json ----------------------------------------------------------
def test_extract_json_whole_object():
    assert G.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_surrounding_prose():
    assert G.extract_json('ось: {"a": 1} кінець') == {"a": 1}


def test_extract_json_strips_thought_wrapper():
    assert G.extract_json("<thought>...</thought>\n{\"a\": 2}") == {"a": 2}


def test_extract_json_ignores_braces_in_strings():
    assert G.extract_json('{"s": "a { b } c", "ok": true}') == {"s": "a { b } c", "ok": True}


def test_extract_json_returns_none_on_garbage():
    assert G.extract_json("no json here") is None
    assert G.extract_json("") is None


# --- generate: injectable + retry ------------------------------------------
def test_generate_returns_activities_list():
    def gen(_p):
        return json.dumps({"activities": [{"type": "cloze"}, {"type": "true-false"}]})

    acts = G.generate("anchor", generator=gen, prompt_builder=_pb)
    assert acts == [{"type": "cloze"}, {"type": "true-false"}]


def test_generate_retries_once_then_succeeds():
    calls = {"n": 0}

    def flaky(_p):
        calls["n"] += 1
        return "garbage" if calls["n"] == 1 else json.dumps({"activities": [{"type": "cloze"}]})

    acts = G.generate("anchor", generator=flaky, prompt_builder=_pb)
    assert acts == [{"type": "cloze"}]
    assert calls["n"] == 2


def test_generate_raises_unparseable_after_two_failures():
    with pytest.raises(G.GenerationUnparseable):
        G.generate("anchor", generator=lambda _p: "nope", prompt_builder=_pb)


def test_generate_accepts_single_activity_object():
    acts = G.generate(
        "anchor",
        generator=lambda _p: json.dumps({"type": "cloze", "instruction": "x", "text": "{gap}"}),
        prompt_builder=_pb,
    )
    assert acts == [{"type": "cloze", "instruction": "x", "text": "{gap}"}]


# --- call_gemma SystemExit -> GeneratorUnavailable -------------------------
def test_call_gemma_wraps_systemexit(monkeypatch):
    import scripts.ai_agent_bridge._opencode as oc

    def boom(*_a, **_k):
        raise SystemExit(2)

    monkeypatch.setattr(oc, "_invoke_opencode", boom)
    with pytest.raises(G.GeneratorUnavailable):
        G.call_gemma("prompt")


def test_call_gemma_passes_through_text(monkeypatch):
    import scripts.ai_agent_bridge._opencode as oc

    monkeypatch.setattr(oc, "_invoke_opencode", lambda *a, **k: '{"activities": []}')
    assert G.call_gemma("prompt") == '{"activities": []}'
