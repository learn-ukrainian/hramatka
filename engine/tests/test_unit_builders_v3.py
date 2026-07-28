"""RED-first regression tests for TeacherReadyDensity.v3 per-type builders."""

from __future__ import annotations

import pytest

from hramatka.engine.short_writing_constraints_v3 import (
    CONSTRAINT_REGISTRY,
    ConstraintSpec,
    validate_constraints,
)
from hramatka.engine.teacher_ready_density_v3 import floor_for
from hramatka.engine.tests.fixtures.density_v3_regression_fixture import (
    complete_inventory,
    insufficient_inventory,
)
from hramatka.engine.true_false_catalog_v3 import (
    MUTATION_CATALOG_VERSION,
    MutationBinding,
    construct_false_statement,
    verify_false_statement,
)
from hramatka.engine.unit_builders_v3 import (
    BUILDERS,
    CertificationInventory,
    build_mark_the_words,
    build_short_writing,
)


@pytest.mark.parametrize("activity_type", tuple(BUILDERS))
def test_each_builder_certifies_its_complete_floor_from_a_deterministic_inventory(
    activity_type: str,
) -> None:
    inventory = complete_inventory()

    plan = BUILDERS[activity_type](inventory, slot_id="P1-A1", phase=1)

    assert plan.disposition == "certified"
    assert len(plan.units) >= floor_for(activity_type).minimum_units
    if activity_type == "error-correction":
        assert all(unit.expected_key_or_rule.certified_error_count == 1 for unit in plan.units)


@pytest.mark.parametrize("activity_type", tuple(BUILDERS))
def test_each_builder_returns_unavailable_for_an_insufficient_anchor(activity_type: str) -> None:
    inventory = insufficient_inventory(activity_type)

    plan = BUILDERS[activity_type](inventory, slot_id="P1-A1", phase=1)

    assert plan.disposition == "unavailable"
    assert plan.units == ()


def test_true_false_catalog_is_versioned_closed_and_literal_evidence_bound() -> None:
    inventory = complete_inventory()
    fact = inventory.true_false_facts[0]
    binding = MutationBinding(
        sentence_id=fact.sentence_id,
        literal_evidence=fact.literal_evidence,
        source_surface=fact.source_surface,
        replacement_surface=fact.replacement_surface,
    )

    statement = construct_false_statement("replace-one-surface.v1", binding)

    assert MUTATION_CATALOG_VERSION == "true-false-mutations.v1"
    assert statement is not None
    assert verify_false_statement("replace-one-surface.v1", binding, statement)
    assert construct_false_statement("free-form", binding) is None
    assert not verify_false_statement("free-form", binding, statement)


def test_short_writing_registry_is_closed_and_uses_regex_and_vesum_evidence() -> None:
    task = complete_inventory().writing_tasks[0]
    case_parse = next(
        parse
        for token in task.sample_tokens
        for parse in token.parses
        if isinstance(parse.get("case"), str) and isinstance(parse.get("lemma"), str)
    )
    case_constraint = ConstraintSpec(
        "target_case_usage",
        {
            "case": case_parse["case"],
            "minimum": 1,
            "lemmas": (case_parse["lemma"],),
        },
    )

    assert set(CONSTRAINT_REGISTRY) == {
        "contains_lemma_set",
        "min_verb_count",
        "target_case_usage",
        "word_count_range",
    }
    assert validate_constraints(task.constraints, task.sample_tokens)
    assert validate_constraints((case_constraint,), task.sample_tokens)
    assert not validate_constraints((ConstraintSpec("llm_judgment", {}),), task.sample_tokens)


def test_mark_the_words_plan_records_exact_certified_target_token_records() -> None:
    plan = build_mark_the_words(complete_inventory(), slot_id="P1-A1", phase=1)

    assert plan.disposition == "certified"
    assert len(plan.certified_target_tokens) == floor_for("mark-the-words").minimum_units
    assert all(
        token.surface and token.end_offset > token.start_offset
        for token in plan.certified_target_tokens
    )


def test_text_question_builder_preserves_the_locked_3_3_2_categories() -> None:
    plan = BUILDERS["text-questions"](complete_inventory(), slot_id="P1-A1", phase=1)
    categories = [str(unit.distinctness["question_category"]) for unit in plan.units]

    assert categories.count("comprehension") >= 3
    assert categories.count("explanation_inference") >= 3
    assert categories.count("anchored_application") >= 2


def test_mark_the_words_rejects_a_partial_or_non_verbatim_target_list() -> None:
    inventory = complete_inventory()
    request = inventory.mark_requests[0]
    partial = CertificationInventory(
        source_id=inventory.source_id,
        sentences=inventory.sentences,
        candidates=inventory.candidates,
        true_false_facts=inventory.true_false_facts,
        atlas_pairs=inventory.atlas_pairs,
        mark_requests=(
            type(request)(
                request_id=request.request_id,
                sentence_ids=request.sentence_ids,
                criterion=request.criterion,
                target_token_ids=request.target_token_ids[:-1],
            ),
        ),
        writing_tasks=inventory.writing_tasks,
    )

    assert build_mark_the_words(partial, slot_id="P1-A1", phase=1).disposition == "unavailable"


def test_short_writing_builder_rejects_an_unregistered_constraint() -> None:
    inventory = complete_inventory()
    task = inventory.writing_tasks[0]
    invalid_task = type(task)(
        task_id=task.task_id,
        sentence_id=task.sentence_id,
        prompt=task.prompt,
        constraints=(
            ConstraintSpec("llm_judgment", {}),
            ConstraintSpec("min_verb_count", {"minimum": 1}),
        ),
        sample_tokens=task.sample_tokens,
    )
    invalid_inventory = CertificationInventory(
        source_id=inventory.source_id,
        sentences=inventory.sentences,
        candidates=inventory.candidates,
        true_false_facts=inventory.true_false_facts,
        atlas_pairs=inventory.atlas_pairs,
        mark_requests=inventory.mark_requests,
        writing_tasks=(invalid_task,),
    )

    assert (
        build_short_writing(invalid_inventory, slot_id="P1-A1", phase=1).disposition
        == "unavailable"
    )


def test_short_writing_builder_requires_a_configured_word_range() -> None:
    inventory = complete_inventory()
    task = inventory.writing_tasks[0]
    no_word_range_task = type(task)(
        task_id=task.task_id,
        sentence_id=task.sentence_id,
        prompt=task.prompt,
        constraints=task.constraints[:2],
        sample_tokens=task.sample_tokens,
    )
    no_word_range_inventory = CertificationInventory(
        source_id=inventory.source_id,
        sentences=inventory.sentences,
        candidates=inventory.candidates,
        true_false_facts=inventory.true_false_facts,
        atlas_pairs=inventory.atlas_pairs,
        mark_requests=inventory.mark_requests,
        writing_tasks=(no_word_range_task,),
    )

    assert (
        build_short_writing(no_word_range_inventory, slot_id="P1-A1", phase=1).disposition
        == "unavailable"
    )
