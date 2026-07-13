"""Tests for generate.py — injectable generator, JSON extraction, retry,
typed failures. NO real Gemma (mock generators only).
"""

from __future__ import annotations

import json

import pytest

from hramatka.engine import generate as G
from hramatka.engine.transport import AISGeneratorPort


def _pb(anchor, level, types, grounding, *, counts=None):
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


def test_generate_baseline_rejects_counts_that_break_its_frozen_prompt():
    with pytest.raises(ValueError, match="one candidate per type"):
        G.generate_baseline_v1("anchor", counts={"cloze": 2})


def test_generate_coalesces_count_aware_prompt_and_parses_multiple_candidates_of_one_type():
    prompts: list[str] = []

    def gen(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "activities": [
                    {"type": "cloze", "instruction": "Перший пропуск."},
                    {"type": "cloze", "instruction": "Другий пропуск."},
                    {"type": "quiz", "instruction": "Перевірка розуміння."},
                ]
            },
            ensure_ascii=False,
        )

    activities = G.generate(
        "опора",
        types=["cloze", "quiz"],
        counts={"cloze": 2, "quiz": 1},
        generator=gen,
        grounding_pack="пакет",
    )

    assert activities == [
        {"type": "cloze", "instruction": "Перший пропуск."},
        {"type": "cloze", "instruction": "Другий пропуск."},
        {"type": "quiz", "instruction": "Перевірка розуміння."},
    ]
    assert len(prompts) == 1  # one prompt covers both types and their quotas
    assert "- cloze: 2" in prompts[0]
    assert "- quiz: 1" in prompts[0]
    assert "різні завдання" in prompts[0]


# --- AISGeneratorPort (the private transport) ------------------------------
def test_ais_port_wraps_transport_systemexit():
    # A fail-closed transport guard must degrade to GeneratorUnavailable, never
    # crash the worker. Key supplied directly so no env is needed.
    def boom(prompt, *, api_key, model, timeout_s):
        raise SystemExit(2)

    with pytest.raises(G.GeneratorUnavailable):
        AISGeneratorPort(api_key="test-key", transport=boom)("prompt")


def test_ais_port_passes_through_text():
    port = AISGeneratorPort(
        api_key="test-key",
        transport=lambda prompt, *, api_key, model, timeout_s: '{"activities": []}',
    )
    assert port("prompt") == '{"activities": []}'


def test_ais_port_missing_key_is_unavailable(monkeypatch):
    # No hardcoded path and no dev-home key: without HRAMATKA_AIS_API_KEY the
    # locked route is simply unavailable (never a crash, never a leaked value).
    monkeypatch.delenv("HRAMATKA_AIS_API_KEY", raising=False)
    with pytest.raises(G.GeneratorUnavailable) as exc:
        AISGeneratorPort()("prompt")
    assert "HRAMATKA_AIS_API_KEY" in str(exc.value)
