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
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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


class ProviderConcurrencyBudget:
    """One process-wide cap for all live provider HTTP requests."""

    def __init__(self, limit: int = 8) -> None:
        self._lock = threading.Lock()
        self._limit = limit
        self._semaphore = threading.BoundedSemaphore(limit)

    def configure(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("Provider concurrency must be positive.")
        with self._lock:
            self._limit = limit
            self._semaphore = threading.BoundedSemaphore(limit)

    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._lock:
            semaphore = self._semaphore
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()


_provider_concurrency_budget = ProviderConcurrencyBudget()


def configure_provider_concurrency(limit: int) -> None:
    """Configure the single process-wide provider request budget at startup."""
    _provider_concurrency_budget.configure(limit)


@contextmanager
def provider_call_slot() -> Iterator[None]:
    """Reserve one of the shared in-flight provider request slots."""
    with _provider_concurrency_budget.slot():
        yield


_STEP_ORDER = {
    "generation": 1,
    "gates": 2,
    "assembly": 3,
}


@dataclass
class _TelemetryState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    traces: list[dict] = field(default_factory=list)
    calls_done: int | None = None
    calls_planned: int | None = None
    step: str | None = None
    duration_fallback: dict[str, Any] | None = None


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
    duration_fallback: dict[str, Any] | None = None
    _state: _TelemetryState | None = field(default=None, repr=False, compare=False)
    _report_phase: bool = field(default=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._state is None:
            self._state = _TelemetryState(
                traces=self.traces,
                calls_done=self.calls_done,
                calls_planned=self.calls_planned,
                step=self.step,
                duration_fallback=self.duration_fallback,
            )
        else:
            self.traces = self._state.traces
            self.calls_done = self._state.calls_done
            self.calls_planned = self._state.calls_planned
            self.step = self._state.step
            self.duration_fallback = self._state.duration_fallback

    def fork(self, *, phase: int) -> TelemetryContext:
        """Make a phase-local context that shares safe aggregate telemetry."""
        return TelemetryContext(
            job_id=self.job_id,
            store=self.store,
            phases_total=self.phases_total,
            calls_planned=self.calls_planned,
            calls_done=self.calls_done,
            phase=phase,
            step=self._state.step if self._state else self.step,
            trace_dir=self.trace_dir,
            _state=self._state,
            _report_phase=False,
        )

    def update_progress_db(
        self,
        *,
        step: str | None = None,
        phase: int | None = None,
        calls_done: int | None = None,
        calls_planned: int | None = None,
    ) -> None:
        assert self._state is not None
        with self._state.lock:
            if step is not None:
                current_order = _STEP_ORDER.get(self._state.step or "", 0)
                new_order = _STEP_ORDER.get(step, 0)
                if new_order >= current_order:
                    self._state.step = step
            if phase is not None:
                self.phase = phase
            if calls_done is not None:
                if self._state.calls_done is None or calls_done > self._state.calls_done:
                    self._state.calls_done = calls_done
            if calls_planned is not None:
                if self._state.calls_planned is None or calls_planned > self._state.calls_planned:
                    self._state.calls_planned = calls_planned

            self.step = self._state.step
            self.calls_done = self._state.calls_done
            self.calls_planned = self._state.calls_planned
            progress_phase = self.phase if self._report_phase else 1
            progress_obj = {
                "phase": progress_phase,
                "phases_total": self.phases_total,
                "step": self.step,
                "calls_done": self.calls_done,
                "calls_planned": self.calls_planned,
            }
            if self._state.duration_fallback is not None:
                progress_obj["duration_fallback"] = self._state.duration_fallback
            snapshot = dict(progress_obj)

        if self.store is not None and self.job_id is not None:
            # Monotonic: never write a smaller calls_done than the shared state holds
            with self._state.lock:
                if (
                    snapshot["calls_done"] is not None
                    and self._state.calls_done is not None
                    and snapshot["calls_done"] < self._state.calls_done
                ):
                    return

            from datetime import UTC, datetime

            timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            snapshot["updated_at"] = timestamp
            try:
                self.store.update_progress(self.job_id, snapshot)
            except Exception as exc:
                log.warning("Failed to update progress in DB for job %s: %s", self.job_id, exc)

    def increase_calls_planned(self) -> None:
        """Atomically account for one dependent regeneration request."""
        assert self._state is not None
        with self._state.lock:
            if self._state.calls_planned is not None:
                self._state.calls_planned += 1
            self.calls_planned = self._state.calls_planned

    def record_provider_call(self, trace_entry: dict) -> None:
        """Atomically keep one trace and progress increment from a phase thread."""
        assert self._state is not None
        with self._state.lock:
            self._state.traces.append(trace_entry)
            self.traces = self._state.traces
            if self._state.calls_done is not None:
                self._state.calls_done += 1
            self.calls_done = self._state.calls_done
            self.save_traces()
            calls_done = self.calls_done
        if calls_done is not None:
            self.update_progress_db(calls_done=calls_done)

    def record_event(self, trace_entry: dict) -> None:
        """Persist a safe non-provider trace event without changing call progress."""
        assert self._state is not None
        with self._state.lock:
            self._state.traces.append(trace_entry)
            self.traces = self._state.traces
            if trace_entry.get("event") == "duration_fallback":
                self._state.duration_fallback = {
                    "requested_duration_kind": trace_entry.get("requested_duration_kind"),
                    "resolved_duration": trace_entry.get("resolved_duration"),
                }
            self.save_traces()
        self.update_progress_db()

    def save_traces(self) -> None:
        if self.trace_dir:
            try:
                assert self._state is not None
                with self._state.lock:
                    trace_file = self.trace_dir / "trace.json"
                    import json

                    trace_file.write_text(
                        json.dumps(self._state.traces, ensure_ascii=False, indent=2),
                        encoding="utf-8",
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
_BAKE_PROVIDERS = ("google-ais", "openrouter")

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

    def __call__(self, prompt: str, *, api_key: str, model: str, timeout_s: int) -> str:
        import time

        import httpx

        url = self.base_url.rstrip("/") + "/chat/completions"
        # Canonical ids carry OUR routing prefix ("google-ais/gemma-4-31b-it");
        # the provider API knows only the bare model id. Sending the prefixed id
        # returns HTTP 404 (bake-off 2026-07-10, all gemma cells). Strip at the
        # wire; keep the canonical id in fingerprints/meta.
        wire_model = model.split("/", 1)[1] if self.strip_model_prefix and "/" in model else model

        temp_env = os.environ.get("HRAMATKA_GEN_TEMPERATURE")
        temperature = 0.2
        if temp_env is not None:
            try:
                temperature = float(temp_env)
            except ValueError:
                pass

        # #171: constrained decoding remains an explicit compatibility probe,
        # never an ambient transport default.  The engineered prompt carries
        # its own complete JSON contract and validates citations locally.
        json_mode_enabled = os.environ.get("HRAMATKA_GEN_JSON_MODE") == "1"

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

                    if response.status_code == 400 and "response_format" in payload:
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
                except httpx.HTTPError as exc:  # connect/transport error — not retriable
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
                    "attempts": attempts,
                    "http_status_class": status_class,
                    "phase": phase,
                    "activity_type": type_slug,
                    "started_at_ms": started_at_ms,
                    "ended_at_ms": ended_at_ms,
                }
                ctx.record_provider_call(trace_entry)


class FailoverGeneratorPort(AISGeneratorPort):
    """One Gemma primary with the other provider as outage-only fallback."""

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
            fallback_host = getattr(self._fallback._transport, "host", "fallback")
            log.warning("gemma fallback engaged host=%s", fallback_host)
            try:
                return self._fallback(prompt)
            except GeneratorUnavailable as fallback_error:
                # Keep the existing typed boundary and avoid surfacing either
                # provider's detail in durable job state.
                raise GeneratorUnavailable("provider generation failed") from fallback_error


def _gemma_routes() -> tuple[AISGeneratorPort, AISGeneratorPort, AISGeneratorPort | None]:
    """Construct provider-native Gemma ports without performing I/O."""
    ais_base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
    openrouter_base = os.environ.get(GEMMA_FALLBACK_BASE_URL_ENV, DEFAULT_GEMMA_FALLBACK_BASE_URL)
    openrouter_model = os.environ.get(GEMMA_FALLBACK_MODEL_ENV, DEFAULT_GEMMA_FALLBACK_MODEL)
    ais = AISGeneratorPort(
        api_key_env=AIS_API_KEY_ENV,
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


def _with_failover(primary: AISGeneratorPort, fallback: AISGeneratorPort) -> FailoverGeneratorPort:
    """Promote one configured port to primary without resolving either secret."""
    return FailoverGeneratorPort(
        fallback=fallback,
        api_key=primary._api_key,
        api_key_env=primary._api_key_env,
        api_key_file_env=primary._api_key_file_env,
        model=primary._model,
        timeout_s=primary._timeout_s,
        transport=primary._transport,
    )


class RoundRobinGeneratorSelector:
    """Thread-safe per-bake primary selection across configured provider pairs."""

    def __init__(self, generators: Mapping[str, AISGeneratorPort]) -> None:
        if not generators:
            raise ValueError("At least one bake generator is required.")
        self._generators = dict(generators)
        self._names = tuple(self._generators)
        self._next = 0
        self._lock = threading.Lock()

    def for_bake(self) -> AISGeneratorPort:
        with self._lock:
            name = self._names[self._next % len(self._names)]
            self._next += 1
        return self._generators[name]

    def __call__(self, prompt: str) -> str:
        """Compatibility call path; EngineLessonBaker uses ``for_bake`` instead."""
        return self.for_bake()(prompt)


def make_bake_generator(
    provider_names: tuple[str, ...] | list[str] | None = None,
) -> RoundRobinGeneratorSelector:
    """Create load-balanced primary routes, each with the opposite failover host."""
    allowed = _get_bake_providers()
    names = tuple(provider_names or allowed)
    unknown = sorted(set(names) - set(allowed))
    if not names or unknown:
        raise ValueError(f"Bake providers must be one or more of {', '.join(allowed)}.")
    ais, openrouter, deepinfra = _gemma_routes()
    routes: dict[str, AISGeneratorPort] = {
        "google-ais": _with_failover(ais, openrouter),
        "openrouter": _with_failover(openrouter, ais),
    }
    if deepinfra is not None:
        routes["deepinfra"] = _with_failover(deepinfra, ais)
    return RoundRobinGeneratorSelector({name: routes[name] for name in dict.fromkeys(names)})


def make_generator(name: str) -> AISGeneratorPort:
    """Name -> a configured generator (prompt -> raw text). The model bake-off
    swaps engines by name through this registry.

      "gemma-ais" / "gemma" / "google-ais" -> AIS-primary Gemma, OpenRouter fallback
      "openrouter"                           -> OpenRouter-primary Gemma, AIS fallback
      "deepinfra"                            -> DeepInfra-primary Gemma, AIS fallback
      "deepseek"                            -> deepseek-chat, HRAMATKA_DEEPSEEK_API_KEY + _BASE_URL
      "deepseek-v4-flash" / "deepseek-v4-pro" -> explicit V4 tier, same DeepSeek route
    """
    key = name.lower()
    if key in ("gemma-ais", "gemma", "google-ais"):
        ais, openrouter, deepinfra = _gemma_routes()
        return _with_failover(ais, openrouter)
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

    known = ["gemma-ais", "openrouter", "deepseek", "deepseek-v4-flash", "deepseek-v4-pro"]
    if "DEEPINFRA_API_KEY" in os.environ:
        known.append("deepinfra")
    raise ValueError(f"unknown generator {name!r}; known: {', '.join(known)}")
