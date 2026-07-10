"""Real engine adapter for the durable-job seam (`baking/port.LessonBaker`).

Runs the private slice-1 engine pipeline and composes a `lu.lesson.v1` block
template from its gate-passing activities. This lands WIRED to the seam but is
NOT yet the `create_app` default — the full mock→real swap (durable-job
plumbing, real generator, pedagogy validation) is a separate step. The
generator is injectable, so the pipeline runs offline in tests with a fake
generator and never touches the network here.

Block mark carries the engine's tri-state gate verdict end-to-end (Sol defect
1): a `review_required` activity ships as `warn` (`external_options: true`) so
the acceptance gate blocks auto-accept until the teacher acknowledges it, while
a `clean` activity ships as `ok`. A `failed` activity never becomes a block —
it, and every per-item salvage, surface in `rejected[]` with a
`gate-failed:<gate>` reason (Sol defect 3): never all-or-nothing, never silent.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

from hramatka.engine import data, pipeline, schema
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
                {"id": b.get("id"), "answer": b.get("answer")}
                for b in activity.get("blanks", [])
            ]
        }
    if a_type == "match-up":
        return {
            "pairs": [
                {"left_index": i, "right_index": i}
                for i in range(len(activity.get("pairs", [])))
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

    def bake(self, anchor: str, duration: int, focus: str | None) -> dict[str, Any]:
        """Run the engine and return a lu.lesson.v1 block template.

        The durable layer (`api.lesson.materialize_lesson`) binds id/anchor/
        timestamps/status; here we only produce `blocks` (+ `rejected`).
        """
        del focus  # slice-1 generation does not branch on focus yet
        plan = _PHASE_PLAN.get(duration, _PHASE_PLAN[45])
        ctx = data.use_bundle(self._bundle) if self._bundle is not None else nullcontext()
        try:
            with ctx:
                result = pipeline.run(
                    anchor,
                    level="B1",
                    pedagogy="ttt",
                    generator=self._generator,
                    use_cache=False,
                    cache_dir=self._cache_dir,
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

        shipped = [
            ir for ir in result.activities if ir.gate_result.status != schema.GATE_FAILED
        ]
        if not shipped:
            raise BakeError(
                "Bake produced no gate-passing activities from this anchor."
            )

        blocks = [
            self._block(shipped[slot % len(shipped)], slot, phase)
            for slot, phase in enumerate(plan)
        ]
        return {"blocks": blocks, "rejected": rejected_entries(result.activities)}

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
            "Згенеровано з опори; є попередження гейтів — підтвердьте перед прийняттям."
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


def _failed_gate(ir) -> str:
    """The gate name of the first FAIL check on a dropped activity."""
    for c in ir.gate_result.checks:
        if c.status == "fail":
            return c.gate
    return "gated"


def rejected_entries(activities: list) -> list[dict]:
    """Per-item salvage visibility (Sol defect 3): every wholly-dropped activity
    AND every salvaged (per-item flagged) item surfaces as a `rejected[]` entry
    with a `gate-failed:<gate>` reason. Never all-or-nothing, never silent — a
    good sibling ships as a block while its bad twin is disclosed here."""
    out: list[dict] = []
    for ir in activities:
        if ir.gate_result.status == schema.GATE_FAILED:
            out.append(
                {
                    "type": "gate-failed",
                    "activity": ir.activity,
                    "reason": f"gate-failed:{_failed_gate(ir)}",
                }
            )
            continue
        for flag in ir.flagged:
            reasons = flag.get("reasons") or [{}]
            out.append(
                {
                    "type": "gate-failed",
                    "activity": flag["item"],
                    "reason": f"gate-failed:{reasons[0].get('gate', 'gated')}",
                }
            )
    return out


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
