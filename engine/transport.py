"""Private generator transport — the ONLY door to the Gemma provider.

Replaces the slice-1 prototype's opencode-bridge import and its dev-home key
file. The locked route is kept — `google-ais/gemma-4-31b-it`, TOOLLESS — but:

  - the AIS secret is read from the `HRAMATKA_AIS_API_KEY` env var (Scaleway
    Secret Manager in production), never a hardcoded path;
  - the concrete provider HTTP call is a pluggable `transport` seam. The default
    is an explicit stub — the real client lands with the mock→real swap (a
    separate step) — so nothing here performs network I/O by default.

Key values are never logged; only the env var NAME appears in messages.
"""

from __future__ import annotations

import os
from typing import Protocol

GEMMA_MODEL = "google-ais/gemma-4-31b-it"
GEMMA_TIMEOUT_S = 900
AIS_API_KEY_ENV = "HRAMATKA_AIS_API_KEY"


class GeneratorUnavailable(RuntimeError):
    """Transport/routing/key failure — the pipeline records a generation error
    rather than crashing."""


class GenerationUnparseable(RuntimeError):
    """Model output could not be parsed as JSON after one retry."""


class Transport(Protocol):
    """A concrete provider call: prompt + secret + route -> raw model text."""

    def __call__(
        self, prompt: str, *, api_key: str, model: str, timeout_s: int
    ) -> str: ...


def _unwired_transport(prompt: str, *, api_key: str, model: str, timeout_s: int) -> str:
    """Default transport: the real google-ais client is not wired here."""
    del prompt, api_key, model, timeout_s
    raise GeneratorUnavailable(
        "google-ais transport is not wired in this environment. The mock→real "
        "swap is a separate step; inject a Transport to reach the provider."
    )


class AISGeneratorPort:
    """Callable `GeneratorPort` (prompt -> raw text) over the locked AIS route.

    Toolless by construction: the request carries no tool schema. The AIS key is
    resolved lazily (env by default) so constructing the port never needs a
    secret; only an actual generation does.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = AIS_API_KEY_ENV,
        model: str = GEMMA_MODEL,
        timeout_s: int = GEMMA_TIMEOUT_S,
        transport: Transport | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._model = model
        self._timeout_s = timeout_s
        self._transport: Transport = transport or _unwired_transport

    def _resolve_key(self) -> str:
        key = self._api_key if self._api_key is not None else os.environ.get(self._api_key_env)
        if not key:
            raise GeneratorUnavailable(
                f"{self._api_key_env} is not set — the locked route "
                f"({self._model}, toolless) requires the key."
            )
        return key

    def __call__(self, prompt: str) -> str:
        key = self._resolve_key()
        try:
            return self._transport(
                prompt, api_key=key, model=self._model, timeout_s=self._timeout_s
            )
        except SystemExit as exc:  # a fail-closed transport guard must not crash the worker
            raise GeneratorUnavailable(
                f"google-ais generation unavailable: {exc}"
            ) from exc
