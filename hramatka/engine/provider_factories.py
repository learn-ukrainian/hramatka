"""Provider route tables and generator factories (#456)."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from hramatka.engine.provider_transports import (
    DEEPINFRA_API_KEY_ENV,
    DEEPINFRA_BASE_URL_ENV,
    DEEPINFRA_MODEL,
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_BASE_URL_ENV,
    DEEPSEEK_MODEL,
    DEEPSEEK_V4_FLASH_MODEL,
    DEEPSEEK_V4_PRO_MODEL,
    DEFAULT_DEEPINFRA_BASE_URL,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_GEMMA_AIS_BASE_URL,
    DEFAULT_GEMMA_FALLBACK_BASE_URL,
    DEFAULT_GEMMA_FALLBACK_MODEL,
    DEFAULT_SUBSCRIPTION_EXECUTABLE,
    DEFAULT_SUBSCRIPTION_MODEL,
    GEMMA_AIS_BASE_URL_ENV,
    GEMMA_FALLBACK_API_KEY_ENV,
    GEMMA_FALLBACK_API_KEY_FILE_ENV,
    GEMMA_FALLBACK_BASE_URL_ENV,
    GEMMA_FALLBACK_MODEL_ENV,
    SUBSCRIPTION_EXECUTABLE_ENV,
    SUBSCRIPTION_HOST,
    SUBSCRIPTION_MODEL_ENV,
    SUBSCRIPTION_PROVIDER,
    VERTEX_API_KEY_ENV,
    VERTEX_API_KEY_FILE_ENV,
    VERTEX_BASE_URL_ENV,
    FailoverGeneratorPort,
    HttpChatTransport,
    SubscriptionGeneratorPort,
    VertexGenerateContentTransport,
    _get_bake_providers,
    _validate_vertex_base_url,
)
from hramatka.engine.transport import (
    AIS_API_KEY_ENV,
    AIS_API_KEY_FILE_ENV,
    GEMMA_MODEL,
    GEMMA_TIMEOUT_S,
    AISGeneratorPort,
)

log = logging.getLogger("hramatka.engine.providers")


def _gemma_routes() -> tuple[AISGeneratorPort, AISGeneratorPort, AISGeneratorPort | None]:
    """Construct provider-native Gemma ports without performing I/O."""
    ais_base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
    openrouter_base = os.environ.get(GEMMA_FALLBACK_BASE_URL_ENV, DEFAULT_GEMMA_FALLBACK_BASE_URL)
    openrouter_model = os.environ.get(GEMMA_FALLBACK_MODEL_ENV, DEFAULT_GEMMA_FALLBACK_MODEL)
    ais = AISGeneratorPort(
        api_key_env=AIS_API_KEY_ENV,
        api_key_file_env=AIS_API_KEY_FILE_ENV,
        model=GEMMA_MODEL,
        timeout_s=GEMMA_TIMEOUT_S,
        transport=HttpChatTransport(base_url=ais_base, host="google-ais"),
    )
    openrouter = AISGeneratorPort(
        api_key_env=GEMMA_FALLBACK_API_KEY_ENV,
        api_key_file_env=GEMMA_FALLBACK_API_KEY_FILE_ENV,
        model=openrouter_model,
        timeout_s=GEMMA_TIMEOUT_S,
        transport=HttpChatTransport(
            base_url=openrouter_base,
            host="openrouter",
            strip_model_prefix=False,
        ),
    )
    deepinfra = None
    if DEEPINFRA_API_KEY_ENV in os.environ:
        deepinfra_base = os.environ.get(DEEPINFRA_BASE_URL_ENV, DEFAULT_DEEPINFRA_BASE_URL)
        deepinfra = AISGeneratorPort(
            api_key_env=DEEPINFRA_API_KEY_ENV,
            model=DEEPINFRA_MODEL,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(
                base_url=deepinfra_base,
                host="deepinfra",
                strip_model_prefix=False,
            ),
        )
    return ais, openrouter, deepinfra


def _gemini_routes(
    *, ais_model: str, vertex_model: str
) -> tuple[AISGeneratorPort, AISGeneratorPort]:
    """Construct AIS-primary and native-Vertex fallback ports without I/O."""
    vertex_base = os.environ.get(VERTEX_BASE_URL_ENV, "")
    ais = _gemini_ais_route(ais_model)
    vertex = AISGeneratorPort(
        api_key_env=VERTEX_API_KEY_ENV,
        api_key_file_env=VERTEX_API_KEY_FILE_ENV,
        model=vertex_model,
        timeout_s=GEMMA_TIMEOUT_S,
        transport=VertexGenerateContentTransport(base_url=vertex_base),
    )
    return ais, vertex


def _gemini_ais_route(model_id: str) -> AISGeneratorPort:
    """Construct the shared OpenAI-compatible Gemini AIS port without I/O."""
    ais_base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
    return AISGeneratorPort(
        api_key_env=AIS_API_KEY_ENV,
        api_key_file_env=AIS_API_KEY_FILE_ENV,
        model=model_id,
        timeout_s=GEMMA_TIMEOUT_S,
        transport=HttpChatTransport(base_url=ais_base, host="google-ais"),
    )


_QUALIFICATION_ROUTE_SPECS: Mapping[str, tuple[str, str, str, str | None, str | None]] = {
    "gemini-flash-ais": (
        "gemini-3.6-flash",
        "google-ais",
        "google-ais/gemini-3.6-flash",
        AIS_API_KEY_ENV,
        AIS_API_KEY_FILE_ENV,
    ),
    "gemini-flash-subscription": (
        "gemini-3.7-flash",
        SUBSCRIPTION_HOST,
        DEFAULT_SUBSCRIPTION_MODEL,
        None,
        None,
    ),
    "gemini-pro-ais": (
        "gemini-3.1-pro",
        "google-ais",
        "google-ais/gemini-3.1-pro-preview",
        AIS_API_KEY_ENV,
        AIS_API_KEY_FILE_ENV,
    ),
    "gemini-pro-subscription": (
        "gemini-3.1-pro",
        SUBSCRIPTION_HOST,
        "gemini-3.1-pro-high",
        None,
        None,
    ),
    "gemma-ais": (
        "gemma-4-31b",
        "google-ais",
        GEMMA_MODEL,
        AIS_API_KEY_ENV,
        AIS_API_KEY_FILE_ENV,
    ),
    "gemma-openrouter": (
        "gemma-4-31b",
        "openrouter",
        DEFAULT_GEMMA_FALLBACK_MODEL,
        GEMMA_FALLBACK_API_KEY_ENV,
        GEMMA_FALLBACK_API_KEY_FILE_ENV,
    ),
}


def _require_qualification_route(
    *, route_id: str, logical_model_id: str, host: str, model_id: str
) -> tuple[str, str | None]:
    """Validate one immutable qualification-matrix cell without I/O.

    The qualification runner calls this before it constructs a provider.  The
    ordinary production factory deliberately keeps Gemma failover; that is not
    an admissible path while proving one exact qualification route.
    """
    expected = _QUALIFICATION_ROUTE_SPECS.get(route_id)
    if expected is None or expected[:3] != (logical_model_id, host, model_id):
        raise ValueError("Qualification route is not an exact configured matrix cell.")
    return expected[3], expected[4]


def qualification_route_credential_present(
    *, route_id: str, logical_model_id: str, host: str, model_id: str
) -> bool:
    """Return credential-source presence for one pinned cell, without logging it.

    This is deliberately a presence gate, not an authentication probe.  It
    never performs provider I/O and does not expose a key or its value.
    """
    key_env, key_file_env = _require_qualification_route(
        route_id=route_id,
        logical_model_id=logical_model_id,
        host=host,
        model_id=model_id,
    )
    if host == SUBSCRIPTION_HOST:
        return SubscriptionGeneratorPort(
            executable=os.environ.get(SUBSCRIPTION_EXECUTABLE_ENV, DEFAULT_SUBSCRIPTION_EXECUTABLE),
            model=model_id,
        ).is_configured()
    if key_env and os.environ.get(key_env):
        return True
    if key_file_env:
        configured_file = os.environ.get(key_file_env)
        if configured_file:
            path = Path(configured_file)
            return path.is_file() and os.access(path, os.R_OK)
    return False


def validate_qualification_route_runtime(
    *, route_id: str, logical_model_id: str, host: str, model_id: str
) -> None:
    """Reject runtime overrides that would invalidate an exact route proof.

    This is deliberately pure environment validation: the live qualification
    preflight calls it for the complete matrix before constructing even the
    first provider.  A noncanonical endpoint or model must not be able to
    self-identify as a configured route in a receipt.
    """
    _require_qualification_route(
        route_id=route_id,
        logical_model_id=logical_model_id,
        host=host,
        model_id=model_id,
    )
    if host == "google-ais":
        configured_base = os.environ.get(GEMMA_AIS_BASE_URL_ENV)
        if configured_base is not None and configured_base.rstrip("/") != (
            DEFAULT_GEMMA_AIS_BASE_URL.rstrip("/")
        ):
            raise ValueError("Qualification route has a noncanonical AIS base URL.")
    elif host == "openrouter":
        configured_base = os.environ.get(GEMMA_FALLBACK_BASE_URL_ENV)
        configured_model = os.environ.get(GEMMA_FALLBACK_MODEL_ENV)
        if configured_base is not None and configured_base.rstrip("/") != (
            DEFAULT_GEMMA_FALLBACK_BASE_URL.rstrip("/")
        ):
            raise ValueError("Qualification route has a noncanonical OpenRouter base URL.")
        if configured_model not in {None, DEFAULT_GEMMA_FALLBACK_MODEL}:
            raise ValueError("Qualification route has a noncanonical OpenRouter model.")
    elif host == "google-vertex":
        configured_base = os.environ.get(VERTEX_BASE_URL_ENV)
        if configured_base is None:
            raise ValueError("Qualification route requires a Vertex base URL.")
        _validate_vertex_base_url(configured_base)
    elif host == SUBSCRIPTION_HOST:
        # Executable presence is a credential-source question, not a canonicity
        # one: qualification_route_credential_present already probes it, and
        # preflight calls that separately through its own injectable seam.
        # Probing it here too made pure route validation depend on the host
        # filesystem, which broke every offline test that validates a route
        # without intending to reach a provider.
        configured_model = os.environ.get(SUBSCRIPTION_MODEL_ENV)
        if configured_model is not None and configured_model != model_id:
            raise ValueError("Qualification route has a noncanonical subscription model.")
    else:  # _require_qualification_route keeps this defensive branch unreachable.
        raise ValueError("Qualification route has an unknown provider host.")


def make_qualification_pinned_generator(
    *, route_id: str, logical_model_id: str, host: str, model_id: str
) -> AISGeneratorPort | SubscriptionGeneratorPort:
    """Construct exactly one provider port for a real qualification cell.

    Unlike the normal job factory, this never wraps a port in failover or
    round-robin selection.  Any provider failure remains a failure of this
    exact cell and cannot become evidence for a sibling route.
    """
    validate_qualification_route_runtime(
        route_id=route_id,
        logical_model_id=logical_model_id,
        host=host,
        model_id=model_id,
    )
    if host == SUBSCRIPTION_HOST:
        port: AISGeneratorPort | SubscriptionGeneratorPort = SubscriptionGeneratorPort(
            executable=os.environ.get(SUBSCRIPTION_EXECUTABLE_ENV, DEFAULT_SUBSCRIPTION_EXECUTABLE),
            model=model_id,
            timeout_s=GEMMA_TIMEOUT_S,
        )
    elif host == "google-vertex":
        port = AISGeneratorPort(
            api_key_env=VERTEX_API_KEY_ENV,
            api_key_file_env=VERTEX_API_KEY_FILE_ENV,
            model=model_id,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=VertexGenerateContentTransport(
                base_url=_validate_vertex_base_url(os.environ[VERTEX_BASE_URL_ENV]),
                max_attempts=1,
            ),
        )
    elif route_id == "gemma-ais":
        port = AISGeneratorPort(
            api_key_env=AIS_API_KEY_ENV,
            api_key_file_env=AIS_API_KEY_FILE_ENV,
            model=GEMMA_MODEL,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(
                base_url=DEFAULT_GEMMA_AIS_BASE_URL,
                host="google-ais",
                max_attempts=1,
                retry_json_mode_on_400=False,
            ),
        )
    elif route_id == "gemma-openrouter":
        port = AISGeneratorPort(
            api_key_env=GEMMA_FALLBACK_API_KEY_ENV,
            api_key_file_env=GEMMA_FALLBACK_API_KEY_FILE_ENV,
            model=DEFAULT_GEMMA_FALLBACK_MODEL,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(
                base_url=DEFAULT_GEMMA_FALLBACK_BASE_URL,
                host="openrouter",
                strip_model_prefix=False,
                max_attempts=1,
                retry_json_mode_on_400=False,
            ),
        )
    else:
        port = AISGeneratorPort(
            api_key_env=AIS_API_KEY_ENV,
            api_key_file_env=AIS_API_KEY_FILE_ENV,
            model=model_id,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(
                base_url=DEFAULT_GEMMA_AIS_BASE_URL,
                host="google-ais",
                max_attempts=1,
            ),
        )
    observed_host = (
        port.host
        if isinstance(port, SubscriptionGeneratorPort)
        else getattr(port._transport, "host", "")
    )
    observed_model = port.model if isinstance(port, SubscriptionGeneratorPort) else port._model
    if observed_host != host or observed_model != model_id:
        raise ValueError("Qualification route does not match current runtime configuration.")
    return port


def _with_failover(
    primary: AISGeneratorPort, fallback: AISGeneratorPort, *, fallback_label: str = "gemma"
) -> FailoverGeneratorPort:
    """Promote one configured port to primary without resolving either secret."""
    return FailoverGeneratorPort(
        fallback=fallback,
        api_key=primary._api_key,
        api_key_env=primary._api_key_env,
        api_key_file_env=primary._api_key_file_env,
        model=primary._model,
        timeout_s=primary._timeout_s,
        transport=primary._transport,
        fallback_label=fallback_label,
    )


class RoundRobinGeneratorSelector:
    """Thread-safe per-bake primary selection across configured provider pairs."""

    def __init__(self, generators: Mapping[str, Callable[[str], str]]) -> None:
        if not generators:
            raise ValueError("At least one bake generator is required.")
        self._generators = dict(generators)
        self._names = tuple(self._generators)
        self._next = 0
        self._lock = threading.Lock()

    def for_bake(self) -> Callable[[str], str]:
        with self._lock:
            name = self._names[self._next % len(self._names)]
            self._next += 1
        return self._generators[name]

    def __call__(self, prompt: str) -> str:
        """Compatibility call path; EngineLessonBaker uses ``for_bake`` instead."""
        return self.for_bake()(prompt)


ALLOWED_MODELS = {
    "google-ais/gemma-4-31b-it",
    "google-ais/gemma-4-26b-a4b-it",
    "google-ais/gemini-3.6-flash",
    "google-ais/gemini-3.1-pro-preview",
    "deepseek/deepseek-v4-pro",
}


def validate_and_get_model() -> str:
    """Validate HRAMATKA_GEN_MODEL environment variable and return the selected model.

    Raises ValueError for unknown models, paid-model authorization issues, or deepseek key absence.
    """
    model = os.environ.get("HRAMATKA_GEN_MODEL", "google-ais/gemma-4-31b-it")
    if not model:
        model = "google-ais/gemma-4-31b-it"
    if model not in ALLOWED_MODELS:
        raise ValueError(
            f"Invalid HRAMATKA_GEN_MODEL {model!r}. "
            f"Allowed values are: {', '.join(sorted(ALLOWED_MODELS))}"
        )
    if model == "google-ais/gemini-3.1-pro-preview":
        if os.environ.get("HRAMATKA_PAID_MODEL_OK") != "1":
            raise ValueError(
                "Paid model 'google-ais/gemini-3.1-pro-preview' selected, "
                "but HRAMATKA_PAID_MODEL_OK=1 is not set. Gemini 3.1 Pro is "
                "a paid model and incurs API charges."
            )
    elif model == "deepseek/deepseek-v4-pro":
        if not os.environ.get("HRAMATKA_DEEPSEEK_API_KEY"):
            raise ValueError(
                "DeepSeek model 'deepseek/deepseek-v4-pro' selected, "
                "but HRAMATKA_DEEPSEEK_API_KEY is missing/empty."
            )
    return model


# Run validation at import time to fail-closed during startup
_ACTIVE_STARTUP_MODEL = validate_and_get_model()


def _build_generator_port(model_id: str) -> AISGeneratorPort:
    """Construct provider-native generator port for the selected model ID."""
    if model_id == "google-ais/gemma-4-31b-it":
        ais, openrouter, _ = _gemma_routes()
        return _with_failover(ais, openrouter)

    elif model_id == "google-ais/gemma-4-26b-a4b-it":
        # Create routes with the specific model ID and fallback
        ais_base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
        openrouter_base = os.environ.get(
            GEMMA_FALLBACK_BASE_URL_ENV, DEFAULT_GEMMA_FALLBACK_BASE_URL
        )
        fallback_model = os.environ.get(GEMMA_FALLBACK_MODEL_ENV, "google/gemma-4-26b-a4b-it")
        ais = AISGeneratorPort(
            api_key_env=AIS_API_KEY_ENV,
            api_key_file_env=AIS_API_KEY_FILE_ENV,
            model="google-ais/gemma-4-26b-a4b-it",
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(base_url=ais_base, host="google-ais"),
        )
        openrouter = AISGeneratorPort(
            api_key_env=GEMMA_FALLBACK_API_KEY_ENV,
            api_key_file_env=GEMMA_FALLBACK_API_KEY_FILE_ENV,
            model=fallback_model,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(
                base_url=openrouter_base,
                host="openrouter",
                strip_model_prefix=False,
            ),
        )
        # Existing OpenRouter fallback applies to gemma ids
        return _with_failover(ais, openrouter)

    elif model_id == "google-ais/gemini-3.6-flash":
        ais, vertex = _gemini_routes(
            ais_model="google-ais/gemini-3.6-flash",
            vertex_model="gemini-3.6-flash",
        )
        return _with_failover(ais, vertex, fallback_label="gemini")

    elif model_id == "google-ais/gemini-3.1-pro-preview":
        ais, vertex = _gemini_routes(
            ais_model="google-ais/gemini-3.1-pro-preview",
            vertex_model="gemini-3.1-pro-preview",
        )
        return _with_failover(ais, vertex, fallback_label="gemini")

    elif model_id == "deepseek/deepseek-v4-pro":
        # DeepSeek gets NO fallback/failover to OpenRouter
        base = os.environ.get(DEEPSEEK_BASE_URL_ENV, DEFAULT_DEEPSEEK_BASE_URL)
        return AISGeneratorPort(
            api_key_env=DEEPSEEK_API_KEY_ENV,
            model="deepseek/deepseek-v4-pro",
            transport=HttpChatTransport(base_url=base, host="deepseek"),
        )
    else:
        raise ValueError(f"Unsupported model ID: {model_id}")


def make_bake_generator(
    provider_names: tuple[str, ...] | list[str] | None = None,
) -> RoundRobinGeneratorSelector:
    """Create load-balanced primary routes, each with the opposite failover host."""
    active_model = validate_and_get_model()
    is_gemma = active_model in ("google-ais/gemma-4-31b-it", "google-ais/gemma-4-26b-a4b-it")
    if is_gemma:
        allowed = _get_bake_providers()
        names = tuple(provider_names or allowed)
        inert = {SUBSCRIPTION_PROVIDER}
        unknown = sorted(set(names) - set(allowed) - inert)
        selected = tuple(name for name in names if name in allowed)
        if not selected or unknown:
            raise ValueError(f"Bake providers must be one or more of {', '.join(allowed)}.")

        if active_model == "google-ais/gemma-4-26b-a4b-it":
            ais_base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
            openrouter_base = os.environ.get(
                GEMMA_FALLBACK_BASE_URL_ENV, DEFAULT_GEMMA_FALLBACK_BASE_URL
            )
            fallback_model = os.environ.get(GEMMA_FALLBACK_MODEL_ENV, "google/gemma-4-26b-a4b-it")
            ais = AISGeneratorPort(
                api_key_env=AIS_API_KEY_ENV,
                api_key_file_env=AIS_API_KEY_FILE_ENV,
                model="google-ais/gemma-4-26b-a4b-it",
                timeout_s=GEMMA_TIMEOUT_S,
                transport=HttpChatTransport(base_url=ais_base, host="google-ais"),
            )
            openrouter = AISGeneratorPort(
                api_key_env=GEMMA_FALLBACK_API_KEY_ENV,
                api_key_file_env=GEMMA_FALLBACK_API_KEY_FILE_ENV,
                model=fallback_model,
                timeout_s=GEMMA_TIMEOUT_S,
                transport=HttpChatTransport(
                    base_url=openrouter_base,
                    host="openrouter",
                    strip_model_prefix=False,
                ),
            )
            deepinfra = None
            if DEEPINFRA_API_KEY_ENV in os.environ:
                deepinfra_base = os.environ.get(DEEPINFRA_BASE_URL_ENV, DEFAULT_DEEPINFRA_BASE_URL)
                deepinfra = AISGeneratorPort(
                    api_key_env=DEEPINFRA_API_KEY_ENV,
                    model=DEEPINFRA_MODEL,
                    timeout_s=GEMMA_TIMEOUT_S,
                    transport=HttpChatTransport(
                        base_url=deepinfra_base,
                        host="deepinfra",
                        strip_model_prefix=False,
                    ),
                )
        else:
            ais, openrouter, deepinfra = _gemma_routes()

        routes: dict[str, AISGeneratorPort] = {
            "google-ais": _with_failover(ais, openrouter),
            "openrouter": _with_failover(openrouter, ais),
        }
        if deepinfra is not None:
            routes["deepinfra"] = _with_failover(deepinfra, ais)
        return RoundRobinGeneratorSelector({name: routes[name] for name in dict.fromkeys(selected)})
    elif active_model == "google-ais/gemini-3.6-flash":
        names = tuple(provider_names or ("google-ais",))
        allowed = {"google-ais", SUBSCRIPTION_PROVIDER}
        inert = {"openrouter", "deepinfra"}
        unknown = sorted(set(names) - allowed - inert)
        selected = tuple(name for name in names if name in allowed)
        if not selected or unknown:
            raise ValueError(
                f"Gemini Flash bake providers must be one or more of {', '.join(sorted(allowed))}."
            )
        routes: dict[str, Callable[[str], str]] = {}
        if "google-ais" in selected:
            ais, vertex = _gemini_routes(
                ais_model=active_model,
                vertex_model="gemini-3.6-flash",
            )
            routes["google-ais"] = _with_failover(ais, vertex, fallback_label="gemini")
        if SUBSCRIPTION_PROVIDER in selected:
            routes[SUBSCRIPTION_PROVIDER] = SubscriptionGeneratorPort(
                executable=os.environ.get(
                    SUBSCRIPTION_EXECUTABLE_ENV, DEFAULT_SUBSCRIPTION_EXECUTABLE
                ),
                model=DEFAULT_SUBSCRIPTION_MODEL,
                timeout_s=GEMMA_TIMEOUT_S,
            )
        return RoundRobinGeneratorSelector(routes)
    else:
        gen = _build_generator_port(active_model)
        return RoundRobinGeneratorSelector({active_model: gen})


def make_logical_model_generator(
    logical_model_id: str,
    provider_names: tuple[str, ...] | list[str] | None = None,
    *,
    qualified_routes: Sequence[Any] | None = None,
) -> RoundRobinGeneratorSelector:
    """Build one job-scoped generator without consulting or mutating model env.

    ``logical_model_id`` is the durable teacher choice. Provider names and wire
    model IDs stay behind this boundary.  Qualification routes are exact: a
    shipped logical model neither constructs nor requires credentials for an
    unqualified sibling route.
    """
    route_catalog: dict[str, dict[str, tuple[str, str, str]]] = {
        "gemini-3.7-flash": {
            SUBSCRIPTION_PROVIDER: (
                "gemini-flash-subscription",
                SUBSCRIPTION_HOST,
                DEFAULT_SUBSCRIPTION_MODEL,
            ),
        },
        "gemini-3.6-flash": {
            "google-ais": (
                "gemini-flash-ais",
                "google-ais",
                "google-ais/gemini-3.6-flash",
            ),
            SUBSCRIPTION_PROVIDER: (
                "gemini-flash-subscription",
                SUBSCRIPTION_HOST,
                DEFAULT_SUBSCRIPTION_MODEL,
            ),
        },
        "gemini-3.1-pro": {
            "google-ais": (
                "gemini-pro-ais",
                "google-ais",
                "google-ais/gemini-3.1-pro-preview",
            ),
            SUBSCRIPTION_PROVIDER: (
                "gemini-pro-subscription",
                SUBSCRIPTION_HOST,
                "gemini-3.1-pro-high",
            ),
        },
        "gemma-4-31b": {
            "google-ais": ("gemma-ais", "google-ais", GEMMA_MODEL),
            "openrouter": (
                "gemma-openrouter",
                "openrouter",
                DEFAULT_GEMMA_FALLBACK_MODEL,
            ),
        },
    }
    available = route_catalog.get(logical_model_id)
    if available is None:
        raise ValueError(f"Unknown qualified logical model ID: {logical_model_id!r}")

    if provider_names is None:
        if qualified_routes is None:
            requested = tuple(available) if logical_model_id == "gemma-4-31b" else ("google-ais",)
        else:
            qualified_route_ids = {
                getattr(route, "id", getattr(route, "route_id", None)) for route in qualified_routes
            }
            requested = tuple(
                provider
                for provider, (route_id, _host, _model) in available.items()
                if route_id in qualified_route_ids
            )
    else:
        requested = tuple(dict.fromkeys(provider_names))
    known_providers = {"google-ais", "openrouter", "deepinfra", SUBSCRIPTION_PROVIDER}
    unknown = sorted(set(requested) - known_providers)
    if unknown:
        raise ValueError(
            f"Qualified provider routes must be one or more of {', '.join(sorted(available))}."
        )
    selected = tuple(provider for provider in requested if provider in available)
    if not selected:
        raise ValueError("A qualified provider route is not enabled for this deployment.")

    routes: dict[str, Callable[[str], str]] = {}
    actual_routes: dict[str, tuple[str, str]] = {}
    configured_hosts: set[str] = set()
    for provider in selected:
        route_id, host, wire_model = available[provider]
        if route_id == "gemini-pro-ais" and os.environ.get("HRAMATKA_PAID_MODEL_OK") != "1":
            raise ValueError("Gemini 3.1 Pro AIS routing requires HRAMATKA_PAID_MODEL_OK=1.")
        if host == SUBSCRIPTION_HOST:
            port: AISGeneratorPort | SubscriptionGeneratorPort = SubscriptionGeneratorPort(
                executable=os.environ.get(
                    SUBSCRIPTION_EXECUTABLE_ENV, DEFAULT_SUBSCRIPTION_EXECUTABLE
                ),
                model=os.environ.get(SUBSCRIPTION_MODEL_ENV, wire_model),
                timeout_s=GEMMA_TIMEOUT_S,
            )
        elif route_id == "gemma-openrouter":
            port = AISGeneratorPort(
                api_key_env=GEMMA_FALLBACK_API_KEY_ENV,
                api_key_file_env=GEMMA_FALLBACK_API_KEY_FILE_ENV,
                model=os.environ.get(GEMMA_FALLBACK_MODEL_ENV, DEFAULT_GEMMA_FALLBACK_MODEL),
                timeout_s=GEMMA_TIMEOUT_S,
                transport=HttpChatTransport(
                    base_url=os.environ.get(
                        GEMMA_FALLBACK_BASE_URL_ENV, DEFAULT_GEMMA_FALLBACK_BASE_URL
                    ),
                    host="openrouter",
                    strip_model_prefix=False,
                ),
            )
        elif route_id == "gemma-ais":
            port = AISGeneratorPort(
                api_key_env=AIS_API_KEY_ENV,
                api_key_file_env=AIS_API_KEY_FILE_ENV,
                model=GEMMA_MODEL,
                timeout_s=GEMMA_TIMEOUT_S,
                transport=HttpChatTransport(
                    base_url=os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL),
                    host="google-ais",
                ),
            )
        else:
            port = _gemini_ais_route(wire_model)
        routes[provider] = port
        actual_routes[route_id] = (
            (
                port.host
                if isinstance(port, SubscriptionGeneratorPort)
                else getattr(port._transport, "host", "")
            ),
            port.model if isinstance(port, SubscriptionGeneratorPort) else port._model,
        )
        configured_hosts.add(host)

    _require_exact_qualified_routes(
        qualified_routes,
        actual_routes,
        configured_hosts=configured_hosts,
    )
    for provider, port in routes.items():
        route_id, _catalog_host, _catalog_model = available[provider]
        host, model_id = actual_routes[route_id]
        port.bind_semantic_review_route(
            route_id=route_id,
            host=host,
            model_id=model_id,
        )
    if qualified_routes is not None:
        for port in routes.values():
            if not port.is_configured():
                raise ValueError("A qualified provider route has no configured credential source.")
    return RoundRobinGeneratorSelector(routes)


def _require_exact_qualified_routes(
    qualified_routes: Sequence[Any] | None,
    actual_routes: Mapping[str, tuple[str, str]],
    *,
    configured_hosts: set[str],
) -> None:
    """Bind runtime host/model construction to the receipt-pinned route set."""
    if qualified_routes is None:
        return
    expected = {
        route_id: (route.host, route.model_id)
        for route in qualified_routes
        if (route_id := getattr(route, "id", getattr(route, "route_id", None))) is not None
        and all(hasattr(route, field) for field in ("host", "model_id"))
    }
    if expected != dict(actual_routes):
        raise ValueError("Qualified provider routes do not match runtime routing.")
    required_hosts = {host for host, _model in actual_routes.values()}
    if not required_hosts <= configured_hosts:
        raise ValueError("A qualified provider route is not enabled for this deployment.")


def make_generator(name: str) -> AISGeneratorPort:
    """Name -> a configured generator (prompt -> raw text). The model bake-off
    swaps engines by name through this registry.
    """
    active_model = validate_and_get_model()
    is_gemma = active_model in ("google-ais/gemma-4-31b-it", "google-ais/gemma-4-26b-a4b-it")

    key = name.lower()
    legacy_gemma_names = {"gemma-ais", "gemma", "google-ais", "openrouter", "deepinfra"}
    if not is_gemma and key in legacy_gemma_names:
        raise ValueError(
            f"Cannot construct legacy gemma provider route {name!r} when "
            f"HRAMATKA_GEN_MODEL is set to non-gemma model {active_model!r}."
        )

    # If the requested name matches one of the legacy Gemma aliases,
    # resolve it to the active model selected by HRAMATKA_GEN_MODEL.
    if key in ("gemma-ais", "gemma", "google-ais"):
        return _build_generator_port(active_model)

    if key == SUBSCRIPTION_PROVIDER:
        if active_model != "google-ais/gemini-3.6-flash":
            raise ValueError(
                "The subscription route requires HRAMATKA_GEN_MODEL=google-ais/gemini-3.6-flash."
            )
        return SubscriptionGeneratorPort(
            executable=os.environ.get(SUBSCRIPTION_EXECUTABLE_ENV, DEFAULT_SUBSCRIPTION_EXECUTABLE),
            model=DEFAULT_SUBSCRIPTION_MODEL,
            timeout_s=GEMMA_TIMEOUT_S,
        )

    # If the requested name is one of the canonical 4 model IDs, build it directly.
    for allowed_model in ALLOWED_MODELS:
        if key == allowed_model.lower():
            if allowed_model == "google-ais/gemini-3.1-pro-preview":
                if os.environ.get("HRAMATKA_PAID_MODEL_OK") != "1":
                    raise ValueError(
                        "Paid model 'google-ais/gemini-3.1-pro-preview' selected, "
                        "but HRAMATKA_PAID_MODEL_OK=1 is not set. Gemini 3.1 Pro is "
                        "a paid model and incurs API charges."
                    )
            elif allowed_model == "deepseek/deepseek-v4-pro":
                if not os.environ.get("HRAMATKA_DEEPSEEK_API_KEY"):
                    raise ValueError(
                        "DeepSeek model 'deepseek/deepseek-v4-pro' selected, "
                        "but HRAMATKA_DEEPSEEK_API_KEY is missing/empty."
                    )
            return _build_generator_port(allowed_model)

    # Support other legacy names for backward compatibility (e.g. in tests)
    if key == "openrouter":
        ais, openrouter, deepinfra = _gemma_routes()
        return _with_failover(openrouter, ais)
    if key == "deepinfra":
        ais, openrouter, deepinfra = _gemma_routes()
        if deepinfra is not None:
            return _with_failover(deepinfra, ais)
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

    known = sorted(list(ALLOWED_MODELS))
    raise ValueError(f"unknown generator {name!r}; known: {', '.join(known)}")
