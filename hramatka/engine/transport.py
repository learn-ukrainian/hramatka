"""Private generator transport — the ONLY door to the Gemma provider.

Replaces the slice-1 prototype's opencode-bridge import and its dev-home key
file. The locked route is kept — `google-ais/gemma-4-31b-it`, TOOLLESS — but:

  - the AIS secret is read from the `HRAMATKA_AIS_API_KEY` env var or the
    root-readable file named by `HRAMATKA_AIS_API_KEY_FILE`, never a hardcoded
    path;
  - the concrete provider HTTP call is a pluggable `transport` seam. The default
    is an explicit stub — the real client lands with the mock→real swap (a
    separate step) — so nothing here performs network I/O by default.

Key values are never logged; only the env var NAME appears in messages.
"""

from __future__ import annotations

import contextvars
import os
from hashlib import sha256
from pathlib import Path
from typing import Protocol

GEMMA_MODEL = "google-ais/gemma-4-31b-it"
GEMMA_TIMEOUT_S = 900
AIS_API_KEY_ENV = "HRAMATKA_AIS_API_KEY"
AIS_API_KEY_FILE_ENV = "HRAMATKA_AIS_API_KEY_FILE"
METERED_PROVIDER_SPEND_ACK_ENV = "HRAMATKA_ACCEPT_METERED_PROVIDER_SPEND"

generator_model_id = contextvars.ContextVar("generator_model_id", default=None)
generator_route_identity = contextvars.ContextVar("generator_route_identity", default=None)
activity_model_registry = contextvars.ContextVar("activity_model_registry", default=None)


class GeneratorUnavailable(RuntimeError):
    """Transport/routing/key failure — the pipeline records a generation error
    rather than crashing."""

    def __init__(self, message: str, *, retry_exhausted: bool = False) -> None:
        super().__init__(message)
        # A caller may use the existing typed signal to distinguish an upstream
        # outage after the transport's own retry discipline from configuration,
        # auth, or malformed-response failures.  The flag is deliberately not
        # rendered into error text or durable telemetry.
        self.retry_exhausted = retry_exhausted


class GenerationUnparseable(RuntimeError):
    """Model output could not be parsed as JSON after bounded retries."""


class Transport(Protocol):
    """A concrete provider call: prompt + secret + route -> raw model text."""

    def __call__(
        self, prompt: str, *, api_key: str, model: str, timeout_s: int
    ) -> str: ...


class GeneratorPort(Protocol):
    """Qualification generator-port contract with content-free provenance."""

    def __call__(self, prompt: str) -> str: ...

    def receipt_provenance(self) -> dict[str, object]: ...


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
        api_key_file_env: str | None = None,
        model: str = GEMMA_MODEL,
        timeout_s: int = GEMMA_TIMEOUT_S,
        transport: Transport | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._api_key_file_env = api_key_file_env
        self._model = model
        self._timeout_s = timeout_s
        self._transport: Transport = transport or _unwired_transport
        self._raw_output_sha256: list[str] = []
        self._semantic_review_route: tuple[str, str, str] | None = None

    def bind_semantic_review_route(
        self, *, route_id: str, host: str, model_id: str
    ) -> None:
        """Seal one qualification route onto this non-failover provider port."""
        actual_host = getattr(self._transport, "host", None)
        if (
            not all(isinstance(value, str) and value for value in (route_id, host, model_id))
            or actual_host != host
            or self._model != model_id
        ):
            raise ValueError("Semantic-review route binding does not match the provider port.")
        binding = (route_id, host, model_id)
        if self._semantic_review_route not in {None, binding}:
            raise ValueError("Semantic-review provider port is already bound to another route.")
        self._semantic_review_route = binding

    def semantic_review_route_identity(self) -> tuple[str, str, str] | None:
        """Return the factory-sealed route; unqualified ports remain unavailable."""
        return self._semantic_review_route

    def resolve_key(self) -> str:
        """Resolve the configured AIS key without exposing its source."""
        key = self._api_key if self._api_key is not None else os.environ.get(self._api_key_env)
        if key:
            key = key.strip()
        if not key and self._api_key_file_env:
            key_file = os.environ.get(self._api_key_file_env)
            if key_file:
                try:
                    key = Path(key_file).read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError) as exc:
                    raise GeneratorUnavailable(
                        f"{self._api_key_file_env} could not be read"
                    ) from exc
        if not key:
            configured_key = (
                f"{self._api_key_env} or {self._api_key_file_env}"
                if self._api_key_file_env
                else self._api_key_env
            )
            raise GeneratorUnavailable(
                f"{configured_key} is not set — the locked route "
                f"({self._model}, toolless) requires the key."
            )
        return key

    def is_configured(self) -> bool:
        """Whether a key source is configured, without resolving its value."""
        return bool(
            self._api_key
            or os.environ.get(self._api_key_env)
            or (self._api_key_file_env and os.environ.get(self._api_key_file_env))
        )

    def receipt_provenance(self) -> dict[str, object]:
        """Return content-free evidence observed at the API boundary."""
        if not self._raw_output_sha256:
            raise GeneratorUnavailable("AIS provenance is incomplete")
        return {
            "tier": "api_observed",
            "client_version": None,
            "requested_model": self._model,
            "raw_output_sha256": tuple(self._raw_output_sha256),
        }

    def __call__(self, prompt: str) -> str:
        if os.environ.get(METERED_PROVIDER_SPEND_ACK_ENV) != "1":
            raise GeneratorUnavailable(
                "Provider generation requires "
                f"{METERED_PROVIDER_SPEND_ACK_ENV}=1 because the key may be billed."
            )
        key = self.resolve_key()
        try:
            res = self._transport(
                prompt, api_key=key, model=self._model, timeout_s=self._timeout_s
            )
            generator_model_id.set(self._model)
            host = getattr(self._transport, "host", None)
            generator_route_identity.set(
                (host, self._model) if isinstance(host, str) and host else None
            )
            self._raw_output_sha256.append(sha256(res.encode("utf-8")).hexdigest())
            return res
        except SystemExit as exc:  # a fail-closed transport guard must not crash the worker
            raise GeneratorUnavailable(
                f"google-ais generation unavailable: {exc}"
            ) from exc
