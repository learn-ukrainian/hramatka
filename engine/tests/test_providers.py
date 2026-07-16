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
    return providers.HttpChatTransport(
        base_url="https://prov.example/v1",
        client=_client(handler),
        retry_backoff_s=0,
    )


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
            retry_backoff_s=0,
        ),
    )
    return (
        providers.FailoverGeneratorPort(
            api_key="AIS-PRIMARY-KEY",
            fallback=fallback,
            transport=providers.HttpChatTransport(
                base_url="https://ais.example/v1",
                client=_client(counted_primary),
                retry_backoff_s=0,
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


# --- retry semantics: bounded backoff on transient provider failures --------
def test_retries_once_on_5xx_then_succeeds():
    handler, calls = _seq([503, 200])
    out = _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert out == '{"activities": []}'
    assert calls["n"] == 2


def test_three_5xx_raise_generator_unavailable_after_bounded_retries():
    handler, calls = _seq([503, 500, 502])
    with pytest.raises(GeneratorUnavailable):
        _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert calls["n"] == 3


def test_retries_once_on_timeout_then_succeeds():
    handler, calls = _seq(["timeout", 200])
    out = _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert out == '{"activities": []}'
    assert calls["n"] == 2


def test_three_timeouts_raise_generator_unavailable():
    handler, calls = _seq(["timeout", "timeout", "timeout"])
    with pytest.raises(GeneratorUnavailable):
        _transport(handler)("p", api_key="k", model="m", timeout_s=5)
    assert calls["n"] == 3


def test_connection_error_retries_with_the_same_bounded_policy():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("simulated connection failure")
        return httpx.Response(200, json=OK_BODY)

    assert _transport(handler)("p", api_key="k", model="m", timeout_s=5) == '{"activities": []}'
    assert calls["n"] == 3


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

    assert primary_calls["n"] == 3  # bounded primary retry discipline
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
    primary_handler, _ = _seq([503, 500, 502])
    fallback_handler, _ = _seq([429])
    generator, primary_calls, fallback_calls = _failover(primary_handler, fallback_handler)

    with caplog.at_level(logging.INFO, logger="hramatka.engine.providers"):
        with pytest.raises(GeneratorUnavailable) as exc:
            generator("prompt")

    assert str(exc.value) == "provider generation failed"
    assert exc.value.retry_exhausted
    assert primary_calls["n"] == 3
    assert fallback_calls["n"] == 1  # do not retry a rate-limited fallback host
    assert "openrouter 429 model=google/gemma-4-31b-it fallback-eligible" in caplog.text
    assert "ais 429 model=" not in caplog.text


def test_ais_and_openrouter_retry_exhaustion_preserves_retry_exhausted(monkeypatch):
    monkeypatch.setenv(providers.GEMMA_FALLBACK_API_KEY_ENV, "fallback-key")
    primary_handler, _ = _seq([503, 500, 502])
    fallback_handler, _ = _seq([503, 500, 502])
    generator, primary_calls, fallback_calls = _failover(primary_handler, fallback_handler)

    with pytest.raises(GeneratorUnavailable) as exc:
        generator("prompt")

    assert exc.value.retry_exhausted is True
    assert primary_calls["n"] == 3
    assert fallback_calls["n"] == 3  # fallback has the same bounded retry discipline


def test_ais_retry_exhaustion_without_fallback_key_preserves_existing_failure(monkeypatch):
    monkeypatch.delenv(providers.GEMMA_FALLBACK_API_KEY_ENV, raising=False)
    monkeypatch.delenv(providers.GEMMA_FALLBACK_API_KEY_FILE_ENV, raising=False)
    primary_handler, _ = _seq([503, 500, 502])
    generator, primary_calls, fallback_calls = _failover(
        primary_handler, lambda _request: pytest.fail("fallback must stay disabled")
    )

    with pytest.raises(GeneratorUnavailable):
        generator("prompt")

    assert primary_calls["n"] == 3
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


def test_calls_done_never_regresses_under_concurrent_updates():
    history = []
    lock = threading.Lock()

    class FakeStore:
        def update_progress(self, job_id, progress):
            if "calls_done" in progress:
                with lock:
                    history.append(progress["calls_done"])

    store = FakeStore()
    ctx = providers.TelemetryContext(
        job_id="test-job",
        store=store,
        phases_total=2,
        calls_planned=10,
        calls_done=0,
    )

    threads = []

    def worker(num_calls):
        forked = ctx.fork(phase=1)
        for _ in range(num_calls):
            forked.record_provider_call({"dummy": "trace"})
            time.sleep(0.001)

    for _ in range(10):
        t = threading.Thread(target=worker, args=(5,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Verify that history of calls_done is monotonically non-decreasing
    assert len(history) > 0
    for i in range(1, len(history)):
        assert history[i] >= history[i - 1], f"Regressed calls_done at index {i}: {history}"


# --- Multi-model selectable generation -------------------------------------
def test_make_generator_resolves_all_4_models(monkeypatch):
    monkeypatch.setenv("HRAMATKA_PAID_MODEL_OK", "1")
    monkeypatch.setenv("HRAMATKA_DEEPSEEK_API_KEY", "test-ds-key")

    for model_id in providers.ALLOWED_MODELS:
        monkeypatch.setenv("HRAMATKA_GEN_MODEL", model_id)
        is_gemma = model_id in ("google-ais/gemma-4-31b-it", "google-ais/gemma-4-26b-a4b-it")
        if is_gemma:
            gen = providers.make_generator("gemma-ais")
            assert gen._model == model_id
        else:
            with pytest.raises(ValueError) as exc:
                providers.make_generator("gemma-ais")
            assert "Cannot construct legacy gemma provider route" in str(exc.value)

        # Direct ID call resolves too
        gen_direct = providers.make_generator(model_id)
        assert gen_direct._model == model_id


def test_make_generator_unknown_model_fails_closed(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "google-ais/unknown-model-id")
    with pytest.raises(ValueError) as exc:
        providers.validate_and_get_model()
    assert "Invalid HRAMATKA_GEN_MODEL" in str(exc.value)
    for model_id in providers.ALLOWED_MODELS:
        assert model_id in str(exc.value)

    with pytest.raises(ValueError):
        providers.make_generator("google-ais/unknown-model-id")


def test_gemini_paid_model_gate(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "google-ais/gemini-3.1-pro-preview")
    monkeypatch.delenv("HRAMATKA_PAID_MODEL_OK", raising=False)

    with pytest.raises(ValueError) as exc:
        providers.validate_and_get_model()
    assert "HRAMATKA_PAID_MODEL_OK=1 is not set" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        providers.make_generator("google-ais/gemini-3.1-pro-preview")
    assert "HRAMATKA_PAID_MODEL_OK=1 is not set" in str(exc.value)


def test_deepseek_key_gate(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "deepseek/deepseek-v4-pro")
    monkeypatch.delenv("HRAMATKA_DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ValueError) as exc:
        providers.validate_and_get_model()
    assert "HRAMATKA_DEEPSEEK_API_KEY is missing/empty" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        providers.make_generator("deepseek/deepseek-v4-pro")
    assert "HRAMATKA_DEEPSEEK_API_KEY is missing/empty" in str(exc.value)


def test_failover_isolation(monkeypatch):
    # Gemma IDs must have failover engaged:
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "google-ais/gemma-4-31b-it")
    gen = providers.make_generator("gemma-ais")
    assert isinstance(gen, providers.FailoverGeneratorPort)

    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "google-ais/gemma-4-26b-a4b-it")
    gen = providers.make_generator("gemma-ais")
    assert isinstance(gen, providers.FailoverGeneratorPort)

    # Gemini and DeepSeek get NO failover (failover port does not engage):
    monkeypatch.setenv("HRAMATKA_PAID_MODEL_OK", "1")
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "google-ais/gemini-3.1-pro-preview")
    gen = providers.make_generator("google-ais/gemini-3.1-pro-preview")
    assert not isinstance(gen, providers.FailoverGeneratorPort)
    assert isinstance(gen, AISGeneratorPort)
    assert gen._model == "google-ais/gemini-3.1-pro-preview"

    # Verify that trying to resolve legacy names fails for non-gemma models
    with pytest.raises(ValueError) as exc:
        providers.make_generator("gemma-ais")
    assert "Cannot construct legacy gemma provider route" in str(exc.value)

    monkeypatch.setenv("HRAMATKA_DEEPSEEK_API_KEY", "test-ds-key")
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "deepseek/deepseek-v4-pro")
    gen = providers.make_generator("deepseek/deepseek-v4-pro")
    assert not isinstance(gen, providers.FailoverGeneratorPort)
    assert isinstance(gen, AISGeneratorPort)
    assert gen._model == "deepseek/deepseek-v4-pro"

    with pytest.raises(ValueError) as exc:
        providers.make_generator("gemma-ais")
    assert "Cannot construct legacy gemma provider route" in str(exc.value)


def test_deepinfra_transport_request_shape(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key-di")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    base = "https://api.deepinfra.com/v1/openai"
    client = _client(handler)

    port = AISGeneratorPort(
        api_key_env="DEEPINFRA_API_KEY",
        model="google/gemma-4-31B-it",
        transport=providers.HttpChatTransport(
            base_url=base,
            client=client,
            host="deepinfra",
            strip_model_prefix=False,
        ),
    )

    out = port("PROMPT-DEEP")
    assert out == '{"activities": []}'
    assert seen["url"] == "https://api.deepinfra.com/v1/openai/chat/completions"
    assert seen["auth"] == "Bearer test-key-di"
    assert seen["body"]["model"] == "google/gemma-4-31B-it"
    assert seen["body"]["messages"] == [{"role": "user", "content": "PROMPT-DEEP"}]
    assert "tools" not in seen["body"]


def test_api_config_gating(monkeypatch):
    from hramatka.api import config

    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    allowed = config._parse_bake_providers(None)
    assert "deepinfra" not in allowed

    with pytest.raises(RuntimeError) as exc:
        config._parse_bake_providers("google-ais,openrouter,deepinfra")
    assert "supports only google-ais, openrouter" in str(exc.value)

    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key")
    allowed = config._parse_bake_providers(None)
    assert "deepinfra" in allowed

    parsed = config._parse_bake_providers("google-ais,deepinfra")
    assert parsed == ("google-ais", "deepinfra")


# --- Quick wins tests: JSON mode, temperature, fallback, and overrides -------
def test_default_payload_shape_and_temperature_present(monkeypatch):
    monkeypatch.delenv("HRAMATKA_GEN_TEMPERATURE", raising=False)
    monkeypatch.delenv("HRAMATKA_GEN_JSON_MODE", raising=False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    _transport(handler)("prompt", api_key="k", model="m", timeout_s=5)
    assert seen["body"]["temperature"] == 0.2
    assert "response_format" not in seen["body"]


def test_payload_env_overrides_honored(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_TEMPERATURE", "0.7")
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "0")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    _transport(handler)("prompt", api_key="k", model="m", timeout_s=5)
    assert seen["body"]["temperature"] == 0.7
    assert "response_format" not in seen["body"]


def test_json_mode_denied_on_google_ais_even_when_env_on(monkeypatch):
    """#171: AIS host must never attach response_format (hang + probe)."""
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "1")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    for host in ("google-ais", "ais"):
        seen.clear()
        transport = providers.HttpChatTransport(
            base_url="https://prov.example/v1",
            client=_client(handler),
            host=host,
            retry_backoff_s=0,
        )
        transport("prompt", api_key="k", model="m", timeout_s=5)
        assert "response_format" not in seen["body"], host


def test_json_mode_enabled_on_openrouter_when_env_on(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "1")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    transport = providers.HttpChatTransport(
        base_url="https://openrouter.example/v1",
        client=_client(handler),
        host="openrouter",
        strip_model_prefix=False,
        retry_backoff_s=0,
    )
    transport("prompt", api_key="k", model="google/gemma-4-31b-it", timeout_s=5)
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_400_fallback_retry_without_json_mode(monkeypatch):
    monkeypatch.delenv("HRAMATKA_GEN_TEMPERATURE", raising=False)
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "1")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(400, text="Bad Request: json mode not supported")
        return httpx.Response(200, json=OK_BODY)

    # Set up TelemetryContext
    ctx = providers.TelemetryContext(
        job_id="test-job-400",
        phases_total=1,
        calls_planned=1,
        calls_done=0,
    )
    token = providers.telemetry_ctx.set(ctx)

    try:
        # #171: JSON mode is AIS-denied; exercise 400 fallback on openrouter.
        transport = providers.HttpChatTransport(
            base_url="https://openrouter.example/v1",
            client=_client(handler),
            host="openrouter",
            strip_model_prefix=False,
            retry_backoff_s=0,
        )
        out = transport("prompt", api_key="k", model="m", timeout_s=5)
        assert out == '{"activities": []}'

        # Verify first call had json_object and second didn't
        assert len(calls) == 2
        assert calls[0]["response_format"] == {"type": "json_object"}
        assert "response_format" not in calls[1]

        # Verify telemetry event was recorded
        events = [t for t in ctx.traces if t.get("event") == "json_mode_unsupported"]
        assert len(events) == 1
        assert events[0]["host"] == "openrouter"
        assert events[0]["model"] == "m"
    finally:
        providers.telemetry_ctx.reset(token)


# --- #171 latency watchdog --------------------------------------------------
def test_evaluate_latency_watchdog_trigger_math():
    """Pure trigger math: 3× rolling median of prior samples only."""
    prior = [100.0, 100.0, 100.0]
    # Exactly at threshold — no fire
    assert providers.evaluate_latency_watchdog(300.0, prior) is None
    # Just over 3× median — fire
    event = providers.evaluate_latency_watchdog(301.0, prior)
    assert event is not None
    assert event["event"] == "latency_watchdog"
    assert event["duration_ms"] == 301.0
    assert event["rolling_median_ms"] == 100.0
    assert event["threshold_ms"] == 300.0
    assert event["sample_count"] == 3
    assert event["ratio"] == pytest.approx(3.01)

    # Too few prior samples — no fire even if huge
    assert providers.evaluate_latency_watchdog(10_000.0, [100.0, 100.0]) is None
    # Zero / negative median — no fire
    assert providers.evaluate_latency_watchdog(1000.0, [0.0, 0.0, 0.0]) is None
    # Uneven samples: median of [10, 20, 30, 40] = 25; 3× = 75
    assert providers.evaluate_latency_watchdog(75.0, [10, 20, 30, 40]) is None
    assert providers.evaluate_latency_watchdog(76.0, [10, 20, 30, 40]) is not None


def test_record_provider_call_emits_latency_watchdog_event():
    ctx = providers.TelemetryContext(job_id="wd-job", phases_total=1, calls_planned=5, calls_done=0)
    # Seed three normal calls (100ms median)
    for _ in range(3):
        ctx.record_provider_call(
            {
                "duration_ms": 100,
                "host": "google-ais",
                "attempts": 1,
                "http_status_class": "2xx",
                "phase": 1,
                "activity_type": "cloze",
            }
        )
    assert not any(t.get("event") == "latency_watchdog" for t in ctx.traces)

    # Slow call: 5× median
    ctx.record_provider_call(
        {
            "duration_ms": 500,
            "host": "google-ais",
            "attempts": 1,
            "http_status_class": "2xx",
            "phase": 1,
            "activity_type": "cloze",
        }
    )
    events = [t for t in ctx.traces if t.get("event") == "latency_watchdog"]
    assert len(events) == 1
    assert events[0]["duration_ms"] == 500
    assert events[0]["rolling_median_ms"] == 100.0
    assert events[0]["host"] == "google-ais"
    assert events[0]["ratio"] == pytest.approx(5.0)
    # Provider call traces still recorded (watchdog is additive)
    call_traces = [t for t in ctx.traces if "duration_ms" in t and t.get("event") is None]
    assert len(call_traces) == 4


def test_provenance_stamp_fallback_fired(monkeypatch):
    primary_calls = 0
    fallback_calls = 0

    def mock_primary_transport(prompt, *, api_key, model, timeout_s):
        nonlocal primary_calls
        primary_calls += 1
        raise GeneratorUnavailable("primary outage", retry_exhausted=True)

    def mock_fallback_transport(prompt, *, api_key, model, timeout_s):
        nonlocal fallback_calls
        fallback_calls += 1
        return '{"activities": []}'

    # Clear/mock environment for fallback
    monkeypatch.setenv("HRAMATKA_GEMMA_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setenv("HRAMATKA_AIS_API_KEY", "ais-key")

    primary = AISGeneratorPort(
        api_key_env="HRAMATKA_AIS_API_KEY",
        model="google-ais/gemma-4-31b-it",
        transport=mock_primary_transport,
    )
    fallback = AISGeneratorPort(
        api_key_env="HRAMATKA_GEMMA_FALLBACK_API_KEY",
        model="google/gemma-4-31b-it",
        transport=mock_fallback_transport,
    )

    port = providers._with_failover(primary, fallback)

    # Reset ContextVar
    from hramatka.engine.transport import generator_model_id
    token = generator_model_id.set(None)
    try:
        res = port("test prompt")
        assert res == '{"activities": []}'
        assert primary_calls == 1
        assert fallback_calls == 1
        # The stamped generator must be the fallback model ID!
        assert generator_model_id.get() == "google/gemma-4-31b-it"
    finally:
        generator_model_id.reset(token)


def test_make_bake_generator_no_env_default(monkeypatch):
    monkeypatch.delenv("HRAMATKA_GEN_MODEL", raising=False)
    selector = providers.make_bake_generator()
    assert "google-ais" in selector._generators
    assert "openrouter" in selector._generators
    assert len(selector._generators) == 2

    # Check that settings.bake_providers is honored (passing provider_names explicitly)
    selector_subset = providers.make_bake_generator(provider_names=("google-ais",))
    assert "google-ais" in selector_subset._generators
    assert len(selector_subset._generators) == 1


def test_make_bake_generator_includes_deepinfra_when_enabled(monkeypatch):
    monkeypatch.delenv("HRAMATKA_GEN_MODEL", raising=False)
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key-di")
    selector = providers.make_bake_generator()
    assert "deepinfra" in selector._generators
    assert "google-ais" in selector._generators
    assert "openrouter" in selector._generators
    assert len(selector._generators) == 3
