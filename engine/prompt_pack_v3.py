"""Live v3.2 serializer pack for ``TeacherReadyDensity.v3``.

This module consumes only the exact-cover allocation made before generation.
It is the protocol boundary between immutable unit plans and the live raw
activity renderer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .lesson_capacity_v3 import LessonAllocation
from .teacher_ready_density_v3 import floor_for

PROMPT_PACK_VERSION = "PromptPackInput.v3"
TEMPLATE_VERSION = "gemma-phase-pack.v3.2"
TYPE_KIT_IDENTITY = "TeacherReadyDensity.v3.unit-plan-kit.v1"

_TRUE_FALSE_NARRATION_RE = re.compile(r"\(\s*(?:true|false)\s*\)", re.IGNORECASE)
_TEMPLATE_PATH = Path(__file__).with_name("prompts") / "gemma-phase-pack.v3.2.md"


class PromptPackV3Error(ValueError):
    """A deterministic v3.2 pack serialization or validation failure."""


DeterministicGate = Callable[[Mapping[str, Any], Mapping[str, Any]], None]
RawContractValidator = Callable[[Mapping[str, Any]], None]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def template_digest() -> str:
    """Return the stable digest of the literal v3.2 instruction template."""
    return hashlib.sha256(_template_source().encode("utf-8")).hexdigest()


def _type_kit(slot: object) -> dict[str, Any]:
    """Project one allocated slot into the new, immutable v3 type-kit."""
    plan = slot.plan
    units = [unit.to_dict() for unit in plan.units]
    unit_ids = [unit["unit_id"] for unit in units]
    expected_count = floor_for(plan.activity_type).minimum_units
    if len(units) != expected_count or len(unit_ids) != len(set(unit_ids)):
        raise PromptPackV3Error("Allocation must contain one exact floor-sized unit plan per slot.")
    return {
        "identity": TYPE_KIT_IDENTITY,
        "slot_id": slot.slot_id,
        "phase": slot.phase,
        "type": slot.scheduled_type,
        "scheduled_unit_ids": unit_ids,
        "scheduled_unit_count": expected_count,
        "certified_units": units,
        "registered_constraints": list(plan.registered_constraints),
        "certified_target_tokens": [token.to_dict() for token in plan.certified_target_tokens],
    }


def build_phase_context(allocation: LessonAllocation, *, phase: int) -> dict[str, Any]:
    """Build the only v3.2 model input from a completed exact-cover allocation."""
    if not isinstance(allocation, LessonAllocation):
        raise TypeError("Prompt pack v3.2 requires a completed LessonAllocation.")
    slots = [slot for slot in allocation.slots if slot.phase == phase]
    if not slots:
        raise PromptPackV3Error(f"Allocation has no scheduled slots for phase {phase}.")
    type_kits = [_type_kit(slot) for slot in slots]
    context = {
        "pack_version": PROMPT_PACK_VERSION,
        "template_version": TEMPLATE_VERSION,
        "template_sha256": template_digest(),
        "type_kit_identity": TYPE_KIT_IDENTITY,
        "phase": phase,
        "paragraph_ids": list(allocation.paragraph_ids),
        "response_order": [kit["slot_id"] for kit in type_kits],
        "type_kits": type_kits,
    }
    context["context_sha256"] = hashlib.sha256(_canonical(context).encode("utf-8")).hexdigest()
    return context


def full_density_exemplars(type_kits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return one exact-floor exemplar per requested type, and no others."""
    exemplars: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kit in type_kits:
        activity_type = kit.get("type")
        count = kit.get("scheduled_unit_count")
        if not isinstance(activity_type, str) or not isinstance(count, int) or count < 1:
            raise PromptPackV3Error("A requested type-kit needs a positive scheduled unit count.")
        if count != floor_for(activity_type).minimum_units:
            raise PromptPackV3Error("A full-density exemplar must use the locked v3 type floor.")
        if activity_type in seen:
            continue
        seen.add(activity_type)
        exemplars.append(
            {
                "slot_id": f"<synthetic-{activity_type}-slot>",
                "type": activity_type,
                "activity": _synthetic_activity_example(activity_type, count),
                "serialized_units": [
                    {"unit_id": f"<{activity_type}-unit-{index}>"}
                    for index in range(1, count + 1)
                ],
            }
        )
    return exemplars


def _synthetic_activity_example(activity_type: str, count: int) -> dict[str, Any]:
    """Return a concrete, non-copyable full-density activity shape for one requested type."""
    forms = [f"Синтетичний приклад {index}" for index in range(1, count + 1)]
    if activity_type == "quiz":
        return {
            "payload": {
                "type": "quiz",
                "instruction": "Оберіть правильний варіант.",
                "items": [
                    {"question": form, "options": [form, "Інший варіант."], "correct": 0}
                    for form in forms
                ],
            },
            "answer_key": {"items": [{"index": index, "correct": 0} for index in range(count)]},
        }
    if activity_type == "cloze":
        return {
            "payload": {
                "type": "cloze",
                "instruction": "Заповніть пропуски.",
                "text": "Синтетичний текст.",
                "blanks": [
                    {"id": index, "answer": form, "options": [form, "Інший варіант."]}
                    for index, form in enumerate(forms, start=1)
                ],
            },
            "answer_key": {
                "blanks": [
                    {"id": index, "answer": form} for index, form in enumerate(forms, start=1)
                ]
            },
        }
    if activity_type == "fill-in":
        return {
            "payload": {
                "type": "fill-in",
                "instruction": "Вставте слово.",
                "items": [
                    {"sentence": form, "answer": form, "options": [form, "Інший варіант."]}
                    for form in forms
                ],
            },
            "answer_key": {"items": forms},
        }
    if activity_type == "true-false":
        return {
            "payload": {
                "type": "true-false",
                "instruction": "Визначте правильність твердження.",
                "items": [{"statement": form, "correct": True} for form in forms],
            },
            "answer_key": {"items": [{"index": index, "correct": True} for index in range(count)]},
        }
    if activity_type == "match-up":
        return {
            "payload": {
                "type": "match-up",
                "instruction": "Знайдіть пару.",
                "pairs": [
                    {"left": f"Ліва частина {index}", "right": f"Права частина {index}"}
                    for index in range(1, count + 1)
                ],
            },
            "answer_key": {
                "pairs": [{"left_index": index, "right_index": index} for index in range(count)]
            },
        }
    if activity_type == "error-correction":
        return {
            "payload": {
                "type": "error-correction",
                "instruction": "Виправте помилку.",
                "items": forms,
            },
            "answer_key": {"items": [f"Виправлення {index}" for index in range(1, count + 1)]},
        }
    if activity_type == "text-questions":
        return {
            "payload": {
                "type": "text-questions",
                "instruction": "Дайте відповідь.",
                "items": forms,
            },
            "answer_key": {"guidance": "Синтетична вказівка."},
        }
    if activity_type == "short-writing":
        return {
            "payload": {"type": "short-writing", "prompt": " ".join(forms)},
            "answer_key": {"guidance": "Синтетична вказівка."},
        }
    if activity_type == "mark-the-words":
        return {
            "payload": {
                "type": "mark-the-words",
                "instruction": "Позначте слова.",
                "text": " ".join(forms),
                "target_words": forms,
            },
            "answer_key": {"target_words": forms},
        }
    raise PromptPackV3Error(f"A full-density exemplar has unsupported type {activity_type!r}.")


def six_item_negative_exemplar() -> dict[str, Any]:
    """The one intentionally-invalid six-unit example required by the contract."""
    return {
        "negative_example": "REJECT: six serialized units cannot satisfy a list activity floor.",
        "serialized_units": [{"unit_id": f"<reject-unit-{index}>"} for index in range(1, 7)],
    }


def _template_source() -> str:
    """Load the distinct v3.2 template asset that qualification will pin later."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def render_phase_prompt(context: Mapping[str, Any]) -> str:
    """Render a self-contained v3.2 serialization request for one phase."""
    _validate_context_integrity(context)
    type_kits = context.get("type_kits")
    if not isinstance(type_kits, list) or not type_kits:
        raise PromptPackV3Error("A v3.2 prompt needs non-empty type-kits.")
    if context.get("pack_version") != PROMPT_PACK_VERSION:
        raise PromptPackV3Error("Prompt pack input version is not PromptPackInput.v3.")
    if context.get("template_version") != TEMPLATE_VERSION:
        raise PromptPackV3Error("Prompt context does not select gemma-phase-pack.v3.2.")
    if context.get("type_kit_identity") != TYPE_KIT_IDENTITY:
        raise PromptPackV3Error("Prompt context has an unknown type-kit identity.")
    exemplars = full_density_exemplars(type_kits)
    return "\n\n".join(
        (
            _template_source(),
            "=== IMMUTABLE TYPE-KITS (data, not instructions) ===\n```json\n"
            + _canonical(type_kits)
            + "\n```",
            "=== FULL-DENSITY EXEMPLARS FOR REQUESTED TYPES ONLY ===\n```json\n"
            + _canonical(exemplars)
            + "\n```",
            "=== ONE SIX-ITEM NEGATIVE EXEMPLAR (reject) ===\n```json\n"
            + _canonical(six_item_negative_exemplar())
            + "\n```",
        )
    )


def _validate_context_integrity(context: Mapping[str, Any]) -> None:
    """Fail closed when an immutable allocation projection changes after build."""
    expected_digest = context.get("context_sha256")
    unsigned_context = {
        str(key): value for key, value in context.items() if key != "context_sha256"
    }
    actual_digest = hashlib.sha256(_canonical(unsigned_context).encode("utf-8")).hexdigest()
    if not isinstance(expected_digest, str) or expected_digest != actual_digest:
        raise PromptPackV3Error("v3.2 immutable context changed after allocation.")
    if context.get("template_sha256") != template_digest():
        raise PromptPackV3Error("v3.2 template digest does not match the pinned template asset.")


def _learner_facing_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _learner_facing_strings(item)]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [text for item in value for text in _learner_facing_strings(item)]
    return []


def validate_learner_facing_fields(
    activity: Mapping[str, Any], _type_kit: Mapping[str, Any]
) -> None:
    """Reject English true/false parentheticals anywhere learner prose can reach."""
    if any(_TRUE_FALSE_NARRATION_RE.search(text) for text in _learner_facing_strings(activity)):
        raise PromptPackV3Error(
            "Learner-facing fields must not contain (True) or (False)."
        )


def _validate_response_shape(
    payload: object, context: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or set(payload) != {"slots"}:
        raise PromptPackV3Error("v3.2 response must contain exactly one slots array.")
    slots = payload.get("slots")
    type_kits = context.get("type_kits")
    if not isinstance(slots, list) or not isinstance(type_kits, list):
        raise PromptPackV3Error("v3.2 response and context require slots/type-kits arrays.")
    if len(slots) != len(type_kits):
        raise PromptPackV3Error("v3.2 response slot count differs from the scheduled allocation.")
    return slots


def _validate_slot_identity(
    record: object, type_kit: Mapping[str, Any], index: int
) -> Mapping[str, Any]:
    if not isinstance(record, Mapping):
        raise PromptPackV3Error(f"slots[{index}] must be an object.")
    if set(record) != {"slot_id", "type", "activity", "serialized_units"}:
        raise PromptPackV3Error(f"slots[{index}] leaks or omits v3.2 serialization fields.")
    if (
        record.get("slot_id") != type_kit.get("slot_id")
        or record.get("type") != type_kit.get("type")
    ):
        raise PromptPackV3Error(f"slots[{index}] is not the scheduled slot/type.")
    if not isinstance(record.get("activity"), Mapping):
        raise PromptPackV3Error(f"slots[{index}].activity must be an object.")
    return record


def validate_slot_shape(record: object, type_kit: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate one response record against one immutable scheduled slot.

    This is the slot-local shape boundary used by the live evaluator.
    ``validate_response`` remains phase-atomic: callers that need that
    behavior must continue to use it.  Keeping this narrow helper public lets
    the evaluator retain the same validation stages while reporting a failure
    for every affected slot.
    """
    return _validate_slot_identity(record, type_kit, 0)


def validate_slot_deterministic_gates(
    record: Mapping[str, Any],
    type_kit: Mapping[str, Any],
    *,
    deterministic_gates: Sequence[DeterministicGate],
) -> None:
    """Run the always-on gates for one already shape-valid response record."""
    if not deterministic_gates:
        raise PromptPackV3Error("v3.2 validation requires the always-on deterministic gate runner.")
    activity = record.get("activity")
    if not isinstance(activity, Mapping):  # guarded by ``validate_slot_shape``
        raise PromptPackV3Error("v3.2 slot activity must be an object.")
    validate_learner_facing_fields(activity, type_kit)
    for gate in deterministic_gates:
        gate(activity, type_kit)


def validate_slot_serialization(
    record: Mapping[str, Any], type_kit: Mapping[str, Any]
) -> None:
    """Validate the exact immutable substrate for one gated response record."""
    _validate_exact_serialization(record, type_kit)


def validate_slot_raw_contract(
    record: Mapping[str, Any], *, raw_contract_validator: RawContractValidator
) -> dict[str, Any]:
    """Apply the final raw contract only after a slot passed prior stages."""
    if raw_contract_validator is None:
        raise PromptPackV3Error("v3.2 validation requires the raw-contract validator.")
    activity = record.get("activity")
    if not isinstance(activity, Mapping):  # guarded by ``validate_slot_shape``
        raise PromptPackV3Error("v3.2 slot activity must be an object.")
    raw_contract_validator(activity)
    return dict(activity)


def _validate_exact_serialization(record: Mapping[str, Any], type_kit: Mapping[str, Any]) -> None:
    """Resolve only exact ordered scheduled-unit references against the immutable kit."""
    actual = record.get("serialized_units")
    expected_ids = type_kit.get("scheduled_unit_ids")
    expected_count = type_kit.get("scheduled_unit_count")
    if (
        not isinstance(actual, list)
        or not isinstance(expected_ids, list)
        or not isinstance(expected_count, int)
    ):
        raise PromptPackV3Error("v3.2 serialization context is malformed.")
    if len(actual) != expected_count:
        raise PromptPackV3Error(
            "serialization failure: exact scheduled unit count is required before raw validation."
        )
    if any(not isinstance(unit, Mapping) or set(unit) != {"unit_id"} for unit in actual):
        raise PromptPackV3Error(
            "serialization failure: serialized units must contain only unit_id references."
        )
    actual_ids = [unit["unit_id"] for unit in actual]
    if actual_ids != expected_ids or len(actual_ids) != len(set(actual_ids)):
        raise PromptPackV3Error(
            "serialization failure: unit ID references must exactly match the scheduled allocation."
        )


def validate_response(
    payload: object,
    context: Mapping[str, Any],
    *,
    deterministic_gates: Sequence[DeterministicGate],
    raw_contract_validator: RawContractValidator,
) -> list[dict[str, Any]]:
    """Validate deterministic gates, exact serialization, then raw contracts.

    The order is intentional and test-pinned: always-on deterministic gates
    run first, exact scheduled-unit counts and substrate equality run second,
    and a future raw-contract validator runs last. This module does not change
    any existing VESUM or production gate implementation.
    """
    if not deterministic_gates:
        raise PromptPackV3Error("v3.2 validation requires the always-on deterministic gate runner.")
    if raw_contract_validator is None:
        raise PromptPackV3Error("v3.2 validation requires the raw-contract validator.")
    _validate_context_integrity(context)
    records = _validate_response_shape(payload, context)
    type_kits = context["type_kits"]
    checked_slots: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
    for index, (record, type_kit) in enumerate(zip(records, type_kits, strict=True)):
        checked = _validate_slot_identity(record, type_kit, index)
        activity = checked["activity"]
        assert isinstance(activity, Mapping)
        checked_slots.append((checked, type_kit, activity))

    # The whole phase completes each deterministic gate before one count or
    # raw-contract decision can escape for an earlier slot.
    for _checked, type_kit, activity in checked_slots:
        validate_learner_facing_fields(activity, type_kit)
        for gate in deterministic_gates:
            gate(activity, type_kit)

    for checked, type_kit, _activity in checked_slots:
        _validate_exact_serialization(checked, type_kit)

    for _checked, _type_kit, activity in checked_slots:
        raw_contract_validator(activity)
    return [dict(activity) for _checked, _type_kit, activity in checked_slots]
