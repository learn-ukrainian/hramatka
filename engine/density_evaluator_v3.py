"""Live per-slot evaluation and bounded repair for density v3.

This boundary consumes a completed :class:`LessonAllocation`.
Capacity, substitutions, and replacement eligibility were settled at t=0 by
``lesson_capacity_v3``; this module judges a model's serialization of that
immutable substrate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from .density_receipt_v3 import BlockDensityReceipt
from .lesson_capacity_v3 import AllocatedSlot, ConditionalReplacement, LessonAllocation
from .prompt_pack_v3 import (
    DeterministicGate,
    PromptPackV3Error,
    RawContractValidator,
    RuleNamedRejection,
    build_phase_context,
    validate_slot_deterministic_gates,
    validate_slot_raw_contract,
    validate_slot_serialization,
    validate_slot_shape,
)
from .teacher_ready_density_v3 import floor_for
from .unit_plan_v3 import UnitPlan

MAX_REPAIR_ROUNDS: int = 4

BlockDisposition = Literal["ready", "tray", "density_shortfall", "failed", "dropped"]
LessonDisposition = Literal["teacher_ready", "recoverable_draft"]
RepairRenderer = Callable[["RepairRequest"], object]
ReplacementRenderer = Callable[["ReplacementRequest"], object]


CAUSE_VOCABULARY: dict[str, str] = {
    "response_shape": "response_shape: exact_scheduled_slot_response",
    "learner_facing_fields": "learner_facing_fields: ukrainian_boolean_labels",
    "activity_binding": "activity_binding: certified_unit_binding",
    "exemplar_contamination": "exemplar_contamination: no_synthetic_or_raw_certified_content",
    "verbatim_answer_ban": "verbatim_answer_ban: answer_forms_excluded_from_learner_prose",
    "elicitation_shape": "elicitation_shape: composed_learner_context",
    "gap_construction": "gap_construction",
    "distractor_adjacency": "distractor_adjacency: vesum_adjacent_distractors",
    "distractor_repeated_token": "distractor_adjacency: error_correction_no_repeated_tokens",
    "serialization_exactness": "serialization_exactness: scheduled_unit_references",
    "raw_contract": "raw_contract: pilot_activity_schema",
    "repair_renderer": "repair_renderer: slot_response_unavailable",
    "replacement_renderer": "replacement_renderer: slot_response_unavailable",
    "unregistered_deterministic_gate": "deterministic_gate: unregistered_rule",
}

_REPAIRABLE_CAUSE_KEYS = frozenset(
    {
        "response_shape",
        "learner_facing_fields",
        "activity_binding",
        "exemplar_contamination",
        "verbatim_answer_ban",
        "elicitation_shape",
        "gap_construction",
        "distractor_adjacency",
        "distractor_repeated_token",
        "serialization_exactness",
        "raw_contract",
    }
)
_REPAIRABLE_CAUSE_PREFIXES = tuple(
    CAUSE_VOCABULARY[key].split(":", 1)[0] + ":" for key in _REPAIRABLE_CAUSE_KEYS
)


class _FrozenDict(dict[str, object]):
    """A JSON-serializable immutable mapping for a repair renderer's input."""

    def __init__(self, values: Mapping[str, object]) -> None:
        dict.__init__(self, values)

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("v3 repair context is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenList(list[object]):
    """A JSON-serializable immutable sequence for a repair renderer's input."""

    def __init__(self, values: Sequence[object]) -> None:
        list.__init__(self, values)

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("v3 repair context is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


@dataclass(frozen=True)
class SlotError:
    """One content-free, slot-addressable evaluation failure."""

    slot_id: str
    cause: str

    def __post_init__(self) -> None:
        if not self.slot_id.strip() or not self.cause.strip():
            raise ValueError("Slot errors need a slot ID and a deterministic cause.")

    def to_dict(self) -> dict[str, str]:
        return {"slot_id": self.slot_id, "cause": self.cause}


@dataclass(frozen=True)
class BlockEvaluation:
    """The independent disposition of one scheduled block attempt."""

    slot_id: str
    phase: int
    activity_type: str
    disposition: BlockDisposition
    observed_units: int
    receipt: BlockDensityReceipt | None
    activity: Mapping[str, Any] | None = None
    attempted_record: Mapping[str, Any] | None = None
    errors: tuple[SlotError, ...] = ()
    generator: str | None = None

    def __post_init__(self) -> None:
        if not self.slot_id.strip() or self.phase < 1 or self.observed_units < 0:
            raise ValueError("Block evaluations need a slot, phase, and non-negative unit count.")
        if self.attempted_record is not None and self.disposition not in {
            "density_shortfall",
            "failed",
            "dropped",
        }:
            raise ValueError(
                "Only non-accepted dispositions may carry the last shape-valid attempt."
            )
        floor = floor_for(self.activity_type)
        if self.disposition in {"ready", "tray"}:
            if self.activity is None or self.receipt is None or not self.receipt.floor_met:
                raise ValueError(
                    "Ready and tray evaluations require a floor-met activity and receipt."
                )
            if self.receipt.disposition != self.disposition:
                raise ValueError("Ready/tray evaluation and receipt dispositions must agree.")
        elif self.disposition == "density_shortfall":
            if self.activity is not None or self.receipt is None or self.receipt.floor_met:
                raise ValueError("Density shortfalls stay hidden and below their per-block floor.")
            if self.receipt.disposition != "density_shortfall":
                raise ValueError(
                    "Density shortfall evaluation and receipt dispositions must agree."
                )
        elif self.disposition == "dropped":
            if self.activity is not None or self.receipt is None:
                raise ValueError("Dropped slots require a content-free final receipt only.")
            if self.receipt.disposition != "dropped":
                raise ValueError("Dropped evaluation and receipt dispositions must agree.")
        elif self.disposition == "failed" and self.receipt is not None:
            raise ValueError("Non-final serialization failures do not issue a replacement receipt.")
        if self.receipt is not None and (
            self.receipt.phase != self.phase or self.receipt.activity_type != self.activity_type
        ):
            raise ValueError(
                "Block evaluation receipts must describe the same slot type and phase."
            )
        if self.disposition == "density_shortfall" and self.observed_units >= floor.minimum_units:
            raise ValueError("A density shortfall must be observed below its locked v3 floor.")
        object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def accepted(self) -> bool:
        return self.disposition in {"ready", "tray"}

    @property
    def repairable(self) -> bool:
        """Return whether a fixed-kit renderer can repair this model output.

        Every named bake-path cause is a deterministic property of a provider
        response, not an allocation or source-data failure. Re-rendering one
        slot from its unchanged certified plan is therefore safe. Preflight,
        bundle, and context failures remain outside this slot-level loop.
        """
        return self.disposition == "density_shortfall" or any(
            error.cause.startswith(_REPAIRABLE_CAUSE_PREFIXES)
            for error in self.errors
        )


@dataclass(frozen=True)
class PhaseEvaluation:
    """Independent outcomes for all submitted slots in one evaluation batch."""

    phase: int
    blocks: tuple[BlockEvaluation, ...]
    unassigned_errors: tuple[SlotError, ...] = ()

    def __post_init__(self) -> None:
        if self.phase < 1 or not self.blocks:
            raise ValueError("Phase evaluations need a phase and at least one block.")
        if any(block.phase != self.phase for block in self.blocks):
            raise ValueError("Every evaluation block must belong to its declared phase.")
        if len({block.slot_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("Evaluation block slot IDs must be unique.")
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "unassigned_errors", tuple(self.unassigned_errors))

    @property
    def receipts(self) -> tuple[BlockDensityReceipt, ...]:
        return tuple(block.receipt for block in self.blocks if block.receipt is not None)

    @property
    def errors(self) -> tuple[SlotError, ...]:
        return (
            *self.unassigned_errors,
            *(error for block in self.blocks for error in block.errors),
        )

    @property
    def accepted(self) -> tuple[BlockEvaluation, ...]:
        return tuple(block for block in self.blocks if block.accepted)

    @property
    def failed(self) -> tuple[BlockEvaluation, ...]:
        return tuple(block for block in self.blocks if not block.accepted)


@dataclass(frozen=True)
class RepairRequest:
    """One bounded repair request over the original immutable allocated plan."""

    round: int
    slot_id: str
    phase: int
    activity_type: str
    plan: UnitPlan
    prompt_context: Mapping[str, Any]
    prior_errors: tuple[SlotError, ...]

    def __post_init__(self) -> None:
        _validate_exact_plan(self.plan, self.slot_id, self.phase, self.activity_type)
        if self.round not in range(1, MAX_REPAIR_ROUNDS + 1) or not isinstance(
            self.prompt_context.get("context_sha256"), str
        ):
            raise ValueError(
                f"Repair requests need round 1..{MAX_REPAIR_ROUNDS} and "
                "a pinned v3.2 context digest."
            )
        object.__setattr__(self, "prior_errors", tuple(self.prior_errors))


@dataclass(frozen=True)
class ReplacementRequest:
    """A renderer request for one conditional plan certified at allocation time."""

    slot_id: str
    phase: int
    requested_type: str
    activity_type: str
    plan: UnitPlan
    prompt_context: Mapping[str, Any]

    def __post_init__(self) -> None:
        _validate_exact_plan(self.plan, self.slot_id, self.phase, self.activity_type)
        if not isinstance(self.prompt_context.get("context_sha256"), str):
            raise ValueError("Replacement requests need a pinned v3.2 context digest.")


@dataclass(frozen=True)
class RepairEvaluation:
    """Final slot outcomes plus the content-free record of every attempt."""

    phase: int
    blocks: tuple[BlockEvaluation, ...]
    attempts: tuple[PhaseEvaluation, ...]

    def __post_init__(self) -> None:
        if self.phase < 1 or not self.blocks or not self.attempts:
            raise ValueError(
                "Repair evaluation needs final blocks and at least the initial attempt."
            )
        if any(block.phase != self.phase for block in self.blocks):
            raise ValueError("Repair results must stay in their declared phase.")
        if len({block.slot_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("Repair result slot IDs must be unique.")
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "attempts", tuple(self.attempts))

    @property
    def disposition(self) -> LessonDisposition:
        return (
            "teacher_ready" if all(block.accepted for block in self.blocks) else "recoverable_draft"
        )

    @property
    def receipts(self) -> tuple[BlockDensityReceipt, ...]:
        attempted = tuple(receipt for attempt in self.attempts for receipt in attempt.receipts)
        dropped = tuple(
            block.receipt for block in self.blocks if block.disposition == "dropped"
        )
        return (*attempted, *dropped)

    @property
    def errors(self) -> tuple[SlotError, ...]:
        return (*(
            error for attempt in self.attempts for error in attempt.errors
        ), *(error for block in self.blocks for error in block.errors))


def evaluate_phase_response(
    allocation: LessonAllocation,
    *,
    phase: int,
    payload: object,
    deterministic_gates: Sequence[DeterministicGate],
    raw_contract_validator: RawContractValidator,
    tray_slot_ids: Sequence[str] = (),
) -> PhaseEvaluation:
    """Grade every ready/tray candidate independently after the gate stage.

    A sub-floor serialized list is deliberately classified before exact-ID
    validation as a hidden ``density_shortfall``.  It therefore cannot become
    ready or claim teacher-review tray credit, even when another block passes.
    """
    slots = _phase_slots(allocation, phase)
    context = build_phase_context(allocation, phase=phase)
    return _evaluate_payload(
        payload,
        context=context,
        slots=slots,
        deterministic_gates=deterministic_gates,
        raw_contract_validator=raw_contract_validator,
        tray_slot_ids=tray_slot_ids,
    )


def evaluate_phase_with_repair(
    allocation: LessonAllocation,
    *,
    phase: int,
    payload: object,
    deterministic_gates: Sequence[DeterministicGate],
    raw_contract_validator: RawContractValidator,
    repair_renderer: RepairRenderer,
    replacement_renderer: ReplacementRenderer | None = None,
    tray_slot_ids: Sequence[str] = (),
    max_repair_rounds: int = MAX_REPAIR_ROUNDS,
) -> RepairEvaluation:
    """Run at most max_repair_rounds same-plan repairs, then use only certified replacements.

    Both repair rounds are passed the same frozen prompt context object and the
    same original ``UnitPlan`` object for a slot.  The callback's output is
    regraded against that unchanged type-kit, so it cannot alter type, units,
    floors, or evidence.  Conditional replacements are the allocator's only
    post-exhaustion fallback; without one, the slot is dropped recoverably.
    """
    if repair_renderer is None:
        raise ValueError("Slice-5 repair requires a renderer for both bounded repair rounds.")
    slots = _phase_slots(allocation, phase)
    slot_by_id = {slot.slot_id: slot for slot in slots}
    context = build_phase_context(allocation, phase=phase)
    frozen_context = _freeze_context(context)
    initial = _evaluate_payload(
        payload,
        context=context,
        slots=slots,
        deterministic_gates=deterministic_gates,
        raw_contract_validator=raw_contract_validator,
        tray_slot_ids=tray_slot_ids,
    )
    attempts: list[PhaseEvaluation] = [initial]
    final = {block.slot_id: block for block in initial.blocks}
    pending = []
    for block in initial.failed:
        if block.repairable:
            pending.append(block.slot_id)
        else:
            final[block.slot_id] = _dropped(block, block.slot_id, reason="not_repairable")

    for repair_round in range(1, max_repair_rounds + 1):
        if not pending:
            break
        repair_slots = tuple(slot_by_id[slot_id] for slot_id in pending)
        records: list[object] = []
        renderer_errors: dict[str, tuple[SlotError, ...]] = {}
        for slot_id in pending:
            previous = final[slot_id]
            request = RepairRequest(
                round=repair_round,
                slot_id=slot_id,
                phase=phase,
                activity_type=previous.activity_type,
                plan=slot_by_id[slot_id].plan,
                prompt_context=frozen_context,
                prior_errors=previous.errors,
            )
            try:
                records.append(_repair_record(repair_renderer(request), slot_id))
            except Exception:  # renderer failures are recoverable and slot-local
                renderer_errors[slot_id] = (_rule_error(slot_id, "repair_renderer"),)
        attempt = _evaluate_payload(
            {"slots": records},
            context=context,
            slots=repair_slots,
            deterministic_gates=deterministic_gates,
            raw_contract_validator=raw_contract_validator,
            tray_slot_ids=_tray_ids_for_slots(tray_slot_ids, repair_slots),
        )
        attempt = _with_renderer_errors(
            attempt,
            previous_by_slot={slot_id: final[slot_id] for slot_id in pending},
            errors_by_slot=renderer_errors,
        )
        attempts.append(attempt)
        final.update({block.slot_id: block for block in attempt.blocks})
        pending = []
        for block in attempt.failed:
            if block.repairable:
                pending.append(block.slot_id)
            else:
                final[block.slot_id] = _dropped(block, block.slot_id, reason="not_repairable")

    for slot_id in pending:
        slot = slot_by_id[slot_id]
        replacement, replacement_attempts = _try_replacements(
            slot,
            allocation=allocation,
            deterministic_gates=deterministic_gates,
            raw_contract_validator=raw_contract_validator,
            replacement_renderer=replacement_renderer,
            tray_slot_ids=tray_slot_ids,
        )
        attempts.extend(replacement_attempts)
        if replacement is not None:
            final[slot_id] = replacement
            continue
        previous = final[slot_id]
        final[slot_id] = _dropped(previous, slot_id, reason="repair_exhausted")

    return RepairEvaluation(
        phase=phase,
        blocks=tuple(final[slot.slot_id] for slot in slots),
        attempts=tuple(attempts),
    )


def _evaluate_payload(
    payload: object,
    *,
    context: Mapping[str, Any],
    slots: Sequence[AllocatedSlot],
    deterministic_gates: Sequence[DeterministicGate],
    raw_contract_validator: RawContractValidator,
    tray_slot_ids: Sequence[str],
) -> PhaseEvaluation:
    if not deterministic_gates:
        raise ValueError("Slice-5 evaluation requires the always-on deterministic gate runner.")
    if raw_contract_validator is None:
        raise ValueError("Slice-5 evaluation requires the raw-contract validator.")
    phase = slots[0].phase
    expected_ids = tuple(slot.slot_id for slot in slots)
    tray_ids = frozenset(tray_slot_ids)
    if not tray_ids.issubset(expected_ids):
        raise ValueError("Teacher-review tray IDs must belong to the evaluated phase allocation.")
    type_kits = _type_kits(context, expected_ids)
    records, errors_by_slot, unassigned_errors = _records_for_slots(payload, expected_ids)
    checked: dict[str, Mapping[str, Any]] = {}

    for slot in slots:
        slot_id = slot.slot_id
        record = records.get(slot_id)
        if record is None:
            continue
        try:
            checked[slot_id] = validate_slot_shape(record, type_kits[slot_id])
        except Exception:
            errors_by_slot.setdefault(slot_id, []).append(_rule_error(slot_id, "response_shape"))

    # Preserve the pack's ordering contract: every deterministic gate completes
    # before this evaluator makes any count, substrate, or raw-contract decision.
    gated: dict[str, Mapping[str, Any]] = {}
    for slot in slots:
        slot_id = slot.slot_id
        record = checked.get(slot_id)
        if record is None:
            continue
        try:
            validate_slot_deterministic_gates(
                record, type_kits[slot_id], deterministic_gates=deterministic_gates
            )
            gated[slot_id] = record
        except RuleNamedRejection as error:
            errors_by_slot.setdefault(slot_id, []).append(
                _rule_error(slot_id, error.rule_key, suffix=error.suffix)
            )
        except Exception:
            errors_by_slot.setdefault(slot_id, []).append(
                _rule_error(slot_id, "unregistered_deterministic_gate")
            )

    serialized: dict[str, Mapping[str, Any]] = {}
    shortfalls: dict[str, BlockDensityReceipt] = {}
    observed_units: dict[str, int] = {slot.slot_id: 0 for slot in slots}
    for slot in slots:
        slot_id = slot.slot_id
        record = gated.get(slot_id)
        if record is None:
            continue
        observed = _serialized_unit_count(record)
        observed_units[slot_id] = observed
        floor = floor_for(slot.scheduled_type)
        if isinstance(record.get("serialized_units"), list) and observed < floor.minimum_units:
            shortfalls[slot_id] = BlockDensityReceipt(
                phase=slot.phase,
                activity_type=slot.scheduled_type,
                disposition="density_shortfall",
                units=observed,
                floor_met=False,
            )
            errors_by_slot.setdefault(slot_id, []).append(
                SlotError(
                    slot_id,
                    "density_shortfall: "
                    f"observed {observed} units below required {floor.minimum_units}",
                )
            )
            continue
        try:
            validate_slot_serialization(record, type_kits[slot_id])
            serialized[slot_id] = record
        except Exception:
            errors_by_slot.setdefault(slot_id, []).append(
                _rule_error(slot_id, "serialization_exactness")
            )

    activities: dict[str, Mapping[str, Any]] = {}
    for slot in slots:
        slot_id = slot.slot_id
        record = serialized.get(slot_id)
        if record is None:
            continue
        try:
            activities[slot_id] = validate_slot_raw_contract(
                record, raw_contract_validator=raw_contract_validator
            )
        except Exception:
            errors_by_slot.setdefault(slot_id, []).append(_rule_error(slot_id, "raw_contract"))

    blocks: list[BlockEvaluation] = []
    for slot in slots:
        slot_id = slot.slot_id
        errors = tuple(errors_by_slot.get(slot_id, ()))
        if slot_id in activities:
            disposition: BlockDisposition = "tray" if slot_id in tray_ids else "ready"
            receipt = BlockDensityReceipt(
                phase=slot.phase,
                activity_type=slot.scheduled_type,
                disposition=disposition,
                units=observed_units[slot_id],
                floor_met=True,
            )
            generator = serialized.get(slot_id, {}).get("_generator_model_id")
            generator = generator if isinstance(generator, str) and generator else None
            blocks.append(
                BlockEvaluation(
                    slot_id=slot_id,
                    phase=slot.phase,
                    activity_type=slot.scheduled_type,
                    disposition=disposition,
                    observed_units=observed_units[slot_id],
                    receipt=receipt,
                    activity=activities[slot_id],
                    errors=errors,
                    generator=generator,
                )
            )
        elif slot_id in shortfalls:
            blocks.append(
                BlockEvaluation(
                    slot_id=slot_id,
                    phase=slot.phase,
                    activity_type=slot.scheduled_type,
                    disposition="density_shortfall",
                    observed_units=observed_units[slot_id],
                    receipt=shortfalls[slot_id],
                    # This attempt's own shape-valid record (#402): captured only
                    # here and in the failed branch, never on ready/tray, and
                    # never accumulated across rounds.
                    attempted_record=checked.get(slot_id),
                    errors=errors,
                )
            )
        else:
            blocks.append(
                BlockEvaluation(
                    slot_id=slot_id,
                    phase=slot.phase,
                    activity_type=slot.scheduled_type,
                    disposition="failed",
                    observed_units=observed_units[slot_id],
                    receipt=None,
                    attempted_record=checked.get(slot_id),
                    errors=errors,
                )
            )
    return PhaseEvaluation(
        phase=phase, blocks=tuple(blocks), unassigned_errors=tuple(unassigned_errors)
    )


def _phase_slots(allocation: LessonAllocation, phase: int) -> tuple[AllocatedSlot, ...]:
    if not isinstance(allocation, LessonAllocation) or phase < 1:
        raise TypeError("Slice-5 evaluation requires an allocated v3 lesson and positive phase.")
    slots = tuple(slot for slot in allocation.slots if slot.phase == phase)
    if not slots:
        raise ValueError(f"Allocation has no scheduled slots for phase {phase}.")
    return slots


def _type_kits(
    context: Mapping[str, Any], expected_ids: Sequence[str]
) -> dict[str, Mapping[str, Any]]:
    kits = context.get("type_kits")
    if not isinstance(kits, Sequence) or isinstance(kits, (bytes, bytearray, str)):
        raise PromptPackV3Error("v3.2 evaluation context has no type-kits.")
    by_slot = {
        kit.get("slot_id"): kit
        for kit in kits
        if isinstance(kit, Mapping) and isinstance(kit.get("slot_id"), str)
    }
    if len(by_slot) != len(kits) or not set(expected_ids).issubset(by_slot):
        raise PromptPackV3Error("v3.2 evaluation context does not match the phase allocation.")
    return {slot_id: by_slot[slot_id] for slot_id in expected_ids}


def _records_for_slots(
    payload: object, expected_ids: Sequence[str]
) -> tuple[dict[str, object], dict[str, list[SlotError]], list[SlotError]]:
    records: dict[str, object] = {}
    errors: dict[str, list[SlotError]] = {}
    unassigned: list[SlotError] = []
    duplicate_ids: set[str] = set()
    if not isinstance(payload, Mapping) or set(payload) != {"slots"} or not isinstance(
        payload.get("slots"), list
    ):
        for slot_id in expected_ids:
            errors[slot_id] = [
                _rule_error(slot_id, "response_shape")
            ]
        return records, errors, unassigned
    for record in payload["slots"]:
        reported = record.get("slot_id") if isinstance(record, Mapping) else None
        if isinstance(reported, str) and reported in expected_ids:
            if reported in records:
                errors.setdefault(reported, []).append(
                    _rule_error(reported, "response_shape")
                )
                records.pop(reported, None)
                duplicate_ids.add(reported)
            elif reported in duplicate_ids:
                errors.setdefault(reported, []).append(
                    _rule_error(reported, "response_shape")
                )
            else:
                records[reported] = record
            continue
        if isinstance(reported, str) and reported.strip():
            unassigned.append(
                _rule_error("unassigned", "response_shape")
            )
        else:
            unassigned.append(
                _rule_error("unassigned", "response_shape")
            )
    for slot_id in expected_ids:
        if slot_id not in records and slot_id not in errors:
            errors[slot_id] = [_rule_error(slot_id, "response_shape")]
    return records, errors, unassigned


def _serialized_unit_count(record: Mapping[str, Any]) -> int:
    units = record.get("serialized_units")
    return len(units) if isinstance(units, list) else 0


def _tray_ids_for_slots(
    tray_slot_ids: Sequence[str], slots: Sequence[AllocatedSlot]
) -> tuple[str, ...]:
    slot_ids = {slot.slot_id for slot in slots}
    return tuple(slot_id for slot_id in tray_slot_ids if slot_id in slot_ids)


def _rule_error(slot_id: str, rule_key: str, *, suffix: str | None = None) -> SlotError:
    """Build a content-free, repair-actionable cause from the fixed vocabulary."""
    cause = CAUSE_VOCABULARY[rule_key]
    return SlotError(slot_id, cause if suffix is None else f"{cause}: {suffix}")


def _freeze_context(context: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(value: object) -> object:
        if isinstance(value, Mapping):
            return _FrozenDict({str(key): freeze(item) for key, item in value.items()})
        if isinstance(value, list):
            return _FrozenList([freeze(item) for item in value])
        return value

    return freeze(context)  # type: ignore[return-value]


def _validate_exact_plan(plan: UnitPlan, slot_id: str, phase: int, activity_type: str) -> None:
    floor = floor_for(activity_type)
    if (
        plan.slot_id != slot_id
        or plan.phase != phase
        or plan.activity_type != activity_type
        or not plan.floor_met
        or len(plan.units) != floor.minimum_units
    ):
        raise ValueError("Repair and replacement plans must be exact floor-sized allocated plans.")


def _repair_record(value: object, slot_id: str) -> object:
    if isinstance(value, Mapping) and set(value) == {"slots"}:
        slots = value.get("slots")
        if not isinstance(slots, list) or len(slots) != 1:
            raise ValueError("repair renderer must return one slot record or a one-slot payload")
        return slots[0]
    return value


def _with_errors(
    evaluation: PhaseEvaluation, errors_by_slot: Mapping[str, tuple[SlotError, ...]]
) -> PhaseEvaluation:
    if not errors_by_slot:
        return evaluation
    blocks = tuple(
        replace(block, errors=(*errors_by_slot.get(block.slot_id, ()), *block.errors))
        for block in evaluation.blocks
    )
    return replace(evaluation, blocks=blocks)


def _with_renderer_errors(
    evaluation: PhaseEvaluation,
    *,
    previous_by_slot: Mapping[str, BlockEvaluation],
    errors_by_slot: Mapping[str, tuple[SlotError, ...]],
) -> PhaseEvaluation:
    """Keep renderer failures repairable without manufacturing response-shape errors."""
    if not errors_by_slot:
        return evaluation
    blocks = tuple(
        replace(
            previous_by_slot[block.slot_id],
            errors=(*previous_by_slot[block.slot_id].errors, *errors_by_slot[block.slot_id]),
        )
        if block.slot_id in errors_by_slot
        else block
        for block in evaluation.blocks
    )
    return replace(evaluation, blocks=blocks)


def _replacement_slot(slot: AllocatedSlot, replacement: ConditionalReplacement) -> AllocatedSlot:
    scheduled_type = replacement.activity_type
    return AllocatedSlot(
        slot_id=slot.slot_id,
        phase=slot.phase,
        requested_type=slot.requested_type,
        scheduled_type=scheduled_type,
        plan=replacement.plan,
        substitution_reason="serialization_exhausted"
        if scheduled_type != slot.requested_type
        else None,
    )


def _try_replacements(
    slot: AllocatedSlot,
    *,
    allocation: LessonAllocation,
    deterministic_gates: Sequence[DeterministicGate],
    raw_contract_validator: RawContractValidator,
    replacement_renderer: ReplacementRenderer | None,
    tray_slot_ids: Sequence[str],
) -> tuple[BlockEvaluation | None, tuple[PhaseEvaluation, ...]]:
    attempts: list[PhaseEvaluation] = []
    if replacement_renderer is None:
        return None, tuple(attempts)
    for replacement in slot.conditional_replacements:
        replacement_slot = _replacement_slot(slot, replacement)
        replacement_allocation = LessonAllocation(
            paragraph_ids=allocation.paragraph_ids, slots=(replacement_slot,)
        )
        context = build_phase_context(replacement_allocation, phase=slot.phase)
        request = ReplacementRequest(
            slot_id=slot.slot_id,
            phase=slot.phase,
            requested_type=slot.requested_type,
            activity_type=replacement.activity_type,
            plan=replacement.plan,
            prompt_context=_freeze_context(context),
        )
        renderer_errors: dict[str, tuple[SlotError, ...]] = {}
        try:
            record = _repair_record(replacement_renderer(request), slot.slot_id)
        except Exception:  # a failed certified replacement stays recoverable
            record = None
            renderer_errors[slot.slot_id] = (_rule_error(slot.slot_id, "replacement_renderer"),)
        attempt = _evaluate_payload(
            {"slots": []} if record is None else {"slots": [record]},
            context=context,
            slots=(replacement_slot,),
            deterministic_gates=deterministic_gates,
            raw_contract_validator=raw_contract_validator,
            tray_slot_ids=_tray_ids_for_slots(tray_slot_ids, (replacement_slot,)),
        )
        attempt = _with_errors(attempt, renderer_errors)
        attempts.append(attempt)
        candidate = attempt.blocks[0]
        if candidate.accepted:
            return candidate, tuple(attempts)
    return None, tuple(attempts)


def _dropped(previous: BlockEvaluation, slot_id: str, *, reason: str) -> BlockEvaluation:
    # #402 flag-don't-drop is presentation only: the internal ``dropped``
    # disposition and ``accepted == False`` semantics must never be renamed or
    # softened to reflect the presence of ``attempted_record``.  Density
    # accounting, dispositions, and the qualification bar stay receipt-driven.
    if reason == "repair_exhausted":
        cause = "repair_exhausted: no certified replacement serialized successfully"
    elif reason == "not_repairable":
        cause = "not_repairable: deterministic gate, shape, or raw-contract failure"
    else:  # pragma: no cover - private callers pin the two allowed reasons
        raise ValueError(f"Unknown drop reason: {reason}")
    error = SlotError(slot_id, cause)
    receipt = BlockDensityReceipt(
        phase=previous.phase,
        activity_type=previous.activity_type,
        disposition="dropped",
        units=previous.observed_units,
        floor_met=previous.observed_units >= floor_for(previous.activity_type).minimum_units,
    )
    return BlockEvaluation(
        slot_id=slot_id,
        phase=previous.phase,
        activity_type=previous.activity_type,
        disposition="dropped",
        observed_units=previous.observed_units,
        receipt=receipt,
        # The last attempt's shape-valid record, frozen at classification time
        # (#402).  Carried forward unchanged; never regenerated after the drop.
        attempted_record=previous.attempted_record,
        errors=(*previous.errors, error),
    )
