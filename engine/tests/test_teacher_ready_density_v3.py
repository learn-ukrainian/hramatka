"""Locked, pre-cutover contracts for ``TeacherReadyDensity.v3``.

These tests deliberately exercise only the isolated v3 contract modules.  The
production baker stays on its current v2 path until the later atomic cutover.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hramatka.contracts import PILOT_ACTIVITY_TYPES
from hramatka.engine import density_receipt_v3, teacher_ready_density_v3, unit_plan_v3
from hramatka.engine.density_receipt_v3 import BlockDensityReceipt
from hramatka.engine.teacher_ready_density_v3 import (
    FLOOR_TABLE,
    TEXT_QUESTION_3_3_2,
    PhaseShape,
    TextQuestionBudget,
    floor_for,
    phase_shape_for,
    type_allowed_in_phase,
)
from hramatka.engine.unit_plan_v3 import (
    CertifiedUnit,
    Citation,
    ExpectedKeyRule,
    ResourceClaim,
    UnitAnchor,
    certify_unit_plan,
    normalize_distinctness_key,
)


def _distinctness(activity_type: str, index: int) -> dict[str, object]:
    if activity_type == "cloze":
        return {"gap": {"sentence_id": "s-1", "token_id": f"t-{index}"}}
    if activity_type == "match-up":
        return {"pair": {"left": f"lemma-{index}", "right": f"atlas-{index}"}}
    if activity_type == "mark-the-words":
        return {"target": {"sentence_id": "s-1", "token_id": f"t-{index}"}}
    if activity_type == "text-questions":
        categories = (
            ("comprehension",) * 3 + ("explanation_inference",) * 3 + ("anchored_application",) * 2
        )
        return {
            "stem": f"Нормований навчальний пункт {index}",
            "question_category": categories[index],
        }
    return {"stem": f"Нормований навчальний пункт {index}"}


def _unit(
    activity_type: str,
    index: int,
    *,
    distinctness: dict[str, object] | None = None,
) -> CertifiedUnit:
    expected = (
        ExpectedKeyRule(kind="rule", value=f"правило-{index}", certified_error_count=1)
        if activity_type == "error-correction"
        else ExpectedKeyRule(kind="key", value=f"ключ-{index}")
    )
    return CertifiedUnit(
        unit_id=f"{activity_type}-{index}",
        resource_claims=(ResourceClaim(kind="sentence", resource_id=f"s-{index}"),),
        anchor=UnitAnchor(kind="evidence", anchor_id=f"e-{index}"),
        allowed_forms=(f"форма-{index}",),
        expected_key_or_rule=expected,
        citation_plan=(Citation(source_id=f"s-{index}", locator=f"units[{index}]"),),
        distinctness=distinctness or _distinctness(activity_type, index),
    )


def _certified_plan(activity_type: str, *, phase: int = 1):
    floor = floor_for(activity_type)
    return certify_unit_plan(
        slot_id=f"P{phase}-A1",
        phase=phase,
        activity_type=activity_type,
        units=tuple(_unit(activity_type, index) for index in range(floor.minimum_units)),
        registered_constraints=("contains_lemma_set", "word_count_range")
        if activity_type == "short-writing"
        else (),
    )


def test_floor_table_is_the_complete_immutable_v3_authority() -> None:
    assert teacher_ready_density_v3.TEACHER_READY_DENSITY_VERSION == "TeacherReadyDensity.v3"
    assert set(FLOOR_TABLE) == set(PILOT_ACTIVITY_TYPES)
    assert {activity_type: floor.minimum_units for activity_type, floor in FLOOR_TABLE.items()} == {
        "true-false": 8,
        "quiz": 8,
        "cloze": 8,
        "match-up": 8,
        "fill-in": 8,
        "error-correction": 8,
        "text-questions": 8,
        "mark-the-words": 8,
        "short-writing": 1,
    }
    assert FLOOR_TABLE["text-questions"].category_minima == TEXT_QUESTION_3_3_2
    assert FLOOR_TABLE["short-writing"].minimum_registered_constraints == 2
    assert FLOOR_TABLE["error-correction"].certified_errors_per_unit == 1
    with pytest.raises(TypeError):
        FLOOR_TABLE["quiz"] = floor_for("quiz")  # type: ignore[index]


def test_unit_plan_records_every_certified_unit_field_and_consumes_floor_authority() -> None:
    plan = _certified_plan("quiz")

    assert unit_plan_v3.floor_for is teacher_ready_density_v3.floor_for
    assert plan.disposition == "certified"
    assert plan.floor_met is True
    assert len(plan.units) == floor_for("quiz").minimum_units
    assert set(plan.units[0].to_dict()) == {
        "unit_id",
        "resource_claims",
        "anchor",
        "allowed_forms",
        "expected_key_or_rule",
        "citation_plan",
        "distinctness",
    }
    with pytest.raises(FrozenInstanceError):
        plan.units[0].unit_id = "changed"  # type: ignore[misc]


def test_unit_anchor_explicitly_supports_deterministic_kit_evidence() -> None:
    kit_anchor = UnitAnchor(kind="kit", anchor_id="kit:lemma-42")

    assert kit_anchor.to_dict() == {"kind": "kit", "anchor_id": "kit:lemma-42"}


def test_incomplete_or_constraintless_builder_result_is_unavailable_before_generation() -> None:
    incomplete = certify_unit_plan(
        slot_id="P1-A1",
        phase=1,
        activity_type="quiz",
        units=tuple(_unit("quiz", index) for index in range(7)),
    )
    writing_without_constraints = certify_unit_plan(
        slot_id="P1-A2",
        phase=1,
        activity_type="short-writing",
        units=(_unit("short-writing", 0),),
    )

    assert incomplete.disposition == "unavailable"
    assert incomplete.units == ()
    assert incomplete.floor_met is False
    assert writing_without_constraints.disposition == "unavailable"
    assert writing_without_constraints.floor_met is False


def test_error_correction_plan_requires_exactly_one_certified_error_per_unit() -> None:
    valid = _certified_plan("error-correction")
    invalid_units = tuple(
        CertifiedUnit(
            unit_id=unit.unit_id,
            resource_claims=unit.resource_claims,
            anchor=unit.anchor,
            allowed_forms=unit.allowed_forms,
            expected_key_or_rule=ExpectedKeyRule(
                kind="rule", value="помилка", certified_error_count=2
            ),
            citation_plan=unit.citation_plan,
            distinctness=unit.distinctness,
        )
        for unit in valid.units
    )

    invalid = certify_unit_plan(
        slot_id="P1-A3", phase=1, activity_type="error-correction", units=invalid_units
    )

    assert valid.disposition == "certified"
    assert invalid.disposition == "unavailable"


@pytest.mark.parametrize(
    ("activity_type", "first", "second"),
    [
        ("quiz", {"stem": " Що   сталося? "}, {"stem": "що сталося?"}),
        (
            "cloze",
            {"gap": {"sentence_id": "S-1", "token_id": "T-2"}},
            {"gap": {"sentence_id": "s-1", "token_id": "t-2"}},
        ),
        (
            "mark-the-words",
            {"target": {"sentence_id": "S-1", "token_id": "T-2"}},
            {"target": {"sentence_id": "s-1", "token_id": "t-2"}},
        ),
        (
            "match-up",
            {"pair": {"left": " ДІМ", "right": "будинок "}},
            {"pair": {"left": "дім", "right": "БУДИНОК"}},
        ),
    ],
)
def test_distinctness_normalizes_duplicate_stems_gaps_targets_and_pairs(
    activity_type: str, first: dict[str, object], second: dict[str, object]
) -> None:
    assert normalize_distinctness_key(activity_type, first) == normalize_distinctness_key(
        activity_type, second
    )


def test_paraphrase_padding_with_the_same_semantic_target_does_not_count() -> None:
    padded_units = list(_certified_plan("quiz").units)
    padded_units[-1] = _unit(
        "quiz",
        99,
        distinctness={
            "stem": "Поясніть наслідок події своїми словами.",
            "semantic_target": "anchor:s-0:fact-0",
        },
    )
    padded_units[0] = _unit(
        "quiz",
        0,
        distinctness={
            "stem": "Який факт повідомляє перше речення?",
            "semantic_target": "anchor:s-0:fact-0",
        },
    )

    plan = certify_unit_plan(
        slot_id="P1-A1", phase=1, activity_type="quiz", units=tuple(padded_units)
    )

    assert plan.disposition == "unavailable"
    assert plan.floor_met is False


def test_receipt_has_only_the_locked_content_free_shape_and_consumes_floor_authority() -> None:
    plan = _certified_plan("true-false", phase=2)
    receipt = BlockDensityReceipt.from_unit_plan(plan, disposition="ready")

    assert density_receipt_v3.floor_for is teacher_ready_density_v3.floor_for
    assert receipt.to_dict() == {
        "phase": 2,
        "type": "true-false",
        "disposition": "ready",
        "units": 8,
        "floor_met": True,
    }
    assert "форма" not in json.dumps(receipt.to_dict(), ensure_ascii=False)


def test_45_minute_lone_phase_three_rejects_text_questions_without_full_budget() -> None:
    shape = phase_shape_for(45)

    assert shape.phase_slots == {1: 3, 2: 4, 3: 1}
    assert type_allowed_in_phase(45, 3, "text-questions") is False
    assert type_allowed_in_phase(45, 3, "short-writing") is True

    explicitly_budgeted = PhaseShape(
        duration_minutes=45,
        phase_slots={1: 3, 2: 4, 3: 1},
        text_question_budget_by_phase={3: TextQuestionBudget(3, 3, 2)},
    )
    assert explicitly_budgeted.allows(3, "text-questions") is True
    assert explicitly_budgeted.text_question_budget_by_phase[3] == TEXT_QUESTION_3_3_2


def test_density_floor_fingerprint_tracks_the_locked_v3_authority() -> None:
    assert (
        teacher_ready_density_v3.density_floor_fingerprint()
        == "1114645f2b2015e453ddb44542347b9af1de8aba6f6c64f875b5768550b946bf"
    )


def test_v3_contract_modules_are_the_default_production_import_graph() -> None:
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("HRAMATKA_"):
            env.pop(name)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; import hramatka.api.app; "
                "print(json.dumps(sorted(name for name in sys.modules "
                "if name.startswith('hramatka.engine.') and name.endswith('_v3'))))"
            ),
        ],
        check=True,
        capture_output=True,
        cwd=root,
        env=env,
        text=True,
    )

    loaded = set(json.loads(completed.stdout))
    assert {
        "hramatka.engine.unit_builders_v3",
        "hramatka.engine.true_false_catalog_v3",
        "hramatka.engine.short_writing_constraints_v3",
        "hramatka.engine.density_evaluator_v3",
        "hramatka.engine.lesson_capacity_v3",
        "hramatka.engine.prompt_pack_v3",
        "hramatka.engine.density_receipt_v3",
        "hramatka.engine.teacher_ready_density_v3",
        "hramatka.engine.unit_plan_v3",
    }.issubset(loaded)
    assert {
        "hramatka.engine.content_density",
        "hramatka.engine.generate",
        "hramatka.engine.pipeline",
        "hramatka.engine.prompt_pack",
        "hramatka.engine.registry",
        "hramatka.engine.repair",
        "hramatka.engine.selector",
    }.isdisjoint(_production_engine_modules(root, env))


def _production_engine_modules(root: Path, env: dict[str, str]) -> set[str]:
    """Return the engine modules loaded by a clean production API import."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; import hramatka.api.app; "
                "print(json.dumps(sorted(name for name in sys.modules "
                "if name.startswith('hramatka.engine.'))))"
            ),
        ],
        check=True,
        capture_output=True,
        cwd=root,
        env=env,
        text=True,
    )
    return set(json.loads(completed.stdout))
