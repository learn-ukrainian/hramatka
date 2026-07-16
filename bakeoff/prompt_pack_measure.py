# ruff: noqa: E501
"""Live, offline-only prompt-pack measurement runner.

Run this from a configured local engine environment.  It never deploys and it
never writes private anchors or raw provider outputs into the repository: the
report contains de-identified counts and SHA-256 digests while the protected
trace directory holds raw text for diagnosis.
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import json
import os
import random
import statistics
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hramatka.api.baking.engine_adapter import _prompt_pack_candidate_count_plan
from hramatka.engine import (
    content_density,
    data,
    pipeline,
    prompt_pack,
    retrieval,
    schema,
    selector,
)
from hramatka.engine.generate import GenerationUnparseable, generate, generate_prompt_pack
from hramatka.engine.providers import configure_provider_concurrency, make_generator
from hramatka.engine.registry import ACTIVITY_REGISTRY
from hramatka.sizing_policy import B1, phase_plan

PROTOCOLS = ("legacy-per-task", "pack-per-phase", "pack-whole-lesson")
REPORT_PATH = Path(".measure/PROMPT-PACK-RESULTS.md")
RESULTS_PATH = Path(".measure/PROMPT-PACK-RESULTS.json")


def _sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _p90(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.9) - 1))], 3)


def _mean(values: Sequence[float]) -> float:
    return round(statistics.fmean(values), 3) if values else 0.0


@dataclass(frozen=True)
class AnchorCase:
    opaque_id: str
    text: str
    source_class: str
    duration: int
    focus: str | None


@dataclass
class CapturedCall:
    call_id: str
    protocol: str
    anchor_id: str
    trial: int
    phase: str
    repair: bool
    requested_slots: list[str]
    prompt_bytes: int
    output_bytes: int
    queued_at: float
    started_at: float
    ended_at: float
    raw_digest: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "protocol": self.protocol,
            "anchor_id": self.anchor_id,
            "trial": self.trial,
            "phase": self.phase,
            "repair": self.repair,
            "requested_slots": self.requested_slots,
            "prompt_bytes": self.prompt_bytes,
            "output_bytes": self.output_bytes,
            "queue_seconds": round(self.started_at - self.queued_at, 6),
            "call_seconds": round(self.ended_at - self.started_at, 6),
            "raw_response_digest": self.raw_digest,
            "error": self.error,
        }


class CapturingGenerator:
    """A thread-safe raw trace wrapper; only the trace dir receives raw text."""

    def __init__(
        self,
        delegate: Callable[[str], str],
        *,
        trace_dir: Path,
        protocol: str,
        anchor_id: str,
        trial: int,
        phase: str,
        repair: bool,
        requested_slots: Sequence[str],
        calls: list[CapturedCall],
        lock: threading.Lock,
    ) -> None:
        self._delegate = delegate
        self._trace_dir = trace_dir
        self._protocol = protocol
        self._anchor_id = anchor_id
        self._trial = trial
        self._phase = phase
        self._repair = repair
        self._requested_slots = list(requested_slots)
        self._calls = calls
        self._lock = lock

    def __call__(self, prompt: str) -> str:
        queued_at = time.perf_counter()
        started_at = time.perf_counter()
        raw: str | None = None
        error: str | None = None
        try:
            raw = self._delegate(prompt)
            return raw
        except Exception as exc:  # caller converts transport/parser failures to metrics
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            ended_at = time.perf_counter()
            digest = _sha(raw) if raw is not None else None
            with self._lock:
                call_id = f"call-{len(self._calls) + 1:05d}"
                self._trace_dir.mkdir(parents=True, exist_ok=True)
                if raw is not None:
                    (self._trace_dir / f"{call_id}.raw.txt").write_text(raw, encoding="utf-8")
                self._calls.append(
                    CapturedCall(
                        call_id=call_id,
                        protocol=self._protocol,
                        anchor_id=self._anchor_id,
                        trial=self._trial,
                        phase=self._phase,
                        repair=self._repair,
                        requested_slots=self._requested_slots,
                        prompt_bytes=len(prompt.encode("utf-8")),
                        output_bytes=len(raw.encode("utf-8")) if raw is not None else 0,
                        queued_at=queued_at,
                        started_at=started_at,
                        ended_at=ended_at,
                        raw_digest=digest,
                        error=error,
                    )
                )


@dataclass
class ProtocolRun:
    protocol: str
    anchor_id: str
    trial: int
    planned_by_phase: dict[int, int]
    candidates_by_phase: dict[int, list[schema.HramatkaActivity]] = field(
        default_factory=lambda: defaultdict(list)
    )
    selected_by_phase: dict[int, list[schema.HramatkaActivity]] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)
    ended_at: float = 0.0


@dataclass(frozen=True)
class PackCallResult:
    """A parsed pack response or a recorded local envelope failure."""

    activities: list[object]
    envelope_error: str | None = None


def _load_real_anchor_cases(path: Path) -> list[AnchorCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or len(payload) != 4:
        raise ValueError("The private real-anchor file must contain exactly four audit lessons.")
    cases = []
    for row in payload:
        if not isinstance(row, Mapping):
            raise ValueError("Invalid private real-anchor row.")
        lesson = row.get("lesson_json", row)
        if not isinstance(lesson, Mapping):
            raise ValueError("Invalid private real lesson document.")
        anchor = lesson.get("anchor", {})
        text = anchor.get("text") if isinstance(anchor, Mapping) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Private real lesson lacks anchor.text.")
        source_id = str(row.get("id") or lesson.get("id") or _sha(text)[:12])
        cases.append(
            AnchorCase(
                opaque_id=f"real-{_sha(source_id)[:12]}",
                text=text,
                source_class="real-audit",
                duration=int(lesson.get("duration") or 45),
                focus=lesson.get("focus") if isinstance(lesson.get("focus"), str) else None,
            )
        )
    return cases


def load_anchor_cases(*, real_anchor_file: Path | None, fixtures_only: bool) -> list[AnchorCase]:
    cases: list[AnchorCase] = []
    if not fixtures_only:
        if real_anchor_file is None or not real_anchor_file.is_file():
            raise ValueError(
                "Four permitted real audit anchors are required; pass --real-anchor-file."
            )
        cases.extend(_load_real_anchor_cases(real_anchor_file))
    fixture_paths = (
        Path("hramatka/engine/tests/fixtures/anchor01.txt"),
        Path("hramatka/bakeoff/2026-07-10/anchors/var.txt"),
    )
    for path in fixture_paths:
        text = path.read_text(encoding="utf-8")
        cases.append(
            AnchorCase(
                opaque_id=f"fixture-{_sha(text)[:12]}",
                text=text,
                source_class="fixture",
                duration=45,
                focus="числівники" if path.name == "var.txt" else "читання",
            )
        )
    return cases


def _whole_context(shared: Mapping[str, Any]) -> dict[str, Any]:
    slots = [
        {
            "slot_id": row["slot_id"],
            "type": row["type"],
            "required_item_count": prompt_pack._required_item_count(row["type"]),
            "allowed_sentence_ids": row["kit"]["allowed_sentence_ids"],
        }
        for row in shared["slots"]
        if row["kit"].get("available")
    ]
    kits = [row["kit"] for row in shared["slots"] if row["kit"].get("available")]
    return {
        "shared": shared,
        "phase": "whole-lesson",
        "lesson_plan": shared["lesson_plan"],
        "phase_request": {
            "phase": "whole-lesson",
            "phase_name": "TTT",
            "mode": "initial",
            "requested_slots": slots,
            "response_order": [row["slot_id"] for row in slots],
        },
        "type_kits": kits,
        "allowed_and_forbidden_forms": [
            {
                "slot_id": kit["slot_id"],
                "allowed": {
                    "forms": kit["allowed_forms"],
                    "evidence_ids": kit["allowed_sentence_ids"],
                },
                "forbidden": kit["forbidden"],
            }
            for kit in kits
        ],
        "repair_failures": [],
        "provenance": shared["provenance"],
    }


def _gate_raw(
    raw_activities: Sequence[object],
    *,
    snapshot: Mapping[str, Any],
    grounding: Mapping[str, Any],
    expected_types: Sequence[str],
    phase: int,
    repair: bool = False,
) -> list[schema.HramatkaActivity]:
    safe = [
        raw
        for raw in raw_activities
        if isinstance(raw, dict)
        and raw.get("type") in ACTIVITY_REGISTRY
        and not ACTIVITY_REGISTRY[raw["type"]].raw_validator(raw)
    ]
    lookup = retrieval.augmented_atlas_lookup(
        str(snapshot["body_uk"]), safe, grounding["atlas_lookup"]
    )
    gated = []
    for index, raw in enumerate(raw_activities):
        provenance = {"measurement_protocol": "offline", "phase": phase, "repair": repair}
        candidate_id = f"p{phase}-candidate-{index:03d}"
        if not isinstance(raw, dict) or raw.get("type") not in expected_types:
            gated.append(
                pipeline.reject_raw_candidate(
                    raw,
                    provenance,
                    candidate_id=candidate_id,
                    detail="candidate outside requested raw activity bank",
                )
            )
        else:
            gated.append(
                pipeline.gate_activity(
                    raw,
                    str(snapshot["body_uk"]),
                    provenance,
                    atlas_lookup=lookup,
                    candidate_id=candidate_id,
                )
            )
    return gated


def _repair_context(
    context: Mapping[str, Any],
    slots: Sequence[Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Keep the original pack immutable while asking only failed slots again."""
    slot_ids = {str(slot["slot_id"]) for slot in slots}
    kits = [kit for kit in context["type_kits"] if str(kit["slot_id"]) in slot_ids]
    policy = [
        row for row in context["allowed_and_forbidden_forms"] if str(row["slot_id"]) in slot_ids
    ]
    return {
        **context,
        "phase_request": {
            **context["phase_request"],
            "mode": "repair",
            "requested_slots": [dict(slot) for slot in slots],
            "response_order": [str(slot["slot_id"]) for slot in slots],
        },
        "type_kits": kits,
        "allowed_and_forbidden_forms": policy,
        "repair_failures": [dict(failure) for failure in failures],
    }


def _failed_slots(
    slots: Sequence[Mapping[str, Any]], candidates: Sequence[schema.HramatkaActivity]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    unresolved: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, slot in enumerate(slots):
        candidate = candidates[index] if index < len(candidates) else None
        if candidate is not None and candidate.gate_result.status == schema.DISPOSITION_READY:
            continue
        gate = "raw_contract"
        locator = None
        if candidate is not None:
            for check in candidate.gate_result.checks:
                if check.status == "fail":
                    gate, locator = check.gate, check.locator
                    break
        unresolved.append(dict(slot))
        failures.append(
            {
                "slot_id": slot["slot_id"],
                "prior_activity_index": index if candidate is not None else None,
                "locator": locator,
                "gate": gate,
                "normalized_detail": f"{gate}: prior slot was not ready",
                "required_fix": "replace only this slot with a contract-valid activity",
            }
        )
    return unresolved, failures


def _raw_contract_class(detail: str) -> str:
    normalized = detail.casefold()
    if "evidence" in normalized:
        return "missing_evidence"
    if "must be an object" in normalized or "json object" in normalized:
        return "wrong_json_shape"
    if "boolean" in normalized or "integer" in normalized or "list" in normalized:
        return "incorrect_scalar_or_collection_type"
    if "learner_" in normalized:
        return "illegal_learner_response_key"
    return "other_raw_contract"


def _envelope_failures(
    slots: Sequence[Mapping[str, Any]], *, phase: int, repair: bool
) -> list[schema.HramatkaActivity]:
    """Keep every failed response slot in the measured rejection denominator."""
    rejected = []
    for index, slot in enumerate(slots):
        gate_result = schema.GateResult()
        gate_result.add(
            "pack_envelope",
            "fail",
            "Prompt-pack JSON/citation/density envelope failed after its parser retry.",
            locator=str(slot["slot_id"]),
        )
        rejected.append(
            schema.HramatkaActivity(
                activity={"type": str(slot["type"])},
                provenance={"measurement_protocol": "offline", "phase": phase, "repair": repair},
                gate_result=gate_result,
                candidate_id=f"p{phase}-envelope-{index:03d}",
            )
        )
    return rejected


def _call_pack(
    context: Mapping[str, Any],
    *,
    captured: CapturingGenerator,
) -> PackCallResult:
    try:
        return PackCallResult(generate_prompt_pack(dict(context), generator=captured))
    except (GenerationUnparseable, prompt_pack.PromptPackError) as exc:
        return PackCallResult([], envelope_error=str(exc))


def _call_legacy(
    anchor: str,
    activity_type: str,
    *,
    captured: CapturingGenerator,
    grounding_text: str,
) -> list[object]:
    try:
        return generate(
            anchor,
            types=[activity_type],
            counts={activity_type: 1},
            generator=captured,
            grounding_pack=grounding_text,
        )
    except GenerationUnparseable:
        return []


def _select(
    run: ProtocolRun,
    *,
    snapshot: Mapping[str, Any],
    count_plans: Mapping[int, Mapping[str, int]],
    shared: Mapping[str, Any],
) -> None:
    slots_by_phase = run.planned_by_phase
    total_count_plan = sum((Counter(plan) for plan in count_plans.values()), Counter())
    run.selected_by_phase = selector.select_composed_lesson(
        {
            phase: [
                candidate
                for candidate in candidates
                if candidate.gate_result.status == schema.DISPOSITION_READY
            ]
            for phase, candidates in run.candidates_by_phase.items()
        },
        slots_by_phase=slots_by_phase,
        count_plan=total_count_plan,
        policy=selector.SelectorPolicy(density_target=sum(slots_by_phase.values())),
        anchor=dict(snapshot),
        focus_context=prompt_pack.focus_selector_context(shared),
    )


def _run_protocol(
    *,
    protocol: str,
    anchor: AnchorCase,
    trial: int,
    generator: Callable[[str], str],
    trace_dir: Path,
    calls: list[CapturedCall],
    calls_lock: threading.Lock,
    concurrency: int,
) -> tuple[ProtocolRun, list[dict[str, Any]]]:
    snapshot = pipeline.snapshot_anchor(anchor.text)
    grounding = retrieval.build_grounding_pack(snapshot["body_uk"], B1)
    plans = _prompt_pack_candidate_count_plan(phase_plan(B1, anchor.duration))
    visible = Counter(phase_plan(B1, anchor.duration))
    shared = prompt_pack.build_shared_input(
        snapshot=snapshot,
        grounding=grounding,
        duration_minutes=anchor.duration,
        focus=anchor.focus,
        phase_count_plans=plans,
        visible_slots_by_phase=visible,
    )
    run = ProtocolRun(
        protocol=protocol,
        anchor_id=anchor.opaque_id,
        trial=trial,
        planned_by_phase=dict(visible),
    )
    candidate_rows: list[dict[str, Any]] = []

    def capture(phase: str, repair: bool, slots: Sequence[str]) -> CapturingGenerator:
        return CapturingGenerator(
            generator,
            trace_dir=trace_dir,
            protocol=protocol,
            anchor_id=anchor.opaque_id,
            trial=trial,
            phase=phase,
            repair=repair,
            requested_slots=slots,
            calls=calls,
            lock=calls_lock,
        )

    def run_phase(phase: int) -> tuple[int, list[schema.HramatkaActivity]]:
        context = prompt_pack.phase_context(shared, phase=phase)
        slots = context["phase_request"]["requested_slots"]
        if protocol == "legacy-per-task":
            gated: list[schema.HramatkaActivity] = []
            for slot in slots:
                initial = _gate_raw(
                    _call_legacy(
                        anchor.text,
                        slot["type"],
                        captured=capture(str(phase), False, [slot["slot_id"]]),
                        grounding_text=grounding["text"],
                    ),
                    snapshot=snapshot,
                    grounding=grounding,
                    expected_types=[slot["type"]],
                    phase=phase,
                )
                gated.extend(initial)
                if not any(
                    candidate.gate_result.status == schema.DISPOSITION_READY
                    for candidate in initial
                ):
                    gated.extend(
                        _gate_raw(
                            _call_legacy(
                                anchor.text,
                                slot["type"],
                                captured=capture(str(phase), True, [slot["slot_id"]]),
                                grounding_text=grounding["text"],
                            ),
                            snapshot=snapshot,
                            grounding=grounding,
                            expected_types=[slot["type"]],
                            phase=phase,
                            repair=True,
                        )
                    )
        else:
            initial = _call_pack(
                context,
                captured=capture(str(phase), False, [slot["slot_id"] for slot in slots]),
            )
            gated = (
                _envelope_failures(slots, phase=phase, repair=False)
                if initial.envelope_error is not None
                else _gate_raw(
                    initial.activities,
                    snapshot=snapshot,
                    grounding=grounding,
                    expected_types=[slot["type"] for slot in slots],
                    phase=phase,
                )
            )
            unresolved, failures = _failed_slots(slots, gated)
            if unresolved:
                repaired = _call_pack(
                    _repair_context(context, unresolved, failures),
                    captured=capture(str(phase), True, [slot["slot_id"] for slot in unresolved]),
                )
                gated.extend(
                    _envelope_failures(unresolved, phase=phase, repair=True)
                    if repaired.envelope_error is not None
                    else _gate_raw(
                        repaired.activities,
                        snapshot=snapshot,
                        grounding=grounding,
                        expected_types=[slot["type"] for slot in unresolved],
                        phase=phase,
                        repair=True,
                    )
                )
        return phase, gated

    if protocol == "pack-whole-lesson":
        context = _whole_context(shared)
        slots = context["phase_request"]["requested_slots"]
        initial = _call_pack(
            context,
            captured=capture("whole-lesson", False, [slot["slot_id"] for slot in slots]),
        )
        initial_by_phase: dict[int, list[schema.HramatkaActivity]] = {}
        slots_by_phase: dict[int, list[dict[str, Any]]] = {}
        offset = 0
        for phase in sorted(plans):
            expected = [slot for slot in slots if str(slot["slot_id"]).startswith(f"P{phase}-")]
            batch = initial.activities[offset : offset + len(expected)]
            offset += len(expected)
            slots_by_phase[phase] = expected
            initial_by_phase[phase] = (
                _envelope_failures(expected, phase=phase, repair=False)
                if initial.envelope_error is not None
                else _gate_raw(
                    batch,
                    snapshot=snapshot,
                    grounding=grounding,
                    expected_types=[slot["type"] for slot in expected],
                    phase=phase,
                )
            )
            run.candidates_by_phase[phase].extend(initial_by_phase[phase])
        unresolved = []
        failures = []
        for phase, initial in initial_by_phase.items():
            missing, phase_failures = _failed_slots(slots_by_phase[phase], initial)
            unresolved.extend(missing)
            failures.extend(phase_failures)
        if unresolved:
            repaired = _call_pack(
                _repair_context(context, unresolved, failures),
                captured=capture("whole-lesson", True, [slot["slot_id"] for slot in unresolved]),
            )
            offset = 0
            for phase in sorted(plans):
                expected = [
                    slot for slot in unresolved if str(slot["slot_id"]).startswith(f"P{phase}-")
                ]
                batch = repaired.activities[offset : offset + len(expected)]
                offset += len(expected)
                if expected:
                    run.candidates_by_phase[phase].extend(
                        _envelope_failures(expected, phase=phase, repair=True)
                        if repaired.envelope_error is not None
                        else _gate_raw(
                            batch,
                            snapshot=snapshot,
                            grounding=grounding,
                            expected_types=[slot["type"] for slot in expected],
                            phase=phase,
                            repair=True,
                        )
                    )
    else:
        # The operational phase arm starts all three context-rich requests at
        # once; the legacy arm also respects the same provider cap.
        with ThreadPoolExecutor(max_workers=min(concurrency, len(plans))) as executor:
            futures = [
                executor.submit(contextvars.copy_context().run, run_phase, phase)
                for phase in sorted(plans)
            ]
            for future in futures:
                phase, gated = future.result()
                run.candidates_by_phase[phase].extend(gated)

    _select(run, snapshot=snapshot, count_plans=plans, shared=shared)
    run.ended_at = time.perf_counter()
    for phase, candidates in run.candidates_by_phase.items():
        for emitted_index, candidate in enumerate(candidates):
            checks = [
                {"gate": check.gate, "status": check.status, "locator": check.locator}
                for check in candidate.gate_result.checks
            ]
            candidate_rows.append(
                {
                    "protocol": protocol,
                    "anchor_id": anchor.opaque_id,
                    "trial": trial,
                    "phase": phase,
                    "emitted_index": emitted_index,
                    "type": candidate.activity.get("type"),
                    "disposition": candidate.gate_result.status,
                    "checks": checks,
                    "raw_contract_classes": [
                        _raw_contract_class(check.detail)
                        for check in candidate.gate_result.checks
                        if check.gate == "raw_contract" and check.status == "fail"
                    ],
                    "selected": any(
                        candidate in selected for selected in run.selected_by_phase.values()
                    ),
                    "density_ok": content_density.meets_content_density(candidate, snapshot),
                    "repair": bool(candidate.provenance.get("repair")),
                }
            )
    return run, candidate_rows


def _aggregate(
    calls: Sequence[CapturedCall],
    candidates: Sequence[Mapping[str, Any]],
    lessons: Sequence[ProtocolRun],
) -> dict[str, Any]:
    emitted = len(candidates)
    rejected = sum(row["disposition"] == schema.DISPOSITION_REJECTED for row in candidates)
    review = sum(row["disposition"] == schema.DISPOSITION_REVIEW for row in candidates)
    raw_failed = [
        row
        for row in candidates
        if any(
            check["gate"] == "raw_contract" and check["status"] == "fail" for check in row["checks"]
        )
    ]
    raw_by_detail = Counter()
    gate_counts = Counter()
    for row in candidates:
        for check in row["checks"]:
            if check["status"] == "fail":
                gate_counts[check["gate"]] += 1
                if check["gate"] == "raw_contract":
                    for detail_class in row.get("raw_contract_classes", ["other_raw_contract"]):
                        raw_by_detail[detail_class] += 1
    selected_rows = [row for row in candidates if row["selected"]]
    rich_by_type = Counter(
        str(row["type"])
        for row in selected_rows
        if row["density_ok"] and isinstance(row["type"], str)
    )
    # Density was already computed with the snapshot for candidate rows.  The
    # selected count is the authoritative shipped-block metric.
    initial_calls = [call for call in calls if not call.repair]
    initial_candidates = [row for row in candidates if not row.get("repair")]
    post_repair_candidates = [row for row in candidates if row.get("repair")]

    def rate(
        rows: Sequence[Mapping[str, Any]], predicate: Callable[[Mapping[str, Any]], bool]
    ) -> float:
        return round(sum(predicate(row) for row in rows) / len(rows), 4) if rows else 0.0

    return {
        "provider_calls": len(calls),
        "initial_provider_calls": len(initial_calls),
        "parse_failure_rate": round(sum(call.error is not None for call in calls) / len(calls), 4)
        if calls
        else 0.0,
        "raw_contract_rate": round(len(raw_failed) / emitted, 4) if emitted else 0.0,
        "overall_rejection_rate": round(rejected / emitted, 4) if emitted else 0.0,
        "review_required_rate": round(review / emitted, 4) if emitted else 0.0,
        "ready_yield": round((emitted - rejected - review) / emitted, 4) if emitted else 0.0,
        "blocks_shipped": len(selected_rows),
        "rich_blocks": sum(row["selected"] and row["density_ok"] for row in candidates),
        "richness_by_type": dict(sorted(rich_by_type.items())),
        "raw_contract_breakdown": dict(raw_by_detail),
        "gate_failures": dict(gate_counts),
        "call_seconds_p50": round(
            statistics.median([call.ended_at - call.started_at for call in calls]), 3
        )
        if calls
        else 0.0,
        "call_seconds_p90": _p90([call.ended_at - call.started_at for call in calls]),
        "lesson_seconds_p50": round(
            statistics.median([lesson.ended_at - lesson.started_at for lesson in lessons]), 3
        )
        if lessons
        else 0.0,
        "lesson_seconds_p90": _p90([lesson.ended_at - lesson.started_at for lesson in lessons]),
        "input_bytes": sum(call.prompt_bytes for call in calls),
        "output_bytes": sum(call.output_bytes for call in calls),
        "initial_candidates": len(initial_candidates),
        "post_repair_candidates": len(post_repair_candidates),
        "initial_raw_contract_rate": rate(
            initial_candidates,
            lambda row: any(
                check["gate"] == "raw_contract" and check["status"] == "fail"
                for check in row["checks"]
            ),
        ),
        "post_repair_raw_contract_rate": rate(
            post_repair_candidates,
            lambda row: any(
                check["gate"] == "raw_contract" and check["status"] == "fail"
                for check in row["checks"]
            ),
        ),
        "initial_rejection_rate": rate(
            initial_candidates,
            lambda row: row["disposition"] == schema.DISPOSITION_REJECTED,
        ),
        "post_repair_rejection_rate": rate(
            post_repair_candidates,
            lambda row: row["disposition"] == schema.DISPOSITION_REJECTED,
        ),
    }


def _write_report(path: Path, result: Mapping[str, Any]) -> None:
    rows = result["summary"]
    lines = [
        "# Prompt-pack offline measurement results",
        "",
        f"Updated: {result['updated_at']}",
        "",
        "All calls used live local Gemma transport with JSON mode explicitly OFF. Raw provider text and private anchors are retained only at the protected trace location recorded in the manifest; table records use opaque anchor IDs and raw-response SHA-256 digests.",
        "",
        "| iteration | protocol | calls | raw_contract | rejection | blocks shipped | rich blocks | call p90 | lesson p90 | input/output bytes |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {iteration} | {protocol} | {provider_calls} | {raw_contract_rate:.1%} | "
            "{overall_rejection_rate:.1%} | {blocks_shipped} | {rich_blocks} | "
            "{call_seconds_p90:.3f}s | {lesson_seconds_p90:.3f}s | "
            "{input_bytes}/{output_bytes} |".format(**row)
        )
    lines.extend(
        [
            "",
            "| protocol | initial candidates | repair candidates | initial raw/rejection | repair raw/rejection |",
            "| --- | ---: | ---: | ---: | ---: |",
            *[
                "| {protocol} | {initial_candidates} | {post_repair_candidates} | "
                "{initial_raw_contract_rate:.1%}/{initial_rejection_rate:.1%} | "
                "{post_repair_raw_contract_rate:.1%}/{post_repair_rejection_rate:.1%} |".format(
                    **row
                )
                for row in rows
            ],
            "",
            "## Verdict",
            "",
            result["verdict"],
            "",
            "## Evidence",
            "",
            f"- Run manifest digest: `{result['manifest_digest']}`",
            f"- Probe raw-response digest: `{result['probe']['raw_digest']}`",
            f"- Probe raw (safe no-anchor call, quoted): `{result['probe']['raw_excerpt']}`",
            f"- Protected trace location: `{result['trace_dir']}`",
            "- Candidate/gate records and call records are in `.measure/PROMPT-PACK-RESULTS.json`; no private anchor or raw response text is committed.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_measurement(
    *,
    generator: Callable[[str], str],
    anchors: Sequence[AnchorCase],
    trials: int,
    iteration: int,
    trace_dir: Path,
    concurrency: int,
    probe_raw: str,
) -> dict[str, Any]:
    configure_provider_concurrency(concurrency)
    calls: list[CapturedCall] = []
    call_lock = threading.Lock()
    candidate_rows: list[dict[str, Any]] = []
    lessons: list[ProtocolRun] = []
    rng = random.Random(f"prompt-pack-{iteration}")
    for anchor in anchors:
        for trial in range(1, trials + 1):
            protocols = list(PROTOCOLS)
            rng.shuffle(protocols)
            for protocol in protocols:
                lesson, rows = _run_protocol(
                    protocol=protocol,
                    anchor=anchor,
                    trial=trial,
                    generator=generator,
                    trace_dir=trace_dir,
                    calls=calls,
                    calls_lock=call_lock,
                    concurrency=concurrency,
                )
                lessons.append(lesson)
                candidate_rows.extend(rows)
                result = _result_payload(
                    iteration=iteration,
                    anchors=anchors,
                    calls=calls,
                    candidates=candidate_rows,
                    lessons=lessons,
                    trace_dir=trace_dir,
                    probe_raw=probe_raw,
                )
                RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                RESULTS_PATH.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                _write_report(REPORT_PATH, result)
    return _result_payload(
        iteration=iteration,
        anchors=anchors,
        calls=calls,
        candidates=candidate_rows,
        lessons=lessons,
        trace_dir=trace_dir,
        probe_raw=probe_raw,
    )


def _result_payload(
    *,
    iteration: int,
    anchors: Sequence[AnchorCase],
    calls: Sequence[CapturedCall],
    candidates: Sequence[Mapping[str, Any]],
    lessons: Sequence[ProtocolRun],
    trace_dir: Path,
    probe_raw: str,
) -> dict[str, Any]:
    summary = []
    for protocol in PROTOCOLS:
        metrics = _aggregate(
            [call for call in calls if call.protocol == protocol],
            [row for row in candidates if row["protocol"] == protocol],
            [lesson for lesson in lessons if lesson.protocol == protocol],
        )
        summary.append({"iteration": iteration, "protocol": protocol, **metrics})
    phase = next((row for row in summary if row["protocol"] == "pack-per-phase"), {})
    target_met = (
        phase.get("raw_contract_rate", 1.0) <= 0.02
        and phase.get("overall_rejection_rate", 1.0) < 0.30
        and phase.get("rich_blocks", 0) >= 6
    )
    verdict = (
        "Pack per-phase meets the predeclared offline target (raw_contract ≈0, rejection <30%, ≥6 rich blocks)."
        if target_met
        else "Pack per-phase misses at least one predeclared target; refine the prompt/kits and re-run (maximum three measured iterations)."
    )
    manifest = {
        "iteration": iteration,
        "model": "google-ais/gemma-4-31b-it",
        "json_mode": "off",
        "trials": len({(lesson.anchor_id, lesson.trial) for lesson in lessons}),
        "anchors": [
            {
                "opaque_id": anchor.opaque_id,
                "source_class": anchor.source_class,
                "sha256": _sha(anchor.text),
                "chars": len(anchor.text),
                "duration": anchor.duration,
                "focus_present": bool(anchor.focus),
            }
            for anchor in anchors
        ],
        "candidate_policy": "max(2, visible_slots), capped at 6",
        "prompt_pack_version": prompt_pack.PROMPT_PACK_VERSION,
    }
    return {
        "updated_at": _utc_now(),
        "manifest": manifest,
        "manifest_digest": _sha(json.dumps(manifest, sort_keys=True, ensure_ascii=False)),
        "trace_dir": str(trace_dir),
        "probe": {"raw_digest": _sha(probe_raw), "raw_excerpt": probe_raw[:240].replace("`", "'")},
        "summary": summary,
        "verdict": verdict,
        "calls": [call.as_dict() for call in calls],
        "candidates": list(candidates),
    }


def _probe(generator: Callable[[str], str], trace_dir: Path) -> str:
    prompt = 'Відповідайте рівно JSON-об\'єктом {"probe":"ok"} без пояснення.'
    raw = generator(prompt)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("Gemma probe returned no text.")
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "probe.raw.txt").write_text(raw, encoding="utf-8")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-anchor-file", type=Path)
    parser.add_argument("--fixtures-only", action="store_true")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--trace-dir", type=Path)
    args = parser.parse_args()
    if args.trials < 1 or args.iteration not in {1, 2, 3} or args.concurrency < 1:
        parser.error("trials/concurrency must be positive and iteration must be 1, 2, or 3")
    if not os.environ.get("HRAMATKA_AIS_API_KEY"):
        print("STOP: HRAMATKA_AIS_API_KEY is absent; no measurements were fabricated.")
        return 2
    # #171 is intentionally off regardless of a shell default.  The separate
    # transport compatibility probe is outside this experiment.
    os.environ["HRAMATKA_GEN_JSON_MODE"] = "0"
    try:
        data.active_bundle()
    except Exception as exc:
        print(f"STOP: configured local data bundle is unavailable ({type(exc).__name__}).")
        return 2
    anchors = load_anchor_cases(
        real_anchor_file=args.real_anchor_file,
        fixtures_only=args.fixtures_only,
    )
    trace_dir = (
        args.trace_dir or Path(tempfile.gettempdir()) / f"hramatka-prompt-pack-{int(time.time())}"
    )
    generator = make_generator("gemma-ais")
    try:
        probe_raw = _probe(generator, trace_dir)
    except Exception as exc:
        print(f"STOP: Gemma probe failed ({type(exc).__name__}); no matrix was run.")
        return 2
    result = run_measurement(
        generator=generator,
        anchors=anchors,
        trials=args.trials,
        iteration=args.iteration,
        trace_dir=trace_dir,
        concurrency=args.concurrency,
        probe_raw=probe_raw,
    )
    RESULTS_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_report(REPORT_PATH, result)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by operator live run
    raise SystemExit(main())
