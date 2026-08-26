"""HTTP, Vertex, subscription, and failover generator transports (#456)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from hramatka.engine.provider_telemetry import (
    json_mode_enabled_for_model,
    provider_call_slot,
    telemetry_ctx,
)
from hramatka.engine.serializer_policy import serializer_temperature
from hramatka.engine.transport import (
    GEMMA_TIMEOUT_S,
    AISGeneratorPort,
    GeneratorUnavailable,
    generator_model_id,
    generator_route_identity,
)

log = logging.getLogger("hramatka.engine.providers")

# The subscription client is deliberately an explicit route.  It is never a
# fallback for an API route (or vice versa): the client has a different
# authentication and provenance boundary.
SUBSCRIPTION_PROVIDER = "antigravity"
SUBSCRIPTION_HOST = "antigravity-cli"
SUBSCRIPTION_EXECUTABLE_ENV = "HRAMATKA_SUBSCRIPTION_EXECUTABLE"
SUBSCRIPTION_MODEL_ENV = "HRAMATKA_SUBSCRIPTION_MODEL"
DEFAULT_SUBSCRIPTION_EXECUTABLE = "agy"
DEFAULT_SUBSCRIPTION_MODEL = "gemini-3.7-flash-high"

# Provider endpoints are OpenAI-compatible `/chat/completions`. Base URLs are
# env-overridable so no deployed address is baked into code (Sol leak audit).
GEMMA_AIS_BASE_URL_ENV = "HRAMATKA_AIS_BASE_URL"
DEFAULT_GEMMA_AIS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

GEMMA_FALLBACK_BASE_URL_ENV = "HRAMATKA_GEMMA_FALLBACK_BASE_URL"
GEMMA_FALLBACK_API_KEY_ENV = "HRAMATKA_GEMMA_FALLBACK_API_KEY"
GEMMA_FALLBACK_API_KEY_FILE_ENV = "HRAMATKA_GEMMA_FALLBACK_API_KEY_FILE"
GEMMA_FALLBACK_MODEL_ENV = "HRAMATKA_GEMMA_FALLBACK_MODEL"
DEFAULT_GEMMA_FALLBACK_BASE_URL = "https://openrouter.ai/api/v1"
# Verified against GET https://openrouter.ai/api/v1/models on 2026-07-13.
DEFAULT_GEMMA_FALLBACK_MODEL = "google/gemma-4-31b-it"
_BAKE_PROVIDERS = ("google-ais", "openrouter")

# Vertex uses Google's native ``generateContent`` API, not the OpenAI-compatible
# AIS endpoint. The complete project/location/publisher prefix remains operator
# configuration: a project identity must never be compiled into this repository.
VERTEX_BASE_URL_ENV = "HRAMATKA_VERTEX_BASE_URL"
VERTEX_API_KEY_ENV = "HRAMATKA_VERTEX_API_KEY"
VERTEX_API_KEY_FILE_ENV = "HRAMATKA_VERTEX_API_KEY_FILE"


def _validate_vertex_base_url(value: str) -> str:
    """Return one configured global Google Vertex publisher prefix or fail closed."""
    parsed = urlsplit(value)
    parts = tuple(part for part in parsed.path.split("/") if part)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "aiplatform.googleapis.com"
        or parts[:2] != ("v1", "projects")
        or len(parts) != 7
        or not parts[2]
        or parts[3:] != ("locations", "global", "publishers", "google")
        or parsed.path.rstrip("/") != f"/v1/projects/{parts[2]}/locations/global/publishers/google"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Vertex base URL must be the global Google publisher prefix.")
    return value.rstrip("/")


DEEPINFRA_API_KEY_ENV = "DEEPINFRA_API_KEY"
DEEPINFRA_BASE_URL_ENV = "HRAMATKA_DEEPINFRA_BASE_URL"
DEFAULT_DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_MODEL = "google/gemma-4-31B-it"


def _get_bake_providers() -> tuple[str, ...]:
    if DEEPINFRA_API_KEY_ENV in os.environ:
        return ("google-ais", "openrouter", "deepinfra")
    return _BAKE_PROVIDERS


DEEPSEEK_API_KEY_ENV = "HRAMATKA_DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL_ENV = "HRAMATKA_DEEPSEEK_BASE_URL"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_V4_PRO_MODEL = "deepseek-v4-pro"


@dataclass
class SubscriptionGeneratorPort:
    """One-shot, non-interactive subscription-client generator.

    Every generation starts a new ``agy`` stream-json process.  The prompt is
    sent through stdin so large teacher prompts never become an argv item. The
    port retains only content-free output hashes for a qualification receipt;
    it never resumes a conversation and intentionally performs no automatic
    retry.
    Retrying an interrupted CLI request could repeat a completed subscription
    call because this client has no idempotency-key protocol.
    """

    executable: str = DEFAULT_SUBSCRIPTION_EXECUTABLE
    model: str = DEFAULT_SUBSCRIPTION_MODEL
    timeout_s: int = GEMMA_TIMEOUT_S
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    version_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    host: str = SUBSCRIPTION_HOST
    _client_version: str | None = field(default=None, init=False, repr=False)
    _raw_output_sha256: list[str] = field(default_factory=list, init=False, repr=False)
    _semantic_review_route: tuple[str, str, str] | None = field(
        default=None, init=False, repr=False
    )

    def bind_semantic_review_route(self, *, route_id: str, host: str, model_id: str) -> None:
        """Seal one qualification route onto this subscription provider port."""
        if (
            not all(isinstance(value, str) and value for value in (route_id, host, model_id))
            or self.host != host
            or self.model != model_id
        ):
            raise ValueError("Semantic-review route binding does not match the provider port.")
        binding = (route_id, host, model_id)
        if self._semantic_review_route not in {None, binding}:
            raise ValueError("Semantic-review provider port is already bound to another route.")
        self._semantic_review_route = binding

    def semantic_review_route_identity(self) -> tuple[str, str, str] | None:
        """Return the factory-sealed route; unqualified ports remain unavailable."""
        return self._semantic_review_route

    def is_configured(self) -> bool:
        """Whether the explicitly configured local executable can be found."""
        if not self.executable:
            return False
        if os.path.sep in self.executable:
            return os.path.isfile(self.executable) and os.access(self.executable, os.X_OK)
        return _which(self.executable) is not None

    def receipt_provenance(self) -> dict[str, object]:
        """Return only auditable, content-free CLI evidence for one receipt."""
        if self._client_version is None or not self._raw_output_sha256:
            raise GeneratorUnavailable("subscription provenance is incomplete")
        return {
            "tier": "cli_self_reported",
            "client_version": self._client_version,
            "requested_model": self.model,
            "raw_output_sha256": tuple(self._raw_output_sha256),
        }

    def _resolve_client_version(self) -> str:
        if self._client_version is not None:
            return self._client_version
        try:
            completed = self.version_runner(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=min(self.timeout_s, 30),
                check=False,
                env=_subscription_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GeneratorUnavailable("subscription client version could not be read") from exc
        version = completed.stdout.strip() if completed.returncode == 0 else ""
        if not version:
            raise GeneratorUnavailable("subscription client version could not be read")
        self._client_version = version
        return version

    def __call__(self, prompt: str) -> str:
        if not isinstance(prompt, str) or not prompt:
            raise GeneratorUnavailable("subscription prompt is empty")
        self._resolve_client_version()
        command = [
            self.executable,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            self.model,
            "--disable-slash-commands",
            "--sandbox",
            "--print-timeout",
            f"{self.timeout_s}s",
        ]
        stream_input = json.dumps(
            {"event": "user", "message": {"content": prompt}},
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        started_at_ms = int(time.perf_counter() * 1000)
        try:
            # Deliberately one invocation only.  A timeout/non-zero result is
            # ambiguous at the subscription boundary and must not be replayed.
            with provider_call_slot():
                completed = self.runner(
                    command,
                    capture_output=True,
                    input=stream_input,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                    env=_subscription_environment(),
                )
        except subprocess.TimeoutExpired:
            # Deliberately unchained.  TimeoutExpired carries the executed argv
            # in .cmd, and this client takes the prompt as an argument, so any
            # renderer of __cause__ -- log.exception, an unhandled-exception
            # hook, a test report -- would write the whole prompt to a durable
            # log.  The message below already carries every fact a caller needs.
            raise GeneratorUnavailable(
                f"subscription client timed out after {self.timeout_s}s"
            ) from None
        except OSError as exc:
            # Safe to chain: a failed spawn names the executable, never argv.
            raise GeneratorUnavailable("subscription client could not be started") from exc
        if completed.returncode != 0:
            raise GeneratorUnavailable("subscription client exited unsuccessfully")
        raw = completed.stdout
        completion = _extract_subscription_completion(raw)
        if completion is None:
            raise GeneratorUnavailable("subscription client did not emit a serializer completion")
        generator_model_id.set(self.model)
        generator_route_identity.set((self.host, self.model))
        self._raw_output_sha256.append(hashlib.sha256(raw.encode("utf-8")).hexdigest())
        ctx = telemetry_ctx.get()
        if ctx is not None:
            ended_at_ms = int(time.perf_counter() * 1000)
            ctx.record_provider_call(
                {
                    "duration_ms": ended_at_ms - started_at_ms,
                    "host": self.host,
                    "model": self.model,
                    "attempts": 1,
                    "http_status_class": "cli",
                    "phase": ctx.phase,
                    "activity_type": "+".join(ctx.activity_types) or "unknown",
                    "started_at_ms": started_at_ms,
                    "ended_at_ms": ended_at_ms,
                }
            )
        return completion


def _which(executable: str) -> str | None:
    """Small seam so tests do not need a real subscription client installed."""
    import shutil

    return shutil.which(executable)


def _subscription_environment() -> dict[str, str]:
    """Pass only the client runtime/auth context, never provider API keys."""
    allowed = {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "USER"}
    return {
        key: value
        for key, value in os.environ.items()
        if value
        and (
            key in allowed
            or key.startswith("AGY_")
            or key.startswith("ANTIGRAVITY_")
            or key.startswith("XDG_")
        )
    }


def _subscription_completion_text(raw: object) -> bool:
    """Reject CLI banners and the known wrong-argument conversational reply.

    The engine accepts fenced JSON in its existing tolerance layer, but a
    subscription invocation must still deliver a JSON completion to that layer
    rather than a conversational acknowledgement or a progress banner.
    """
    if not isinstance(raw, str) or not raw.strip():
        return False
    from .json_tolerance import extract_json

    normalized = raw.lstrip().lower()
    conversational = (
        "understood",
        "sure",
        "certainly",
        "i will",
        "i'll",
        "here is",
        "as an ai",
    )
    if normalized.startswith(conversational):
        return False
    return extract_json(raw) is not None


def _extract_subscription_completion(raw: object) -> str | None:
    """Extract one terminal SUCCESS response from a stream-json transcript.

    The subscription client may emit progress events before its terminal
    result. Every non-empty stdout line must still be valid JSON, and the
    terminal result must be the final event. This keeps malformed, duplicate,
    failed, or partial streams fail-closed without exposing provider output in
    an exception message.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None

    completion: str | None = None
    terminal_seen = False
    for line in raw.splitlines():
        if not line.strip():
            continue
        if terminal_seen:
            return None
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(event, dict):
            return None
        event_name = event.get("event")
        if not isinstance(event_name, str) or not event_name:
            return None
        if event_name == "error":
            return None
        if event_name != "result":
            continue
        result = event.get("result")
        if not isinstance(result, dict) or result.get("status") != "SUCCESS":
            return None
        response = result.get("response")
        if not isinstance(response, str) or not _subscription_completion_text(response):
            return None
        if completion is not None:
            return None
        completion = response
        terminal_seen = True
    return completion


def _extract_text(body: Any) -> str:
    """Pull the assistant message text out of an OpenAI-compatible envelope."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeneratorUnavailable("provider response had no choices[0].message.content") from exc
    if not isinstance(content, str):
        raise GeneratorUnavailable("provider response content was not text")
    return content


@dataclass
class HttpChatTransport:
    """A `transport.Transport` over an OpenAI-compatible `/chat/completions` API.

    Retries transient 5xx, timeout, and connection failures with bounded
    exponential backoff. HTTP 429 immediately becomes fallback-eligible rather
    than sleeping on a known rate-limited host. Other 4xx and malformed
    envelopes are immediate `GeneratorUnavailable` failures. `client` is
    injectable so unit tests drive it with an `httpx.MockTransport` and NEVER
    hit the network.
    """

    base_url: str
    client: Any | None = None  # httpx.Client | None (injected in tests)
    host: str = "ais"
    strip_model_prefix: bool = True
    max_attempts: int = 3
    retry_backoff_s: float = 0.75
    retry_json_mode_on_400: bool = True

    def __call__(self, prompt: str, *, api_key: str, model: str, timeout_s: int) -> str:
        import time

        import httpx

        url = self.base_url.rstrip("/") + "/chat/completions"
        # Canonical ids carry OUR routing prefix ("google-ais/gemma-4-31b-it");
        # the provider API knows only the bare model id. Sending the prefixed id
        # returns HTTP 404 (bake-off 2026-07-10, all gemma cells). Strip at the
        # wire; keep the canonical id in fingerprints/meta.
        wire_model = model.split("/", 1)[1] if self.strip_model_prefix and "/" in model else model

        temperature = serializer_temperature()

        # Gemini can enforce JSON at the API boundary, including AIS, when
        # explicitly enabled. Gemma remains excluded by the #171 measurements.
        json_mode_enabled = json_mode_enabled_for_model(model)

        payload = {
            "model": wire_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": temperature,
        }  # TOOLLESS: no `tools` key by construction
        if json_mode_enabled:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        prompt_bytes = len(prompt.encode("utf-8"))
        last_error: GeneratorUnavailable | None = None

        start_time = time.perf_counter()
        started_at_ms = int(start_time * 1000)
        attempts = 0
        status_class = "error"

        try:
            for attempt in range(1, self.max_attempts + 1):
                attempts = attempt
                log.info(
                    "%s request model=%s attempt=%d prompt_bytes=%d",
                    self.host,
                    model,
                    attempt,
                    prompt_bytes,
                )
                client = self.client or httpx.Client(timeout=timeout_s)
                owned = self.client is None
                try:
                    with provider_call_slot():
                        response = client.post(url, json=payload, headers=headers)

                    if (
                        self.retry_json_mode_on_400
                        and response.status_code == 400
                        and "response_format" in payload
                    ):
                        log.warning(
                            "%s 400 JSON mode bad request. Retrying without it. model=%s",
                            self.host,
                            model,
                        )
                        ctx = telemetry_ctx.get()
                        if ctx is not None:
                            ctx.record_event(
                                {
                                    "event": "json_mode_unsupported",
                                    "host": self.host,
                                    "model": model,
                                }
                            )
                        payload.pop("response_format", None)
                        with provider_call_slot():
                            response = client.post(url, json=payload, headers=headers)
                except httpx.TimeoutException:
                    log.warning("%s timeout model=%s attempt=%d", self.host, model, attempt)
                    last_error = GeneratorUnavailable(f"provider timed out after {timeout_s}s")
                    status_class = "timeout"
                    if attempt < self.max_attempts:
                        time.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
                        continue
                    break
                except httpx.HTTPError as exc:  # transport error — retried w/ backoff
                    log.warning(
                        "%s transport error=%s model=%s attempt=%d",
                        self.host,
                        type(exc).__name__,
                        model,
                        attempt,
                    )
                    last_error = GeneratorUnavailable(
                        f"provider transport error: {type(exc).__name__}"
                    )
                    status_class = "error"
                    if attempt < self.max_attempts:
                        time.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
                        continue
                    break
                finally:
                    if owned:
                        client.close()

                code = response.status_code
                if code >= 500:
                    log.warning(
                        "%s 5xx model=%s status=%d attempt=%d",
                        self.host,
                        model,
                        code,
                        attempt,
                    )
                    last_error = GeneratorUnavailable(f"provider returned HTTP {code}")
                    status_class = "5xx"
                    if attempt < self.max_attempts:
                        time.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
                        continue
                    break
                if code == 429:
                    # Rate limiting is an availability failure. Do not retry the
                    # same host, but expose the existing typed outage signal so an
                    # eligible caller can use its configured fallback.
                    log.warning("%s 429 model=%s fallback-eligible", self.host, model)
                    status_class = "4xx"
                    raise GeneratorUnavailable(
                        f"provider returned HTTP {code} for model {model}", retry_exhausted=True
                    )
                if code >= 400:
                    # auth/bad-request — do not retry, never echo the body
                    status_class = "4xx"
                    raise GeneratorUnavailable(f"provider returned HTTP {code} for model {model}")
                text = _extract_text(response.json())
                log.info(
                    "%s response model=%s status=%d response_bytes=%d attempt=%d",
                    self.host,
                    model,
                    code,
                    len(response.content),
                    attempt,
                )
                status_class = "2xx"
                return text

            if last_error is not None:
                raise GeneratorUnavailable(str(last_error), retry_exhausted=True) from last_error
            raise GeneratorUnavailable("provider generation failed")
        finally:
            ended_at_ms = int(time.perf_counter() * 1000)
            duration_ms = ended_at_ms - started_at_ms
            ctx = telemetry_ctx.get()
            phase = ctx.phase if ctx is not None else None
            activity_types = ctx.activity_types if ctx is not None else []
            phase_str = str(phase) if phase is not None else "null"
            type_slug = "+".join(activity_types) if activity_types else "unknown"

            log.info(
                "gen call phase=%s type=%s host=%s dur_ms=%d attempts=%d",
                phase_str,
                type_slug,
                self.host,
                duration_ms,
                attempts,
            )

            if ctx is not None:
                trace_entry = {
                    "duration_ms": duration_ms,
                    "host": self.host,
                    "model": model,
                    "attempts": attempts,
                    "http_status_class": status_class,
                    "phase": phase,
                    "activity_type": type_slug,
                    "started_at_ms": started_at_ms,
                    "ended_at_ms": ended_at_ms,
                }
                ctx.record_provider_call(trace_entry)


def _extract_vertex_text(body: Any) -> str:
    """Pull text parts from a native Vertex ``generateContent`` envelope."""
    try:
        parts = body["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeneratorUnavailable("Vertex response had no candidates[0].content.parts") from exc
    if not isinstance(parts, list):
        raise GeneratorUnavailable("Vertex response content parts were not a list")
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str)
    )
    if not text:
        raise GeneratorUnavailable("Vertex response content had no text parts")
    return text


@dataclass
class VertexGenerateContentTransport:
    """Google Vertex native ``generateContent`` transport.

    Vertex is intentionally not sent through the OpenAI-compatible AIS client:
    it requires a publisher-model URL, ``x-goog-api-key`` authentication, and a
    ``contents``/``parts`` envelope.  ``client`` remains injectable so tests can
    prove the wire contract without any provider I/O.
    """

    base_url: str
    client: Any | None = None  # httpx.Client | None (injected in tests)
    host: str = "google-vertex"
    max_attempts: int = 3
    retry_backoff_s: float = 0.75
    retry_json_mode_on_400: bool = True

    def __call__(self, prompt: str, *, api_key: str, model: str, timeout_s: int) -> str:
        import time

        import httpx

        try:
            base_url = _validate_vertex_base_url(self.base_url)
        except ValueError as exc:
            raise GeneratorUnavailable("Vertex base URL is invalid") from exc
        if not model or "/" in model:
            raise GeneratorUnavailable("Vertex model identifier must be a bare model ID")
        url = f"{base_url}/models/{quote(model, safe='-._')}:generateContent"

        temperature = serializer_temperature()
        generation_config = {"temperature": temperature}
        if json_mode_enabled_for_model(model):
            generation_config["responseMimeType"] = "application/json"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }  # TOOLLESS: native Vertex payload has no ``tools`` key by construction
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        prompt_bytes = len(prompt.encode("utf-8"))
        last_error: GeneratorUnavailable | None = None

        start_time = time.perf_counter()
        started_at_ms = int(start_time * 1000)
        attempts = 0
        status_class = "error"
        try:
            for attempt in range(1, self.max_attempts + 1):
                attempts = attempt
                log.info(
                    "%s request model=%s attempt=%d prompt_bytes=%d",
                    self.host,
                    model,
                    attempt,
                    prompt_bytes,
                )
                client = self.client or httpx.Client(timeout=timeout_s)
                owned = self.client is None
                try:
                    with provider_call_slot():
                        response = client.post(url, json=payload, headers=headers)

                    if (
                        self.retry_json_mode_on_400
                        and response.status_code == 400
                        and "responseMimeType" in generation_config
                    ):
                        log.warning(
                            "%s 400 JSON mode bad request. Retrying without it. model=%s",
                            self.host,
                            model,
                        )
                        ctx = telemetry_ctx.get()
                        if ctx is not None:
                            ctx.record_event(
                                {
                                    "event": "json_mode_unsupported",
                                    "host": self.host,
                                    "model": model,
                                }
                            )
                        generation_config.pop("responseMimeType", None)
                        with provider_call_slot():
                            response = client.post(url, json=payload, headers=headers)
                except httpx.TimeoutException:
                    log.warning("%s timeout model=%s attempt=%d", self.host, model, attempt)
                    last_error = GeneratorUnavailable(f"provider timed out after {timeout_s}s")
                    status_class = "timeout"
                    if attempt < self.max_attempts:
                        time.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
                        continue
                    break
                except httpx.HTTPError as exc:
                    log.warning(
                        "%s transport error=%s model=%s attempt=%d",
                        self.host,
                        type(exc).__name__,
                        model,
                        attempt,
                    )
                    last_error = GeneratorUnavailable(
                        f"provider transport error: {type(exc).__name__}"
                    )
                    status_class = "error"
                    if attempt < self.max_attempts:
                        time.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
                        continue
                    break
                finally:
                    if owned:
                        client.close()

                code = response.status_code
                if code >= 500:
                    log.warning(
                        "%s 5xx model=%s status=%d attempt=%d",
                        self.host,
                        model,
                        code,
                        attempt,
                    )
                    last_error = GeneratorUnavailable(f"provider returned HTTP {code}")
                    status_class = "5xx"
                    if attempt < self.max_attempts:
                        time.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
                        continue
                    break
                if code == 429:
                    log.warning("%s 429 model=%s fallback-eligible", self.host, model)
                    status_class = "4xx"
                    raise GeneratorUnavailable(
                        f"provider returned HTTP {code} for model {model}", retry_exhausted=True
                    )
                if code >= 400:
                    status_class = "4xx"
                    raise GeneratorUnavailable(f"provider returned HTTP {code} for model {model}")
                text = _extract_vertex_text(response.json())
                log.info(
                    "%s response model=%s status=%d response_bytes=%d attempt=%d",
                    self.host,
                    model,
                    code,
                    len(response.content),
                    attempt,
                )
                status_class = "2xx"
                return text

            if last_error is not None:
                raise GeneratorUnavailable(str(last_error), retry_exhausted=True) from last_error
            raise GeneratorUnavailable("provider generation failed")
        finally:
            ended_at_ms = int(time.perf_counter() * 1000)
            duration_ms = ended_at_ms - started_at_ms
            ctx = telemetry_ctx.get()
            phase = ctx.phase if ctx is not None else None
            activity_types = ctx.activity_types if ctx is not None else []
            phase_str = str(phase) if phase is not None else "null"
            type_slug = "+".join(activity_types) if activity_types else "unknown"
            log.info(
                "gen call phase=%s type=%s host=%s dur_ms=%d attempts=%d",
                phase_str,
                type_slug,
                self.host,
                duration_ms,
                attempts,
            )
            if ctx is not None:
                ctx.record_provider_call(
                    {
                        "duration_ms": duration_ms,
                        "host": self.host,
                        "model": model,
                        "attempts": attempts,
                        "http_status_class": status_class,
                        "phase": phase,
                        "activity_type": type_slug,
                        "started_at_ms": started_at_ms,
                        "ended_at_ms": ended_at_ms,
                    }
                )


class FailoverGeneratorPort(AISGeneratorPort):
    """One primary with an outage-only fallback that preserves actual provenance."""

    def __init__(
        self, *, fallback: AISGeneratorPort, fallback_label: str = "gemma", **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._fallback = fallback
        self._fallback_label = fallback_label

    def bind_semantic_review_route(self, *, route_id: str, host: str, model_id: str) -> None:
        """Transparent fallback is incompatible with exact semantic-review attribution."""
        del route_id, host, model_id
        raise ValueError("A failover provider cannot be a qualified semantic reviewer route.")

    def __call__(self, prompt: str) -> str:
        try:
            return super().__call__(prompt)
        except GeneratorUnavailable as primary_error:
            # Transport retry exhaustion and 429 represent the sanctioned AIS
            # outage path. Missing keys, other 4xx responses, and malformed
            # output must not spend paid OpenRouter tokens.
            if not primary_error.retry_exhausted or not self._fallback.is_configured():
                raise
            fallback_host = getattr(self._fallback._transport, "host", "fallback")
            log.warning("%s fallback engaged host=%s", self._fallback_label, fallback_host)
            try:
                return self._fallback(prompt)
            except GeneratorUnavailable as fallback_error:
                # Keep the existing typed boundary and avoid surfacing either
                # provider's detail in durable job state. The final fallback
                # outcome controls whether a fresh bake is eligible to retry.
                raise GeneratorUnavailable(
                    "provider generation failed",
                    retry_exhausted=bool(fallback_error.retry_exhausted),
                ) from fallback_error
