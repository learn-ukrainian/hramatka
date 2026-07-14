"""Tests for providers.py — the real OpenAI-compatible HTTP transport + the
name→generator registry. NO real network: every call is driven through an
`httpx.MockTransport`, so the actual provider API is never hit.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


def _failover(
    primary_handler, fallback_handler
) -> tuple[providers.FailoverGeneratorPort, dict, dict]:
    """A fully fake two-host Gemma route; no test here uses the network."""
    primary_calls = {"n": 0}
    fallback_calls = {"n": 0}

    def counted_primary(request: httpx.Request) -> httpx.Response:
        primary_calls["n"] += 1
        return primary_handler(request)

    def counted_fallback(request: httpx.Request) -> httpx.Response:
        fallback_calls["n"] += 1
        return fallback_handler(request)

    fallback = AISGeneratorPort(
        api_key_env=providers.GEMMA_FALLBACK_API_KEY_ENV,
        api_key_file_env=providers.GEMMA_FALLBACK_API_KEY_FILE_ENV,
        model=providers.DEFAULT_GEMMA_FALLBACK_MODEL,
        transport=providers.HttpChatTransport(
            base_url="https://openrouter.example/v1",
            client=_client(counted_fallback),
            host="openrouter",
            strip_model_prefix=False,
        ),
    )
    return (
        providers.FailoverGeneratorPort(
            api_key="AIS-PRIMARY-KEY",
            fallback=fallback,
            transport=providers.HttpChatTransport(
                base_url="https://ais.example/v1", client=_client(counted_primary)
            ),
        ),
        primary_calls,
        fallback_calls,
    )


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


# --- AIS -> OpenRouter failover --------------------------------------------
def test_healthy_ais_never_calls_openrouter(monkeypatch):
    monkeypatch.setenv(providers.GEMMA_FALLBACK_API_KEY_ENV, "fallback-key")
    primary_handler, _ = _seq([200])
    generator, primary_calls, fallback_calls = _failover(
        primary_handler, lambda _request: pytest.fail("healthy AIS must not use fallback")
    )

    assert generator("prompt") == '{"activities": []}'
    assert primary_calls["n"] == 1
    assert fallback_calls["n"] == 0


def test_ais_retry_exhaustion_uses_openrouter_once_and_never_logs_key_or_prompt(
    monkeypatch, caplog
):
    sentinel_key = "OPENROUTER-FALLBACK-SECRET"
    sentinel_prompt = "SENSITIVE-FALLBACK-PROMPT"
    monkeypatch.setenv(providers.GEMMA_FALLBACK_API_KEY_ENV, sentinel_key)
    primary_handler, _ = _seq([503, 500])
    seen: dict = {}

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    generator, primary_calls, fallback_calls = _failover(primary_handler, fallback_handler)
    with caplog.at_level(logging.INFO, logger="hramatka.engine.providers"):
        assert generator(sentinel_prompt) == '{"activities": []}'

    assert primary_calls["n"] == 2  # existing primary retry discipline
    assert fallback_calls["n"] == 1
    assert seen["body"]["model"] == providers.DEFAULT_GEMMA_FALLBACK_MODEL
    assert seen["body"]["messages"] == [{"role": "user", "content": sentinel_prompt}]
    assert caplog.text.count("gemma fallback engaged host=openrouter") == 1
    assert sentinel_key not in caplog.text
    assert sentinel_prompt not in caplog.text
    assert all(sentinel_key not in record.getMessage() for record in caplog.records)


def test_ais_429_uses_openrouter_once_without_retrying_ais(monkeypatch, caplog):
    monkeypatch.setenv(providers.GEMMA_FALLBACK_API_KEY_ENV, "fallback-key")
    primary_handler, _ = _seq([429])
    fallback_handler, _ = _seq([200])
    generator, primary_calls, fallback_calls = _failover(primary_handler, fallback_handler)

    with caplog.at_level(logging.INFO, logger="hramatka.engine.providers"):
        assert generator("prompt") == '{"activities": []}'

    assert primary_calls["n"] == 1  # do not retry a rate-limited host
    assert fallback_calls["n"] == 1
    assert "ais 429 model=google-ais/gemma-4-31b-it fallback-eligible" in caplog.text
    assert caplog.text.count("gemma fallback engaged host=openrouter") == 1


def test_ais_401_never_uses_openrouter_or_logs_429_warning(monkeypatch, caplog):
    monkeypatch.setenv(providers.GEMMA_FALLBACK_API_KEY_ENV, "fallback-key")
    primary_handler, _ = _seq([401])
    generator, primary_calls, fallback_calls = _failover(
        primary_handler, lambda _request: pytest.fail("401 must not use fallback")
    )

    with caplog.at_level(logging.INFO, logger="hramatka.engine.providers"):
        with pytest.raises(GeneratorUnavailable, match="provider returned HTTP 401"):
            generator("prompt")

    assert primary_calls["n"] == 1
    assert fallback_calls["n"] == 0
    assert "429 model=" not in caplog.text
    assert "gemma fallback engaged host=openrouter" not in caplog.text


def test_openrouter_429_after_ais_retry_exhaustion_is_sanitized(monkeypatch, caplog):
    monkeypatch.setenv(providers.GEMMA_FALLBACK_API_KEY_ENV, "fallback-key")
    primary_handler, _ = _seq([503, 500])
    fallback_handler, _ = _seq([429])
    generator, primary_calls, fallback_calls = _failover(primary_handler, fallback_handler)

    with caplog.at_level(logging.INFO, logger="hramatka.engine.providers"):
        with pytest.raises(GeneratorUnavailable) as exc:
            generator("prompt")

    assert str(exc.value) == "provider generation failed"
    assert not exc.value.retry_exhausted
    assert primary_calls["n"] == 2
    assert fallback_calls["n"] == 1  # do not retry a rate-limited fallback host
    assert "openrouter 429 model=google/gemma-4-31b-it fallback-eligible" in caplog.text
    assert "ais 429 model=" not in caplog.text


def test_ais_and_openrouter_retry_exhaustion_stays_generator_unavailable(monkeypatch):
    monkeypatch.setenv(providers.GEMMA_FALLBACK_API_KEY_ENV, "fallback-key")
    primary_handler, _ = _seq([503, 500])
    fallback_handler, _ = _seq([503, 500])
    generator, primary_calls, fallback_calls = _failover(primary_handler, fallback_handler)

    with pytest.raises(GeneratorUnavailable):
        generator("prompt")

    assert primary_calls["n"] == 2
    assert fallback_calls["n"] == 2  # fallback has the same one-retry discipline


def test_ais_retry_exhaustion_without_fallback_key_preserves_existing_failure(monkeypatch):
    monkeypatch.delenv(providers.GEMMA_FALLBACK_API_KEY_ENV, raising=False)
    monkeypatch.delenv(providers.GEMMA_FALLBACK_API_KEY_FILE_ENV, raising=False)
    primary_handler, _ = _seq([503, 500])
    generator, primary_calls, fallback_calls = _failover(
        primary_handler, lambda _request: pytest.fail("fallback must stay disabled")
    )

    with pytest.raises(GeneratorUnavailable):
        generator("prompt")

    assert primary_calls["n"] == 2
    assert fallback_calls["n"] == 0


def test_fallback_key_file_is_read_at_call_time(monkeypatch, tmp_path):
    key_file = tmp_path / "openrouter-key"
    key_file.write_text("file-backed-fallback-key\n", encoding="utf-8")
    monkeypatch.setenv(providers.GEMMA_FALLBACK_API_KEY_FILE_ENV, str(key_file))
    primary_handler, _ = _seq([503, 500])
    seen: dict = {}

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=OK_BODY)

    generator, _primary_calls, fallback_calls = _failover(primary_handler, fallback_handler)
    assert generator("prompt") == '{"activities": []}'
    assert fallback_calls["n"] == 1
    assert seen["authorization"] == "Bearer file-backed-fallback-key"


# --- registry --------------------------------------------------------------
def test_make_generator_gemma_ais_uses_locked_route():
    gen = providers.make_generator("gemma-ais")
    assert isinstance(gen, AISGeneratorPort)
    assert gen._model == GEMMA_MODEL
    assert isinstance(gen._transport, providers.HttpChatTransport)
    assert isinstance(gen, providers.FailoverGeneratorPort)
    assert gen._fallback._model == providers.DEFAULT_GEMMA_FALLBACK_MODEL
    assert gen._fallback._transport.base_url == providers.DEFAULT_GEMMA_FALLBACK_BASE_URL


def test_round_robin_selector_spreads_bakes_across_both_provider_primaries():
    handled: list[str] = []
    handled_lock = threading.Lock()

    def generator(name: str):
        def call(_prompt: str) -> str:
            with handled_lock:
                handled.append(name)
            return name

        return call

    selector = providers.RoundRobinGeneratorSelector(
        {"google-ais": generator("google-ais"), "openrouter": generator("openrouter")}
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        outputs = list(executor.map(lambda _: selector.for_bake()("prompt"), range(12)))

    assert sorted(outputs) == ["google-ais"] * 6 + ["openrouter"] * 6
    assert sorted(handled) == ["google-ais"] * 6 + ["openrouter"] * 6


def test_global_provider_budget_caps_inflight_http_calls():
    active = 0
    max_active = 0
    lock = threading.Lock()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.04)
            return httpx.Response(200, json=OK_BODY)
        finally:
            with lock:
                active -= 1

    providers.configure_provider_concurrency(2)
    try:
        transport = _transport(handler)
        with ThreadPoolExecutor(max_workers=4) as executor:
            outputs = list(
                executor.map(
                    lambda _: transport("prompt", api_key="key", model="m", timeout_s=5),
                    range(4),
                )
            )
    finally:
        providers.configure_provider_concurrency(8)

    assert outputs == ['{"activities": []}'] * 4
    assert max_active == 2


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
