"""Atomic production adapter for the TeacherReadyDensity.v3 generation path.

The live baker performs one fixed sequence: deterministic v3 preflight,
gemma-phase-pack.v3.2 serialization, and v3 same-plan evaluation/repair.  It
does not import or select a legacy prompt, planner, density authority, or
repair loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from hramatka.engine import data, vendoring
from hramatka.engine.anchor_inventory_v3 import (
    DEGREE_60_SCHEDULE,
    inventory_for_group,
    inventory_from_anchor,
)
from hramatka.engine.density_evaluator_v3 import (
    MAX_REPAIR_ROUNDS,
    RepairRequest,
    ReplacementRequest,
    SlotError,
    evaluate_phase_with_repair,
)
from hramatka.engine.gates.vesum import is_anchor_verbatim
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
    one_slot_context,
    render_phase_prompt,
    validate_activity_purpose,
    validate_degree_lesson_plan,
    validate_distractor_adjacency,
    validate_elicitation_shape,
    validate_exemplar_contamination,
    validate_gap_construction,
    validate_non_revealing_sequence,
    validate_verbatim_answer_ban,
    validate_visible_writing_constraints,
)
from hramatka.engine.providers import TelemetryContext, telemetry_ctx
from hramatka.engine.teacher_ready_density_v3 import phase_shape_for
from hramatka.engine.transport import (
    GenerationUnparseable,
    GeneratorUnavailable,
    generator_model_id,
)
from hramatka.engine.unit_builders_v3 import BUILDERS

from ..store import canonical_json
from ..validation import cloze_markers_align
from .artifacts import bake_artifact_dir
from .port import FloorUnmetError, GenerationFailed, ProviderUnavailable

_ACTIVITY_SCHEMA = vendoring.read_json(vendoring.PILOT_LU_ACTIVITY, "lu.activity.v1.schema.json")
_ACTIVITY_VALIDATOR = Draft7Validator(_ACTIVITY_SCHEMA)
_RAW_PARSE_FAILURE_MAX_BYTES = 512 * 1024
_RAW_PARSE_FAILURE_TRUNCATION_MARKER = "\n...TRUNCATED\n"
_V3_TOP_LEVEL_KEYS = frozenset({"slots"})
_UKRAINIAN_WORD_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ'’]+")

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
_TEXT_QUESTION_GUIDANCE_LABELS = {
    "causal-clause.v1": "Причина",
    "purpose-clause.v1": "Мета",
    "temporal-clause.v1": "Часова умова",
    "licensed-vid-cause.v1": "Причина стану",
}


def _external_option_surfaces(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return learner choice surfaces that must be honest about outside text."""
    surfaces: list[str] = []
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            options = item.get("options")
            if isinstance(options, list):
                surfaces.extend(option for option in options if isinstance(option, str))
    blanks = payload.get("blanks")
    if isinstance(blanks, list):
        for blank in blanks:
            options = blank.get("options") if isinstance(blank, Mapping) else None
            if isinstance(options, list):
                surfaces.extend(option for option in options if isinstance(option, str))
    pairs = payload.get("pairs")
    if isinstance(pairs, list):
        for pair in pairs:
            if isinstance(pair, Mapping):
                surfaces.extend(
                    value
                    for key in ("left", "right")
                    if isinstance((value := pair.get(key)), str)
                )
    return tuple(surfaces)


def _has_external_options(payload: Mapping[str, Any], anchor_text: str | None) -> bool:
    surfaces = _external_option_surfaces(payload)
    return bool(
        anchor_text
        and surfaces
        and any(not is_anchor_verbatim(surface, anchor_text) for surface in surfaces)
    )

# Teacher-facing Ukrainian for the engine's flag verdict (#402), keyed by the
# rule-named ``CAUSE_VOCABULARY`` of the CURRENT evaluator/gate stack
# (re-derived from #406) plus the ``_dropped()``/shortfall wrapper causes.  Raw
# causes and validator class names must never reach a ``*_uk`` field.  Engine-
# internal rules (serialization exactness, raw contract, unregistered gates)
# deliberately share the generic row: their distinction means nothing to a
# teacher.  ``gap_construction`` matches without a colon — its cause may carry
# a rule-name suffix or arrive bare.
_FLAG_REASON_UK_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("density_shortfall:", "Вправа занадто коротка — недостатньо мовного матеріалу."),
    ("response_shape:", "Двигун не зміг створити цю вправу."),
    ("learner_facing_fields:", "У тексті для учнів є англійські позначки."),
    ("activity_binding:", "Вправа не відповідає запланованому матеріалу уроку."),
    (
        "exemplar_contamination:",
        "Вправа повторює шаблонний приклад замість власного змісту.",
    ),
    ("verbatim_answer_ban:", "У завданні видно правильну відповідь."),
    ("elicitation_shape:", "Завдання не сформульовано повним реченням."),
    ("gap_construction", "Пропуски у вправі побудовано неправильно."),
    ("distractor_adjacency:", "Варіанти відповідей у вправі неякісні."),
    ("non_revealing_sequence:", "Завдання повторюються або підказують відповіді."),
    ("activity_purpose:", "Завдання не перевіряє розуміння змісту."),
    (
        "short_writing_visible_constraints:",
        "У завданні на письмо не видно всіх обов'язкових умов.",
    ),
    ("serialization_exactness:", "Двигун виявив помилку в цій вправі."),
    ("raw_contract:", "Двигун виявив помилку в цій вправі."),
    ("deterministic_gate:", "Двигун виявив помилку в цій вправі."),
    ("not_repairable:", "Двигун вважає цю вправу неякісною."),
    (
        "repair_exhausted:",
        "Двигун кілька разів намагався виправити цю вправу, але не зміг.",
    ),
)
# Rendering-infra failures are not a quality judgment; their causes stay off
# the teacher copy.  When one is the freshest granular cause, the wrapper
# disposition (``repair_exhausted``/``not_repairable``) supplies the wording.
_INFRA_CAUSE_PREFIXES = ("repair_renderer:", "replacement_renderer:")
_FLAG_REASON_UK_FALLBACK = "Двигун виявив помилку в цій вправі."


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


def _is_degree_focus(focus: str | None) -> bool:
    if not isinstance(focus, str):
        return False
    normalized = focus.casefold()
    return (
        "компаратив" in normalized
        or "суперлатив" in normalized
        or ("ступен" in normalized and "порівнян" in normalized)
    )


def _scheduled_types(duration: int, focus: str | None = None) -> tuple[str, ...]:
    """Return the one deterministic v3 schedule for a supported phase shape."""
    if duration == 60 and _is_degree_focus(focus):
        return tuple(activity_type for _slot_id, activity_type, _role in DEGREE_60_SCHEDULE)
    by_duration = {
        45: (
            "quiz",
            "cloze",
            "match-up",
            "true-false",
            "text-questions",
            "short-writing",
        ),
        60: (
            "quiz",
            "cloze",
            "fill-in",
            "quiz",
            "match-up",
            "error-correction",
            "text-questions",
            "fill-in",
            "match-up",
            "short-writing",
        ),
        90: (
            "quiz",
            "cloze",
            "fill-in",
            "quiz",
            "match-up",
            "error-correction",
            "text-questions",
            "short-writing",
            "quiz",
            "cloze",
            "fill-in",
            "quiz",
        ),
    }
    return by_duration[duration]


def _lesson_slots(duration: int, focus: str | None = None) -> tuple[LessonSlot, ...]:
    shape = phase_shape_for(duration)
    scheduled = _scheduled_types(duration, focus)
    if len(scheduled) != sum(shape.phase_slots.values()):  # pragma: no cover - static guard
        raise RuntimeError("v3 schedule must fill the complete configured phase shape.")
    slots: list[LessonSlot] = []
    replacement_policy = {
        "match-up": ("quiz", "fill-in"),
    }
    index = 0
    for phase, count in sorted(shape.phase_slots.items()):
        for position in range(1, count + 1):
            slots.append(
                LessonSlot(
                    slot_id=f"P{phase}-A{position}",
                    phase=phase,
                    requested_type=scheduled[index],
                    replacement_types=replacement_policy.get(scheduled[index], ()),
                )
            )
            index += 1
    return tuple(slots)


def _slot_builders(slots: tuple[LessonSlot, ...]) -> Mapping[str, Callable[..., object]]:
    """Bind repeated types to distinct pre-certified anchor resource groups."""
    occurrence_by_slot_type: dict[tuple[str, str], int] = {}
    occurrences: Counter[str] = Counter()
    for slot in slots:
        activity_type = slot.requested_type
        occurrences[activity_type] += 1
        occurrence_by_slot_type[(slot.slot_id, activity_type)] = occurrences[activity_type]
    for slot in slots:
        for activity_type in slot.replacement_types:
            occurrences[activity_type] += 1
            occurrence_by_slot_type[(slot.slot_id, activity_type)] = occurrences[activity_type]

    def builder_for(activity_type: str) -> Callable[..., object]:
        def build(inventory: object, *, slot_id: str, phase: int) -> object:
            occurrence = occurrence_by_slot_type.get((slot_id, activity_type))
            if occurrence is None:
                raise ValueError("v3 preflight received an unknown scheduled slot.")
            selected = inventory_for_group(
                inventory,  # type: ignore[arg-type]
                activity_type=activity_type,
                group_number=occurrence,
            )
            return BUILDERS[activity_type](selected, slot_id=slot_id, phase=phase)

        return build

    return {activity_type: builder_for(activity_type) for activity_type in BUILDERS}


def _inventory_candidate_types(slots: tuple[LessonSlot, ...]) -> tuple[str, ...]:
    """Return the primary inventory lane in stable slot order."""
    return tuple(slot.requested_type for slot in slots)


def _inventory_replacement_types(slots: tuple[LessonSlot, ...]) -> tuple[str, ...]:
    """Return the optional replacement lane after all primary occurrences."""
    return tuple(activity_type for slot in slots for activity_type in slot.replacement_types)


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


def _canonicalize_redundant_payload_fields(
    parsed: object,
) -> tuple[dict[str, object], ...]:
    """Restore only schema-required copies already present in the answer key.

    Quiz ``correct`` indices and cloze ``answer`` strings are represented in
    both payload and answer key by the public activity contract.  A missing
    payload copy carries no independent pedagogical information, so restore it
    deterministically from the same indexed/same-ID key.  Supplied values are
    never overwritten; malformed or ambiguous keys remain untouched and fail
    the ordinary exact-binding gates.
    """
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("slots"), list):
        return ()
    events: list[dict[str, object]] = []
    for record in parsed["slots"]:
        if not isinstance(record, dict):
            continue
        slot_id = record.get("slot_id")
        activity = record.get("activity")
        if not isinstance(slot_id, str) or not isinstance(activity, dict):
            continue
        payload = activity.get("payload")
        answer_key = activity.get("answer_key")
        if not isinstance(payload, dict) or not isinstance(answer_key, Mapping):
            continue
        activity_type = payload.get("type")
        restored = 0
        field = ""
        if activity_type == "quiz":
            items = payload.get("items")
            key_items = answer_key.get("items")
            if isinstance(items, list) and isinstance(key_items, list):
                for index, item in enumerate(items):
                    if not isinstance(item, dict) or "correct" in item or index >= len(key_items):
                        continue
                    key_item = key_items[index]
                    correct = key_item.get("correct") if isinstance(key_item, Mapping) else None
                    key_index = key_item.get("index") if isinstance(key_item, Mapping) else None
                    if (
                        isinstance(key_index, int)
                        and not isinstance(key_index, bool)
                        and key_index == index
                        and isinstance(correct, int)
                        and not isinstance(correct, bool)
                    ):
                        item["correct"] = correct
                        restored += 1
                field = "payload.items[].correct"
        elif activity_type == "cloze":
            blanks = payload.get("blanks")
            key_blanks = answer_key.get("blanks")
            if isinstance(blanks, list) and isinstance(key_blanks, list):
                keyed: dict[int, str] = {}
                duplicate_ids: set[int] = set()
                for key_blank in key_blanks:
                    if not isinstance(key_blank, Mapping):
                        continue
                    blank_id = key_blank.get("id")
                    answer = key_blank.get("answer")
                    if not isinstance(blank_id, int) or isinstance(blank_id, bool):
                        continue
                    if not isinstance(answer, str) or not answer:
                        continue
                    if blank_id in keyed:
                        duplicate_ids.add(blank_id)
                    keyed[blank_id] = answer
                for blank in blanks:
                    if not isinstance(blank, dict) or "answer" in blank:
                        continue
                    blank_id = blank.get("id")
                    if (
                        not isinstance(blank_id, int)
                        or isinstance(blank_id, bool)
                        or blank_id in duplicate_ids
                        or blank_id not in keyed
                    ):
                        continue
                    blank["answer"] = keyed[blank_id]
                    restored += 1
                field = "payload.blanks[].answer"
        if restored:
            events.append(
                {
                    "event": "v3_redundant_field_canonicalized",
                    "slot_id": slot_id,
                    "activity_type": activity_type,
                    "field": field,
                    "count": restored,
                }
            )
    return tuple(events)


def _deterministic_quiz_replacement(request: ReplacementRequest) -> dict[str, object]:
    """Serialize a certified contextual quiz fallback without an LLM call."""
    if request.activity_type != "quiz":
        raise ValueError("Deterministic replacement supports only quiz plans.")
    items: list[dict[str, object]] = []
    key_items: list[dict[str, object]] = []
    unit_refs: list[dict[str, str]] = []
    for index, unit in enumerate(request.plan.units):
        if not unit.allowed_forms:
            raise ValueError("Certified quiz replacement lacks an answer form.")
        answer = unit.allowed_forms[0]
        distinctness = unit.distinctness
        raw_bank = distinctness.get("choice_bank")
        gap = distinctness.get("gap")
        if (
            not isinstance(raw_bank, Sequence)
            or isinstance(raw_bank, (str, bytes))
            or answer not in raw_bank
            or len(raw_bank) < 3
            or not isinstance(gap, Mapping)
        ):
            raise ValueError("Certified quiz replacement lacks its closed learner surface.")
        start = gap.get("start_offset")
        end = gap.get("end_offset")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or unit.rendering_surface[start:end] != answer
        ):
            raise ValueError("Certified quiz replacement gap is detached from its answer.")
        bank = list(raw_bank)
        correct = index % len(bank)
        options = [option for option in bank if option != answer]
        options.insert(correct, answer)
        items.append(
            {
                "question": unit.rendering_surface[:start] + "___" + unit.rendering_surface[end:],
                "options": options,
                "correct": correct,
            }
        )
        key_items.append({"index": index, "correct": correct})
        unit_refs.append({"unit_id": unit.unit_id})
    return {
        "slot_id": request.slot_id,
        "type": "quiz",
        "activity": {
            "payload": {
                "type": "quiz",
                "instruction": "Оберіть правильну форму.",
                "items": items,
            },
            "answer_key": {"items": key_items},
        },
        "serialized_units": unit_refs,
        "_generator_model_id": "hramatka-deterministic-v3",
    }


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
        if "detached from certified" in str(error):
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
    """Return the exact evidence surface allocated to each contextual unit."""
    units = kit.get("certified_units")
    if not isinstance(units, list):  # guarded by ``_certified_forms``
        raise ValueError("v3 type-kit has no certified units.")
    surfaces: list[str] = []
    for unit in units:
        surface = unit.get("rendering_surface") if isinstance(unit, Mapping) else None
        if not isinstance(surface, str) or not surface:
            raise ValueError("v3 contextual unit has no certified rendering surface.")
        surfaces.append(surface)
    return tuple(surfaces)


def _certified_choice_banks(kit: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Return each closed unit's exact VESUM-certified option bank."""
    units = kit.get("certified_units")
    if not isinstance(units, list):
        raise ValueError("v3 type-kit has no certified units.")
    banks: list[tuple[str, ...]] = []
    for unit in units:
        distinctness = unit.get("distinctness") if isinstance(unit, Mapping) else None
        raw_bank = distinctness.get("choice_bank") if isinstance(distinctness, Mapping) else None
        if raw_bank is None:
            banks.append(())
            continue
        if not isinstance(raw_bank, list) or len(raw_bank) < 3:
            raise ValueError("v3 contextual unit has no certified choice bank.")
        bank = tuple(item for item in raw_bank if isinstance(item, str) and item)
        if len(bank) != len(raw_bank) or len(bank) != len(set(bank)):
            raise ValueError("v3 contextual unit has an invalid certified choice bank.")
        banks.append(bank)
    return tuple(banks)


def _certified_gapped_surfaces(
    kit: Mapping[str, Any], answers: tuple[str, ...]
) -> tuple[str, ...]:
    """Return and independently verify each serialized learner gap surface."""
    units = kit.get("certified_units")
    if not isinstance(units, list) or len(units) != len(answers):
        raise ValueError("v3 type-kit has no certified gapped units.")
    gapped_surfaces: list[str] = []
    for unit, answer in zip(units, answers, strict=True):
        if not isinstance(unit, Mapping):
            raise ValueError("v3 type-kit has a malformed gapped unit.")
        surface = unit.get("rendering_surface")
        serialized = unit.get("gapped_rendering_surface")
        distinctness = unit.get("distinctness")
        gap = distinctness.get("gap") if isinstance(distinctness, Mapping) else None
        start = gap.get("start_offset") if isinstance(gap, Mapping) else None
        end = gap.get("end_offset") if isinstance(gap, Mapping) else None
        if (
            not isinstance(surface, str)
            or not isinstance(serialized, str)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or surface[start:end] != answer
        ):
            raise ValueError("v3 gapped surface is detached from its certified source span.")
        expected = surface[:start] + "___" + surface[end:]
        if serialized != expected:
            raise ValueError("v3 serialized gap is detached from its certified source span.")
        gapped_surfaces.append(serialized)
    return tuple(gapped_surfaces)


def _options_match_bank(options: object, bank: tuple[str, ...]) -> bool:
    return (
        isinstance(options, list)
        and len(options) == len(bank)
        and len(options) == len(set(options))
        and set(options) == set(bank)
    )


def _normalized_surface(value: str) -> str:
    return " ".join(value.split())


def _reconstructs_surface(
    learner_surface: object, certified_surface: str, answer: str, *, marker: str
) -> bool:
    """Return whether one marker restores the exact certified source surface."""
    return (
        isinstance(learner_surface, str)
        and learner_surface.count(marker) == 1
        and _normalized_surface(learner_surface.replace(marker, answer))
        == _normalized_surface(certified_surface)
    )


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
        surfaces = _certified_rendering_surfaces(kit)
        gapped_surfaces = _certified_gapped_surfaces(kit, primary)
        banks = _certified_choice_banks(kit)
        items = payload.get("items")
        key_items = answer_key.get("items")
        if (
            not isinstance(items, list)
            or not isinstance(key_items, list)
            or len(items) != len(primary)
            or len(key_items) != len(primary)
        ):
            raise ValueError("v3 quiz payload/answer_key count is detached from certified units.")
        for index, (item, form, surface, gapped_surface, bank) in enumerate(
            zip(items, primary, surfaces, gapped_surfaces, banks, strict=True)
        ):
            if not isinstance(item, Mapping) or not isinstance(item.get("options"), list):
                raise ValueError("v3 quiz payload is detached from certified units.")
            if bank and not _options_match_bank(item["options"], bank):
                raise ValueError("v3 quiz options are detached from certified choice bank.")
            if item.get("question") != gapped_surface:
                raise ValueError("v3 quiz question is detached from certified gapped surface.")
            if not _reconstructs_surface(item.get("question"), surface, form, marker="___"):
                raise ValueError("v3 quiz question is detached from certified rendering surface.")
            key_entry = key_items[index]
            if not isinstance(key_entry, Mapping):
                raise ValueError("v3 quiz answer key entry is malformed.")
            declared_correct = key_entry.get("correct")
            if not isinstance(declared_correct, int) or not (
                0 <= declared_correct < len(item["options"])
            ):
                raise ValueError("v3 quiz answer key correct index is invalid.")
            if item["options"][declared_correct] != form:
                raise ValueError("v3 quiz answer_key does not point to the certified form.")
            if item.get("correct") != declared_correct:
                raise ValueError("v3 quiz payload correct index disagrees with answer_key.")
        return

    if activity_type == "cloze":
        surfaces = _certified_rendering_surfaces(kit)
        banks = _certified_choice_banks(kit)
        blanks = payload.get("blanks")
        key_blanks = answer_key.get("blanks")
        if (
            not isinstance(blanks, list)
            or not isinstance(key_blanks, list)
            or len(blanks) != len(primary)
            or len(key_blanks) != len(primary)
        ):
            raise ValueError("v3 cloze payload/answer_key count is detached from certified units.")
        for index, (blank, form, bank) in enumerate(
            zip(blanks, primary, banks, strict=True), start=1
        ):
            if not isinstance(blank, Mapping) or not isinstance(blank.get("options"), list):
                raise ValueError("v3 cloze payload is detached from certified units.")
            if bank and not _options_match_bank(blank["options"], bank):
                raise ValueError("v3 cloze options are detached from certified choice bank.")
            if blank.get("id") != index:
                raise ValueError("v3 cloze blank id is detached from certified units.")
            key_blank = key_blanks[index - 1]
            if not isinstance(key_blank, Mapping) or key_blank.get("id") != index:
                raise ValueError("v3 cloze answer_key blank id is malformed.")
            if key_blank.get("answer") != form:
                raise ValueError("v3 cloze answer_key does not bind the certified form.")
            if form not in blank["options"]:
                raise ValueError("v3 cloze options do not contain the certified form.")
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("v3 cloze text is detached from certified rendering surfaces.")
        marked_surface = kit.get("marked_rendering_surface")
        if not isinstance(marked_surface, str) or text != marked_surface:
            raise ValueError("v3 cloze text does not copy its certified marked passage.")
        carrier_passage = " ".join(dict.fromkeys(surfaces))
        span_rows: list[tuple[int, int, int, str]] = []
        units = kit.get("certified_units")
        if not isinstance(units, list):
            raise ValueError("v3 cloze kit is detached from certified rendering surfaces.")
        for index, (unit, form) in enumerate(zip(units, primary, strict=True), start=1):
            distinctness = unit.get("distinctness") if isinstance(unit, Mapping) else None
            gap = distinctness.get("gap") if isinstance(distinctness, Mapping) else None
            start = gap.get("start_offset") if isinstance(gap, Mapping) else None
            end = gap.get("end_offset") if isinstance(gap, Mapping) else None
            if not isinstance(start, int) or not isinstance(end, int):
                raise ValueError("v3 cloze gap span is detached from certified units.")
            if carrier_passage[start:end] != form:
                raise ValueError("v3 cloze gap span is detached from certified units.")
            span_rows.append((start, end, index, form))
        expected_text = carrier_passage
        for start, end, index, _form in reversed(span_rows):
            expected_text = expected_text[:start] + f"{{{index}}}" + expected_text[end:]
        if marked_surface != expected_text:
            raise ValueError(
                "v3 cloze kit marked passage is detached from certified gap positions."
            )
        if _normalized_surface(text) != _normalized_surface(expected_text):
            raise ValueError("v3 cloze markers are detached from certified gap positions.")
        reconstructed = text
        for index, form in enumerate(primary, start=1):
            marker = f"{{{index}}}"
            if reconstructed.count(marker) != 1:
                raise ValueError("v3 cloze text is detached from certified rendering surfaces.")
            reconstructed = reconstructed.replace(marker, form)
        if _normalized_surface(reconstructed) != _normalized_surface(carrier_passage):
            raise ValueError("v3 cloze text is detached from certified rendering surfaces.")
        return

    if activity_type == "fill-in":
        surfaces = _certified_rendering_surfaces(kit)
        gapped_surfaces = _certified_gapped_surfaces(kit, primary)
        banks = _certified_choice_banks(kit)
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
        for item, form, key_form, surface, gapped_surface, bank in zip(
            items, primary, key_forms, surfaces, gapped_surfaces, banks, strict=True
        ):
            if not isinstance(item, Mapping) or not isinstance(item.get("options"), list):
                raise ValueError("v3 fill-in payload is detached from certified units.")
            if bank and not _options_match_bank(item["options"], bank):
                raise ValueError("v3 fill-in options are detached from certified choice bank.")
            if item.get("sentence") != gapped_surface:
                raise ValueError("v3 fill-in sentence is detached from certified gapped surface.")
            if not _reconstructs_surface(item.get("sentence"), surface, form, marker="___"):
                raise ValueError(
                    "v3 fill-in sentence is detached from certified rendering surface."
                )
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
        for index, (item, expected, statement) in enumerate(
            zip(items, expected_keys, primary, strict=True)
        ):
            if not isinstance(item, Mapping) or not isinstance(item.get("correct"), bool):
                raise ValueError("v3 true-false payload is detached from certified units.")
            if item.get("statement") != statement:
                raise ValueError("v3 true-false statement is detached from certified units.")
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

    if activity_type == "mark-the-words":
        target_words = payload.get("target_words")
        key_targets = answer_key.get("target_words")
        rendering_surfaces = _certified_rendering_surfaces(kit)
        merged_text = "\n".join(dict.fromkeys(rendering_surfaces))
        if payload.get("text") != merged_text:
            raise ValueError("v3 mark-the-words text is detached from certified units.")
        _bound_list(target_words, primary, label="mark-the-words target words")
        _bound_list(key_targets, primary, label="answer key")
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
        _bound_list(items, primary, label="error-correction source items")
        _bound_list(key_items, expected_keys, label="answer key")
        return

    if activity_type == "text-questions":
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != len(primary):
            raise ValueError("v3 text-questions payload count is detached from certified units.")
        if not all(isinstance(item, str) and item.strip() for item in items):
            raise ValueError("v3 text-questions items must be non-empty strings.")
        units = kit.get("certified_units")
        if not isinstance(units, list) or len(units) != len(items):
            raise ValueError("v3 text-questions kit is detached from certified units.")
        for item, unit in zip(items, units, strict=True):
            distinctness = unit.get("distinctness") if isinstance(unit, Mapping) else None
            frame = (
                distinctness.get("question_frame")
                if isinstance(distinctness, Mapping)
                else None
            )
            prefixes = frame.get("allowed_prefixes") if isinstance(frame, Mapping) else None
            normalized = item.strip().casefold()
            if (
                not isinstance(prefixes, list)
                or not prefixes
                or not any(
                    isinstance(prefix, str)
                    and (
                        normalized.startswith(prefix.strip().casefold())
                        and (
                            len(normalized) == len(prefix.strip())
                            or not normalized[len(prefix.strip())].isalnum()
                        )
                    )
                    for prefix in prefixes
                )
            ):
                raise ValueError(
                    "v3 text-question is detached from certified question frame."
                )
            answer_span = (
                distinctness.get("answer_span")
                if isinstance(distinctness, Mapping)
                else None
            )
            topic = (
                distinctness.get("question_topic")
                if isinstance(distinctness, Mapping)
                else None
            )
            if answer_span is not None or topic is not None:
                surface = unit.get("rendering_surface")
                allowed = unit.get("allowed_forms")
                start = (
                    answer_span.get("start_offset")
                    if isinstance(answer_span, Mapping)
                    else None
                )
                end = answer_span.get("end_offset") if isinstance(answer_span, Mapping) else None
                text = answer_span.get("text") if isinstance(answer_span, Mapping) else None
                if (
                    not isinstance(surface, str)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or start < 0
                    or end <= start
                    or surface[start:end] != text
                    or not isinstance(allowed, list)
                    or allowed != [text]
                    or not isinstance(topic, Mapping)
                    or not isinstance(topic.get("token_id"), str)
                    or not isinstance(topic.get("lemma"), str)
                ):
                    raise ValueError(
                        "v3 text-question answer span is detached from its certified source."
                    )
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
            if not _contains_form(prompt, fragment):
                raise ValueError(
                    f"v3 short-writing payload.prompt is missing certified constraint {fragment!r}."
                )
        return

    raise ValueError("v3 activity payload has no certified-unit binding rule.")


def _reason_class_for(block: Any) -> str:
    """Return the freshest granular cause for a non-accepted block (#402).

    ``_dropped()`` always appends its own wrapper cause last, so the classifying
    cause of the terminal attempt sits at ``errors[-2]`` whenever a pre-wrapper
    cause exists.  ``errors[-1]`` is a defensive fallback only: every dropped
    block carries at least its wrapper plus the terminal attempt's own cause.
    """
    errors = tuple(block.errors)
    if not errors:
        raise ValueError("A flagged block needs at least one recorded cause.")
    if len(errors) >= 2:
        return errors[-2].cause
    return errors[-1].cause


def _flag_reason_fields(block: Any) -> tuple[str, str]:
    """One source feeds both the wire ``engine_reason_class`` and the UK copy."""
    reason_class = _reason_class_for(block)
    lookup = reason_class
    if lookup.startswith(_INFRA_CAUSE_PREFIXES):
        # Infra failure is not a quality judgment; phrase the wrapper instead.
        lookup = tuple(block.errors)[-1].cause
    for prefix, reason_uk in _FLAG_REASON_UK_BY_PREFIX:
        if lookup.startswith(prefix):
            return reason_class, reason_uk
    return reason_class, _FLAG_REASON_UK_FALLBACK


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
            canonicalization_events = _canonicalize_redundant_payload_fields(parsed)
            context = telemetry_ctx.get()
            if context is not None:
                for event in canonicalization_events:
                    context.record_event(event)
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
        """Render a repair request containing only the immutable failed slot."""
        prior_errors_section = ""
        if prior_errors:
            prior_errors_section = (
                "=== V3 PRIOR REJECTION ERRORS (fix these) ===\n```json\n"
                + json.dumps(
                    [{"message": error.cause, "slot_id": error.slot_id} for error in prior_errors],
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
                    render_phase_prompt(one_slot_context(context, slot_id=slot_id)),
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
                slots = _lesson_slots(duration, focus)
                bundle = self.resolve_data_bundle()
                with data.use_bundle(bundle):
                    inventory = inventory_from_anchor(
                        anchor_text,
                        scheduled_types=_inventory_candidate_types(slots),
                        replacement_types=_inventory_replacement_types(slots),
                        duration_minutes=duration,
                        focus=focus,
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
                    try:
                        validate_degree_lesson_plan(preflight.allocation)
                    except ValueError as error:
                        raise FloorUnmetError(
                            "Bake failed: certified lesson plan did not meet "
                            "activity quality rules.",
                            blames_source=False,
                        ) from error
                    blocks: list[dict[str, Any]] = []
                    rejected: list[dict[str, Any]] = []
                    phase_evaluations: list[tuple[Any, str, int]] = []
                    raw_attempt_counter = [0]
                    raw_out_root = self._engine_out_dir
                    raw_bake_id = job_id if isinstance(job_id, str) else None
                    for phase in sorted({slot.phase for slot in preflight.allocation.slots}):
                        context.update_progress_db(phase=phase, step="generation")
                        # The evaluator independently recreates and hashes this same context.
                        phase_context = build_phase_context(
                            preflight.allocation, phase=phase, focus=focus
                        )
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
                                validate_non_revealing_sequence,
                                validate_activity_purpose,
                                validate_visible_writing_constraints,
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
                            focus=focus,
                        )
                        self._record_qualification_evaluation(phase, evaluated)
                        phase_evaluations.append((evaluated, initial_raw, initial_attempt))
                        for block in evaluated.blocks:
                            if block.accepted:
                                allocated = next(
                                    slot
                                    for slot in preflight.allocation.slots
                                    if slot.slot_id == block.slot_id
                                )
                                blocks.append(
                                    self._block(
                                        block,
                                        len(blocks),
                                        plan=allocated.plan,
                                        anchor_text=anchor_text,
                                    )
                                )
                                continue
                            rejected.append(self._flag_notice(block))
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
                        # A teacher-ready lesson has no failed slots.  Shipping
                        # a shape-valid but semantically rejected activity made
                        # the product report success for unusable lessons.
                        raise FloorUnmetError(
                            "Bake failed: v3 serialization did not produce every certified slot.",
                            blames_source=False,
                        )
                    context.update_progress_db(step="assembly")
                    return {"blocks": blocks, "rejected": rejected}
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
        if request.activity_type == "quiz":
            return _deterministic_quiz_replacement(request)
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
    def _block(
        evaluation: Any,
        index: int,
        *,
        plan: Any | None = None,
        anchor_text: str | None = None,
    ) -> dict[str, Any]:
        activity = evaluation.activity
        assert isinstance(activity, Mapping)
        payload = activity["payload"]
        answer_key = activity["answer_key"]
        assert isinstance(payload, Mapping) and isinstance(answer_key, Mapping)
        activity_type = evaluation.activity_type
        external_options = _has_external_options(payload, anchor_text)
        rendered_answer_key = dict(answer_key)
        if activity_type == "text-questions" and plan is not None:
            source_guidance: list[str] = []
            for unit in plan.units:
                answer_span = unit.distinctness.get("answer_span")
                answer = (
                    answer_span.get("text") if isinstance(answer_span, Mapping) else None
                )
                if not isinstance(answer, str) or not answer.strip():
                    answer = unit.rendering_surface
                if not isinstance(answer, str) or not answer.strip():
                    continue
                category = unit.distinctness.get("question_category")
                intent = unit.distinctness.get("question_intent")
                label = (
                    "Факт за текстом"
                    if category == "comprehension"
                    else _TEXT_QUESTION_GUIDANCE_LABELS.get(intent, "Явний зв'язок")
                    if category == "explanation_inference"
                    else "Критерій: реалістичне застосування думки"
                )
                source_guidance.append(f"{label}: {answer}")
            if source_guidance:
                source_guidance = list(dict.fromkeys(source_guidance))
                rendered_answer_key["guidance"] = "Орієнтири для вчителя:\n" + "\n".join(
                    f"{position}. {surface}"
                    for position, surface in enumerate(source_guidance, start=1)
                )
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
            "answer_key": rendered_answer_key,
            "provenance": {"source": "generated", "generator": model, "gates": ["v3"]},
        }
        outer_answer_key = dict(rendered_answer_key)
        if activity_type == "error-correction" and plan is not None:
            corrections: list[dict[str, str]] = []
            for unit in plan.units:
                wrong_sentence = unit.allowed_forms[0]
                source_sentence = unit.rendering_surface
                correction = unit.expected_key_or_rule.value
                if not isinstance(source_sentence, str):
                    continue
                wrong_words = _UKRAINIAN_WORD_RE.findall(wrong_sentence)
                source_words = _UKRAINIAN_WORD_RE.findall(source_sentence)
                if len(wrong_words) != len(source_words):
                    continue
                changed = [
                    position
                    for position, (wrong, source) in enumerate(
                        zip(wrong_words, source_words, strict=True)
                    )
                    if wrong.casefold() != source.casefold()
                ]
                if (
                    len(changed) != 1
                    or source_words[changed[0]].casefold() != correction.casefold()
                ):
                    continue
                corrections.append(
                    {
                        "sentence": wrong_sentence,
                        "error": wrong_words[changed[0]],
                        "correction": correction,
                    }
                )
            if len(corrections) != len(plan.units):
                raise ValueError(
                    "Certified error-correction plan could not produce its UI answer key."
                )
            outer_answer_key["corrections"] = corrections
        return {
            "id": f"block-{index + 1}",
            "phase": evaluation.phase,
            "type": activity_type,
            "mode": _mode(evaluation.phase, activity_type),
            "activity": envelope,
            "answer_key": outer_answer_key,
            "mark": "warn" if external_options else "ok",
            "note": (
                "Є варіанти поза текстом опори — звірте вправу перед уроком."
                if external_options
                else "Згенеровано з опори; гейти v3 пройдено."
            ),
            "edited": False,
            "provenance": {
                "source": "generated",
                "generator": model,
                "gates": ["v3"],
                "external_options": external_options,
            },
            "quality": "engine_ok",
            "flag_reason_uk": None,
            "flagged_content_hash": None,
            "engine_reason_class": None,
        }

    @staticmethod
    def _flagged_block(evaluation: Any, index: int) -> dict[str, Any] | None:
        """Mirror ``_block`` for a dropped slot's last shape-valid attempt (#402).

        Returns ``None`` when the attempt cannot legally render inline: the
        envelope must satisfy the same pinned pilot activity contract and cloze
        marker invariant that ``validate_lesson`` enforces on every block, and
        its payload must be the scheduled type.  Content is never fabricated to
        force an inline card — those drops stay in the rejected tray as a
        contentless notice.
        """
        record = evaluation.attempted_record
        if not isinstance(record, Mapping):
            return None
        activity = record.get("activity")
        if not isinstance(activity, Mapping):
            return None
        payload = activity.get("payload")
        answer_key = activity.get("answer_key")
        if not isinstance(payload, Mapping) or not isinstance(answer_key, Mapping):
            return None
        activity_type = evaluation.activity_type
        if activity_type not in _TITLES or payload.get("type") != activity_type:
            return None
        model = record.get("_generator_model_id")
        model = model if isinstance(model, str) and model else "unknown"
        envelope = {
            "id": f"activity-{activity_type}-{index + 1}",
            "type": activity_type,
            "title": _TITLES[activity_type],
            "level": "b1",
            "payload": dict(payload),
            "answer_key": dict(answer_key),
            # Truthful provenance: no v3 gate certified this content.
            "provenance": {"source": "generated", "generator": model, "gates": []},
        }
        if next(iter(_ACTIVITY_VALIDATOR.iter_errors(envelope)), None) is not None:
            return None
        if not cloze_markers_align(envelope):
            return None
        reason_class, reason_uk = _flag_reason_fields(evaluation)
        return {
            "id": f"block-{index + 1}",
            "phase": evaluation.phase,
            "type": activity_type,
            "mode": _mode(evaluation.phase, activity_type),
            "activity": envelope,
            "answer_key": dict(answer_key),
            "mark": "ok",
            "note": None,
            "edited": False,
            "provenance": {
                "source": "generated",
                "generator": model,
                "gates": [],
                "external_options": False,
            },
            "quality": "engine_flagged",
            "flag_reason_uk": reason_uk,
            # Frozen at flag time from the exact wire envelope bytes; feedback
            # staleness compares against this, never a recomputed hash.
            "flagged_content_hash": hashlib.sha256(
                canonical_json(envelope).encode("utf-8")
            ).hexdigest(),
            "engine_reason_class": reason_class,
        }

    @staticmethod
    def _flag_notice(evaluation: Any) -> dict[str, Any]:
        """Contentless rejected-tray notice for a drop with nothing safe to inline."""
        _reason_class, reason_uk = _flag_reason_fields(evaluation)
        return {"type": "flagged-notice", "activity": None, "reason": reason_uk}
