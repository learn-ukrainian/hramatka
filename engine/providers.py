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

import logging
import os
from dataclasses import dataclass
from typing import Any

from .transport import (
    AIS_API_KEY_ENV,
    GEMMA_MODEL,
    GEMMA_TIMEOUT_S,
    AISGeneratorPort,
    GeneratorUnavailable,
)

log = logging.getLogger(__name__)

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

        for attempt in (1, 2):  # single retry on 5xx/timeout
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
                continue
            except httpx.HTTPError as exc:  # connect/transport error — not retriable
                raise GeneratorUnavailable(
                    f"provider transport error: {type(exc).__name__}"
                ) from exc
            finally:
                if owned:
                    client.close()

            code = response.status_code
            if code >= 500:
                log.warning("%s 5xx model=%s status=%d attempt=%d", self.host, model, code, attempt)
                last_error = GeneratorUnavailable(f"provider returned HTTP {code}")
                continue
            if code == 429:
                # Rate limiting is an availability failure. Do not retry the
                # same host, but expose the existing typed outage signal so an
                # eligible caller can use its configured fallback.
                log.warning("%s 429 model=%s fallback-eligible", self.host, model)
                raise GeneratorUnavailable(
                    f"provider returned HTTP {code} for model {model}", retry_exhausted=True
                )
            if code >= 400:
                # auth/bad-request — do not retry, never echo the body
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
            return text

        if last_error is not None:
            raise GeneratorUnavailable(str(last_error), retry_exhausted=True) from last_error
        raise GeneratorUnavailable("provider generation failed")


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
