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

DEEPSEEK_API_KEY_ENV = "HRAMATKA_DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL_ENV = "HRAMATKA_DEEPSEEK_BASE_URL"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"


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

    One retry on 5xx/timeout; any other failure (4xx, connect error, malformed
    envelope) is an immediate `GeneratorUnavailable`. `client` is injectable so
    unit tests drive it with an `httpx.MockTransport` and NEVER hit the network.
    """

    base_url: str
    client: Any | None = None  # httpx.Client | None (injected in tests)

    def __call__(self, prompt: str, *, api_key: str, model: str, timeout_s: int) -> str:
        import httpx

        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
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
                "ais request model=%s attempt=%d prompt_bytes=%d", model, attempt, prompt_bytes
            )
            client = self.client or httpx.Client(timeout=timeout_s)
            owned = self.client is None
            try:
                response = client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException:
                log.warning("ais timeout model=%s attempt=%d", model, attempt)
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
                log.warning("ais 5xx model=%s status=%d attempt=%d", model, code, attempt)
                last_error = GeneratorUnavailable(f"provider returned HTTP {code}")
                continue
            if code >= 400:
                # auth/quota/bad-request — do not retry, never echo the body
                raise GeneratorUnavailable(
                    f"provider returned HTTP {code} for model {model}"
                )
            text = _extract_text(response.json())
            log.info(
                "ais response model=%s status=%d response_bytes=%d attempt=%d",
                model,
                code,
                len(response.content),
                attempt,
            )
            return text

        raise last_error or GeneratorUnavailable("provider generation failed")


def make_generator(name: str) -> AISGeneratorPort:
    """Name -> a configured generator (prompt -> raw text). The model bake-off
    swaps engines by name through this registry.

      "gemma-ais" / "gemma" / "google-ais" -> locked Gemma route, HRAMATKA_AIS_API_KEY
      "deepseek"                            -> HRAMATKA_DEEPSEEK_API_KEY + _BASE_URL
    """
    key = name.lower()
    if key in ("gemma-ais", "gemma", "google-ais"):
        base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
        return AISGeneratorPort(
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
    raise ValueError(f"unknown generator {name!r}; known: gemma-ais, deepseek")
