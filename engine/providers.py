"""Concrete provider transports + a name→generator registry (step-4a swap).

The real Google-AIS Gemma call and the deepseek bake-off engine both speak the
same OpenAI-compatible `/chat/completions` shape, differing only by base URL,
model id, and which env var holds the key — so one HTTP transport serves both
and `make_generator(name)` wires the right `AISGeneratorPort`. The model
bake-off (#41) swaps engines through this registry.

TOOLLESS by construction: the request body never carries a `tools` field.

Secret + payload hygiene: the key is never logged (it lives only in the
Authorization header) and prompt/response CONTENT is never logged — only byte
SIZES, model id, HTTP status, and attempt number, all at INFO.
"""

from __future__ import annotations

import contextvars
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .transport import (
    AIS_API_KEY_ENV,
    GEMMA_MODEL,
    GEMMA_TIMEOUT_S,
    AISGeneratorPort,
    GeneratorUnavailable,
)

log = logging.getLogger(__name__)


@dataclass
class TelemetryContext:
    job_id: str | None = None
    store: Any | None = None
    phases_total: int = 0
    calls_planned: int | None = None
    calls_done: int | None = None
    phase: int | None = None
    step: str | None = None
    trace_dir: Path | None = None
    traces: list[dict] = field(default_factory=list)
    activity_types: list[str] = field(default_factory=list)

    def update_progress_db(
        self,
        *,
        step: str | None = None,
        phase: int | None = None,
        calls_done: int | None = None,
        calls_planned: int | None = None,
    ) -> None:
        if step is not None:
            self.step = step
        if phase is not None:
            self.phase = phase
        if calls_done is not None:
            self.calls_done = calls_done
        if calls_planned is not None:
            self.calls_planned = calls_planned

        if self.store is not None and self.job_id is not None:
            from datetime import UTC, datetime
            timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            progress_obj = {
                "phase": self.phase,
                "phases_total": self.phases_total,
                "step": self.step,
                "calls_done": self.calls_done,
                "calls_planned": self.calls_planned,
                "updated_at": timestamp,
            }
            try:
                self.store.update_progress(self.job_id, progress_obj)
            except Exception as exc:
                log.warning("Failed to update progress in DB for job %s: %s", self.job_id, exc)

    def save_traces(self) -> None:
        if self.trace_dir:
            try:
                trace_file = self.trace_dir / "trace.json"
                import json
                trace_file.write_text(
                    json.dumps(self.traces, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception as exc:
                log.warning("Failed to write trace.json: %s", exc)


telemetry_ctx = contextvars.ContextVar("telemetry_ctx", default=None)

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

DEEPSEEK_API_KEY_ENV = "HRAMATKA_DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL_ENV = "HRAMATKA_DEEPSEEK_BASE_URL"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_V4_PRO_MODEL = "deepseek-v4-pro"


def _extract_text(body: Any) -> str:
    """Pull the assistant message text out of an OpenAI-compatible envelope."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeneratorUnavailable(
            "provider response had no choices[0].message.content"
        ) from exc
    if not isinstance(content, str):
        raise GeneratorUnavailable("provider response content was not text")
    return content


@dataclass
class HttpChatTransport:
    """A `transport.Transport` over an OpenAI-compatible `/chat/completions` API.

    One retry on 5xx/timeout; HTTP 429 is immediately availability-failing so a
    caller may fail over without retrying the rate-limited host. Other 4xx,
    connect errors, and malformed envelopes are immediate `GeneratorUnavailable`
    failures. `client` is injectable so unit tests drive it with an
    `httpx.MockTransport` and NEVER hit the network.
    """

    base_url: str
    client: Any | None = None  # httpx.Client | None (injected in tests)
    host: str = "ais"
    strip_model_prefix: bool = True

    def __call__(self, prompt: str, *, api_key: str, model: str, timeout_s: int) -> str:
        import time

        import httpx

        url = self.base_url.rstrip("/") + "/chat/completions"
        # Canonical ids carry OUR routing prefix ("google-ais/gemma-4-31b-it");
        # the provider API knows only the bare model id. Sending the prefixed id
        # returns HTTP 404 (bake-off 2026-07-10, all gemma cells). Strip at the
        # wire; keep the canonical id in fingerprints/meta.
        wire_model = model.split("/", 1)[1] if self.strip_model_prefix and "/" in model else model
        payload = {
            "model": wire_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }  # TOOLLESS: no `tools` key by construction
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        prompt_bytes = len(prompt.encode("utf-8"))
        last_error: GeneratorUnavailable | None = None

        start_time = time.perf_counter()
        attempts = 0
        status_class = "error"

        try:
            for attempt in (1, 2):  # single retry on 5xx/timeout
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
                    response = client.post(url, json=payload, headers=headers)
                except httpx.TimeoutException:
                    log.warning("%s timeout model=%s attempt=%d", self.host, model, attempt)
                    last_error = GeneratorUnavailable(f"provider timed out after {timeout_s}s")
                    status_class = "timeout"
                    continue
                except httpx.HTTPError as exc:  # connect/transport error — not retriable
                    status_class = "error"
                    raise GeneratorUnavailable(
                        f"provider transport error: {type(exc).__name__}"
                    ) from exc
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
                    continue
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
                    raise GeneratorUnavailable(
                        f"provider returned HTTP {code} for model {model}"
                    )
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
            duration_ms = int((time.perf_counter() - start_time) * 1000)
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
                    "attempts": attempts,
                    "http_status_class": status_class,
                    "phase": phase,
                    "activity_type": type_slug,
                }
                ctx.traces.append(trace_entry)
                ctx.save_traces()

                if ctx.calls_done is not None:
                    ctx.calls_done += 1
                    ctx.update_progress_db(calls_done=ctx.calls_done)


class FailoverGeneratorPort(AISGeneratorPort):
    """AIS primary with an opt-in OpenRouter Gemma host for AIS outages only."""

    def __init__(self, *, fallback: AISGeneratorPort, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fallback = fallback

    def __call__(self, prompt: str) -> str:
        try:
            return super().__call__(prompt)
        except GeneratorUnavailable as primary_error:
            # Transport retry exhaustion and 429 represent the sanctioned AIS
            # outage path. Missing keys, other 4xx responses, and malformed
            # output must not spend paid OpenRouter tokens.
            if not primary_error.retry_exhausted or not self._fallback.is_configured():
                raise
            log.warning("gemma fallback engaged host=openrouter")
            try:
                return self._fallback(prompt)
            except GeneratorUnavailable as fallback_error:
                # Keep the existing typed boundary and avoid surfacing either
                # provider's detail in durable job state.
                raise GeneratorUnavailable("provider generation failed") from fallback_error


def make_generator(name: str) -> AISGeneratorPort:
    """Name -> a configured generator (prompt -> raw text). The model bake-off
    swaps engines by name through this registry.

      "gemma-ais" / "gemma" / "google-ais" -> locked Gemma route, HRAMATKA_AIS_API_KEY
      "deepseek"                            -> deepseek-chat, HRAMATKA_DEEPSEEK_API_KEY + _BASE_URL
      "deepseek-v4-flash" / "deepseek-v4-pro" -> explicit V4 tier, same DeepSeek route
    """
    key = name.lower()
    if key in ("gemma-ais", "gemma", "google-ais"):
        base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
        fallback_base = os.environ.get(
            GEMMA_FALLBACK_BASE_URL_ENV, DEFAULT_GEMMA_FALLBACK_BASE_URL
        )
        fallback_model = os.environ.get(GEMMA_FALLBACK_MODEL_ENV, DEFAULT_GEMMA_FALLBACK_MODEL)
        fallback = AISGeneratorPort(
            api_key_env=GEMMA_FALLBACK_API_KEY_ENV,
            api_key_file_env=GEMMA_FALLBACK_API_KEY_FILE_ENV,
            model=fallback_model,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(
                base_url=fallback_base,
                host="openrouter",
                strip_model_prefix=False,
            ),
        )
        return FailoverGeneratorPort(
            fallback=fallback,
            api_key_env=AIS_API_KEY_ENV,
            model=GEMMA_MODEL,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(base_url=base),
        )
    if key == "deepseek":
        base = os.environ.get(DEEPSEEK_BASE_URL_ENV, DEFAULT_DEEPSEEK_BASE_URL)
        return AISGeneratorPort(
            api_key_env=DEEPSEEK_API_KEY_ENV,
            model=DEEPSEEK_MODEL,
            transport=HttpChatTransport(base_url=base),
        )
    if key in (DEEPSEEK_V4_FLASH_MODEL, DEEPSEEK_V4_PRO_MODEL):
        base = os.environ.get(DEEPSEEK_BASE_URL_ENV, DEFAULT_DEEPSEEK_BASE_URL)
        return AISGeneratorPort(
            api_key_env=DEEPSEEK_API_KEY_ENV,
            model=key,
            transport=HttpChatTransport(base_url=base),
        )
    raise ValueError(
        "unknown generator "
        f"{name!r}; known: gemma-ais, deepseek, deepseek-v4-flash, deepseek-v4-pro"
    )
