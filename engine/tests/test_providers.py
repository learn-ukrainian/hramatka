"""Tests for providers.py — the real OpenAI-compatible HTTP transport + the
name→generator registry. NO real network: every call is driven through an
`httpx.MockTransport`, so the actual provider API is never hit.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from hramatka.engine import providers
from hramatka.engine.transport import GEMMA_MODEL, AISGeneratorPort, GeneratorUnavailable

OK_BODY = {"choices": [{"message": {"content": '{"activities": []}'}}]}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _seq(steps: list, ok_body: dict | None = None):
    """A MockTransport handler that walks `steps` (an HTTP status int or the
    string 'timeout') per request, returning a counter so tests can assert how
    many attempts were made."""
    body = ok_body if ok_body is not None else OK_BODY
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        step = steps[min(calls["n"], len(steps) - 1)]
        calls["n"] += 1
        if step == "timeout":
            raise httpx.TimeoutException("simulated timeout")
        if step == 200:
            return httpx.Response(200, json=body)
        return httpx.Response(step)

    return handler, calls


def _transport(handler) -> providers.HttpChatTransport:
    return providers.HttpChatTransport(base_url="https://prov.example/v1", client=_client(handler))


# --- request shape ---------------------------------------------------------
def test_request_is_toolless_openai_shape_with_bearer_auth():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    out = _transport(handler)("PROMPT-BODY", api_key="secret-key", model="m1", timeout_s=5)
    assert out == '{"activities": []}'
    assert seen["url"] == "https://prov.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"]["model"] == "m1"
    assert seen["body"]["messages"] == [{"role": "user", "content": "PROMPT-BODY"}]
    assert "tools" not in seen["body"]  # TOOLLESS by construction


def test_wire_model_strips_our_provider_prefix():
    """Regression: bake-off 2026-07-10 — sending the canonical routed id
    ("google-ais/gemma-4-31b-it") to the provider returned HTTP 404 for every
    cell. The wire payload must carry the bare model id; the canonical id stays
    in fingerprints/meta only."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    _transport(handler)("p", api_key="k", model="google-ais/gemma-4-31b-it", timeout_s=5)
    assert seen["body"]["model"] == "gemma-4-31b-it"
    assert "/" not in seen["body"]["model"]


# --- retry semantics: single retry on 5xx / timeout ------------------------
def test_retries_once_on_5xx_then_succeeds():
    handler, calls = _seq([503, 200])
    out = _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert out == '{"activities": []}'
    assert calls["n"] == 2


def test_two_5xx_raise_generator_unavailable_after_one_retry():
    handler, calls = _seq([503, 500])
    with pytest.raises(GeneratorUnavailable):
        _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert calls["n"] == 2  # exactly one retry, no third attempt


def test_retries_once_on_timeout_then_succeeds():
    handler, calls = _seq(["timeout", 200])
    out = _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert out == '{"activities": []}'
    assert calls["n"] == 2


def test_two_timeouts_raise_generator_unavailable():
    handler, calls = _seq(["timeout", "timeout"])
    with pytest.raises(GeneratorUnavailable):
        _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert calls["n"] == 2


def test_4xx_is_unavailable_and_not_retried():
    handler, calls = _seq([401])
    with pytest.raises(GeneratorUnavailable):
        _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert calls["n"] == 1  # auth/quota errors are terminal, never retried


def test_malformed_envelope_is_unavailable():
    handler, _ = _seq([200], ok_body={"nonsense": True})
    with pytest.raises(GeneratorUnavailable):
        _transport(handler)("p", api_key="k", model="m", timeout_s=5)


# --- secret + content hygiene ----------------------------------------------
def test_logs_sizes_never_content_or_key(caplog):
    handler, _ = _seq([200])
    with caplog.at_level(logging.INFO, logger="hramatka.engine.providers"):
        _transport(handler)(
            "SENSITIVE-PROMPT-BODY", api_key="TOP-SECRET-KEY", model="m", timeout_s=5
        )
    text = caplog.text
    assert "prompt_bytes" in text and "response_bytes" in text
    assert "TOP-SECRET-KEY" not in text  # key never logged
    assert "SENSITIVE-PROMPT-BODY" not in text  # content never logged


# --- registry --------------------------------------------------------------
def test_make_generator_gemma_ais_uses_locked_route():
    gen = providers.make_generator("gemma-ais")
    assert isinstance(gen, AISGeneratorPort)
    assert gen._model == GEMMA_MODEL
    assert isinstance(gen._transport, providers.HttpChatTransport)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("deepseek", providers.DEEPSEEK_MODEL),
        ("deepseek-v4-flash", providers.DEEPSEEK_V4_FLASH_MODEL),
        ("deepseek-v4-pro", providers.DEEPSEEK_V4_PRO_MODEL),
    ],
)
def test_make_generator_deepseek_reads_env(monkeypatch, name, model):
    monkeypatch.setenv("HRAMATKA_DEEPSEEK_BASE_URL", "https://deep.example/v9")
    monkeypatch.setenv("HRAMATKA_DEEPSEEK_API_KEY", "ds-key")
    gen = providers.make_generator(name)
    assert gen._model == model
    assert gen._transport.base_url == "https://deep.example/v9"
    # the port resolves the deepseek-specific key env
    assert gen._resolve_key() == "ds-key"


def test_make_generator_deepseek_missing_key_names_its_env(monkeypatch):
    monkeypatch.delenv("HRAMATKA_DEEPSEEK_API_KEY", raising=False)
    gen = providers.make_generator("deepseek")
    with pytest.raises(GeneratorUnavailable) as exc:
        gen._resolve_key()
    assert "HRAMATKA_DEEPSEEK_API_KEY" in str(exc.value)


def test_make_generator_unknown_name_raises():
    with pytest.raises(ValueError):
        providers.make_generator("not-a-model")


# --- port + transport wired together (still no network) --------------------
def test_port_over_http_transport_passes_prompt_through():
    handler, _ = _seq([200])
    port = AISGeneratorPort(api_key="k", transport=_transport(handler))
    assert port("prompt") == '{"activities": []}'
