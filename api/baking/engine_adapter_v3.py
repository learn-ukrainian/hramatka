"""Atomic production adapter for the TeacherReadyDensity.v3 generation path.

The live baker performs one fixed sequence: deterministic v3 preflight,
gemma-phase-pack.v3.2 serialization, and v3 same-plan evaluation/repair.  It
does not import or select a legacy prompt, planner, density authority, or
repair loop.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from hramatka.engine import data, vendoring
from hramatka.engine.anchor_inventory_v3 import inventory_for_group, inventory_from_anchor
from hramatka.engine.density_evaluator_v3 import (
    MAX_REPAIR_ROUNDS,
    RepairRequest,
    ReplacementRequest,
    SlotError,
    evaluate_phase_with_repair,
)
from hramatka.engine.json_tolerance import extract_json, repair_split_envelope
from hramatka.engine.lesson_capacity_v3 import (
    AnchorParagraph,
    AnchorWindow,
    LessonSlot,
    preflight_lesson,
)
from hramatka.engine.prompt_pack_v3 import (
    RepairableSerializationError,
    _contains_form,
    build_phase_context,
    render_phase_prompt,
    validate_distractor_adjacency,
    validate_elicitation_shape,
    validate_exemplar_contamination,
    validate_gap_construction,
    validate_verbatim_answer_ban,
)
from hramatka.engine.providers import TelemetryContext, telemetry_ctx
from hramatka.engine.teacher_ready_density_v3 import phase_shape_for
from hramatka.engine.transport import (
    GenerationUnparseable,
    GeneratorUnavailable,
    generator_model_id,
)
from hramatka.engine.unit_builders_v3 import BUILDERS

from .artifacts import bake_artifact_dir
from .port import FloorUnmetError, GenerationFailed, ProviderUnavailable

_ACTIVITY_SCHEMA = vendoring.read_json(vendoring.PILOT_LU_ACTIVITY, "lu.activity.v1.schema.json")
_ACTIVITY_VALIDATOR = Draft7Validator(_ACTIVITY_SCHEMA)
_RAW_PARSE_FAILURE_MAX_BYTES = 512 * 1024
_RAW_PARSE_FAILURE_TRUNCATION_MARKER = "\n...TRUNCATED\n"
_V3_TOP_LEVEL_KEYS = frozenset({"slots"})

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


def _persist_raw_parse_failure(raw: str, out_dir: str | Path, attempt: int) -> None:
    """Keep failed v3 model output in the private engine artifact directory only.

    This mirrors the legacy prompt-pack behavior without importing its legacy
    generation graph into the production-only v3 adapter.
    """
    raw_path = out_dir / f"generation-raw-attempt{attempt}.txt"
    encoded = raw.encode("utf-8")
    if len(encoded) > _RAW_PARSE_FAILURE_MAX_BYTES:
        capped = encoded[:_RAW_PARSE_FAILURE_MAX_BYTES].decode("utf-8", errors="ignore")
        raw_path.write_text(capped + _RAW_PARSE_FAILURE_TRUNCATION_MARKER, encoding="utf-8")
        return
    raw_path.write_text(raw, encoding="utf-8")


def _mode(phase: int, activity_type: str) -> str:
    if phase == 3:
        return "вдома"
    return "письмово" if activity_type == "cloze" else "усно"


def _scheduled_types(duration: int) -> tuple[str, ...]:
    """Return the one deterministic v3 schedule for a supported phase shape."""
    by_duration = {
        45: (
            "quiz",
            "cloze",
            "fill-in",
            "true-false",
            "match-up",
            "error-correction",
            "text-questions",
            "short-writing",
        ),
        60: (
            "quiz",
            "cloze",
            "fill-in",
            "true-false",
            "match-up",
            "error-correction",
            "text-questions",
            "quiz",
            "short-writing",
            "cloze",
        ),
        90: (
            "quiz",
            "cloze",
            "fill-in",
            "true-false",
            "match-up",
            "error-correction",
            "text-questions",
            "short-writing",
            "quiz",
            "cloze",
            "fill-in",
            "true-false",
        ),
    }
    return by_duration[duration]


def _lesson_slots(duration: int) -> tuple[LessonSlot, ...]:
    shape = phase_shape_for(duration)
    scheduled = _scheduled_types(duration)
    if len(scheduled) != sum(shape.phase_slots.values()):  # pragma: no cover - static guard
        raise RuntimeError("v3 schedule must fill the complete configured phase shape.")
    slots: list[LessonSlot] = []
    index = 0
    for phase, count in sorted(shape.phase_slots.items()):
        for position in range(1, count + 1):
            slots.append(
                LessonSlot(
                    slot_id=f"P{phase}-A{position}",
                    phase=phase,
                    requested_type=scheduled[index],
                )
            )
            index += 1
    return tuple(slots)


def _slot_builders(slots: tuple[LessonSlot, ...]) -> Mapping[str, Callable[..., object]]:
    """Bind repeated types to distinct pre-certified anchor resource groups."""
    occurrence_by_slot: dict[str, int] = {}
    occurrences: Counter[str] = Counter()
    for slot in slots:
        occurrences[slot.requested_type] += 1
        occurrence_by_slot[slot.slot_id] = occurrences[slot.requested_type]

    def builder_for(activity_type: str) -> Callable[..., object]:
        def build(inventory: object, *, slot_id: str, phase: int) -> object:
            if slot_id not in occurrence_by_slot:
                raise ValueError("v3 preflight received an unknown scheduled slot.")
            selected = inventory_for_group(
                inventory,  # type: ignore[arg-type]
                activity_type=activity_type,
                group_number=occurrence_by_slot[slot_id],
            )
            return BUILDERS[activity_type](selected, slot_id=slot_id, phase=phase)

        return build

    return {activity_type: builder_for(activity_type) for activity_type in BUILDERS}


def _anchor_text(anchor: str | Mapping[str, object]) -> str:
    if isinstance(anchor, str):
        return anchor
    for key in ("body_uk", "text"):
        value = anchor.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise ValueError("A v3 bake needs a non-empty teacher anchor.")


def _parse_payload(raw: object) -> object:
    if not isinstance(raw, str):
        raise GenerationUnparseable("v3 serializer returned a non-string response.")
    parsed = extract_json(raw, preferred_keys=_V3_TOP_LEVEL_KEYS)
    if _is_v3_slots_payload(parsed):
        return parsed
    repaired = repair_split_envelope(raw, required_keys=_V3_TOP_LEVEL_KEYS)
    if repaired is not None and _is_v3_slots_payload(repaired[0]):
        return repaired[0]
    if parsed is not None:
        return parsed
    raise GenerationUnparseable("v3 serializer returned invalid JSON.")


def _is_v3_slots_payload(payload: object) -> bool:
    return (
        isinstance(payload, Mapping)
        and set(payload) == _V3_TOP_LEVEL_KEYS
        and isinstance(payload.get("slots"), list)
    )


def _activity_gate(activity: Mapping[str, Any], kit: Mapping[str, Any]) -> None:
    if set(activity) != {"answer_key", "payload"}:
        raise ValueError("v3 activity must contain exactly payload and answer_key.")
    payload = activity.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != kit.get("type"):
        raise ValueError("v3 activity payload is not the scheduled type.")
    if not isinstance(activity.get("answer_key"), Mapping):
        raise ValueError("v3 activity answer_key must be an object.")
    try:
        _bind_learner_payload_to_certified_units(activity, kit)
    except ValueError as error:
        # This is a model-side rendering of an already certified slot, rather
        # than an arbitrary content gate or an allocation failure.  Preserve
        # the immutable-plan-only repair contract by marking just this exact
        # substrate-binding class for the evaluator's bounded repair path.
        if "detached from certified units" in str(error):
            raise RepairableSerializationError(str(error)) from error
        raise


def _certified_forms(kit: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    units = kit.get("certified_units")
    if not isinstance(units, list) or not units:
        raise ValueError("v3 type-kit has no certified units to bind.")
    forms: list[tuple[str, ...]] = []
    for unit in units:
        if not isinstance(unit, Mapping) or not isinstance(unit.get("allowed_forms"), list):
            raise ValueError("v3 type-kit has malformed certified unit forms.")
        allowed = tuple(form for form in unit["allowed_forms"] if isinstance(form, str) and form)
        if len(allowed) != len(unit["allowed_forms"]) or not allowed:
            raise ValueError("v3 type-kit has invalid certified unit forms.")
        forms.append(allowed)
    return tuple(forms)


def _expected_keys(kit: Mapping[str, Any]) -> tuple[str, ...]:
    units = kit.get("certified_units")
    if not isinstance(units, list):  # guarded by ``_certified_forms``
        raise ValueError("v3 type-kit has no certified units.")
    values: list[str] = []
    for unit in units:
        rule = unit.get("expected_key_or_rule") if isinstance(unit, Mapping) else None
        value = rule.get("value") if isinstance(rule, Mapping) else None
        if not isinstance(value, str) or not value:
            raise ValueError("v3 type-kit has an invalid certified answer key.")
        values.append(value)
    return tuple(values)


def _bound_list(value: object, expected: tuple[object, ...], *, label: str) -> None:
    if not isinstance(value, list) or tuple(value) != expected:
        raise ValueError(f"v3 learner payload {label} is detached from certified units.")


def _certified_rendering_surfaces(kit: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the exact evidence surface allocated to each fill-in unit."""
    units = kit.get("certified_units")
    if not isinstance(units, list):  # guarded by ``_certified_forms``
        raise ValueError("v3 type-kit has no certified units.")
    surfaces: list[str] = []
    for unit in units:
        surface = unit.get("rendering_surface") if isinstance(unit, Mapping) else None
        if not isinstance(surface, str) or not surface:
            raise ValueError("v3 fill-in unit has no certified rendering surface.")
        surfaces.append(surface)
    return tuple(surfaces)


def _expected_truth_value(expected: str) -> bool:
    """Map a certified expected key/rule string to a true-false boolean."""
    return expected == "true"


def _bind_learner_payload_to_certified_units(
    activity: Mapping[str, Any], kit: Mapping[str, Any]
) -> None:
    """Require the graded answer_key to bind exactly to the certified substrate.

    Learner-facing prose is no longer the binding surface; the answer_key is.
    This gate verifies that every certified unit is represented by a graded
    answer and that closed items carry the certified form among their options.
    """
    payload = activity["payload"]
    answer_key = activity["answer_key"]
    assert isinstance(payload, Mapping) and isinstance(answer_key, Mapping)
    activity_type = payload["type"]
    forms = _certified_forms(kit)
    primary = tuple(item[0] for item in forms)
    expected_keys = _expected_keys(kit)

    if activity_type == "quiz":
        items = payload.get("items")
        key_items = answer_key.get("items")
        if (
            not isinstance(items, list)
            or not isinstance(key_items, list)
            or len(items) != len(primary)
            or len(key_items) != len(primary)
        ):
            raise ValueError("v3 quiz payload/answer_key count is detached from certified units.")
        for index, (item, form) in enumerate(zip(items, primary, strict=True)):
            if not isinstance(item, Mapping) or not isinstance(item.get("options"), list):
                raise ValueError("v3 quiz payload is detached from certified units.")
            key_entry = key_items[index]
            if not isinstance(key_entry, Mapping):
                raise ValueError("v3 quiz answer key entry is malformed.")
            declared_correct = key_entry.get("correct")
            if (
                not isinstance(declared_correct, int)
                or not (0 <= declared_correct < len(item["options"]))
            ):
                raise ValueError("v3 quiz answer key correct index is invalid.")
            if item["options"][declared_correct] != form:
                raise ValueError("v3 quiz answer_key does not point to the certified form.")
            if item.get("correct") != declared_correct:
                raise ValueError("v3 quiz payload correct index disagrees with answer_key.")
        return

    if activity_type == "cloze":
        blanks = payload.get("blanks")
        key_blanks = answer_key.get("blanks")
        if (
            not isinstance(blanks, list)
            or not isinstance(key_blanks, list)
            or len(blanks) != len(primary)
            or len(key_blanks) != len(primary)
        ):
            raise ValueError("v3 cloze payload/answer_key count is detached from certified units.")
        for index, (blank, form) in enumerate(zip(blanks, primary, strict=True), start=1):
            if not isinstance(blank, Mapping) or not isinstance(blank.get("options"), list):
                raise ValueError("v3 cloze payload is detached from certified units.")
            if blank.get("id") != index:
                raise ValueError("v3 cloze blank id is detached from certified units.")
            key_blank = key_blanks[index - 1]
            if not isinstance(key_blank, Mapping) or key_blank.get("id") != index:
                raise ValueError("v3 cloze answer_key blank id is malformed.")
            if key_blank.get("answer") != form:
                raise ValueError("v3 cloze answer_key does not bind the certified form.")
            if form not in blank["options"]:
                raise ValueError("v3 cloze options do not contain the certified form.")
        return

    if activity_type == "fill-in":
        items = payload.get("items")
        key_forms = answer_key.get("items")
        if (
            not isinstance(items, list)
            or not isinstance(key_forms, list)
            or len(items) != len(primary)
            or len(key_forms) != len(primary)
        ):
            raise ValueError(
                "v3 fill-in payload/answer_key count is detached from certified units."
            )
        for item, form, key_form in zip(items, primary, key_forms, strict=True):
            if not isinstance(item, Mapping) or not isinstance(item.get("options"), list):
                raise ValueError("v3 fill-in payload is detached from certified units.")
            if key_form != form:
                raise ValueError("v3 fill-in answer_key does not bind the certified form.")
            if form not in item["options"]:
                raise ValueError("v3 fill-in options do not contain the certified form.")
        return

    if activity_type == "true-false":
        items = payload.get("items")
        key_items = answer_key.get("items")
        if (
            not isinstance(items, list)
            or not isinstance(key_items, list)
            or len(items) != len(primary)
            or len(key_items) != len(primary)
        ):
            raise ValueError(
                "v3 true-false payload/answer_key count is detached from certified units."
            )
        for index, (item, expected) in enumerate(zip(items, expected_keys, strict=True)):
            if not isinstance(item, Mapping) or not isinstance(item.get("correct"), bool):
                raise ValueError("v3 true-false payload is detached from certified units.")
            key_entry = key_items[index]
            if not isinstance(key_entry, Mapping) or not isinstance(key_entry.get("correct"), bool):
                raise ValueError("v3 true-false answer_key entry is malformed.")
            expected_bool = _expected_truth_value(expected)
            if key_entry["correct"] != expected_bool:
                raise ValueError("v3 true-false answer_key does not bind the certified rule.")
            if item["correct"] != expected_bool:
                raise ValueError(
                    "v3 true-false payload correct value disagrees with certified rule."
                )
        return

    if activity_type == "match-up":
        pairs = payload.get("pairs")
        key_pairs = answer_key.get("pairs")
        if (
            not isinstance(pairs, list)
            or not isinstance(key_pairs, list)
            or len(pairs) != len(forms)
            or len(key_pairs) != len(forms)
        ):
            raise ValueError(
                "v3 match-up payload/answer_key count is detached from certified units."
            )
        for pair, allowed in zip(pairs, forms, strict=True):
            if (
                len(allowed) != 2
                or not isinstance(pair, Mapping)
                or (pair.get("left"), pair.get("right")) != allowed
            ):
                raise ValueError("v3 match-up payload is detached from certified units.")
        _bound_list(
            key_pairs,
            tuple({"left_index": index, "right_index": index} for index in range(len(forms))),
            label="answer key",
        )
        return

    if activity_type == "error-correction":
        items = payload.get("items")
        key_items = answer_key.get("items")
        if (
            not isinstance(items, list)
            or not isinstance(key_items, list)
            or len(items) != len(primary)
            or len(key_items) != len(primary)
        ):
            raise ValueError(
                "v3 error-correction payload/answer_key count is detached from certified units."
            )
        if not all(isinstance(item, str) and item.strip() for item in items):
            raise ValueError("v3 error-correction items must be non-empty strings.")
        _bound_list(key_items, expected_keys, label="answer key")
        return

    if activity_type == "text-questions":
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != len(primary):
            raise ValueError("v3 text-questions payload count is detached from certified units.")
        if not all(isinstance(item, str) and item.strip() for item in items):
            raise ValueError("v3 text-questions items must be non-empty strings.")
        return

    if activity_type == "short-writing":
        prompt = payload.get("prompt")
        guidance = answer_key.get("guidance")
        prompt_fragments = tuple(fragment for unit_forms in forms for fragment in unit_forms)
        if not isinstance(prompt, str) or not isinstance(guidance, str):
            raise ValueError(
                "v3 short-writing payload/answer_key is detached from certified constraints."
            )
        for fragment in prompt_fragments:
            if not _contains_form(guidance, fragment):
                raise ValueError(
                    f"v3 short-writing answer_key.guidance is missing certified form {fragment!r}."
                )
            if _contains_form(prompt, fragment):
                raise ValueError(
                    f"v3 short-writing payload.prompt contains certified form {fragment!r} "
                    "verbatim."
                )
        return

    raise ValueError("v3 activity payload has no certified-unit binding rule.")


def _raw_activity_contract(activity: Mapping[str, Any]) -> None:
    payload = activity["payload"]
    answer_key = activity["answer_key"]
    assert isinstance(payload, Mapping) and isinstance(answer_key, Mapping)
    activity_type = payload.get("type")
    if not isinstance(activity_type, str) or activity_type not in _TITLES:
        raise ValueError("v3 activity payload has an unsupported type.")
    document = {
        "id": f"activity-{activity_type}",
        "type": activity_type,
        "title": _TITLES[activity_type],
        "level": "b1",
        "payload": dict(payload),
        "answer_key": dict(answer_key),
        "provenance": {"source": "generated", "generator": "v3", "gates": ["v3"]},
    }
    error = next(iter(_ACTIVITY_VALIDATOR.iter_errors(document)), None)
    if error is not None:
        raise ValueError("v3 activity does not satisfy the pilot activity contract.")


class EngineLessonBaker:
    """Production-only v3 baker exposed by ``api.app`` after the cutover."""

    def __init__(
        self,
        *,
        generator: Callable[[str], str],
        bundle: data.DataBundle | None = None,
        cache_dir: str | Path | None = None,
        engine_out_dir: str | Path | None = None,
        store: Any | None = None,
        logical_generator_factory: Callable[[str], Callable[[str], str]] | None = None,
        logical_model_id: str | None = None,
    ) -> None:
        del cache_dir
        self._generator = generator
        self._resolved_bundle = bundle
        self._engine_out_dir = engine_out_dir
        self.store = store
        self._logical_generator_factory = logical_generator_factory
        self._logical_model_id = logical_model_id

    def for_logical_model(self, logical_model_id: str | None) -> EngineLessonBaker:
        if logical_model_id is None:
            return self
        if self._logical_generator_factory is None:
            raise ValueError("This v3 engine baker has no logical-model routing factory.")
        return EngineLessonBaker(
            generator=self._logical_generator_factory(logical_model_id),
            bundle=self._resolved_bundle,
            engine_out_dir=self._engine_out_dir,
            store=self.store,
            logical_generator_factory=self._logical_generator_factory,
            logical_model_id=logical_model_id,
        )

    def resolve_data_bundle(self) -> data.DataBundle:
        if self._resolved_bundle is None:
            self._resolved_bundle = data.active_bundle()
        return self._resolved_bundle

    @staticmethod
    def _generator_model_id(generator: object) -> str | None:
        """Return the actual model identity used for the last provider call.

        Generators set ``generator_model_id`` during the call.  Static
        attributes are a fallback for test callables that wrap a plain port.
        """
        model = generator_model_id.get(None)
        if isinstance(model, str) and model:
            return model
        model = getattr(generator, "model", None) or getattr(generator, "_model", None)
        if isinstance(model, str) and model:
            return model
        return None

    @staticmethod
    def _inject_model_provenance(payload: object, model_id: str | None) -> None:
        """Attach the generator identity to each response slot for the gate layer."""
        if model_id is None or not isinstance(payload, Mapping):
            return
        slots = payload.get("slots")
        if not isinstance(slots, list):
            return
        for record in slots:
            if isinstance(record, Mapping):
                record["_generator_model_id"] = model_id

    def _call_generator(
        self,
        prompt: str,
        *,
        raw_attempt_counter: list[int],
        raw_out_root: str | Path | None,
        raw_bake_id: str | None,
    ) -> tuple[object, str, int, str | None]:
        generator = (
            self._generator.for_bake() if hasattr(self._generator, "for_bake") else self._generator
        )
        raw: object = None
        try:
            raw_attempt_counter[0] += 1
            attempt = raw_attempt_counter[0]
            generator_model_id.set(None)
            raw = generator(prompt)
            model_id = self._generator_model_id(generator)
            parsed = _parse_payload(raw)
            self._inject_model_provenance(parsed, model_id)
            assert isinstance(raw, str)  # enforced by _parse_payload
            return parsed, raw, attempt, model_id
        except GeneratorUnavailable as error:
            raise ProviderUnavailable(str(error), retry_exhausted=error.retry_exhausted) from error
        except GenerationUnparseable as error:
            if isinstance(raw, str) and raw_out_root is not None:
                _persist_raw_parse_failure(
                    raw,
                    bake_artifact_dir(raw_out_root, bake_id=raw_bake_id),
                    raw_attempt_counter[0],
                )
            raise GenerationFailed(
                "Bake failed: v3 serializer response was not valid JSON.",
                generation_error_type=type(error).__name__,
            ) from error

    @staticmethod
    def _repair_prompt(
        context: Mapping[str, Any],
        *,
        mode: str,
        slot_id: str,
        repair_round: int | None,
        prior_errors: tuple[SlotError, ...],
    ) -> str:
        """Annotate a one-slot callback without changing the v3.2 pack body."""
        prior_errors_section = ""
        if prior_errors:
            prior_errors_section = (
                "=== V3 PRIOR REJECTION ERRORS (fix these) ===\n```json\n"
                + json.dumps(
                    [
                        {"message": error.cause, "slot_id": error.slot_id}
                        for error in prior_errors
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n```"
            )
        return "\n\n".join(
            tuple(
                item
                for item in (
                    render_phase_prompt(context),
                    prior_errors_section,
                    "=== V3 REPAIR REQUEST (metadata) ===\n```json\n"
                    + json.dumps(
                        {
                            "mode": mode,
                            "repair_round": repair_round,
                            "slot_id": slot_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n```",
                )
                if item
            )
        )

    @staticmethod
    def _one_slot_record(payload: object, slot_id: str) -> object:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("slots"), list):
            raise ValueError("v3 repair renderer returned no slots payload.")
        matches = [
            record
            for record in payload["slots"]
            if isinstance(record, Mapping) and record.get("slot_id") == slot_id
        ]
        if len(matches) != 1:
            raise ValueError("v3 repair renderer did not return exactly the requested slot.")
        return matches[0]

    def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]:
        del focus
        job_id = anchor.get("anchor_id") if isinstance(anchor, dict) else None
        context = TelemetryContext(
            job_id=job_id if isinstance(job_id, str) else None,
            store=self.store,
            phases_total=3,
            calls_planned=3,
            calls_done=0,
            phase=1,
            step="generation",
            logical_model_id=self._logical_model_id,
        )
        context_token = telemetry_ctx.set(context)
        context.update_progress_db()
        try:
            try:
                anchor_text = _anchor_text(anchor)
                slots = _lesson_slots(duration)
                bundle = self.resolve_data_bundle()
                with data.use_bundle(bundle):
                    inventory = inventory_from_anchor(
                        anchor_text, scheduled_types=tuple(slot.requested_type for slot in slots)
                    )
                    preflight = preflight_lesson(
                        AnchorWindow(
                            paragraphs=(AnchorParagraph("teacher-anchor", inventory),),
                            initial_start=0,
                            initial_end=0,
                        ),
                        duration_minutes=duration,
                        slots=slots,
                        builders=_slot_builders(slots),
                    )
                    if preflight.allocation is None:
                        context.record_event(
                            {"event": "v3_preflight", "outcome": "insufficient_anchor_capacity"}
                        )
                        raise FloorUnmetError(
                            "Bake failed: insufficient_anchor_capacity before generation.",
                            blames_source=False,
                        )
                    blocks: list[dict[str, Any]] = []
                    phase_evaluations: list[tuple[Any, str, int]] = []
                    raw_attempt_counter = [0]
                    raw_out_root = self._engine_out_dir
                    raw_bake_id = job_id if isinstance(job_id, str) else None
                    for phase in sorted({slot.phase for slot in preflight.allocation.slots}):
                        context.update_progress_db(phase=phase, step="generation")
                        # The evaluator independently recreates and hashes this same context.
                        phase_context = build_phase_context(preflight.allocation, phase=phase)
                        initial_payload, initial_raw, initial_attempt, _ = self._call_generator(
                            render_phase_prompt(phase_context),
                            raw_attempt_counter=raw_attempt_counter,
                            raw_out_root=raw_out_root,
                            raw_bake_id=raw_bake_id,
                        )
                        evaluated = evaluate_phase_with_repair(
                            preflight.allocation,
                            phase=phase,
                            payload=initial_payload,
                            deterministic_gates=(
                                _activity_gate,
                                validate_exemplar_contamination,
                                validate_verbatim_answer_ban,
                                validate_elicitation_shape,
                                validate_gap_construction,
                                validate_distractor_adjacency,
                            ),
                            raw_contract_validator=_raw_activity_contract,
                            repair_renderer=lambda request: self._render_repair(
                                request,
                                raw_attempt_counter=raw_attempt_counter,
                                raw_out_root=raw_out_root,
                                raw_bake_id=raw_bake_id,
                            ),
                            replacement_renderer=lambda request: self._render_replacement(
                                request,
                                raw_attempt_counter=raw_attempt_counter,
                                raw_out_root=raw_out_root,
                                raw_bake_id=raw_bake_id,
                            ),
                        )
                        self._record_qualification_evaluation(phase, evaluated)
                        phase_evaluations.append((evaluated, initial_raw, initial_attempt))
                        blocks.extend(
                            self._block(block, len(blocks))
                            for block in evaluated.blocks
                            if block.accepted
                        )
                    if any(
                        evaluated.disposition != "teacher_ready"
                        for evaluated, _initial_raw, _initial_attempt in phase_evaluations
                    ):
                        if raw_out_root is not None:
                            artifact_dir = bake_artifact_dir(raw_out_root, bake_id=raw_bake_id)
                            for evaluated, initial_raw, initial_attempt in phase_evaluations:
                                if evaluated.disposition != "teacher_ready":
                                    _persist_raw_parse_failure(
                                        initial_raw,
                                        artifact_dir,
                                        initial_attempt,
                                    )
                        raise FloorUnmetError(
                            "Bake failed: v3 serialization did not produce every certified slot.",
                            blames_source=False,
                        )
                    context.update_progress_db(step="assembly")
                    return {"blocks": blocks, "rejected": []}
            except (data.DataConfigError, data.DataDriftError) as error:
                raise GenerationFailed(
                    "Bake failed: the lesson data bundle is unavailable or has drifted.",
                    generation_error_type=type(error).__name__,
                ) from error
        finally:
            telemetry_ctx.reset(context_token)

    @staticmethod
    def _record_qualification_evaluation(phase: int, evaluated: Any) -> None:
        """Persist content-free proof from the same live evaluator result."""
        context = telemetry_ctx.get()
        if context is None:
            return
        accepted = [block for block in evaluated.blocks if block.accepted]
        phase_density = {
            str(item_phase): {
                "visible_blocks": sum(
                    block.phase == item_phase and block.accepted for block in evaluated.blocks
                ),
                "response_units": sum(
                    block.receipt.units
                    for block in accepted
                    if block.phase == item_phase and block.receipt is not None
                ),
            }
            for item_phase in (1, 2, 3)
        }
        generated = len(evaluated.blocks)
        ready = len(accepted)
        repair_attempts = tuple(evaluated.attempts[1:])
        for attempt_index, attempt in enumerate(evaluated.attempts):
            for block in attempt.blocks:
                if block.receipt is None:
                    continue
                context.record_qualification_slot_trace(
                    {
                        "slot_id": block.slot_id,
                        "phase": block.phase,
                        "type": block.activity_type,
                        "disposition": block.receipt.disposition,
                        "units": block.receipt.units,
                        "floor_met": block.receipt.floor_met,
                        "contract_version": block.receipt.contract_version,
                        "repair_rounds": min(attempt_index, MAX_REPAIR_ROUNDS),
                        "replacement_used": attempt_index > MAX_REPAIR_ROUNDS,
                        "unassigned_errors_count": len(attempt.unassigned_errors),
                    }
                )
        for block in evaluated.blocks:
            if block.disposition != "dropped" or block.receipt is None:
                continue
            context.record_qualification_slot_trace(
                {
                    "slot_id": block.slot_id,
                    "phase": block.phase,
                    "type": block.activity_type,
                    "disposition": "dropped",
                    "units": block.receipt.units,
                    "floor_met": block.receipt.floor_met,
                    "contract_version": block.receipt.contract_version,
                    "repair_rounds": MAX_REPAIR_ROUNDS,
                    "replacement_used": False,
                    "unassigned_errors_count": 0,
                }
            )
        for attempt_index, attempt in enumerate(repair_attempts, start=1):
            before = evaluated.attempts[attempt_index - 1]
            context.record_qualification_repair_trace(
                {
                    "outcome": "amended"
                    if any(block.accepted for block in attempt.blocks)
                    else "provider_failure",
                    "phase": phase,
                    "round": attempt_index,
                    "visible_blocks_before": sum(block.accepted for block in before.blocks),
                    "visible_blocks_after": sum(block.accepted for block in attempt.blocks),
                    "response_units_before": sum(
                        block.observed_units for block in before.blocks if block.accepted
                    ),
                    "response_units_after": sum(
                        block.observed_units for block in attempt.blocks if block.accepted
                    ),
                    "gate_drops": sum(not block.accepted for block in attempt.blocks),
                }
            )
        context.record_qualification_density_trace(
            {
                "stage": "initial",
                "phase_density": phase_density,
                "gate_outcomes_by_phase": {
                    str(item_phase): {
                        "requested": sum(block.phase == item_phase for block in evaluated.blocks),
                        "generated": generated if item_phase == phase else 0,
                        "ready": ready if item_phase == phase else 0,
                        "review": 0,
                        "dropped": generated - ready if item_phase == phase else 0,
                    }
                    for item_phase in (1, 2, 3)
                },
                "density_error_codes": ["v3_serialization"] if evaluated.errors else [],
                "repair_invocations": len(context.qualification_repair_traces),
            }
        )

    def _render_repair(
        self,
        request: RepairRequest,
        *,
        raw_attempt_counter: list[int],
        raw_out_root: str | Path | None,
        raw_bake_id: str | None,
    ) -> object:
        payload, _raw, _attempt, _model_id = self._call_generator(
            self._repair_prompt(
                request.prompt_context,
                mode="repair",
                slot_id=request.slot_id,
                repair_round=request.round,
                prior_errors=request.prior_errors,
            ),
            raw_attempt_counter=raw_attempt_counter,
            raw_out_root=raw_out_root,
            raw_bake_id=raw_bake_id,
        )
        return self._one_slot_record(payload, request.slot_id)

    def _render_replacement(
        self,
        request: ReplacementRequest,
        *,
        raw_attempt_counter: list[int],
        raw_out_root: str | Path | None,
        raw_bake_id: str | None,
    ) -> object:
        payload, _raw, _attempt, _model_id = self._call_generator(
            self._repair_prompt(
                request.prompt_context,
                mode="replacement",
                slot_id=request.slot_id,
                repair_round=None,
                prior_errors=(),
            ),
            raw_attempt_counter=raw_attempt_counter,
            raw_out_root=raw_out_root,
            raw_bake_id=raw_bake_id,
        )
        return self._one_slot_record(payload, request.slot_id)

    @staticmethod
    def _block(evaluation: Any, index: int) -> dict[str, Any]:
        activity = evaluation.activity
        assert isinstance(activity, Mapping)
        payload = activity["payload"]
        answer_key = activity["answer_key"]
        assert isinstance(payload, Mapping) and isinstance(answer_key, Mapping)
        activity_type = evaluation.activity_type
        # Truthful provenance only.  A missing generator identity is stamped as
        # "unknown" rather than the old silent GEMMA default that made every
        # block falsely claim google-ais/gemma-4-31b-it.
        model = getattr(evaluation, "generator", None) or "unknown"
        envelope = {
            "id": f"activity-{activity_type}-{index + 1}",
            "type": activity_type,
            "title": _TITLES[activity_type],
            "level": "b1",
            "payload": dict(payload),
            "answer_key": dict(answer_key),
            "provenance": {"source": "generated", "generator": model, "gates": ["v3"]},
        }
        return {
            "id": f"block-{index + 1}",
            "phase": evaluation.phase,
            "type": activity_type,
            "mode": _mode(evaluation.phase, activity_type),
            "activity": envelope,
            "answer_key": dict(answer_key),
            "mark": "ok",
            "note": "Згенеровано з опори; гейти v3 пройдено.",
            "edited": False,
            "provenance": {
                "source": "generated",
                "generator": model,
                "gates": ["v3"],
                "external_options": False,
            },
        }
