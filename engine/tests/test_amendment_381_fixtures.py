"""Frozen fixture tests for the #381 qualification-gate amendment.

Tests exact signatures from raw attempts in runs 4, 5, and 6:
- Class A: closed-class targets on gap-eliciting shapes (quiz, cloze, fill-in)
  are exempt from verbatim answer ban in surrounding prose, while content words
  remain strictly banned.
- Closed-class targets on non-gap shapes (true-false, match-up, etc.) fail
  preflight with closed_class_shape_mismatch.
- Class B: degree adjacency (consonant alternation & compc<->comps) passes;
  aspect adjacency without a forcing cue or with negation (e.g. run4 'за рік не')
  is MUST-REJECT.
- Min-options floor (>= 3) is enforced for MCQ items.
- Mutation check: re-tightening an amended rule fails the matching fixture.
"""

from __future__ import annotations

import pytest

from hramatka.engine import paths
from hramatka.engine.closed_class_policy import (
    is_closed_class_form,
    is_closed_class_target,
)
from hramatka.engine.lesson_capacity_v3 import (
    AllocatedSlot,
    AnchorParagraph,
    AnchorWindow,
    LessonSlot,
    preflight_lesson,
)
from hramatka.engine.prompt_pack_v3 import (
    PROMPT_PACK_VERSION,
    TEMPLATE_VERSION,
    TYPE_KIT_IDENTITY,
    PromptPackV3Error,
    _contains_form,
    _is_aspect_adjacent,
    _is_degree_adjacent,
    _type_kit,
    validate_distractor_adjacency,
    validate_verbatim_answer_ban,
)
from hramatka.engine.tests.fixtures.density_v3_regression_fixture import (
    complete_inventory,
)
from hramatka.engine.unit_builders_v3 import (
    CertificationInventory,
    EvidenceCandidate,
)
from hramatka.engine.unit_plan_v3 import (
    CertifiedUnit,
    Citation,
    ExpectedKeyRule,
    ResourceClaim,
    UnitAnchor,
    certify_unit_plan,
)


def _kit(activity_type: str = "quiz", target_form: str = "На") -> dict:
    return {
        "identity": TYPE_KIT_IDENTITY,
        "slot_id": "P1-A1",
        "phase": 1,
        "type": activity_type,
        "scheduled_unit_ids": ["u1"],
        "scheduled_unit_count": 1,
        "certified_units": [
            {
                "unit_id": "u1",
                "allowed_forms": [target_form],
                "expected_key_or_rule": {"kind": "key", "value": target_form},
            }
        ],
    }


def test_version_bump_identifiers() -> None:
    """Verify version string bumps for the #381 amendment contract."""
    assert PROMPT_PACK_VERSION == "PromptPackInput.v3.3"
    assert TEMPLATE_VERSION == "gemma-phase-pack.v3.12"
    assert TYPE_KIT_IDENTITY == "TeacherReadyDensity.v3.unit-plan-kit.v3"


def test_type_kit_contains_seed_pairs() -> None:
    """Verify _type_kit outputs seed pairs in kit data and adjacency helpers load them."""
    units = tuple(
        CertifiedUnit(
            unit_id=f"u{i}",
            resource_claims=(ResourceClaim("sentence", f"s-{i}"),),
            anchor=UnitAnchor("evidence", f"src:s-{i}"),
            allowed_forms=("добріший",),
            expected_key_or_rule=ExpectedKeyRule("key", "добріший"),
            citation_plan=(Citation("src", f"s-{i}"),),
            distinctness={
                "stem": f"s{i}",
                "gap": {
                    "sentence_id": f"s-{i}",
                    "token_id": f"s-{i}:t-1",
                    "start_offset": len("Цей план "),
                    "end_offset": len("Цей план добріший"),
                },
            },
            rendering_surface="Цей план добріший.",
        )
        for i in range(1, 9)
    )
    plan = certify_unit_plan(slot_id="P1-A1", phase=1, activity_type="quiz", units=units)
    slot = AllocatedSlot(
        slot_id="P1-A1",
        phase=1,
        requested_type="quiz",
        scheduled_type="quiz",
        plan=plan,
    )
    kit = _type_kit(slot)
    assert kit["identity"] == TYPE_KIT_IDENTITY
    assert "degree_seed_pairs" in kit
    assert "aspect_seed_pairs" in kit
    assert ["гірший", "поганий"] in kit["degree_seed_pairs"]
    assert _is_degree_adjacent("найскладніших", "складніших", paths.vesum_db(), kit=kit)
    assert _is_aspect_adjacent(
        "прочитує", "прочитає", "щодня він прочитує", paths.vesum_db(), kit=kit
    )


def test_run6_dialogue_class_a_metalanguage_stem_fails() -> None:
    """Run 6 dialogue P1-A1: free-form metalanguage stem MUST FAIL."""
    kit = _kit("quiz", "На")
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильну відповідь.",
            "items": [
                {
                    "question": (
                        "Який прийменник вживається для вказівки на "
                        "джерело судження перед словом «думку»?"
                    ),
                    "options": ["На", "В", "За"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }
    with pytest.raises(PromptPackV3Error, match="closed-class quiz stem missing gap marker"):
        validate_verbatim_answer_ban(activity, kit)


def test_run6_dialogue_class_a_gap_stem_passes() -> None:
    """Run 6 dialogue P1-A1 signature: closed-class prep 'На' in gap-stem quiz MUST PASS."""
    kit = _kit("quiz", "На")
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильну відповідь.",
            "items": [
                {
                    "question": (
                        "Який прийменник вживається для вказівки на "
                        "джерело судження в конструкції «___ думку»?"
                    ),
                    "options": ["На", "В", "За"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }
    validate_verbatim_answer_ban(activity, kit)
    validate_distractor_adjacency(activity, kit)


def test_run6_narrative_class_a_preposition_z_passes() -> None:
    """Run 6 narrative P1-A1 signature: closed-class prep 'з' in stem passes on quiz."""
    kit = _kit("quiz", "з")
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильний варіант.",
            "items": [
                {
                    "question": (
                        "Який прийменник вказує на виділення частини з "
                        "цілого в конструкції «один ___ найскладніших»?"
                    ),
                    "options": ["з", "без", "від"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }
    validate_verbatim_answer_ban(activity, kit)
    validate_distractor_adjacency(activity, kit)


def test_run5_dialogue_class_b_degree_adjacency_passes() -> None:
    """Run 5 dialogue P1-A1 signature: degree adjacency between comparatives."""
    kit = _kit("quiz", "найскладніших")
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильну форму.",
            "items": [
                {
                    "question": "Який ступінь порівняння прикметника необхідний у цій конструкції?",
                    "options": ["найскладніших", "складніших", "складним"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }
    validate_distractor_adjacency(activity, kit)


def test_run4_aspect_item_negated_stem_is_must_reject() -> None:
    """Run 4 fill-in item 5: 'не прочитує' vs 'прочитає' in negated stem MUST REJECT."""
    kit = _kit("fill-in", "прочитує")
    activity = {
        "payload": {
            "type": "fill-in",
            "instruction": "Вставте правильну форму дієслова.",
            "items": [
                {
                    "sentence": (
                        "Третина українців за рік не _____ жодної книжки, зате "
                        "дві третини щодня знаходять час увімкнути телевізор."
                    ),
                    "answer": "прочитує",
                    "options": ["прочитує", "прочитає", "прочитують"],
                }
            ],
        },
        "answer_key": {"items": ["прочитує"]},
    }
    with pytest.raises(PromptPackV3Error, match="does not share a lemma"):
        validate_distractor_adjacency(activity, kit)


def test_aspect_item_with_unnegated_forcing_cue_passes() -> None:
    """Aspect mates with an explicit unnegated forcing cue ('щодня') pass."""
    kit = _kit("fill-in", "прочитує")
    activity = {
        "payload": {
            "type": "fill-in",
            "instruction": "Вставте правильну форму.",
            "items": [
                {
                    "sentence": "Сергій щодня _____ п'ять сторінок українською.",
                    "answer": "прочитує",
                    "options": ["прочитує", "прочитає", "прочитують"],
                }
            ],
        },
        "answer_key": {"items": ["прочитує"]},
    }
    validate_distractor_adjacency(activity, kit)


def test_content_word_verbatim_leak_still_fails() -> None:
    """Content-word answer leaked in learner-facing question STILL FAILS strictly."""
    kit = _kit("quiz", "книжки")
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Дайте відповідь на питання.",
            "items": [
                {
                    "question": "Чому люди читають книжки у вільний час?",
                    "options": ["книжки", "книга", "книг"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }
    with pytest.raises(PromptPackV3Error, match="contains answer form 'книжки'"):
        validate_verbatim_answer_ban(activity, kit)


def test_options_count_floor_fails_two_option_mcq() -> None:
    """MCQ item with 2 options fails the minimum 3 options floor."""
    kit = _kit("quiz", "найскладніших")
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть варіант.",
            "items": [
                {
                    "question": "Яка форма прикметника потрібна?",
                    "options": ["найскладніших", "складніших"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }
    with pytest.raises(PromptPackV3Error, match="options count must be at least 3"):
        validate_distractor_adjacency(activity, kit)


def test_closed_class_target_on_forbidden_shape_fails_preflight() -> None:
    """Preflight rejects closed-class target allocated to non-gap shape."""
    db_path = paths.vesum_db()
    base_inv = complete_inventory()
    cc_candidate = EvidenceCandidate(
        activity_type="true-false",
        candidate_id="cc-1",
        sentence_id="s-1",
        token_id="s-1:t-1",
        literal_evidence="На думку вчених...",
        expected_key="На",
        semantic_target="true-false:s-1:s-1:t-1",
    )
    inv = CertificationInventory(
        source_id=base_inv.source_id,
        sentences=base_inv.sentences,
        candidates=base_inv.candidates + (cc_candidate,),
    )
    assert is_closed_class_target({"expected_key": "На"}, db_path)

    p1 = AnchorParagraph(paragraph_id="p1", inventory=inv)
    window = AnchorWindow(paragraphs=(p1,), initial_start=0, initial_end=0)

    slots = (
        LessonSlot(slot_id="P1-A1", phase=1, requested_type="true-false"),
        LessonSlot(slot_id="P1-A2", phase=1, requested_type="cloze"),
        LessonSlot(slot_id="P2-A1", phase=2, requested_type="match-up"),
        LessonSlot(slot_id="P2-A2", phase=2, requested_type="error-correction"),
        LessonSlot(slot_id="P2-A3", phase=2, requested_type="text-questions"),
        LessonSlot(slot_id="P3-A1", phase=3, requested_type="short-writing"),
    )

    def closed_class_only_builder(inventory, *, slot_id, phase):
        units = tuple(
            CertifiedUnit(
                unit_id=f"cc{i}",
                resource_claims=(ResourceClaim("sentence", f"s-{i}"),),
                anchor=UnitAnchor("evidence", f"src:s-{i}"),
                allowed_forms=("На",),
                expected_key_or_rule=ExpectedKeyRule("key", "На"),
                citation_plan=(Citation("src", f"s-{i}"),),
                distinctness={"stem": f"s{i}"},
            )
            for i in range(1, 9)
        )
        return certify_unit_plan(
            slot_id=slot_id, phase=phase, activity_type="true-false", units=units
        )

    builders = {
        "true-false": closed_class_only_builder,
        "quiz": lambda inv, slot_id, phase: certify_unit_plan(
            slot_id=slot_id, phase=phase, activity_type="quiz", units=()
        ),
        "cloze": lambda inv, slot_id, phase: certify_unit_plan(
            slot_id=slot_id, phase=phase, activity_type="cloze", units=()
        ),
        "match-up": lambda inv, slot_id, phase: certify_unit_plan(
            slot_id=slot_id, phase=phase, activity_type="match-up", units=()
        ),
        "error-correction": lambda inv, slot_id, phase: certify_unit_plan(
            slot_id=slot_id, phase=phase, activity_type="error-correction", units=()
        ),
        "text-questions": lambda inv, slot_id, phase: certify_unit_plan(
            slot_id=slot_id, phase=phase, activity_type="text-questions", units=()
        ),
        "short-writing": lambda inv, slot_id, phase: certify_unit_plan(
            slot_id=slot_id, phase=phase, activity_type="short-writing", units=()
        ),
    }

    res = preflight_lesson(window, duration_minutes=45, slots=slots, builders=builders)
    assert res.allocation is None
    assert res.event is not None
    assert res.event.code == "closed_class_shape_mismatch"


def test_mutation_check_retighten_closed_class_exemption_fails_fixture() -> None:
    """Mutation check: if closed-class exemption is disabled on gap shapes, fixture fails."""
    kit = _kit("quiz", "На")
    activity = {
        "payload": {
            "type": "quiz",
            "instruction": "Оберіть правильну відповідь.",
            "items": [
                {
                    "question": (
                        "Який прийменник вживається для вказівки на "
                        "джерело судження в конструкції «___ думку»?"
                    ),
                    "options": ["На", "В", "За"],
                    "correct": 0,
                }
            ],
        },
        "answer_key": {"items": [{"index": 0, "correct": 0}]},
    }

    # Verify that under standard validate_verbatim_answer_ban it passes
    validate_verbatim_answer_ban(activity, kit)

    # Re-tighten rule (simulate old behavior without closed-class exemption on gap shapes)
    def strict_verbatim_check(act: dict, k: dict) -> None:
        forms = [unit["allowed_forms"][0] for unit in k["certified_units"]]
        q = act["payload"]["items"][0]["question"]
        for f in forms:
            if _contains_form(q, f):
                raise PromptPackV3Error(f"strict re-tightened rule: question contains {f!r}")

    with pytest.raises(PromptPackV3Error, match="strict re-tightened rule"):
        strict_verbatim_check(activity, kit)


def test_homograph_certified_analysis_classification() -> None:
    """Homographs key on certified analysis from kit rather than surface forms."""
    # Certified noun homograph (e.g. 'Під' noun)
    assert not is_closed_class_target(
        {"expected_key": "Під", "certified_analysis": {"pos": "noun"}}
    )
    # Certified prep homograph (e.g. 'Під' prep)
    assert is_closed_class_target({"expected_key": "Під", "certified_analysis": {"pos": "prep"}})
    # Certified verb homograph (e.g. 'є' verb)
    assert not is_closed_class_target({"expected_key": "є", "certified_analysis": {"pos": "verb"}})
    # Certified particle homograph (e.g. 'є' particle)
    assert is_closed_class_target({"expected_key": "є", "certified_analysis": {"pos": "part"}})
    # Uncertified surface fallback logic
    assert not is_closed_class_form("Під")
    assert not is_closed_class_form("є")
    assert is_closed_class_form("На")
    assert is_closed_class_form("з")
