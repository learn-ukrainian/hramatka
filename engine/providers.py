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
import statistics
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .transport import (
    AIS_API_KEY_ENV,
    GEMMA_MODEL,
    GEMMA_TIMEOUT_S,
    AISGeneratorPort,
    GeneratorUnavailable,
)

log = logging.getLogger(__name__)

# JSON output is a transport contract for current Gemini seats, not a prompt
# convention. The #171 Gemma measurements remain valid, so Gemma is excluded
# by model identity rather than through a stale host-wide deny.
_GEMINI_JSON_MODE_MODELS = frozenset({"gemini-3.6-flash", "gemini-3.1-pro-preview"})

# Latency watchdog: fire when one call exceeds multiplier × rolling median of
# prior samples in the same TelemetryContext (hang-class visibility for #165).
LATENCY_WATCHDOG_MULTIPLIER = 3.0
LATENCY_WATCHDOG_MIN_SAMPLES = 3
LATENCY_WATCHDOG_WINDOW = 100


def evaluate_latency_watchdog(
    duration_ms: float,
    prior_samples_ms: Sequence[float],
    *,
    multiplier: float = LATENCY_WATCHDOG_MULTIPLIER,
    min_samples: int = LATENCY_WATCHDOG_MIN_SAMPLES,
) -> dict[str, Any] | None:
    """Return a latency_watchdog event if duration exceeds multiplier × median.

    Pure function (no I/O, no mutation) so unit tests cover the trigger math
    without a live provider call. Requires ``min_samples`` prior observations;
    the current duration is NOT included in the median.
    """
    if min_samples < 1 or multiplier <= 0:
        return None
    if len(prior_samples_ms) < min_samples:
        return None
    med = float(statistics.median(prior_samples_ms))
    if med <= 0:
        return None
    threshold = multiplier * med
    if duration_ms <= threshold:
        return None
    return {
        "event": "latency_watchdog",
        "duration_ms": duration_ms,
        "rolling_median_ms": med,
        "threshold_ms": threshold,
        "ratio": duration_ms / med,
        "sample_count": len(prior_samples_ms),
    }


def json_mode_enabled_for_model(model: str) -> bool:
    """Whether a model gets API-enforced JSON output for this run.

    Gemini seats default to constrained JSON on every supported transport.
    Set ``HRAMATKA_GEN_JSON_MODE=0`` to disable it for one run. Gemma remains
    denied because #171 measured its JSON mode as unsafe; its prompt contract
    is intentionally not promoted to a transport contract.
    """
    if os.environ.get("HRAMATKA_GEN_JSON_MODE") == "0":
        return False
    return model.split("/", 1)[-1] in _GEMINI_JSON_MODE_MODELS


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


def _content_free_qualification_route_trace(value: object) -> dict[str, Any] | None:
    """Allowlist the optional qualification route trace before durable storage.

    Qualification receipts may retain only selected routing metadata.  This
    rejects arbitrary provider payloads rather than letting a caller tunnel
    prompt, response, or anchor content through the extensible progress blob.
    """
    if not isinstance(value, dict) or set(value) != {
        "expected_route",
        "mode",
        "observed_route",
        "phase",
    }:
        return None
    if (
        value.get("mode") not in {"initial", "repair"}
        or type(value.get("phase")) is not int
        or value.get("phase") not in {1, 2, 3}
    ):
        return None
    for key in ("expected_route", "observed_route"):
        route = value.get(key)
        if (
            not isinstance(route, dict)
            or set(route) != {"host", "model_id", "route_id"}
            or not all(isinstance(item, str) and item for item in route.values())
        ):
            return None
    return deepcopy(value)


def _content_free_qualification_density_trace(value: object) -> dict[str, Any] | None:
    """Allowlist aggregate density evidence for a qualification diagnostic.

    The record deliberately contains only counts and fixed error codes.  It
    cannot carry anchor text, prompts, activities, provider responses, or gate
    details into a durable job row.
    """
    expected = {
        "density_error_codes",
        "gate_outcomes_by_phase",
        "phase_density",
        "repair_invocations",
        "stage",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return None
    if value.get("stage") not in {"initial", "repair"}:
        return None
    if type(value.get("repair_invocations")) is not int or value["repair_invocations"] < 0:
        return None
    errors = value.get("density_error_codes")
    if not isinstance(errors, list) or not all(isinstance(error, str) for error in errors):
        return None
    phase_density = value.get("phase_density")
    gate_outcomes = value.get("gate_outcomes_by_phase")
    if not isinstance(phase_density, dict) or not isinstance(gate_outcomes, dict):
        return None
    if set(phase_density) != {"1", "2", "3"} or set(gate_outcomes) != {"1", "2", "3"}:
        return None
    for phase in ("1", "2", "3"):
        density = phase_density[phase]
        outcomes = gate_outcomes[phase]
        if (
            not isinstance(density, dict)
            or set(density) != {"response_units", "visible_blocks"}
            or not all(type(count) is int and count >= 0 for count in density.values())
            or not isinstance(outcomes, dict)
            or set(outcomes) != {"dropped", "generated", "ready", "review", "requested"}
            or not all(type(count) is int and count >= 0 for count in outcomes.values())
            or outcomes["generated"] != outcomes["ready"] + outcomes["review"] + outcomes["dropped"]
        ):
            return None
    return deepcopy(value)


def _content_free_qualification_repair_trace(value: object) -> dict[str, Any] | None:
    """Allowlist one count-only repair attempt for the diagnostic receipt."""
    expected = {
        "gate_drops",
        "outcome",
        "phase",
        "response_units_after",
        "response_units_before",
        "round",
        "visible_blocks_after",
        "visible_blocks_before",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return None
    if value.get("outcome") not in {"amended", "provider_failure"}:
        return None
    if type(value.get("phase")) is not int or value["phase"] not in {1, 2, 3}:
        return None
    if type(value.get("round")) is not int or value["round"] < 1:
        return None
    if not all(
        type(value[field]) is int and value[field] >= 0
        for field in expected - {"outcome", "phase", "round"}
    ):
        return None
    return deepcopy(value)


def _content_free_qualification_slot_trace(value: object) -> dict[str, Any] | None:
    """Allowlist one content-free v3 slot evaluation for qualification.

    The durable proof needs each scheduled slot's count and disposition, but
    must never retain its prompt, learner activity, source text, or answer.
    Final receipt validation remains in ``qualification.receipts``; this
    boundary only ensures the generic progress blob cannot carry extra data.
    """
    expected = {
        "disposition",
        "floor_met",
        "phase",
        "repair_rounds",
        "replacement_used",
        "slot_id",
        "type",
        "unassigned_errors_count",
        "units",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return None
    if (
        not isinstance(value["slot_id"], str)
        or not value["slot_id"]
        or type(value["phase"]) is not int
        or value["phase"] not in {1, 2, 3}
        or not isinstance(value["type"], str)
        or not value["type"]
        or value["disposition"] not in {"ready", "tray", "density_shortfall", "dropped"}
        or type(value["units"]) is not int
        or value["units"] < 0
        or type(value["floor_met"]) is not bool
        or type(value["repair_rounds"]) is not int
        or value["repair_rounds"] not in {0, 1, 2}
        or type(value["replacement_used"]) is not bool
        or type(value["unassigned_errors_count"]) is not int
        or value["unassigned_errors_count"] < 0
    ):
        return None
    return deepcopy(value)


@dataclass
class _TelemetryState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    traces: list[dict] = field(default_factory=list)
    calls_done: int | None = None
    calls_planned: int | None = None
    step: str | None = None
    duration_fallback: dict[str, Any] | None = None
    latency_samples_ms: list[float] = field(default_factory=list)
    repair: dict[str, Any] | None = None
    generation_path: str | None = None
    fallback_reason: str | None = None
    latency_watchdogs: list[dict[str, Any]] = field(default_factory=list)
    logical_model_id: str | None = None
    provider_routes: list[dict[str, str]] = field(default_factory=list)
    qualification_route_traces: list[dict[str, Any]] = field(default_factory=list)
    qualification_density_traces: list[dict[str, Any]] = field(default_factory=list)
    qualification_repair_traces: list[dict[str, Any]] = field(default_factory=list)
    qualification_slot_telemetry: list[dict[str, Any]] = field(default_factory=list)
    teacher_ready_density: dict[str, Any] | None = None


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
    repair: dict[str, Any] | None = None
    generation_path: str | None = None
    fallback_reason: str | None = None
    latency_watchdogs: list[dict[str, Any]] = field(default_factory=list)
    logical_model_id: str | None = None
    provider_routes: list[dict[str, str]] = field(default_factory=list)
    qualification_route_traces: list[dict[str, Any]] = field(default_factory=list)
    qualification_density_traces: list[dict[str, Any]] = field(default_factory=list)
    qualification_repair_traces: list[dict[str, Any]] = field(default_factory=list)
    qualification_slot_telemetry: list[dict[str, Any]] = field(default_factory=list)
    teacher_ready_density: dict[str, Any] | None = None
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
                repair=self.repair,
                generation_path=self.generation_path,
                fallback_reason=self.fallback_reason,
                latency_watchdogs=self.latency_watchdogs,
                logical_model_id=self.logical_model_id,
                provider_routes=self.provider_routes,
                qualification_route_traces=self.qualification_route_traces,
                qualification_density_traces=self.qualification_density_traces,
                qualification_repair_traces=self.qualification_repair_traces,
                qualification_slot_telemetry=self.qualification_slot_telemetry,
                teacher_ready_density=self.teacher_ready_density,
            )
        else:
            with self._state.lock:
                self._sync_from_shared_state()

    def _sync_from_shared_state(self) -> None:
        """Refresh this context's scalar mirrors from the locked shared state."""
        assert self._state is not None
        self.traces = self._state.traces
        self.calls_done = self._state.calls_done
        self.calls_planned = self._state.calls_planned
        self.step = self._state.step
        self.duration_fallback = self._state.duration_fallback
        self.repair = self._state.repair
        self.generation_path = self._state.generation_path
        self.fallback_reason = self._state.fallback_reason
        self.latency_watchdogs = self._state.latency_watchdogs
        self.logical_model_id = self._state.logical_model_id
        self.provider_routes = self._state.provider_routes
        self.qualification_route_traces = self._state.qualification_route_traces
        self.qualification_density_traces = self._state.qualification_density_traces
        self.qualification_repair_traces = self._state.qualification_repair_traces
        self.qualification_slot_telemetry = self._state.qualification_slot_telemetry
        self.teacher_ready_density = self._state.teacher_ready_density

    def fork(self, *, phase: int) -> TelemetryContext:
        """Make a phase-local context that shares safe aggregate telemetry."""
        return TelemetryContext(
            job_id=self.job_id,
            store=self.store,
            phases_total=self.phases_total,
            phase=phase,
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

            self._sync_from_shared_state()
            progress_phase = self.phase if self._report_phase else 1
            progress_obj = {
                "phase": progress_phase,
                "phases_total": self.phases_total,
                "step": self.step,
                "calls_done": self._state.calls_done,
                "calls_planned": self._state.calls_planned,
            }
            if self._state.duration_fallback is not None:
                progress_obj["duration_fallback"] = deepcopy(self._state.duration_fallback)
            if self._state.repair is not None:
                progress_obj["repair"] = deepcopy(self._state.repair)
            if self._state.generation_path is not None:
                progress_obj["generation_path"] = self._state.generation_path
            if self._state.fallback_reason is not None:
                progress_obj["fallback_reason"] = self._state.fallback_reason
            if self._state.latency_watchdogs:
                progress_obj["latency_watchdogs"] = list(self._state.latency_watchdogs)
            if self._state.logical_model_id is not None:
                progress_obj["logical_model_id"] = self._state.logical_model_id
            if self._state.provider_routes:
                progress_obj["provider_routes"] = deepcopy(self._state.provider_routes)
            if self._state.qualification_route_traces:
                progress_obj["qualification_route_traces"] = deepcopy(
                    self._state.qualification_route_traces
                )
            if self._state.qualification_density_traces:
                progress_obj["qualification_density_traces"] = deepcopy(
                    self._state.qualification_density_traces
                )
            if self._state.qualification_repair_traces:
                progress_obj["qualification_repair_traces"] = deepcopy(
                    self._state.qualification_repair_traces
                )
            if self._state.qualification_slot_telemetry:
                progress_obj["qualification_slot_telemetry"] = deepcopy(
                    self._state.qualification_slot_telemetry
                )
            if self._state.teacher_ready_density is not None:
                progress_obj["teacher_ready_density"] = deepcopy(self._state.teacher_ready_density)
            snapshot = dict(progress_obj)

            if self.store is not None and self.job_id is not None:
                # Keep the durable write inside the shared lock.  This makes the
                # shared state the only authority and prevents an older fork
                # snapshot from overwriting either call counter after a newer
                # fork has advanced it.
                from datetime import UTC, datetime

                timestamp = (
                    datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                )
                snapshot["updated_at"] = timestamp
                try:
                    self.store.update_progress(self.job_id, snapshot)
                except Exception as exc:
                    log.warning("Failed to update progress in DB for job %s: %s", self.job_id, exc)

    def increase_calls_planned(self, count: int = 1) -> int | None:
        """Atomically account for dependent requests and return their shared total."""
        if count < 0:
            raise ValueError("Planned-call increment must be non-negative.")
        assert self._state is not None
        with self._state.lock:
            if self._state.calls_planned is not None:
                self._state.calls_planned += count
            self._sync_from_shared_state()
            return self.calls_planned

    def record_provider_call(self, trace_entry: dict) -> None:
        """Atomically keep one trace and progress increment from a phase thread.

        Also evaluates the #171 latency watchdog against the rolling median of
        prior provider-call durations in this bake's shared telemetry state.
        """
        assert self._state is not None
        with self._state.lock:
            duration_ms = trace_entry.get("duration_ms")
            if isinstance(duration_ms, (int, float)):
                prior = list(self._state.latency_samples_ms)
                watchdog = evaluate_latency_watchdog(float(duration_ms), prior)
                if watchdog is not None:
                    watchdog = {
                        **watchdog,
                        "host": trace_entry.get("host"),
                        "phase": trace_entry.get("phase"),
                        "activity_type": trace_entry.get("activity_type"),
                        "http_status_class": trace_entry.get("http_status_class"),
                    }
                    self._state.traces.append(watchdog)
                    self._state.latency_watchdogs.append(
                        {
                            "duration_ms": watchdog.get("duration_ms"),
                            "threshold_ms": watchdog.get("threshold_ms"),
                            "ratio": float(watchdog.get("ratio") or 0.0),
                            "host": watchdog.get("host"),
                            "phase": watchdog.get("phase"),
                        }
                    )
                    log.warning(
                        "latency_watchdog host=%s dur_ms=%s median_ms=%s ratio=%.2f",
                        watchdog.get("host"),
                        watchdog.get("duration_ms"),
                        watchdog.get("rolling_median_ms"),
                        float(watchdog.get("ratio") or 0.0),
                    )
                self._state.latency_samples_ms.append(float(duration_ms))
                if len(self._state.latency_samples_ms) > LATENCY_WATCHDOG_WINDOW:
                    self._state.latency_samples_ms = self._state.latency_samples_ms[
                        -LATENCY_WATCHDOG_WINDOW:
                    ]
            self._state.traces.append(trace_entry)
            host = trace_entry.get("host")
            model = trace_entry.get("model")
            if isinstance(host, str) and isinstance(model, str):
                route = {"host": host, "model": model}
                if route not in self._state.provider_routes:
                    self._state.provider_routes.append(route)
            qualification_trace = _content_free_qualification_route_trace(
                trace_entry.get("qualification_route_trace")
            )
            if qualification_trace is not None:
                self._state.qualification_route_traces.append(qualification_trace)
            if self._state.calls_done is not None:
                self._state.calls_done += 1
            self._sync_from_shared_state()
            self.save_traces()
            calls_done = self.calls_done
        if calls_done is not None:
            self.update_progress_db(calls_done=calls_done)

    def record_qualification_route_trace(self, trace_entry: dict) -> None:
        """Persist a validated route binding without counting another provider call.

        A transport records the actual HTTP attempt.  The qualification wrapper
        records the route identity separately so initial generation and every
        repair can be checked against the immutable expected cell.
        """
        binding = _content_free_qualification_route_trace(trace_entry)
        if binding is None:
            raise ValueError("Qualification route trace is not content-free or valid.")
        assert self._state is not None
        with self._state.lock:
            self._state.qualification_route_traces.append(binding)
            self._state.traces.append({"event": "qualification_route", **binding})
            self._sync_from_shared_state()
            self.save_traces()
        self.update_progress_db()

    def record_qualification_density_trace(self, trace_entry: dict) -> None:
        """Persist one validated count-only composition snapshot."""
        trace = _content_free_qualification_density_trace(trace_entry)
        if trace is None:
            raise ValueError("Qualification density trace is not content-free or valid.")
        assert self._state is not None
        with self._state.lock:
            self._state.qualification_density_traces.append(trace)
            self._state.traces.append({"event": "qualification_density", **trace})
            self._sync_from_shared_state()
            self.save_traces()
        self.update_progress_db()

    def record_qualification_repair_trace(self, trace_entry: dict) -> None:
        """Persist one validated count-only generic-repair invocation."""
        trace = _content_free_qualification_repair_trace(trace_entry)
        if trace is None:
            raise ValueError("Qualification repair trace is not content-free or valid.")
        assert self._state is not None
        with self._state.lock:
            self._state.qualification_repair_traces.append(trace)
            self._state.traces.append({"event": "qualification_repair", **trace})
            self._sync_from_shared_state()
            self.save_traces()
        self.update_progress_db()

    def record_qualification_slot_trace(self, trace_entry: dict) -> None:
        """Persist one allowlisted v3 slot receipt for a qualification cell."""
        trace = _content_free_qualification_slot_trace(trace_entry)
        if trace is None:
            raise ValueError("Qualification slot trace is not content-free or valid.")
        assert self._state is not None
        with self._state.lock:
            self._state.qualification_slot_telemetry.append(trace)
            self._state.traces.append({"event": "qualification_slot", **trace})
            self._sync_from_shared_state()
            self.save_traces()
        self.update_progress_db()

    def record_event(self, trace_entry: dict) -> None:
        """Persist a safe non-provider trace event without changing call progress."""
        assert self._state is not None
        with self._state.lock:
            self._state.traces.append(trace_entry)
            self.traces = self._state.traces
            event = trace_entry.get("event")
            if event == "duration_fallback":
                self._state.duration_fallback = {
                    "requested_duration_kind": trace_entry.get("requested_duration_kind"),
                    "resolved_duration": trace_entry.get("resolved_duration"),
                }
            elif event == "slot_repair_stopped":
                if self._state.repair is None:
                    self._state.repair = {"attempts": 0, "rounds": 0, "stop_reason": "unknown"}
                self._state.repair["stop_reason"] = trace_entry.get("reason", "unknown")
                self._state.repair["rounds"] = max(
                    self._state.repair["rounds"], trace_entry.get("round", 0)
                )
            elif event == "slot_repair_attempt":
                if self._state.repair is None:
                    self._state.repair = {"attempts": 0, "rounds": 0, "stop_reason": "unknown"}
                self._state.repair["attempts"] += 1
                self._state.repair["rounds"] = max(
                    self._state.repair["rounds"], trace_entry.get("round", 0)
                )
            elif event == "prompt_pack_enabled":
                self._state.generation_path = "pack"
            elif event == "prompt_pack_fallback":
                self._state.generation_path = "legacy_fallback"
                if "reason" in trace_entry:
                    self._state.fallback_reason = trace_entry["reason"]
            elif event == "teacher_ready_density":
                self._state.teacher_ready_density = {
                    key: deepcopy(trace_entry[key])
                    for key in (
                        "phase_counts",
                        "ready_phase_counts",
                        "tray_phase_counts",
                        "delivered_blocks",
                        "ready_blocks",
                        "tray_blocks",
                        "floor_blocks",
                        "ready_response_units",
                        "tray_response_units",
                        "response_units",
                        "disposition",
                    )
                    if key in trace_entry
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
        or parsed.path.rstrip("/")
        != f"/v1/projects/{parts[2]}/locations/global/publishers/google"
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

        temp_env = os.environ.get("HRAMATKA_GEN_TEMPERATURE")
        temperature = 0.2
        if temp_env is not None:
            try:
                temperature = float(temp_env)
            except ValueError:
                pass

        # Current Gemini seats enforce JSON at the API boundary, including AIS.
        # Gemma remains excluded by the #171 measurements. The per-run escape
        # hatch is HRAMATKA_GEN_JSON_MODE=0.
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

        temp_env = os.environ.get("HRAMATKA_GEN_TEMPERATURE")
        temperature = 0.2
        if temp_env is not None:
            try:
                temperature = float(temp_env)
            except ValueError:
                pass
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


def _gemini_routes(
    *, ais_model: str, vertex_model: str
) -> tuple[AISGeneratorPort, AISGeneratorPort]:
    """Construct AIS-primary and native-Vertex fallback ports without I/O."""
    ais_base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
    vertex_base = os.environ.get(VERTEX_BASE_URL_ENV, "")
    ais = AISGeneratorPort(
        api_key_env=AIS_API_KEY_ENV,
        model=ais_model,
        timeout_s=GEMMA_TIMEOUT_S,
        transport=HttpChatTransport(base_url=ais_base, host="google-ais"),
    )
    vertex = AISGeneratorPort(
        api_key_env=VERTEX_API_KEY_ENV,
        api_key_file_env=VERTEX_API_KEY_FILE_ENV,
        model=vertex_model,
        timeout_s=GEMMA_TIMEOUT_S,
        transport=VertexGenerateContentTransport(base_url=vertex_base),
    )
    return ais, vertex


_QUALIFICATION_ROUTE_SPECS: Mapping[str, tuple[str, str, str, str | None, str | None]] = {
    "gemini-flash-ais": (
        "gemini-3.6-flash",
        "google-ais",
        "google-ais/gemini-3.6-flash",
        AIS_API_KEY_ENV,
        None,
    ),
    "gemini-flash-vertex": (
        "gemini-3.6-flash",
        "google-vertex",
        "gemini-3.6-flash",
        VERTEX_API_KEY_ENV,
        VERTEX_API_KEY_FILE_ENV,
    ),
    "gemini-pro-ais": (
        "gemini-3.1-pro",
        "google-ais",
        "google-ais/gemini-3.1-pro-preview",
        AIS_API_KEY_ENV,
        None,
    ),
    "gemini-pro-vertex": (
        "gemini-3.1-pro",
        "google-vertex",
        "gemini-3.1-pro-preview",
        VERTEX_API_KEY_ENV,
        VERTEX_API_KEY_FILE_ENV,
    ),
    "gemma-ais": (
        "gemma-4-31b",
        "google-ais",
        GEMMA_MODEL,
        AIS_API_KEY_ENV,
        None,
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
    else:  # _require_qualification_route keeps this defensive branch unreachable.
        raise ValueError("Qualification route has an unknown provider host.")


def make_qualification_pinned_generator(
    *, route_id: str, logical_model_id: str, host: str, model_id: str
) -> AISGeneratorPort:
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
    if host == "google-vertex":
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
            model=model_id,
            timeout_s=GEMMA_TIMEOUT_S,
            transport=HttpChatTransport(
                base_url=DEFAULT_GEMMA_AIS_BASE_URL,
                host="google-ais",
                max_attempts=1,
            ),
        )
    observed_host = getattr(port._transport, "host", "")
    if observed_host != host or port._model != model_id:
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
        unknown = sorted(set(names) - set(allowed))
        if not names or unknown:
            raise ValueError(f"Bake providers must be one or more of {', '.join(allowed)}.")

        if active_model == "google-ais/gemma-4-26b-a4b-it":
            ais_base = os.environ.get(GEMMA_AIS_BASE_URL_ENV, DEFAULT_GEMMA_AIS_BASE_URL)
            openrouter_base = os.environ.get(
                GEMMA_FALLBACK_BASE_URL_ENV, DEFAULT_GEMMA_FALLBACK_BASE_URL
            )
            fallback_model = os.environ.get(GEMMA_FALLBACK_MODEL_ENV, "google/gemma-4-26b-a4b-it")
            ais = AISGeneratorPort(
                api_key_env=AIS_API_KEY_ENV,
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
        return RoundRobinGeneratorSelector({name: routes[name] for name in dict.fromkeys(names)})
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
    model IDs stay behind this boundary; a Gemma job may use either qualified
    route, while Gemini keeps AIS primary with an outage-only Vertex fallback.
    """
    if logical_model_id == "gemma-4-31b":
        ais, openrouter, _ = _gemma_routes()
        routes: dict[str, AISGeneratorPort] = {
            "google-ais": _with_failover(ais, openrouter),
            "openrouter": _with_failover(openrouter, ais),
        }
        requested = tuple(provider_names or routes)
        selected = tuple(name for name in dict.fromkeys(requested) if name in routes)
        if not selected:
            raise ValueError("Gemma 4 31B requires a qualified AIS or OpenRouter route.")
        _require_exact_qualified_routes(
            qualified_routes,
            {
                "gemma-ais": (getattr(ais._transport, "host", ""), ais._model),
                "gemma-openrouter": (
                    getattr(openrouter._transport, "host", ""),
                    openrouter._model,
                ),
            },
            configured_hosts=set(selected),
        )
        if qualified_routes is not None and (
            not ais.is_configured() or not openrouter.is_configured()
        ):
            raise ValueError("A qualified provider route has no configured credential source.")
        return RoundRobinGeneratorSelector({name: routes[name] for name in selected})

    if logical_model_id == "gemini-3.6-flash":
        model_id = "google-ais/gemini-3.6-flash"
        vertex_model_id = "gemini-3.6-flash"
        ais_route_id = "gemini-flash-ais"
        vertex_route_id = "gemini-flash-vertex"
    elif logical_model_id == "gemini-3.1-pro":
        if os.environ.get("HRAMATKA_PAID_MODEL_OK") != "1":
            raise ValueError("Gemini 3.1 Pro routing requires HRAMATKA_PAID_MODEL_OK=1.")
        model_id = "google-ais/gemini-3.1-pro-preview"
        vertex_model_id = "gemini-3.1-pro-preview"
        ais_route_id = "gemini-pro-ais"
        vertex_route_id = "gemini-pro-vertex"
    else:
        raise ValueError(f"Unknown qualified logical model ID: {logical_model_id!r}")
    ais, vertex = _gemini_routes(ais_model=model_id, vertex_model=vertex_model_id)
    primary = _with_failover(ais, vertex, fallback_label="gemini")
    configured_hosts = set(provider_names or ("google-ais",))
    # Vertex is an automatic outage fallback, not an independently selected
    # bake primary. Its configured route remains receipt-bound nevertheless.
    configured_hosts.add("google-vertex")
    _require_exact_qualified_routes(
        qualified_routes,
        {
            ais_route_id: (getattr(ais._transport, "host", ""), ais._model),
            vertex_route_id: (getattr(vertex._transport, "host", ""), vertex._model),
        },
        configured_hosts=configured_hosts,
    )
    if qualified_routes is not None:
        if not ais.is_configured() or not vertex.is_configured():
            raise ValueError("A qualified provider route has no configured credential source.")
        try:
            _validate_vertex_base_url(os.environ.get(VERTEX_BASE_URL_ENV, ""))
        except ValueError as exc:
            raise ValueError(
                "A qualified provider route has no canonical Vertex base URL."
            ) from exc
    return RoundRobinGeneratorSelector({"google-ais": primary})


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
        route.id: (route.host, route.model_id)
        for route in qualified_routes
        if all(hasattr(route, field) for field in ("id", "host", "model_id"))
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
