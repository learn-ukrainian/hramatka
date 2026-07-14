"""Real engine adapter for the durable-job seam (`baking/port.LessonBaker`).

Runs the private slice-1 engine pipeline and composes a `lu.lesson.v1` block
template from its gate-passing activities. This lands WIRED to the seam but is
NOT yet the `create_app` default — the full mock→real swap (durable-job
plumbing, real generator, pedagogy validation) is a separate step. The
generator is injectable, so the pipeline runs offline in tests with a fake
generator and never touches the network here.

Wave 0 consumes the engine selector rather than reimplementing selection here:
`ready` candidates always compose first. `review_required` material stays in
the pipeline IR review tray unless a phase still has a visible-slot deficit;
only then can it deliberately fill that deficit as an acknowledged warning
block. It never becomes a warning block by accident; rejected candidates and
per-item salvage remain visible through `rejected[]`.
"""

from __future__ import annotations

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
from hramatka.engine import data, pipeline, registry, schema
from hramatka.engine.gates import vesum as vesum_gate
from hramatka.engine.generate import GEMMA_MODEL, call_gemma

from .port import BakeError, ProviderUnavailable

# TTT phase plan per duration → the phase of each composed block. Mirrors the
# visible-block budget the store enforces ({45:{1:2,2:3,3:1}}, ...).
_PHASE_PLAN: dict[int, list[int]] = {
    45: [1, 1, 2, 2, 2, 3],
    60: [1, 1, 1, 2, 2, 2, 2, 3, 3],
    90: [1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3],
}

# Real-Gemma measurement and pilot bakes yielded roughly 60–75% gate-ready
# candidates. Use the conservative measured floor: a plan of N TTT slots asks
# for ceil(N / 0.60) candidates, so its expected ready pool is at least N.
# For example, the 45-minute six-slot plan asks for 10 candidates and expects
# six ready. Two targeted regeneration rounds remain a bounded safety net for
# variance: they improve recovery from a short bank without unbounded provider
# cost or an open-ended bake.
_READY_SURVIVAL_FLOOR = 0.60
_MAX_REGENERATION_ATTEMPTS = 2

# Keep diagnostics for fourteen days: this covers the daily-backup recovery
# investigation window while putting a fixed bound on the host's state volume.
_ENGINE_OUT_RETENTION_DAYS = 14


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


def _activity_identity(ir: Any) -> str:
    """Stable semantic identity used to keep composed visible blocks distinct."""
    import json

    return json.dumps(ir.activity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _preferred_phase_types(phases: list[int]) -> dict[int, tuple[str, ...]]:
    """Assign each phase-constrained pilot type to its earliest legal TTT slot."""
    requested_phases = set(phases)
    preferred: dict[int, list[str]] = {phase: [] for phase in sorted(requested_phases)}
    for activity_type in PILOT_ACTIVITY_TYPES:
        legal = set(registry.ACTIVITY_REGISTRY[activity_type].ttt_phases) & requested_phases
        if legal and not requested_phases.issubset(
            registry.ACTIVITY_REGISTRY[activity_type].ttt_phases
        ):
            preferred[min(legal)].append(activity_type)
    return {phase: tuple(types) for phase, types in preferred.items()}


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
            elif isinstance(item.get("sentence"), str):
                corrected.append(item["sentence"])
        return {"items": corrected}
    if a_type == "text-questions":
        key: dict[str, Any] = {
            "guidance": raw.get("teacher_guidance")
            or "Перевірте відповіді за текстом якоря.",
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
    payload = {key: value for key, value in activity.items() if key not in {"id", "title", "notes"}}
    if a_type == "cloze":
        # The engine's internal gap convention is {gap} / {{N}}; the frozen
        # activity contract (golden fixture) renders blanks as [___:N] matched
        # to blank ids. Without this conversion the player shows a literal
        # "{gap}" to the learner (deployed launch-gate find, 2026-07-13).
        text = payload.get("text", "")
        blank_ids = [b.get("id") for b in payload.get("blanks", [])]
        first_id = blank_ids[0] if blank_ids else 1
        text = text.replace("{gap}", f"[___:{first_id}]")
        text = re.sub(r"\{\{(\d+)\}\}", r"[___:\1]", text)
        payload["text"] = text
    return payload


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

    def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]:
        """Run the engine and return a lu.lesson.v1 block template.

        The durable layer (`api.lesson.materialize_lesson`) binds id/anchor/
        timestamps/status; here we only produce `blocks` (+ `rejected`).
        """
        del focus  # slice-1 generation does not branch on focus yet
        plan = _PHASE_PLAN.get(duration, _PHASE_PLAN[45])
        phase_count_plans = _candidate_count_plan(plan)
        bundle = self.resolve_data_bundle()
        out_root = _engine_out_root()

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
            if any(result.generation_error for result in phase_results.values()):
                if any(
                    (result.generation_error or "").startswith("GeneratorUnavailable:")
                    for result in phase_results.values()
                ):
                    raise ProviderUnavailable("Bake failed: the lesson generator is unavailable.")
                raise BakeError("Bake failed: the lesson generator is unavailable.")

            if not any(
                result.ready or result.review_required for result in phase_results.values()
            ):
                raise BakeError(
                    "Bake produced no automatically includable activities from this anchor."
                )

            preferred_by_phase = _preferred_phase_types(plan)
            seen_activities: set[str] = set()
            blocks = []
            for phase in phase_count_plans:
                slots = plan.count(phase)
                candidates = [
                    candidate
                    for candidate in phase_results[phase].ready
                    if _activity_identity(candidate) not in seen_activities
                ]
                selected: list[Any] = []
                for activity_type in preferred_by_phase[phase]:
                    candidate = next(
                        (
                            candidate
                            for candidate in candidates
                            if candidate.activity["type"] == activity_type
                        ),
                        None,
                    )
                    if candidate is not None:
                        selected.append(candidate)
                        candidates.remove(candidate)
                selected.extend(candidates[: max(0, slots - len(selected))])

                # The review tray is a deliberate, deficit-only recovery path:
                # ready candidates above keep their complete priority.  Apply the
                # same constrained-type preference to the review tray, but never
                # duplicate an already selected ready activity.
                if len(selected) < slots:
                    selected_identities = {_activity_identity(candidate) for candidate in selected}
                    review_candidates = [
                        candidate
                        for candidate in phase_results[phase].review_required
                        if _activity_identity(candidate) not in seen_activities
                        and _activity_identity(candidate) not in selected_identities
                    ]
                    for activity_type in preferred_by_phase[phase]:
                        if len(selected) >= slots:
                            break
                        candidate = next(
                            (
                                candidate
                                for candidate in review_candidates
                                if candidate.activity["type"] == activity_type
                            ),
                            None,
                        )
                        if candidate is not None:
                            selected.append(candidate)
                            review_candidates.remove(candidate)
                            selected_identities.add(_activity_identity(candidate))
                    for candidate in review_candidates:
                        if len(selected) >= slots:
                            break
                        identity = _activity_identity(candidate)
                        if identity in selected_identities:
                            continue
                        selected.append(candidate)
                        selected_identities.add(identity)
                if len(selected) < slots:
                    raise BakeError(
                        "Bake produced too few distinct automatically includable activities "
                        f"for TTT phase {phase} ({len(selected)} of {slots} candidates)."
                    )
                for candidate in selected:
                    seen_activities.add(_activity_identity(candidate))
                    blocks.append(self._block(candidate, len(blocks), phase))
            # Anchor diagnostics stay in engine-out artifacts only; the pilot wire
            # lesson schema forbids fingerprint/diagnostics on anchor.
            first_result = next(iter(phase_results.values()))
            anchor_body = first_result.anchor["body_uk"]
            anchor_diagnostics = first_result.anchor.get("diagnostics")
            if anchor_diagnostics is None:
                anchor_diagnostics = vesum_gate.anchor_baseline_diagnostics(anchor_body)

            tel_ctx.update_progress_db(step="assembly")

            return {
                "blocks": blocks,
                "rejected": rejected_entries(
                    [
                        activity
                        for result in phase_results.values()
                        for activity in result.activities
                    ]
                ),
                "anchor_diagnostics": anchor_diagnostics,
            }
        except (data.DataConfigError, data.DataDriftError) as exc:
            # review-p46 nit 4: a misconfigured/drifted data bundle is a safe,
            # teacher-visible BakeError like any other bake failure — never a
            # raw stack trace, never a leaked path.
            raise BakeError(
                "Bake failed: the lesson data bundle is unavailable or has drifted."
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
        return {
            "id": f"block-{slot + 1}",
            "phase": phase,
            "type": a_type,
            "mode": _mode(phase, a_type),
            "activity": envelope,
            "answer_key": "Підтвердьте ключ разом з учителем.",
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
