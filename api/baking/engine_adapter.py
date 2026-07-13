"""Real engine adapter for the durable-job seam (`baking/port.LessonBaker`).

Runs the private slice-1 engine pipeline and composes a `lu.lesson.v1` block
template from its gate-passing activities. This lands WIRED to the seam but is
NOT yet the `create_app` default — the full mock→real swap (durable-job
plumbing, real generator, pedagogy validation) is a separate step. The
generator is injectable, so the pipeline runs offline in tests with a fake
generator and never touches the network here.

Wave 0 consumes the engine selector rather than reimplementing selection here:
only `ready` candidates become lesson blocks. `review_required` material stays
in the pipeline IR review tray and never becomes a warning block by accident;
rejected candidates and per-item salvage remain visible through `rejected[]`.
"""

from __future__ import annotations

import os
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from hramatka.engine import data, pipeline, registry, schema
from hramatka.engine.gates import vesum as vesum_gate
from hramatka.engine.generate import GEMMA_MODEL, call_gemma

from .port import BakeError

# TTT phase plan per duration → the phase of each composed block. Mirrors the
# visible-block budget the store enforces ({45:{1:2,2:3,3:1}}, ...).
_PHASE_PLAN: dict[int, list[int]] = {
    45: [1, 1, 2, 2, 2, 3],
    60: [1, 1, 1, 2, 2, 2, 2, 3, 3],
    90: [1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3],
}


def _answer_key(activity: dict) -> dict:
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
    return {"note": "answer key unavailable"}


_TITLES = {
    "true-false": "Перевірмо розуміння",
    "cloze": "Заповніть пропуски",
    "match-up": "Знайдіть пару",
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


class EngineLessonBaker:
    """`LessonBaker` backed by the real engine pipeline (generator injectable)."""

    def __init__(
        self,
        *,
        generator=call_gemma,
        bundle: data.DataBundle | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        self._generator = generator
        self._bundle = bundle
        self._cache_dir = cache_dir

    def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]:
        """Run the engine and return a lu.lesson.v1 block template.

        The durable layer (`api.lesson.materialize_lesson`) binds id/anchor/
        timestamps/status; here we only produce `blocks` (+ `rejected`).
        """
        del focus  # slice-1 generation does not branch on focus yet
        plan = _PHASE_PLAN.get(duration, _PHASE_PLAN[45])
        ctx = data.use_bundle(self._bundle) if self._bundle is not None else nullcontext()
        out_root = _engine_out_root()
        try:
            with ctx:
                result = pipeline.run(
                    anchor,
                    level="B1",
                    pedagogy="ttt",
                    generator=self._generator,
                    use_cache=False,
                    cache_dir=self._cache_dir,
                    out_dir=(out_root / uuid.uuid4().hex) if out_root else None,
                )
        except (data.DataConfigError, data.DataDriftError) as exc:
            # review-p46 nit 4: a misconfigured/drifted data bundle is a safe,
            # teacher-visible BakeError like any other bake failure — never a
            # raw stack trace, never a leaked path.
            raise BakeError(
                "Bake failed: the lesson data bundle is unavailable or has drifted."
            ) from exc

        # pipeline.run degrades transport/parse failures to generation_error
        # rather than raising, so surface a safe, teacher-visible failure here.
        if result.generation_error:
            raise BakeError("Bake failed: the lesson generator is unavailable.")

        if not result.ready:
            raise BakeError(
                "Bake produced no automatically includable activities from this anchor."
            )

        available = list(result.selected)
        blocks = []
        for slot, phase in enumerate(plan):
            selected_index = next(
                (
                    index
                    for index, candidate in enumerate(available)
                    if phase in registry.ACTIVITY_REGISTRY[candidate.activity["type"]].ttt_phases
                ),
                None,
            )
            if selected_index is None:
                raise BakeError(
                    "Bake produced too few distinct automatically includable activities "
                    f"for the TTT plan ({len(blocks)} of {len(plan)})."
                )
            blocks.append(self._block(available.pop(selected_index), slot, phase))
        # These diagnostics are required by the digest-pinned public lesson
        # schema.  The API resource projection omits them from the narrower
        # frozen browser wire representation.
        anchor_body = result.anchor["body_uk"]
        anchor_diagnostics = result.anchor.get("diagnostics")
        if anchor_diagnostics is None:
            anchor_diagnostics = vesum_gate.anchor_baseline_diagnostics(anchor_body)
        return {
            "blocks": blocks,
            "rejected": rejected_entries(result.activities),
            "anchor_diagnostics": anchor_diagnostics,
        }

    def _block(self, ir, slot: int, phase: int) -> dict:
        activity = ir.activity
        a_type = activity["type"]
        gates = sorted({c.gate for c in ir.gate_result.checks}) or ["gated"]
        # Tri-state -> block mark (Sol defect 1): review_required must block
        # auto-accept (warn + external_options), clean ships as ok.
        review = ir.gate_result.status == schema.GATE_REVIEW
        envelope = {
            "id": f"activity-{a_type}-{slot + 1}",
            "type": a_type,
            "title": _TITLES.get(a_type, "Завдання"),
            "level": "b1",
            "payload": activity,
            "answer_key": _answer_key(activity),
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
    reason_gates: list[str] = []
    for check in ir.gate_result.checks:
        if check.status == "warn" and check.gate not in reason_gates:
            reason_gates.append(check.gate)
    for flag in ir.flagged:
        for reason in flag.get("reasons", []):
            gate = reason.get("gate")
            if isinstance(gate, str) and gate not in reason_gates:
                reason_gates.append(gate)
    phrases = "; ".join(_reason_phrase(gate) for gate in reason_gates)
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
                    "activity": _rejected_activity_document(ir.activity, index),
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
                    "activity": _rejected_activity_document(ir.activity, index),
                    "reason": f"gate-failed:{gate}",
                }
            )
    return out


def _rejected_activity_document(activity: dict, index: int) -> dict:
    """Wrap rejected pipeline activity data in the frozen activity document.

    The pipeline's activity is a payload, not the browser-facing
    ``ActivityDocument`` required by OpenAPI.  Rejections must remain viewable
    without emitting a private IR fragment or an unsupported synthetic type.
    """
    activity_type = activity["type"]
    return {
        "id": f"rejected-{activity_type}-{index}",
        "type": activity_type,
        "title": _TITLES.get(activity_type, "Завдання"),
        "level": "b1",
        "payload": activity,
        "answer_key": _answer_key(activity),
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
