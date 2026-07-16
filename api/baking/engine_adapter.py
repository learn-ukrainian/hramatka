"""Real engine adapter for the durable-job seam (`baking/port.LessonBaker`).

Runs the private slice-1 engine pipeline and composes a `lu.lesson.v1` block
template from its gate-passing activities. This lands WIRED to the seam but is
NOT yet the `create_app` default — the full mock→real swap (durable-job
plumbing, real generator, pedagogy validation) is a separate step. The
generator is injectable, so the pipeline runs offline in tests with a fake
generator and never touches the network here.

The assembler selects only ``ready`` candidates under one whole-lesson policy.
``review_required`` material is retained in the rejected/reserve tray, never
promoted into a visible warning block to hide a density shortfall.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any

from hramatka.contracts import PILOT_ACTIVITY_TYPES
from hramatka.engine import (
    content_density,
    data,
    pipeline,
    prompt_pack,
    registry,
    retrieval,
    schema,
    selector,
)
from hramatka.engine.gates import vesum as vesum_gate
from hramatka.engine.generate import GEMMA_MODEL, call_gemma
from hramatka.sizing_policy import B1, phase_plan, resolve_duration

from .port import FloorUnmetError, GenerationFailed, ProviderUnavailable

log = logging.getLogger(__name__)

# Real-Gemma measurement and pilot bakes yielded roughly 60–75% gate-ready
# candidates. Use the conservative measured floor: a plan of N TTT slots asks
# for ceil(N / 0.60) candidates, so its expected ready pool is at least N.
# For example, the 45-minute eight-slot plan asks for 14 candidates and expects
# eight ready. Two targeted regeneration rounds remain a bounded safety net for
# variance: they improve recovery from a short bank without unbounded provider
# cost or an open-ended bake.
_READY_SURVIVAL_FLOOR = 0.60
_MAX_REGENERATION_ATTEMPTS = 2

# Keep diagnostics for fourteen days: this covers the daily-backup recovery
# investigation window while putting a fixed bound on the host's state volume.
_ENGINE_OUT_RETENTION_DAYS = 14


def _generation_failure_message(error_type: str) -> str:
    """Return an operator-safe, type-specific adapter message.

    The runner deliberately replaces these with its frozen teacher-facing
    wording before durable storage. Keeping the typed message here prevents
    pipeline traces from collapsing parse/content/pack failures into an outage.
    """
    messages = {
        "GenerationUnparseable": "Bake failed: generator response was not valid JSON.",
        "PromptPackError": (
            "Bake failed: prompt-pack response did not meet validation requirements."
        ),
        "NoEligibleActivities": "Bake failed: no eligible activities were generated.",
        "DataConfigError": "Bake failed: lesson data configuration is unavailable.",
        "DataDriftError": "Bake failed: lesson data validation detected drift.",
        "mixed": "Bake failed: generation produced mixed unusable outcomes.",
    }
    return messages.get(error_type, "Bake failed: generation did not yield usable activities.")


def _candidate_count_plan(phases: list[int]) -> dict[int, dict[str, int]]:
    """Plan a mixed candidate bank independently for every TTT phase.

    Each phase receives ``ceil(slots / 0.60)`` candidates.  Types whose phase
    legality is narrower than the requested TTT plan are reserved in every
    legal phase, giving match-up and short-writing real generation capacity.
    The remaining capacity is assigned by the PILOT_ACTIVITY_TYPES × TTT-phase
    incidence matrix, preferring the least-requested legal type globally.  The
    residual all-phase capacity in every current TTT phase remains enough to
    fill that phase's visible slots when optional constrained types do not make
    it through the bank.
    """
    if not phases:
        raise ValueError("TTT phase plan must contain at least one slot")
    requested_phases = set(phases)
    requested_counts = Counter(phases)
    registry_types = set(registry.ACTIVITY_REGISTRY)
    if set(PILOT_ACTIVITY_TYPES) != registry_types:
        raise ValueError("Pilot activity registry does not match PILOT_ACTIVITY_TYPES.")

    plans: dict[int, dict[str, int]] = {}
    global_counts: Counter[str] = Counter()
    for phase in sorted(requested_counts):
        slots = requested_counts[phase]
        budget = ceil(slots / _READY_SURVIVAL_FLOOR)
        legal = [
            activity_type
            for activity_type in PILOT_ACTIVITY_TYPES
            if phase in registry.ACTIVITY_REGISTRY[activity_type].ttt_phases
        ]
        if not legal:
            raise ValueError(f"No pilot activity type can fill TTT phase {phase}.")

        # A constrained type is still requested in every phase where it is
        # legal. Composition later chooses it once at its earliest legal phase;
        # the additional phase capacity is an honest bank fallback, not a
        # hard-coded type-name preference.
        constrained = [
            activity_type
            for activity_type in legal
            if not requested_phases.issubset(registry.ACTIVITY_REGISTRY[activity_type].ttt_phases)
        ]
        if len(constrained) > budget:
            raise ValueError(f"TTT phase {phase} cannot reserve its constrained activity types.")

        counts: Counter[str] = Counter(constrained)
        global_counts.update(constrained)
        while sum(counts.values()) < budget:
            activity_type = min(
                legal,
                key=lambda candidate: (
                    global_counts[candidate],
                    PILOT_ACTIVITY_TYPES.index(candidate),
                ),
            )
            counts[activity_type] += 1
            global_counts[activity_type] += 1

        flexible_capacity = sum(
            count
            for activity_type, count in counts.items()
            if requested_phases.issubset(registry.ACTIVITY_REGISTRY[activity_type].ttt_phases)
        )
        if flexible_capacity < slots:
            raise ValueError(
                f"TTT phase {phase} lacks a flexible fallback for {slots} visible slots."
            )
        plans[phase] = {
            activity_type: counts[activity_type]
            for activity_type in PILOT_ACTIVITY_TYPES
            if counts[activity_type]
        }
    return plans


def _prompt_pack_candidate_count_plan(phases: list[int]) -> dict[int, dict[str, int]]:
    """Use ``max(2, visible_slots)`` candidates, capped at six, per TTT phase.

    This intentionally differs from the legacy survival-inflated bank while
    keeping each phase's candidate count directly visible in the pack.
    """
    visible = Counter(phases)
    base_types = ("true-false", "quiz", "cloze", "fill-in")
    plans: dict[int, dict[str, int]] = {}
    for phase, slots in sorted(visible.items()):
        quota = prompt_pack.phase_candidate_quota(slots)
        # Phase three deliberately reserves the two productive forms when its
        # quota permits; all selected types remain legal under the registry.
        candidates = ("text-questions", "short-writing", *base_types) if phase == 3 else base_types
        counts: Counter[str] = Counter()
        for index in range(quota):
            activity_type = candidates[index % len(candidates)]
            if phase not in registry.ACTIVITY_REGISTRY[activity_type].ttt_phases:
                raise ValueError(f"Prompt-pack selected illegal TTT type {activity_type!r}.")
            counts[activity_type] += 1
        plans[phase] = dict(counts)
    return plans


def _activity_identity(ir: Any) -> str:
    """Stable semantic identity used to keep composed visible blocks distinct."""
    import json

    return json.dumps(ir.activity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _answer_key(ir) -> dict:
    activity = ir.activity
    raw = ir.raw_candidate if isinstance(ir.raw_candidate, dict) else activity
    a_type = activity.get("type")
    if a_type == "true-false":
        return {
            "items": [
                {"index": i, "correct": bool(it.get("correct"))}
                for i, it in enumerate(activity.get("items", []))
            ]
        }
    if a_type == "cloze":
        return {
            "blanks": [
                {"id": b.get("id"), "answer": b.get("answer")} for b in activity.get("blanks", [])
            ]
        }
    if a_type == "match-up":
        return {
            "pairs": [
                {"left_index": i, "right_index": i} for i in range(len(activity.get("pairs", [])))
            ]
        }
    if a_type == "quiz":
        return {
            "items": [
                {"index": i, "correct": int(it.get("correct", 0))}
                for i, it in enumerate(activity.get("items", []))
            ]
        }
    if a_type == "mark-the-words":
        return {"target_words": list(activity.get("target_words", []))}
    if a_type == "fill-in":
        return {"items": [item.get("answer", "") for item in activity.get("items", [])]}
    if a_type == "error-correction":
        corrected: list[str] = []
        for item in raw.get("items", []):
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence")
            if isinstance(evidence, str) and evidence.strip():
                corrected.append(evidence)
                continue
            sentence = item.get("sentence")
            error = item.get("error")
            correction = item.get("correction")
            if isinstance(sentence, str) and isinstance(error, str) and isinstance(correction, str):
                corrected.append(sentence.replace(error, correction, 1))
            elif isinstance(sentence, str):
                corrected.append(sentence)
        return {"items": corrected}
    if a_type == "text-questions":
        key: dict[str, Any] = {
            "guidance": raw.get("teacher_guidance") or "Перевірте відповіді за текстом якоря.",
        }
        model_answers = [
            item.get("model_answer")
            for item in raw.get("items", [])
            if isinstance(item, dict) and isinstance(item.get("model_answer"), str)
        ]
        if model_answers:
            key["model_answers"] = model_answers
        rubric = raw.get("rubric") or raw.get("rubric_hint")
        if isinstance(rubric, str) and rubric.strip():
            key["rubric"] = rubric
        return key
    if a_type == "short-writing":
        key = {
            "guidance": raw.get("teacher_guidance")
            or "Вільна відповідь — перевірте форми та зміст.",
        }
        model_answer = raw.get("model_answer")
        if isinstance(model_answer, str) and model_answer.strip():
            key["model_answer"] = model_answer
        rubric = raw.get("rubric") or raw.get("rubric_hint")
        if isinstance(rubric, str) and rubric.strip():
            key["rubric"] = rubric
        return key
    return {"guidance": "Підтвердьте ключ разом з учителем."}


def _strip_internal_item_fields(payload: dict) -> dict:
    """Remove engine-only per-item fields before pilot schema validation."""
    items = payload.get("items")
    if isinstance(items, list):
        payload["items"] = [
            {key: value for key, value in item.items() if key != "evidence"}
            if isinstance(item, dict)
            else item
            for item in items
        ]
    pairs = payload.get("pairs")
    if isinstance(pairs, list):
        payload["pairs"] = [
            {key: value for key, value in pair.items() if key != "evidence"}
            if isinstance(pair, dict)
            else pair
            for pair in pairs
        ]
    return payload


def _pilot_payload(activity: dict) -> dict:
    """Project a gate-passing engine activity into the frozen pilot payload shape."""
    a_type = activity.get("type")
    if a_type == "error-correction":
        return {
            "type": "error-correction",
            "instruction": activity["instruction"],
            "items": [
                item["sentence"] if isinstance(item, dict) else item
                for item in activity.get("items", [])
            ],
        }
    if a_type == "text-questions":
        payload: dict[str, Any] = {
            "type": "text-questions",
            "instruction": activity["instruction"],
            "items": [
                item["question"] if isinstance(item, dict) else item
                for item in activity.get("items", [])
            ],
        }
        source_ref = activity.get("source_ref")
        if isinstance(source_ref, str) and source_ref.strip():
            payload["source_ref"] = source_ref
        return payload
    if a_type == "short-writing":
        payload = {"type": "short-writing", "prompt": activity["prompt"]}
        source_ref = activity.get("source_ref")
        if isinstance(source_ref, str) and source_ref.strip():
            payload["source_ref"] = source_ref
        return payload
    if a_type == "quiz":
        return _strip_internal_item_fields(
            {
                "type": "quiz",
                "instruction": activity["instruction"],
                "items": activity.get("items", []),
            }
        )
    payload = {key: value for key, value in activity.items() if key not in {"id", "title", "notes"}}
    if a_type == "cloze":
        # The engine's internal gap convention is {gap} / {{N}}; the frozen
        # activity contract (golden fixture) renders blanks as [___:N] matched
        # to blank ids. Without this conversion the player shows a literal
        # "{gap}" to the learner (deployed launch-gate find, 2026-07-13).
        text = payload.get("text", "")
        blanks = payload.get("blanks", [])
        gap_tokens = ["{gap}", "{gap2}", "{gap3}", "{gap4}"]
        for token, blank in zip(gap_tokens, blanks, strict=False):
            if not isinstance(blank, dict):
                continue
            blank_id = blank.get("id")
            if blank_id is not None:
                text = text.replace(token, f"[___:{blank_id}]")
        text = re.sub(r"\{\{(\d+)\}\}", r"[___:\1]", text)
        payload["text"] = text
    return _strip_internal_item_fields(payload)


_TITLES = {
    "true-false": "Перевірмо розуміння",
    "cloze": "Заповніть пропуски",
    "match-up": "Знайдіть пару",
    "quiz": "Тест",
    "mark-the-words": "Позначте слова",
    "fill-in": "Вставте слово",
    "error-correction": "Виправте помилку",
    "text-questions": "Питання до тексту",
    "short-writing": "Коротке письмо",
}


def _mode(phase: int, a_type: str) -> str:
    if phase == 3:
        return "вдома"
    return "письмово" if a_type == "cloze" else "усно"


def _engine_out_root() -> Path | None:
    """Writable root for pipeline artifacts (lesson.b1/ir.json diagnostics).

    The engine's default is `hramatka/engine/.out` INSIDE the package — fine in
    dev, but on a deploy host the release checkout is immutable (root-owned +
    systemd `ReadOnlyPaths`), so the bake dies with EACCES at artifact-write
    time (hit live on the pilot host 2026-07-13). Resolution order:
    `HRAMATKA_ENGINE_OUT_DIR` env → systemd `STATE_DIRECTORY`/engine-out →
    None (dev default, package `.out`).
    """
    explicit = os.environ.get("HRAMATKA_ENGINE_OUT_DIR")
    if explicit:
        return Path(explicit)
    state_dir = os.environ.get("STATE_DIRECTORY")
    if state_dir:
        # systemd may pass a colon-separated list; the unit declares one.
        return Path(state_dir.split(":", 1)[0]) / "engine-out"
    return None


def _prune_engine_out(
    root: Path,
    *,
    protected_names: set[str],
    now: datetime | None = None,
) -> None:
    """Remove only aged completed UUID artifact directories from the state root.

    The one-process runner permits one active bake. Its newly allocated UUID is
    always in ``protected_names``; callers can protect additional in-progress
    directories when needed. Non-UUID directories and symlinks are never part
    of this retention policy.
    """
    if not root.is_dir():
        return
    cutoff = (now or datetime.now(UTC)) - timedelta(days=_ENGINE_OUT_RETENTION_DAYS)
    for candidate in root.iterdir():
        if candidate.name in protected_names or candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            uuid.UUID(candidate.name)
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
        except (OSError, ValueError):
            continue
        if modified >= cutoff:
            continue
        try:
            shutil.rmtree(candidate)
        except OSError:
            # Diagnostics retention must not turn an otherwise healthy fresh
            # bake into a failure merely because an old directory is locked.
            continue


class EngineLessonBaker:
    """`LessonBaker` backed by the real engine pipeline (generator injectable)."""

    def __init__(
        self,
        *,
        generator=call_gemma,
        bundle: data.DataBundle | None = None,
        cache_dir: str | Path | None = None,
        store: Any | None = None,
    ) -> None:
        self._generator = generator
        self._resolved_bundle = bundle
        self._cache_dir = cache_dir
        self.store = store

    def resolve_data_bundle(self) -> data.DataBundle:
        """Resolve the digest-pinned input bundle once for readiness and bakes."""
        if self._resolved_bundle is None:
            # ``active_bundle`` resolves and verifies the production manifest on
            # first use, then caches it. Engine tests may intentionally install
            # an isolated fixture bundle through that same seam.
            self._resolved_bundle = data.active_bundle()
        return self._resolved_bundle

    def bake(
        self,
        anchor: str | dict,
        duration: int,
        focus: str | None,
        *,
        _allow_prompt_pack: bool = True,
    ) -> dict[str, Any]:
        """Run the engine and return a lu.lesson.v1 block template.

        The durable layer (`api.lesson.materialize_lesson`) binds id/anchor/
        timestamps/status; here we only produce `blocks` (+ `rejected`).
        """
        resolved_duration, fallback_kind = resolve_duration(B1, duration)
        if fallback_kind is not None:
            log.warning(
                "Invalid bake duration (%s); using the 60-minute B1 sizing plan.", fallback_kind
            )
        plan = phase_plan(B1, resolved_duration)
        prompt_pack_enabled = _allow_prompt_pack and prompt_pack.enabled()
        phase_count_plans = (
            _prompt_pack_candidate_count_plan(plan)
            if prompt_pack_enabled
            else _candidate_count_plan(plan)
        )
        bundle = self.resolve_data_bundle()
        out_root = _engine_out_root()
        shared_pack: dict[str, Any] | None = None
        precomputed_snapshot: dict[str, Any] | None = None
        precomputed_grounding: dict[str, Any] | None = None
        if prompt_pack_enabled:
            # One immutable snapshot/grounding/kit pass is shared by all three
            # phase workers.  It is local, deterministic, and content-hashed.
            try:
                with data.use_bundle(bundle):
                    precomputed_snapshot = pipeline.snapshot_anchor(anchor)
                    precomputed_grounding = retrieval.build_grounding_pack(
                        precomputed_snapshot["body_uk"], B1
                    )
                    precomputed_snapshot["lemmas"] = sorted(precomputed_grounding["lemmas"])
                    precomputed_snapshot["numerals"] = precomputed_grounding["numeral_inventory"]
                    shared_pack = prompt_pack.build_shared_input(
                        snapshot=precomputed_snapshot,
                        grounding=precomputed_grounding,
                        duration_minutes=resolved_duration,
                        focus=focus,
                        phase_count_plans=phase_count_plans,
                        visible_slots_by_phase=Counter(plan),
                    )
            except prompt_pack.PromptPackError:
                # The pack is an output-quality protocol, not an availability
                # dependency.  A deterministic preflight rejection must leave
                # the ordinary generator/gate pipeline available; all normal
                # gates still run on that fallback path.  Never log the error
                # text because it may name a source-derived condition.
                log.warning("Prompt-pack preflight rejected; using legacy generation path.")
                prompt_pack_enabled = False
                phase_count_plans = _candidate_count_plan(plan)
                precomputed_snapshot = None
                precomputed_grounding = None

        from hramatka.engine.providers import TelemetryContext, telemetry_ctx

        job_id = anchor.get("anchor_id") if isinstance(anchor, dict) else None
        phases_total = len(phase_count_plans)

        tel_ctx = TelemetryContext(
            job_id=job_id,
            store=self.store,
            phases_total=phases_total,
            calls_planned=phases_total,
            calls_done=0,
            phase=1,
            step="generation",
        )
        ctx_token = telemetry_ctx.set(tel_ctx)
        tel_ctx.update_progress_db()

        try:
            job_out = out_root / str(uuid.uuid4()) if out_root else None
            if job_out is not None:
                job_out.mkdir(parents=True)
                _prune_engine_out(out_root, protected_names={job_out.name})
                tel_ctx.trace_dir = job_out
            if fallback_kind is not None:
                tel_ctx.record_event(
                    {
                        "event": "duration_fallback",
                        "requested_duration_kind": fallback_kind,
                        "resolved_duration": resolved_duration,
                    }
                )
            if prompt_pack_enabled:
                tel_ctx.record_event(
                    {
                        "event": "prompt_pack_enabled",
                        "version": prompt_pack.PROMPT_PACK_VERSION,
                        "candidate_policy": "max(2, visible_slots), capped at 6",
                        "focus_status": shared_pack["lesson_plan"]["focus"]["status"],
                    }
                )

            # Select a primary once for this durable bake.  All independent
            # phase calls retain that primary and its opposite-provider
            # failover, preserving deterministic per-job routing semantics.
            generator_for_bake = getattr(self._generator, "for_bake", None)
            generator = generator_for_bake() if callable(generator_for_bake) else self._generator

            def run_phase(phase: int, count_plan: dict[str, int]):
                phase_ctx = tel_ctx.fork(phase=phase)
                phase_token = telemetry_ctx.set(phase_ctx)
                try:
                    # Context variables are not inherited by executor threads;
                    # keep the exact digest-pinned bundle scoped to this phase.
                    with data.use_bundle(bundle):
                        pack_context = (
                            prompt_pack.phase_context(shared_pack, phase=phase)
                            if shared_pack is not None
                            else None
                        )
                        return pipeline.run(
                            anchor,
                            level="B1",
                            pedagogy="ttt",
                            phase=str(phase),
                            types=list(count_plan),
                            generator=generator,
                            use_cache=False,
                            cache_dir=self._cache_dir,
                            out_dir=(job_out / f"phase-{phase}") if job_out else None,
                            count_plan=count_plan,
                            max_regeneration_attempts=_MAX_REGENERATION_ATTEMPTS,
                            prompt_pack_context=pack_context,
                            precomputed_snapshot=precomputed_snapshot,
                            precomputed_grounding=precomputed_grounding,
                        )
                finally:
                    telemetry_ctx.reset(phase_token)

            # The phase plans share only immutable input.  Regeneration stays
            # serial inside each ``pipeline.run`` because it depends on that
            # phase's first-batch gate deficits.
            phase_results: dict[int, Any] = {}
            with ThreadPoolExecutor(
                max_workers=len(phase_count_plans), thread_name_prefix="hramatka-phase"
            ) as executor:
                futures = {
                    phase: executor.submit(run_phase, phase, count_plan)
                    for phase, count_plan in phase_count_plans.items()
                }
                for phase in phase_count_plans:
                    phase_results[phase] = futures[phase].result()

            # pipeline.run degrades transport/parse failures to generation_error
            # rather than raising, so surface a safe, teacher-visible failure here.
            errored = {
                phase: result.generation_error
                for phase, result in phase_results.items()
                if result.generation_error
            }
            if errored:
                error_results = [
                    result for result in phase_results.values() if result.generation_error
                ]
                error_types = {
                    result.generation_error_type or "Unknown" for result in error_results
                }
                error_type = next(iter(error_types)) if len(error_types) == 1 else "mixed"
                if prompt_pack_enabled and error_types == {"PromptPackError"}:
                    # Prompt-pack validation is a quality enhancement.  If all
                    # phase responses violate that private envelope, retry the
                    # same bake through the ordinary generator/gate path rather
                    # than turning its stricter protocol into an outage.
                    log.warning("Prompt-pack response rejected; using legacy generation path.")
                    return self.bake(
                        anchor,
                        duration,
                        focus,
                        _allow_prompt_pack=False,
                    )
                if prompt_pack_enabled:
                    had_success = any(
                        (result.ready or result.review_required)
                        for result in phase_results.values()
                    )
                    if had_success:
                        # Partial: degrade only the errored phase(s); record durable telemetry.
                        # Composition + floor decide (FloorUnmetError non-blaming if below).
                        for phase, err in errored.items():
                            error_class = (err or "").split(":", 1)[0] if err else "Unknown"
                            tel_ctx.record_event(
                                {
                                    "event": "phase_generation_degraded",
                                    "phase": phase,
                                    "error_class": error_class,
                                }
                            )
                        # fall through; do not abort the bake
                    else:
                        # Total failure under pack: preserve classification
                        if any(
                            result.generation_error_type == "GeneratorUnavailable"
                            for result in error_results
                        ):
                            raise ProviderUnavailable(
                                "Bake failed: the lesson generator is unavailable.",
                                retry_exhausted=all(
                                    result.generation_retry_exhausted
                                    for result in error_results
                                    if result.generation_error_type == "GeneratorUnavailable"
                                ),
                            )
                        raise GenerationFailed(
                            _generation_failure_message(error_type),
                            generation_error_type=error_type,
                        )
                else:
                    # Legacy path (flag off): behavior UNCHANGED — any error aborts whole.
                    if any(
                        result.generation_error_type == "GeneratorUnavailable"
                        for result in error_results
                    ):
                        raise ProviderUnavailable(
                            "Bake failed: the lesson generator is unavailable.",
                            retry_exhausted=all(
                                result.generation_retry_exhausted
                                for result in error_results
                                if result.generation_error_type == "GeneratorUnavailable"
                            ),
                        )
                    raise GenerationFailed(
                        _generation_failure_message(error_type),
                        generation_error_type=error_type,
                    )

            if not any(result.ready or result.review_required for result in phase_results.values()):
                raise GenerationFailed(
                    "Bake produced no automatically includable activities from this anchor.",
                    generation_error_type="NoEligibleActivities",
                )

            slots_by_phase = Counter(plan)
            total_count_plan = sum(
                (Counter(count_plan) for count_plan in phase_count_plans.values()), Counter()
            )
            first_result = next(iter(phase_results.values()))
            anchor_snapshot = first_result.anchor
            floor = content_density.LESSON_FLOORS.get(resolved_duration)
            selected_by_phase = selector.select_composed_lesson(
                {phase: result.ready for phase, result in phase_results.items()},
                slots_by_phase=slots_by_phase,
                count_plan=total_count_plan,
                policy=selector.SelectorPolicy(
                    density_target=len(plan),
                    require_productive=bool(floor and floor.require_productive),
                ),
                anchor=anchor_snapshot,
                focus_context=(
                    prompt_pack.focus_selector_context(shared_pack)
                    if shared_pack is not None
                    else None
                ),
            )
            selected = [
                candidate
                for phase in sorted(slots_by_phase)
                for candidate in selected_by_phase[phase]
            ]
            blocks = [
                self._block(candidate, slot, phase)
                for slot, (phase, candidate) in enumerate(
                    (phase, candidate)
                    for phase in sorted(slots_by_phase)
                    for candidate in selected_by_phase[phase]
                )
            ]
            all_activities = [
                activity for result in phase_results.values() for activity in result.activities
            ]
            review_reserve = [
                activity for result in phase_results.values() for activity in result.review_required
            ]
            selected_identities = {_activity_identity(candidate) for candidate in selected}
            composition_reserve = [
                activity
                for result in phase_results.values()
                for activity in result.ready
                if _activity_identity(activity) not in selected_identities
            ]
            rejected = rejected_entries(all_activities)
            rejected.extend(
                reserve_entries(
                    review_reserve,
                    reason="review-required: retained for teacher review; never auto-included",
                )
            )
            if floor is not None and not content_density.meets_lesson_floor(
                selected,
                duration=resolved_duration,
                selected_by_phase=selected_by_phase,
            ):
                if content_density.source_lacks_lesson_evidence(anchor_snapshot):
                    raise FloorUnmetError(
                        content_density.THIN_SOURCE_UA_MESSAGE, blames_source=True
                    )
                raise FloorUnmetError(
                    "Bake failed: the lesson could not reach the minimum activity density.",
                    blames_source=False,
                )
            shortfall = len(blocks) < len(plan)
            if shortfall:
                rejected.extend(
                    reserve_entries(
                        composition_reserve,
                        reason="composition-reserve: not auto-included by lesson-wide policy",
                    )
                )
                _annotate_shortfall(rejected, blocks=blocks, planned=len(plan))
            # Anchor diagnostics stay in engine-out artifacts only; the pilot wire
            # lesson schema forbids fingerprint/diagnostics on anchor.
            anchor_body = anchor_snapshot["body_uk"]
            anchor_diagnostics = anchor_snapshot.get("diagnostics")
            if anchor_diagnostics is None:
                anchor_diagnostics = vesum_gate.anchor_baseline_diagnostics(anchor_body)

            tel_ctx.update_progress_db(step="assembly")
            tel_ctx.record_event(
                _composition_trace(
                    selected=selected,
                    reserve=[*review_reserve, *(composition_reserve if shortfall else [])],
                    planned=len(plan),
                )
            )

            return {
                "blocks": blocks,
                "rejected": rejected,
                "anchor_diagnostics": anchor_diagnostics,
                **(
                    {"focus_notice": shared_pack["lesson_plan"]["focus"]["notice_uk"]}
                    if shared_pack is not None
                    and shared_pack["lesson_plan"]["focus"].get("status") == "unsupported"
                    else {}
                ),
            }
        except prompt_pack.PromptPackError as exc:
            if prompt_pack_enabled:
                # `phase_context()` can reject an unavailable slot before
                # `pipeline.run()` has a result to classify.  Treat that just
                # like a pack preflight/response rejection: retain the gated
                # legacy generation path instead of failing the entire bake.
                log.warning("Prompt-pack phase setup rejected; using legacy generation path.")
                return self.bake(
                    anchor,
                    duration,
                    focus,
                    _allow_prompt_pack=False,
                )
            raise GenerationFailed(
                "Bake failed: prompt-pack preflight could not verify source material.",
                generation_error_type="PromptPackError",
            ) from exc
        except (data.DataConfigError, data.DataDriftError) as exc:
            # review-p46 nit 4: a misconfigured/drifted data bundle is a safe,
            # teacher-visible BakeError like any other bake failure — never a
            # raw stack trace, never a leaked path.
            raise GenerationFailed(
                "Bake failed: the lesson data bundle is unavailable or has drifted.",
                generation_error_type=type(exc).__name__,
            ) from exc
        finally:
            telemetry_ctx.reset(ctx_token)

    def _block(self, ir, slot: int, phase: int) -> dict:
        activity = ir.activity
        a_type = activity["type"]
        gates = sorted({c.gate for c in ir.gate_result.checks}) or ["gated"]
        # Tri-state -> block mark (Sol defect 1): review_required must block
        # auto-accept (warn + external_options), clean ships as ok.
        review = ir.gate_result.status == schema.GATE_REVIEW
        payload = _pilot_payload(activity)
        envelope = {
            "id": f"activity-{a_type}-{slot + 1}",
            "type": a_type,
            "title": _TITLES.get(a_type, "Завдання"),
            "level": "b1",
            "payload": payload,
            "answer_key": _answer_key(ir),
            "provenance": {"source": "generated", "generator": GEMMA_MODEL, "gates": gates},
        }
        note = (
            _teacher_note(ir)
            if review
            else "Згенеровано з опори; гейти чисті — звірте перед уроком."
        )
        real_key = _answer_key(ir)
        block_key = "Підтвердьте ключ разом з учителем."
        if a_type in (
            "true-false",
            "cloze",
            "match-up",
            "quiz",
            "mark-the-words",
            "fill-in",
            "error-correction",
        ):
            block_key = real_key
        elif a_type == "text-questions" and "model_answers" in real_key:
            block_key = real_key

        return {
            "id": f"block-{slot + 1}",
            "phase": phase,
            "type": a_type,
            "mode": _mode(phase, a_type),
            "activity": envelope,
            "answer_key": block_key,
            "mark": "warn" if review else "ok",
            "note": note,
            "edited": False,
            "provenance": {
                "source": "generated",
                "generator": GEMMA_MODEL,
                "gates": gates,
                "external_options": review,
            },
        }


_NOTE_REASONS = {
    "evidence_span": "опору не вдалося повністю підтвердити",
    "false_statement": "хибне твердження потребує підтвердження",
    "vesum_token": "є неперевірена або позначена словоформа",
    "numeral": "є попередження щодо числівника",
    "matchup_semantics": "зв’язок у парі потребує звірки",
    "matchup_left": "ліве слово не підтверджено опорою",
    "external_options": "є варіанти поза текстом опори",
    "cloze_answer": "відповідь у пропуску потребує звірки",
    "cloze_gap": "у вправі бракує коректного пропуску",
    "partition": "частину вправи вилучено після перевірки",
    "schema": "структура вправи потребує звірки",
}


def _reason_phrase(gate: str) -> str:
    """Short Ukrainian teacher-facing wording for a machine gate reason."""
    return _NOTE_REASONS.get(gate, "є попередження автоматичної перевірки")


def _teacher_note(ir) -> str:
    """Surface every warn/flag cause in the block the teacher actually sees."""
    warning_details: list[str] = []
    fallback_warning_gates: list[str] = []
    flagged_gates: list[str] = []
    for check in ir.gate_result.checks:
        if check.status == "warn":
            if check.detail and check.detail not in warning_details:
                warning_details.append(check.detail)
            elif check.gate and check.gate not in fallback_warning_gates:
                fallback_warning_gates.append(check.gate)
    for flag in ir.flagged:
        for reason in flag.get("reasons", []):
            gate = reason.get("gate")
            if isinstance(gate, str) and gate not in flagged_gates:
                flagged_gates.append(gate)
    # The block note is the only frozen, teacher-visible warning carrier. Keep
    # pipeline warning details verbatim so deficit-filled review candidates are
    # explicit about what the teacher is acknowledging.
    phrases = "; ".join(
        [
            *warning_details,
            *(_reason_phrase(gate) for gate in [*fallback_warning_gates, *flagged_gates]),
        ]
    )
    if not phrases:
        phrases = _reason_phrase("partition")
    return f"Згенеровано з опори; перевірте: {phrases} — підтвердьте перед прийняттям."


def _failed_gate(ir) -> str:
    """Return only a concrete failed gate name; never fabricate a vague cause."""
    for check in ir.gate_result.checks:
        if check.status == "fail" and check.gate:
            return check.gate
    return "unknown"


def rejected_entries(activities: list) -> list[dict]:
    """Per-item salvage visibility (Sol defect 3): every wholly-dropped activity
    AND every salvaged (per-item flagged) item surfaces as a `rejected[]` entry
    with a `gate-failed:<gate>` reason. Never all-or-nothing, never silent — a
    good sibling ships as a block while its bad twin is disclosed here."""
    out: list[dict] = []
    for index, ir in enumerate(activities, start=1):
        if ir.gate_result.status == schema.GATE_FAILED:
            activity_type = ir.activity.get("type") if isinstance(ir.activity, dict) else None
            if activity_type == "unknown" or activity_type not in PILOT_ACTIVITY_TYPES:
                continue
            out.append(
                {
                    "type": "gate-failed",
                    "activity": _rejected_activity_document(ir, index),
                    "reason": f"gate-failed:{_failed_gate(ir)}",
                }
            )
            continue
        for flag in ir.flagged:
            reasons = flag.get("reasons") or []
            gate = next(
                (
                    reason["gate"]
                    for reason in reasons
                    if isinstance(reason.get("gate"), str) and reason["gate"]
                ),
                "unknown",
            )
            out.append(
                {
                    "type": "gate-failed",
                    "activity": _rejected_activity_document(ir, index),
                    "reason": f"gate-failed:{gate}",
                }
            )
    return out


def reserve_entries(activities: list, *, reason: str) -> list[dict]:
    """Project non-visible candidates into the review/reserve tray.

    Review-required candidates are real activity documents, not invisible
    deficit fillers.  The frozen lesson contract models that tray as
    ``rejected[]``; retaining the activity lets a teacher inspect or restore
    it deliberately.
    """
    return [
        {
            "type": activity.activity["type"],
            "activity": _rejected_activity_document(activity, index),
            "reason": reason,
        }
        for index, activity in enumerate(activities, start=1)
    ]


def _annotate_shortfall(rejected: list[dict], *, blocks: list[dict], planned: int) -> None:
    """Make a clean-candidate deficit durable without inventing a fake block."""
    reason = f"shortfall: composed {len(blocks)} of {planned} clean activities"
    if rejected:
        rejected[0]["reason"] = f"{reason}; {rejected[0]['reason']}"
        return
    if not blocks:
        # The caller already rejects a completely empty generation bank.  This
        # branch is therefore defensive, and keeps the template shape honest.
        return
    # The pinned rejected-draft schema only accepts activity documents.  A
    # duplicate *notice* is the only legal carrier in the degenerate case in
    # which every generated activity is visible and no reserve exists.
    block = blocks[0]
    rejected.append(
        {
            "type": block["type"],
            "activity": block["activity"],
            "reason": f"shortfall-notice: {reason}; informational, not a restore candidate",
        }
    )


def _composition_trace(*, selected: list, reserve: list, planned: int) -> dict:
    """Emit whole-lesson type-presence telemetry without candidate content."""
    selected_counts = Counter(activity.activity["type"] for activity in selected)
    reserve_counts = Counter(activity.activity["type"] for activity in reserve)
    return {
        "event": "composition",
        "selected_type_counts": {
            activity_type: selected_counts[activity_type] for activity_type in PILOT_ACTIVITY_TYPES
        },
        "selected_present_types": sorted(selected_counts),
        "reserve_type_counts": {
            activity_type: reserve_counts[activity_type] for activity_type in PILOT_ACTIVITY_TYPES
        },
        "reserve_present_types": sorted(reserve_counts),
        "planned_blocks": planned,
        "composed_blocks": len(selected),
        "shortfall": len(selected) < planned,
    }


def _rejected_activity_document(ir, index: int) -> dict:
    """Wrap rejected pipeline activity data in the frozen activity document.

    The pipeline's activity is a payload, not the browser-facing
    ``ActivityDocument`` required by OpenAPI.  Rejections must remain viewable
    without emitting a private IR fragment or an unsupported synthetic type.
    """
    activity = ir.activity
    activity_type = activity["type"]
    return {
        "id": f"rejected-{activity_type}-{index}",
        "type": activity_type,
        "title": _TITLES.get(activity_type, "Завдання"),
        "level": "b1",
        "payload": _pilot_payload(activity),
        "answer_key": _answer_key(ir),
        "provenance": {
            "source": "generated",
            "generator": GEMMA_MODEL,
            "gates": ["gated"],
        },
    }


def bake_accounting(result) -> dict[str, int]:
    """Fate of every generated activity, as a partition (Sol defect 3):
    generated == shipped + flagged + rejected. `shipped` ship cleanly, `flagged`
    ship but salvaged >=1 item, `rejected` were dropped whole."""
    activities = result.activities
    rejected = [ir for ir in activities if ir.gate_result.status == schema.GATE_FAILED]
    shipped_all = [ir for ir in activities if ir.gate_result.status != schema.GATE_FAILED]
    flagged = [ir for ir in shipped_all if ir.flagged]
    shipped = [ir for ir in shipped_all if not ir.flagged]
    return {
        "generated": len(activities),
        "shipped": len(shipped),
        "flagged": len(flagged),
        "rejected": len(rejected),
    }
