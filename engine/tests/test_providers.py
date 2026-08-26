"""Tests for the provider registry (split across telemetry/transports/factories).

NO real network: every call is driven through an `httpx.MockTransport`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from hramatka.api.qualified_models import LOGICAL_MODELS
from hramatka.engine import provider_factories, provider_telemetry, provider_transports, providers
from hramatka.engine.transport import (
    AIS_API_KEY_FILE_ENV,
    GEMMA_MODEL,
    METERED_PROVIDER_SPEND_ACK_ENV,
    AISGeneratorPort,
    GeneratorUnavailable,
)
from hramatka.qualification.receipts import ProviderProvenance, RouteBinding

OK_BODY = {"choices": [{"message": {"content": '{"activities": []}'}}]}
VERTEX_BASE_URL = (
    "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global/publishers/google"
)
VERTEX_OK_BODY = {"candidates": [{"content": {"parts": [{"text": '{"activities": []}'}]}}]}


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


def _vertex_transport(handler) -> providers.VertexGenerateContentTransport:
    return providers.VertexGenerateContentTransport(
        base_url=VERTEX_BASE_URL,
        client=_client(handler),
        retry_backoff_s=0,
    )


def _subscription_completed(
    stdout: str, *, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["agy"], returncode, stdout=stdout, stderr="ignored")


def _subscription_stream_result(
    response: str = '{"activities": []}', *, status: str = "SUCCESS"
) -> str:
    return (
        json.dumps(
            {"event": "result", "result": {"status": status, "response": response}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _subscription_port(runner):
    return providers.SubscriptionGeneratorPort(
        executable="agy",
        model="gemini-3.6-flash-high",
        timeout_s=17,
        runner=runner,
        version_runner=lambda *_args, **_kwargs: _subscription_completed("1.1.10\n"),
    )


# --- explicit headless subscription route ---------------------------------
def test_subscription_generator_uses_stream_json_stdin_contract_and_extracts_fenced_json():
    seen: list[list[str]] = []
    fenced = '```json\n{"activities": []}\n```\n'
    stdout = _subscription_stream_result(fenced)

    def runner(command, **kwargs):
        seen.append(command)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 17
        assert kwargs["check"] is False
        assert "HRAMATKA_AIS_API_KEY" not in kwargs["env"]
        assert kwargs["input"] == (
            '{"event":"user","message":{"content":"SERIALIZER-STYLE-PROMPT"}}\n'
        )
        assert json.loads(kwargs["input"]) == {
            "event": "user",
            "message": {"content": "SERIALIZER-STYLE-PROMPT"},
        }
        return _subscription_completed(stdout)

    port = _subscription_port(runner)
    assert port("SERIALIZER-STYLE-PROMPT") == fenced
    assert seen == [
        [
            "agy",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            "gemini-3.6-flash-high",
            "--disable-slash-commands",
            "--sandbox",
            "--print-timeout",
            "17s",
        ]
    ]
    assert port.receipt_provenance() == {
        "tier": "cli_self_reported",
        "client_version": "1.1.10",
        "requested_model": "gemini-3.6-flash-high",
        "raw_output_sha256": ("11d9dca155a9402b480da4b6ad3ba2db4aa2b8c01c8fba2ac5c79f3ae31f3ee1",),
    }


def test_subscription_generator_does_not_require_metered_provider_acknowledgement(monkeypatch):
    monkeypatch.delenv(METERED_PROVIDER_SPEND_ACK_ENV)

    def runner(*_args, **_kwargs):
        return _subscription_completed(_subscription_stream_result())

    port = _subscription_port(runner)

    assert port("PROMPT") == '{"activities": []}'


def test_subscription_generator_rejects_conversational_wrong_argument_reply():
    port = _subscription_port(
        lambda *_args, **_kwargs: _subscription_completed(
            _subscription_stream_result(
                "Understood. I will not recommend any slash commands."
            )
        )
    )
    with pytest.raises(GeneratorUnavailable, match="serializer completion"):
        port("PROMPT")


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json\n",
        _subscription_stream_result(status="ERROR"),
        json.dumps({"event": "error", "error": {"message": "failed"}}) + "\n",
        _subscription_stream_result(response="not-json"),
        json.dumps({"event": "progress", "state": "running"}) + "\n",
        _subscription_stream_result() + _subscription_stream_result(),
        _subscription_stream_result() + "not-json\n",
    ],
)
def test_subscription_generator_rejects_malformed_error_or_incomplete_streams(stdout: str):
    port = _subscription_port(lambda *_args, **_kwargs: _subscription_completed(stdout))

    with pytest.raises(GeneratorUnavailable, match="serializer completion"):
        port("PROMPT")


def test_subscription_generator_sends_large_prompt_via_stdin_without_argv_item():
    prompt = "x" * 195_295
    seen: dict[str, object] = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["input"] = kwargs["input"]
        assert prompt not in command
        assert all(len(argument.encode("utf-8")) < 131_072 for argument in command)
        request = json.loads(kwargs["input"])
        assert request == {"event": "user", "message": {"content": prompt}}
        assert kwargs["input"].endswith("\n")
        return _subscription_completed(_subscription_stream_result())

    port = _subscription_port(runner)
    assert port(prompt) == '{"activities": []}'
    assert len(seen["input"].encode("utf-8")) > 195_295


def test_subscription_generator_is_stateless_between_calls():
    prompts: list[str] = []

    def runner(command, **kwargs):
        prompts.append(json.loads(kwargs["input"])["message"]["content"])
        assert "--continue" not in command
        assert "--conversation" not in command
        return _subscription_completed(_subscription_stream_result())

    port = _subscription_port(runner)
    assert port("first isolated prompt") == '{"activities": []}'
    assert port("second isolated prompt") == '{"activities": []}'
    assert prompts == ["first isolated prompt", "second isolated prompt"]
    assert len(port.receipt_provenance()["raw_output_sha256"]) == 2


@pytest.mark.parametrize(
    "runner",
    [
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(
                ["agy", "--input-format", "stream-json", "--output-format", "stream-json"],
                17,
            )
        ),
        lambda *_args, **_kwargs: _subscription_completed("", returncode=9),
    ],
)
def test_subscription_timeout_and_nonzero_surface_typed_unavailable(runner):
    with pytest.raises(GeneratorUnavailable):
        _subscription_port(runner)("PROMPT")


def test_subscription_timeout_never_carries_the_prompt_into_a_traceback():
    """A timeout must not put prompt text anywhere a logger can render it.

    The prompt is sent through stdin, so a real TimeoutExpired carries an argv
    without prompt content. Chaining that exception could still publish
    command details through ``__cause__`` to log.exception, an unhandled
    exception hook, or a test report, which this module forbids for prompt and
    response CONTENT. The parametrised test above cannot catch this: its
    fixture builds a TimeoutExpired whose cmd omits the prompt.
    """
    prompt = "TEACHER-PASTED-UKRAINIAN-TEXT-Привіт-світ"

    def runner(command, **kwargs):
        assert prompt not in command
        assert json.loads(kwargs["input"])["message"]["content"] == prompt
        raise subprocess.TimeoutExpired(command, 17)

    try:
        _subscription_port(runner)(prompt)
    except GeneratorUnavailable:
        rendered = traceback.format_exc()
    else:  # pragma: no cover - the runner always raises
        pytest.fail("a timing-out client must surface GeneratorUnavailable")

    assert prompt not in rendered
    assert "Привіт" not in rendered
    assert "timed out after 17s" in rendered


def test_subscription_bake_route_requires_explicit_selection(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "google-ais/gemini-3.6-flash")
    default = providers.make_bake_generator()
    assert providers.SUBSCRIPTION_PROVIDER not in default._generators
    selected = providers.make_bake_generator((providers.SUBSCRIPTION_PROVIDER,))
    assert tuple(selected._generators) == (providers.SUBSCRIPTION_PROVIDER,)


def test_logical_subscription_route_is_explicit_and_never_builds_api_fallback(monkeypatch):
    monkeypatch.setattr(providers.SubscriptionGeneratorPort, "is_configured", lambda _self: True)
    flash = next(model for model in LOGICAL_MODELS if model.id == "gemini-3.7-flash")
    selector = providers.make_logical_model_generator(
        flash.id,
        (providers.SUBSCRIPTION_PROVIDER,),
        qualified_routes=flash.provider_routes,
    )
    assert tuple(selector._generators) == (providers.SUBSCRIPTION_PROVIDER,)
    assert isinstance(
        selector._generators[providers.SUBSCRIPTION_PROVIDER], providers.SubscriptionGeneratorPort
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


# --- AIS secret-file credentials -------------------------------------------
def test_ais_port_uses_file_credential_and_calls_mock_transport(monkeypatch, tmp_path):
    key_file = tmp_path / "google-ais.key"
    key_file.write_text("file-only-ais-key\n", encoding="utf-8")
    monkeypatch.delenv("HRAMATKA_AIS_API_KEY", raising=False)
    monkeypatch.setenv(AIS_API_KEY_FILE_ENV, str(key_file))
    seen: dict[str, str | int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["calls"] = int(seen.get("calls", 0)) + 1
        return httpx.Response(200, json=OK_BODY)

    ais, _, _ = providers._gemma_routes()
    ais._transport = _transport(handler)

    assert ais.is_configured()
    assert ais("prompt") == '{"activities": []}'
    assert seen == {"authorization": "Bearer file-only-ais-key", "calls": 1}


def test_ais_direct_credential_takes_precedence_over_file(monkeypatch, tmp_path):
    key_file = tmp_path / "google-ais.key"
    key_file.write_text("file-ais-key\n", encoding="utf-8")
    monkeypatch.setenv("HRAMATKA_AIS_API_KEY", "direct-ais-key")
    monkeypatch.setenv(AIS_API_KEY_FILE_ENV, str(key_file))
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=OK_BODY)

    ais, _, _ = providers._gemma_routes()
    ais._transport = _transport(handler)

    assert ais._api_key_file_env == AIS_API_KEY_FILE_ENV
    assert ais("prompt") == '{"activities": []}'
    assert seen["authorization"] == "Bearer direct-ais-key"


def test_ais_whitespace_direct_credential_defers_to_file(monkeypatch, tmp_path):
    key_file = tmp_path / "google-ais.key"
    key_file.write_text("file-ais-key\n", encoding="utf-8")
    monkeypatch.setenv("HRAMATKA_AIS_API_KEY", " \t\n")
    monkeypatch.setenv(AIS_API_KEY_FILE_ENV, str(key_file))
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=OK_BODY)

    ais, _, _ = providers._gemma_routes()
    ais._transport = _transport(handler)

    assert ais("prompt") == '{"activities": []}'
    assert seen["authorization"] == "Bearer file-ais-key"


@pytest.mark.parametrize("unreadable", (False, True), ids=("missing", "unreadable"))
def test_ais_unreadable_or_missing_key_file_raises_typed_value_free_error(
    monkeypatch, tmp_path, unreadable
):
    missing_file = tmp_path / "missing-google-ais.key"
    if unreadable:
        missing_file.write_text("unreadable-ais-key", encoding="utf-8")

        def unreadable_read_text(self, *args, **kwargs):
            raise PermissionError("test unreadable file")

        monkeypatch.setattr(Path, "read_text", unreadable_read_text)
    monkeypatch.delenv("HRAMATKA_AIS_API_KEY", raising=False)
    monkeypatch.setenv(AIS_API_KEY_FILE_ENV, str(missing_file))
    ais, _, _ = providers._gemma_routes()

    with pytest.raises(GeneratorUnavailable) as exc:
        ais("prompt")

    assert ais._api_key_file_env == AIS_API_KEY_FILE_ENV
    assert str(exc.value) == f"{AIS_API_KEY_FILE_ENV} could not be read"
    assert "missing-google-ais.key" not in str(exc.value)
    assert "file-ais-key" not in str(exc.value)
    assert "unreadable-ais-key" not in str(exc.value)
    assert "Traceback" not in str(exc.value)


def test_ais_without_either_credential_remains_not_configured(monkeypatch):
    monkeypatch.delenv("HRAMATKA_AIS_API_KEY", raising=False)
    monkeypatch.delenv(AIS_API_KEY_FILE_ENV, raising=False)
    ais, _, _ = providers._gemma_routes()

    with pytest.raises(GeneratorUnavailable) as exc:
        ais("prompt")

    assert ais._api_key_file_env == AIS_API_KEY_FILE_ENV
    assert "is not set" in str(exc.value)
    assert "requires the key" in str(exc.value)


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


@pytest.mark.parametrize("model", ("gemini-3.6-flash", "gemini-3.1-pro-preview"))
def test_vertex_request_uses_native_generate_content_shape_and_separate_key_header(
    monkeypatch, model
):
    monkeypatch.delenv("HRAMATKA_GEN_TEMPERATURE", raising=False)
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "1")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["vertex_key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=VERTEX_OK_BODY)

    out = _vertex_transport(handler)(
        "PROMPT-BODY", api_key="vertex-only-key", model=model, timeout_s=5
    )

    assert out == '{"activities": []}'
    assert seen["url"] == f"{VERTEX_BASE_URL}/models/{model}:generateContent"
    assert seen["authorization"] is None
    assert seen["vertex_key"] == "vertex-only-key"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["body"] == {
        "contents": [{"role": "user", "parts": [{"text": "PROMPT-BODY"}]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }
    assert "tools" not in seen["body"]


def test_extract_vertex_text_skips_thought_parts():
    body = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "text": "<reasoning>"},
                        {"text": "<answer>"},
                    ]
                }
            }
        ]
    }

    assert providers._extract_vertex_text(body) == "<answer>"


def test_vertex_retries_5xx_and_refuses_prefixed_model_ids():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=VERTEX_OK_BODY)

    assert _vertex_transport(handler)("p", api_key="k", model="gemini-3.6-flash", timeout_s=5) == (
        '{"activities": []}'
    )
    assert calls["n"] == 2
    with pytest.raises(GeneratorUnavailable, match="bare model ID"):
        _vertex_transport(handler)(
            "p", api_key="k", model="google-ais/gemini-3.6-flash", timeout_s=5
        )


def test_vertex_400_fallback_retries_without_json_mime(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "1")
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(400, text="responseMimeType is unsupported")
        return httpx.Response(200, json=VERTEX_OK_BODY)

    context = providers.TelemetryContext(
        job_id="test-vertex-400",
        phases_total=1,
        calls_planned=1,
        calls_done=0,
    )
    token = providers.telemetry_ctx.set(context)
    try:
        assert _vertex_transport(handler)(
            "prompt", api_key="k", model="gemini-3.6-flash", timeout_s=5
        ) == ('{"activities": []}')
    finally:
        providers.telemetry_ctx.reset(token)

    assert len(calls) == 2
    assert calls[0]["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseMimeType" not in calls[1]["generationConfig"]
    events = [trace for trace in context.traces if trace.get("event") == "json_mode_unsupported"]
    assert events == [
        {
            "event": "json_mode_unsupported",
            "host": "google-vertex",
            "model": "gemini-3.6-flash",
        }
    ]


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


def test_qualified_flash_default_constructs_no_vertex_transport(monkeypatch):
    def fail_if_vertex_path_is_constructed(*_args, **_kwargs):
        raise AssertionError("qualified Flash must not construct a Vertex transport")

    monkeypatch.setenv(providers.AIS_API_KEY_ENV, "ais-key")
    # Patch the canonical factories seam — providers.py is only a re-export (#456).
    monkeypatch.setattr(provider_factories, "_gemini_routes", fail_if_vertex_path_is_constructed)
    monkeypatch.setattr(
        provider_factories, "_validate_vertex_base_url", fail_if_vertex_path_is_constructed
    )

    selector = providers.make_logical_model_generator("gemini-3.6-flash")

    assert tuple(selector._generators) == ("google-ais",)
    generator = selector._generators["google-ais"]
    assert isinstance(generator, AISGeneratorPort)
    assert not isinstance(generator, providers.FailoverGeneratorPort)
    assert generator._transport.host == "google-ais"


def test_providers_facade_identity_matches_canonical_modules() -> None:
    """Compatibility re-exports must be the same objects as the split modules."""
    assert providers.make_logical_model_generator is provider_factories.make_logical_model_generator
    assert providers._gemini_routes is provider_factories._gemini_routes
    assert providers._gemini_ais_route is provider_factories._gemini_ais_route
    assert providers._validate_vertex_base_url is provider_transports._validate_vertex_base_url
    assert providers.FailoverGeneratorPort is provider_transports.FailoverGeneratorPort
    assert providers.TelemetryContext is provider_telemetry.TelemetryContext
    assert providers.provider_call_slot is provider_telemetry.provider_call_slot


def test_facade_only_monkeypatch_does_not_hide_factory_seam(monkeypatch):
    """Patching providers._gemini_routes alone must not silence factories divergence."""
    facade_hits = {"n": 0}

    def facade_only(*_args, **_kwargs):
        facade_hits["n"] += 1
        raise AssertionError("facade-only patch should not run for factory construction")

    monkeypatch.setenv(providers.AIS_API_KEY_ENV, "ais-key")
    monkeypatch.setattr(providers, "_gemini_routes", facade_only)

    # Factories still owns the live binding; a facade-only patch must not trip.
    selector = providers.make_logical_model_generator("gemini-3.6-flash")
    assert facade_hits["n"] == 0
    assert tuple(selector._generators) == ("google-ais",)
    # And the facade binding has diverged from the canonical seam.
    assert providers._gemini_routes is not provider_factories._gemini_routes


def test_route_binding_qualified_routes_resolve_route_ids(monkeypatch):
    monkeypatch.setenv(providers.AIS_API_KEY_ENV, "ais-key")
    bindings = (
        RouteBinding(
            "gemini-flash-ais",
            "google-ais",
            "google-ais/gemini-3.6-flash",
        ),
    )

    selector = providers.make_logical_model_generator(
        "gemini-3.6-flash",
        ("google-ais",),
        qualified_routes=bindings,
    )

    generator = selector._generators["google-ais"]
    assert isinstance(generator, AISGeneratorPort)
    assert generator._model == "google-ais/gemini-3.6-flash"


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
    assert gen.resolve_key() == "ds-key"


def test_make_generator_deepseek_missing_key_names_its_env(monkeypatch):
    monkeypatch.delenv("HRAMATKA_DEEPSEEK_API_KEY", raising=False)
    gen = providers.make_generator("deepseek")
    with pytest.raises(GeneratorUnavailable) as exc:
        gen.resolve_key()
    assert "HRAMATKA_DEEPSEEK_API_KEY" in str(exc.value)


def test_make_generator_unknown_name_raises():
    with pytest.raises(ValueError):
        providers.make_generator("not-a-model")


# --- port + transport wired together (still no network) --------------------
def test_port_over_http_transport_passes_prompt_through():
    handler, _ = _seq([200])
    port = AISGeneratorPort(api_key="k", transport=_transport(handler))
    assert port("prompt") == '{"activities": []}'


def test_ais_generator_reports_api_observed_provenance_from_its_own_call():
    handler, _ = _seq([200])
    port = AISGeneratorPort(
        api_key="k",
        model="google-ais/gemma-4-31b-it",
        transport=_transport(handler),
    )

    assert port("prompt") == '{"activities": []}'
    provenance = port.receipt_provenance()
    assert provenance == {
        "tier": "api_observed",
        "client_version": None,
        "requested_model": "google-ais/gemma-4-31b-it",
        "raw_output_sha256": ("35bf4703564648295baf3d35c7d9dc8536f06a20368e2dc60cbb3ef1924c63f9",),
    }
    assert ProviderProvenance.from_dict(provenance).requested_model == "google-ais/gemma-4-31b-it"


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
def test_make_generator_resolves_all_allowed_models(monkeypatch):
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


def test_gemini_flash_is_an_allowed_free_seat(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "google-ais/gemini-3.6-flash")
    monkeypatch.delenv("HRAMATKA_PAID_MODEL_OK", raising=False)

    assert providers.validate_and_get_model() == "google-ais/gemini-3.6-flash"
    assert providers.make_generator("google-ais/gemini-3.6-flash")._model == (
        "google-ais/gemini-3.6-flash"
    )


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

    # Gemini keeps AIS primary with a native Vertex outage fallback.
    monkeypatch.setenv("HRAMATKA_PAID_MODEL_OK", "1")
    monkeypatch.setenv("HRAMATKA_GEN_MODEL", "google-ais/gemini-3.1-pro-preview")
    gen = providers.make_generator("google-ais/gemini-3.1-pro-preview")
    assert isinstance(gen, providers.FailoverGeneratorPort)
    assert isinstance(gen, AISGeneratorPort)
    assert gen._model == "google-ais/gemini-3.1-pro-preview"
    assert gen._fallback._model == "gemini-3.1-pro-preview"
    assert isinstance(gen._fallback._transport, providers.VertexGenerateContentTransport)

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
    assert "supports only antigravity, openrouter" in str(exc.value)

    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key")
    allowed = config._parse_bake_providers(None)
    assert "deepinfra" in allowed

    parsed = config._parse_bake_providers("antigravity,deepinfra")
    assert parsed == ("antigravity", "deepinfra")


# --- Quick wins tests: JSON mode, temperature, fallback, and overrides -------
def test_default_payload_shape_and_temperature_present(monkeypatch):
    monkeypatch.delenv("HRAMATKA_GEN_TEMPERATURE", raising=False)
    monkeypatch.delenv("HRAMATKA_GEN_JSON_MODE", raising=False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    _transport(handler)("prompt", api_key="k", model="m", timeout_s=5)
    assert seen["body"]["temperature"] == 0.0
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


def test_vertex_payload_temperature_override_is_honored(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_TEMPERATURE", "0.7")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=VERTEX_OK_BODY)

    _vertex_transport(handler)("prompt", api_key="k", model="gemini-3.6-flash", timeout_s=5)

    assert seen["body"]["generationConfig"]["temperature"] == 0.7


@pytest.mark.parametrize(
    "model",
    (
        "google-ais/gemini-3.6-flash",
        "google-ais/gemini-3.1-pro-preview",
        "google-ais/gemma-4-31b-it",
        "google-ais/gemma-4-26b-a4b-it",
    ),
)
def test_ais_json_mode_is_off_by_default(monkeypatch, model):
    monkeypatch.delenv("HRAMATKA_GEN_JSON_MODE", raising=False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    transport = providers.HttpChatTransport(
        base_url="https://prov.example/v1",
        client=_client(handler),
        host="google-ais",
        retry_backoff_s=0,
    )
    transport("prompt", api_key="k", model=model, timeout_s=5)
    assert "response_format" not in seen["body"]


def test_json_mode_can_be_enabled_per_run_for_gemini(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "1")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    transport = providers.HttpChatTransport(
        base_url="https://prov.example/v1",
        client=_client(handler),
        host="google-ais",
        retry_backoff_s=0,
    )
    transport("prompt", api_key="k", model="google-ais/gemini-3.6-flash", timeout_s=5)
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_gemma_json_mode_remains_denied_even_when_legacy_env_is_enabled(monkeypatch):
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
    assert "response_format" not in seen["body"]


def test_json_mode_can_be_disabled_per_run_for_gemini(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "0")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    transport = providers.HttpChatTransport(
        base_url="https://prov.example/v1",
        client=_client(handler),
        host="google-ais",
        retry_backoff_s=0,
    )
    transport("prompt", api_key="k", model="google-ais/gemini-3.6-flash", timeout_s=5)
    assert "response_format" not in seen["body"]


def test_vertex_json_mode_can_be_disabled_per_run_for_gemini(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GEN_JSON_MODE", "0")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=VERTEX_OK_BODY)

    _vertex_transport(handler)("prompt", api_key="k", model="gemini-3.6-flash", timeout_s=5)
    assert "responseMimeType" not in seen["body"]["generationConfig"]


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
        # Gemini JSON mode must retain the one-call 400 fallback without weakening
        # the non-Gemini (Gemma) deny policy.
        transport = providers.HttpChatTransport(
            base_url="https://ais.example/v1",
            client=_client(handler),
            host="google-ais",
            retry_backoff_s=0,
        )
        out = transport("prompt", api_key="k", model="google-ais/gemini-3.6-flash", timeout_s=5)
        assert out == '{"activities": []}'

        # Verify first call had json_object and second didn't
        assert len(calls) == 2
        assert calls[0]["response_format"] == {"type": "json_object"}
        assert "response_format" not in calls[1]

        # Verify telemetry event was recorded
        events = [t for t in ctx.traces if t.get("event") == "json_mode_unsupported"]
        assert len(events) == 1
        assert events[0]["host"] == "google-ais"
        assert events[0]["model"] == "google-ais/gemini-3.6-flash"
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
