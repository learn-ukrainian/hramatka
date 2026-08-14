"""Provider telemetry, concurrency, and bake-progress context (#456)."""

from __future__ import annotations

import contextvars
import logging
import os
import statistics
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hramatka.engine.density_evaluator_v3 import MAX_REPAIR_ROUNDS

log = logging.getLogger("hramatka.engine.providers")

# JSON output is opt-in for Gemini seats, not a prompt convention. The #171
# Gemma measurements remain valid, so Gemma is excluded by model identity
# rather than through a stale host-wide deny.
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

    Gemini seats get constrained JSON on every supported transport only when
    ``HRAMATKA_GEN_JSON_MODE=1``. Gemma remains denied because #171 measured
    its JSON mode as unsafe; its prompt contract is intentionally not promoted
    to a transport contract.
    """
    if os.environ.get("HRAMATKA_GEN_JSON_MODE") != "1":
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
        value.get("mode") not in {"initial", "repair", "semantic_review"}
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
        "contract_version",
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
        or not isinstance(value["contract_version"], str)
        or not value["contract_version"]
        or type(value["repair_rounds"]) is not int
        or value["repair_rounds"] < 0
        or value["repair_rounds"] > MAX_REPAIR_ROUNDS
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
