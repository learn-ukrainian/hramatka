"""RED-first regression tests for TeacherReadyDensity.v3 per-type builders."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace

import pytest

from hramatka.engine.anchor_inventory_v3 import (
    DEGREE_WRITING_LEMMAS,
    DEGREE_WRITING_SCENARIO,
    DEGREE_WRITING_WARRANTS,
    _application_carrier_rank,
    _break_trivial_truth_pattern,
    _certified_choice_bank,
    _contextual_choice_bank,
    _contextual_error_replacements,
    _cross_gap_choice_banks,
    _eligible_tokens,
    _error_replacement,
    _focus_rank,
    _is_degree_token,
    _lemma_for,
    _safe_source_proposition_carrier,
    _sentences,
    _unambiguous_content_lemma_pos,
    inventory_for_group,
    inventory_from_anchor,
)
from hramatka.engine.short_writing_constraints_v3 import (
    CONSTRAINT_REGISTRY,
    ConstraintSpec,
    validate_constraints,
)
from hramatka.engine.teacher_ready_density_v3 import floor_for
from hramatka.engine.tests.fixtures.density_v3_regression_fixture import (
    DENSITY_V3_ANCHOR,
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
    AnchorSentence,
    AnchorToken,
    CertificationInventory,
    MarkTheWordsRequest,
    TrueFalseFact,
    build_mark_the_words,
    build_short_writing,
)

_PRODUCTION_ANCHOR = DENSITY_V3_ANCHOR
_WORD_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ'’]+")


def _production_plan(activity_type: str, *, duration: int = 45, focus: str | None = None):
    inventory = inventory_from_anchor(
        _PRODUCTION_ANCHOR,
        scheduled_types=(activity_type,),
        duration_minutes=duration,
        focus=focus,
    )
    selected = inventory_for_group(inventory, activity_type=activity_type, group_number=1)
    return BUILDERS[activity_type](selected, slot_id="P1-A1", phase=1)


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


@pytest.mark.parametrize(
    "activity_type",
    (
        "quiz",
        "cloze",
        "fill-in",
        "error-correction",
        "text-questions",
    ),
)
def test_production_inventory_units_use_the_required_sentence_spread(
    activity_type: str,
) -> None:
    plan = _production_plan(activity_type)
    sentence_ids = [
        claim.resource_id
        for unit in plan.units
        for claim in unit.resource_claims
        if claim.kind == "sentence"
    ]

    assert plan.disposition == "certified"
    assert len(plan.units) == floor_for(activity_type).minimum_units
    if activity_type == "error-correction":
        assert len(set(sentence_ids)) == len(sentence_ids)
    else:
        assert len(set(sentence_ids)) >= 4
        assert max(Counter(sentence_ids).values()) <= 2


def test_contextual_choice_bank_deduplicates_morphological_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = AnchorToken(
        "s-1",
        "s-1:t-2",
        "скидається",
        5,
        15,
        ({"pos": "verb", "raw": "verb:rev:imperf:pres:s:3", "lemma": "скидатися"},),
    )
    sentence = AnchorSentence("s-1", "Риба скидається.", (token,))
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3._contextual_error_replacements",
        lambda *_args, **_kwargs: (
            ("скидаюсь", "subject-verb-person", "visible subject"),
            ("скидаюся", "subject-verb-person", "visible subject"),
            ("скидаєшся", "subject-verb-person", "visible subject"),
        ),
    )
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3._replacement_tag_sets",
        lambda _token, form, *, pos: (
            {"verb", "rev", "imperf", "pres", "s", "1"}
            if form in {"скидаюсь", "скидаюся"}
            else {"verb", "rev", "imperf", "pres", "s", "2"},
        ),
    )

    bank = _contextual_choice_bank(sentence, token)

    assert bank is not None
    assert set(bank[0]) == {"скидається", "скидаюся", "скидаєшся"}
    assert "скидаюсь" not in bank[0]


def test_whitespace_only_sentence_matches_keep_dense_ids_and_cloze_capacity() -> None:
    anchor = _PRODUCTION_ANCHOR.replace("\n", " \n")
    sentences = _sentences(anchor)

    assert [sentence.sentence_id for sentence in sentences] == [
        f"s-{index}" for index in range(1, len(sentences) + 1)
    ]
    inventory = inventory_from_anchor(
        anchor,
        scheduled_types=("cloze",),
        duration_minutes=45,
    )
    selected = inventory_for_group(inventory, activity_type="cloze", group_number=1)
    plan = BUILDERS["cloze"](selected, slot_id="P1-A1", phase=1)
    assert plan.disposition == "certified"


def test_generic_builder_rejects_mixed_focus_alignment() -> None:
    inventory = complete_inventory()
    changed = False
    candidates = []
    for candidate in inventory.candidates:
        if candidate.activity_type == "fill-in" and not changed:
            candidate = replace(candidate, focus_alignment="degree-reinforcement")
            changed = True
        candidates.append(candidate)
    assert changed

    plan = BUILDERS["fill-in"](
        replace(inventory, candidates=tuple(candidates)),
        slot_id="P1-A1",
        phase=1,
    )
    assert plan.disposition == "unavailable"


@pytest.mark.parametrize("activity_type", ("quiz", "cloze", "fill-in"))
def test_production_closed_units_certify_exact_spans_banks_and_exclusions(
    activity_type: str,
) -> None:
    plan = _production_plan(activity_type)

    assert plan.disposition == "certified"
    for unit in plan.units:
        answer = unit.allowed_forms[0]
        gap = unit.distinctness["gap"]
        bank = unit.distinctness["choice_bank"]
        exclusions = unit.distinctness["exclusion_warrants"]
        assert unit.rendering_surface[gap["start_offset"] : gap["end_offset"]] == answer
        assert len(bank) >= 3
        assert len(bank) == len(set(bank))
        assert answer in bank
        assert set(exclusions) == set(bank) - {answer}


def test_generic_cloze_uses_other_gap_answers_as_lexical_distractors() -> None:
    inventory = inventory_from_anchor(
        _PRODUCTION_ANCHOR,
        scheduled_types=("cloze",),
        duration_minutes=45,
    )
    selected = inventory_for_group(inventory, activity_type="cloze", group_number=1)
    plan = BUILDERS["cloze"](selected, slot_id="P1-A1", phase=1)
    token_by_id = {
        token.token_id: token for sentence in selected.sentences for token in sentence.tokens
    }
    answers = {unit.allowed_forms[0] for unit in plan.units}

    assert plan.disposition == "certified"
    for unit in plan.units:
        answer = unit.allowed_forms[0]
        bank = unit.distinctness["choice_bank"]
        answer_token = token_by_id[unit.distinctness["gap"]["token_id"]]
        answer_identity = _unambiguous_content_lemma_pos(answer_token)
        distractor_tokens = [
            token
            for token in token_by_id.values()
            if token.surface in set(bank) - {answer}
        ]
        distractor_identities = [
            _unambiguous_content_lemma_pos(token) for token in distractor_tokens
        ]
        assert unit.distinctness["frame_family"] == "cross-gap-lexical.v1"
        assert set(bank) - {answer} <= answers - {answer}
        assert answer_identity is not None
        assert all(identity is not None for identity in distractor_identities)
        assert all(identity[0] != answer_identity[0] for identity in distractor_identities)
        assert all(identity[1] == answer_identity[1] for identity in distractor_identities)


def test_generic_cloze_never_uses_an_atlas_synonym_as_a_distractor(monkeypatch) -> None:
    def token(index: int, surface: str, lemma: str, pos: str) -> AnchorToken:
        return AnchorToken(
            sentence_id=f"s-{index}",
            token_id=f"s-{index}:t-1",
            surface=surface,
            start_offset=0,
            end_offset=len(surface),
            vesum_parses=({"pos": pos, "raw": f"{pos}:inanim:n:v_naz", "lemma": lemma},),
        )

    tokens = (
        token(1, "багаття", "багаття", "noun"),
        token(2, "вогнище", "вогнище", "noun"),
        token(3, "дерево", "дерево", "noun"),
        token(4, "намет", "намет", "noun"),
        token(5, "росте", "рости", "verb"),
        token(6, "зростає", "зростати", "verb"),
        token(7, "шумить", "шуміти", "verb"),
    )
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3.build_atlas_lookup",
        lambda *_args, **_kwargs: {
            "вогнище": {"synonyms": ["багаття"]},
            "багаття": {"synonyms": []},
        },
    )

    banks = _cross_gap_choice_banks(tokens)

    assert banks is not None
    assert "вогнище" not in banks[tokens[0].token_id][0]
    assert "багаття" not in banks[tokens[1].token_id][0]


def test_production_cloze_uses_context_warranted_same_lemma_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path
    from types import SimpleNamespace

    tokens = tuple(
        AnchorToken(
            sentence_id=f"s-{index}",
            token_id=f"s-{index}:t-1",
            surface=surface,
            start_offset=0,
            end_offset=len(surface),
            vesum_parses=({"pos": "noun", "raw": raw, "lemma": lemma},),
        )
        for index, (surface, lemma, raw) in enumerate(
            (
                ("лісі", "ліс", "noun:inanim:m:v_mis"),
                ("саду", "сад", "noun:inanim:m:v_rod"),
                ("домом", "дім", "noun:inanim:m:v_oru"),
            ),
            start=1,
        )
    )
    sentences = {
        token.sentence_id: AnchorSentence(token.sentence_id, token.surface, (token,))
        for token in tokens
    }
    banks = {
        tokens[0].token_id: (
            ("лісі", "лісом", "ліси"),
            (("лісом", "visible head"), ("ліси", "visible head")),
        ),
        tokens[1].token_id: (
            ("саду", "садом", "сади"),
            (("садом", "visible head"), ("сади", "visible head")),
        ),
        tokens[2].token_id: (
            ("домом", "дому", "доми"),
            (("дому", "visible head"), ("доми", "visible head")),
        ),
    }
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3.data.active_bundle",
        lambda: SimpleNamespace(
            manifest={"version": "production"},
            atlas_db=Path("unused-atlas.db"),
        ),
    )
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3._contextual_choice_bank",
        lambda _sentence, token: banks[token.token_id],
    )

    assert _cross_gap_choice_banks(tokens, sentences=sentences) == banks


def test_production_cloze_keeps_contextually_certified_adjectives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    tokens = (
        AnchorToken("s-1", "s-1:t-1", "Вони", 0, 4, ({"pos": "pron", "raw": "pron:p:3"},)),
        AnchorToken(
            "s-1", "s-1:t-2", "бачать", 5, 11, ({"pos": "verb", "raw": "verb:p:3"},)
        ),
        AnchorToken(
            "s-1",
            "s-1:t-3",
            "срібну",
            12,
            18,
            ({"pos": "adj", "raw": "adj:f:v_zna", "lemma": "срібний"},),
        ),
        AnchorToken(
            "s-1",
            "s-1:t-4",
            "річку",
            19,
            24,
            ({"pos": "noun", "raw": "noun:inanim:f:v_zna", "lemma": "річка"},),
        ),
    )
    sentence = AnchorSentence("s-1", "Вони бачать срібну річку.", tokens)
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3.data.active_bundle",
        lambda: SimpleNamespace(manifest={"version": "production"}),
    )
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3._contextual_choice_bank",
        lambda _sentence, token: (
            (("срібну", "срібна", "срібної"), (("срібна", "head"), ("срібної", "head")))
            if token.token_id == "s-1:t-3"
            else None
        ),
    )

    assert _eligible_tokens("cloze", sentence) == (tokens[2],)


def test_contextual_cloze_ignores_an_unrelated_noun_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = (
        AnchorToken(
            "s-1",
            "s-1:t-1",
            "Срібна",
            0,
            6,
            ({"pos": "adj", "raw": "adj:f:s:v_naz", "lemma": "срібний"},),
        ),
        AnchorToken(
            "s-1",
            "s-1:t-2",
            "вода",
            7,
            11,
            ({"pos": "noun", "raw": "noun:inanim:f:s:v_naz", "lemma": "вода"},),
        ),
        AnchorToken("s-1", "s-1:t-3", "тече", 12, 16, ()),
        AnchorToken("s-1", "s-1:t-4", "біля", 17, 21, ()),
        AnchorToken(
            "s-1",
            "s-1:t-5",
            "річки",
            22,
            27,
            ({"pos": "noun", "raw": "noun:inanim:f:s:v_rod", "lemma": "річка"},),
        ),
    )
    sentence = AnchorSentence("s-1", "Срібна вода тече біля річки.", tokens)
    source_tags = {"adj", "f", "s", "v_naz"}
    replacement_tags = {"adj", "f", "s", "v_rod"}
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3._safe_inflection_rows",
        lambda _token: (("Срібної", "adj", source_tags, replacement_tags),),
    )
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3._replacement_tag_sets",
        lambda _token, _form, *, pos: (replacement_tags,) if pos == "adj" else (),
    )

    rows = _contextual_error_replacements(sentence, tokens[0], one_per_mismatch_class=False)

    assert rows == (
        (
            "Срібної",
            "agreement-case",
            "the mutated adjective no longer agrees with its visible source noun",
        ),
    )


def test_production_text_question_explanations_have_real_causal_warrants() -> None:
    plan = _production_plan("text-questions")
    causal = [
        unit
        for unit in plan.units
        if unit.distinctness.get("question_intent") == "explicit-causal"
    ]

    assert plan.disposition == "certified"
    assert len(causal) == 2
    assert all(
        re.search(
            r"\b(?:тому|бо|адже|оскільки|завдяки|через\s+те)\b",
            unit.rendering_surface or "",
            re.IGNORECASE,
        )
        for unit in causal
    )
    assert all(unit.distinctness.get("question_frame") for unit in plan.units)


def test_colons_and_dashes_do_not_create_text_question_causal_capacity() -> None:
    anchor = _PRODUCTION_ANCHOR
    unwarranted = (
        anchor.replace(", бо ", ": ")
        .replace(", щоб ", " — ")
        .replace(", оскільки ", ": ")
    )
    inventory = inventory_from_anchor(
        unwarranted,
        scheduled_types=("text-questions",),
        duration_minutes=45,
    )
    selected = inventory_for_group(
        inventory,
        activity_type="text-questions",
        group_number=1,
    )
    plan = BUILDERS["text-questions"](selected, slot_id="P1-A1", phase=1)

    assert not any(
        candidate.question_intent == "explicit-causal" for candidate in inventory.candidates
    )
    assert plan.disposition == "unavailable"


def test_production_true_false_refuses_generic_predicate_negation() -> None:
    plan = _production_plan("true-false")

    assert plan.disposition == "unavailable"
    assert plan.units == ()


def test_match_up_lemma_prefers_geographic_parse_and_rejects_person_homonym() -> None:
    token = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-1",
        surface="Львові",
        start_offset=0,
        end_offset=6,
        vesum_parses=(
            {
                "pos": "noun",
                "raw": "noun:anim:m:v_dav:prop:fname",
                "lemma": "Лев",
            },
            {
                "pos": "noun",
                "raw": "noun:inanim:m:v_mis:prop:geo",
                "lemma": "Львів",
            },
        ),
    )

    assert _lemma_for(token) == "Львів"


def test_anchor_inventory_splits_an_unpunctuated_title_from_the_first_sentence() -> None:
    rows = _sentences("Короткий заголовок\nМи читаємо текст.")

    assert [row.text for row in rows] == ["Короткий заголовок", "Ми читаємо текст."]


def test_comparison_focus_prioritizes_certified_degree_forms() -> None:
    comparative = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-1",
        surface="тепліша",
        start_offset=0,
        end_offset=7,
        vesum_parses=({"pos": "adj", "raw": "adj:f:v_naz:compc", "lemma": "тепліший"},),
    )
    unrelated = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-2",
        surface="квартира",
        start_offset=8,
        end_offset=16,
        vesum_parses=({"pos": "noun", "raw": "noun:inanim:f:v_naz", "lemma": "квартира"},),
    )

    focus = "ступені порівняння прикметників"
    assert _focus_rank(comparative, focus) < _focus_rank(unrelated, focus)


def test_predicative_adverb_homonym_is_not_an_adjective_degree_target() -> None:
    ambiguous = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-1",
        surface="тепліше",
        start_offset=0,
        end_offset=7,
        vesum_parses=(
            {"pos": "adj", "raw": "adj:n:v_naz:compc", "lemma": "тепліший"},
            {"pos": "adv", "raw": "adv:compc:predic", "lemma": "тепло"},
        ),
    )

    assert not _is_degree_token(ambiguous)


def test_closed_class_homograph_cannot_supply_a_content_choice_bank() -> None:
    token = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-1",
        surface="Під",
        start_offset=0,
        end_offset=3,
        vesum_parses=(
            {"pos": "prep", "raw": "prep", "lemma": "під"},
            {"pos": "noun", "raw": "noun:inanim:m:v_naz", "lemma": "под"},
        ),
    )

    assert _certified_choice_bank(token) is None


@pytest.mark.parametrize(
    ("surface", "parses"),
    (
        (
            "зараз",
            (
                {"pos": "adv", "raw": "adv:pron:dem", "lemma": "зараз"},
                {"pos": "noun", "raw": "noun:inanim:p:v_rod", "lemma": "зараза"},
            ),
        ),
        (
            "кілька",
            (
                {"pos": "numr", "raw": "numr:p:v_naz:pron:ind", "lemma": "кілька"},
                {"pos": "noun", "raw": "noun:anim:f:v_naz", "lemma": "кілька"},
            ),
        ),
    ),
)
def test_cross_pos_homograph_cannot_supply_a_morphology_choice_bank(
    surface: str, parses
) -> None:
    token = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-1",
        surface=surface,
        start_offset=0,
        end_offset=len(surface),
        vesum_parses=parses,
    )

    assert _certified_choice_bank(token) is None


def test_choice_bank_deduplicates_equivalent_morphological_analyses(monkeypatch) -> None:
    token = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-1",
        surface="щулишся",
        start_offset=0,
        end_offset=8,
        vesum_parses=(
            {"pos": "verb", "raw": "verb:rev:imperf:pres:s:2", "lemma": "щулитися"},
        ),
    )
    monkeypatch.setattr(
        "hramatka.engine.anchor_inventory_v3.verify_lemma",
        lambda *_args, **_kwargs: (
            {"word_form": "щулитесь", "pos": "verb", "tags": "verb:rev:imperf:pres:p:2"},
            {"word_form": "щулитеся", "pos": "verb", "tags": "verb:rev:imperf:pres:p:2"},
            {"word_form": "щулюся", "pos": "verb", "tags": "verb:rev:imperf:pres:s:1"},
        ),
    )

    bank = _certified_choice_bank(token)

    assert bank is not None
    assert bank[0] == ("щулишся", "щулитесь", "щулюся")


def test_application_carrier_prefers_transferable_action_over_natural_process() -> None:
    action_text = "Можна активно виправлятися і вчинити щось благородне."
    action = AnchorSentence(
        "s-1",
        action_text,
        (
            AnchorToken(
                "s-1",
                "s-1:t-1",
                "виправлятися",
                action_text.index("виправлятися"),
                action_text.index("виправлятися") + len("виправлятися"),
                ({"pos": "verb", "raw": "verb:imperf:inf", "lemma": "виправлятися"},),
            ),
            AnchorToken(
                "s-1",
                "s-1:t-2",
                "вчинити",
                action_text.index("вчинити"),
                action_text.index("вчинити") + len("вчинити"),
                ({"pos": "verb", "raw": "verb:perf:inf", "lemma": "вчинити"},),
            ),
        ),
    )
    scenery_text = "Сонце засяє за кілька хвилин."
    scenery = AnchorSentence(
        "s-2",
        scenery_text,
        (
            AnchorToken(
                "s-2",
                "s-2:t-1",
                "Сонце",
                0,
                5,
                ({"pos": "noun", "raw": "noun:inanim:n:v_naz", "lemma": "сонце"},),
            ),
            AnchorToken(
                "s-2",
                "s-2:t-2",
                "хвилин",
                scenery_text.index("хвилин"),
                scenery_text.index("хвилин") + len("хвилин"),
                ({"pos": "noun", "raw": "noun:inanim:p:v_rod", "lemma": "хвилина"},),
            ),
        ),
    )

    assert _application_carrier_rank(action) < _application_carrier_rank(scenery)


def test_second_person_narration_is_not_a_standalone_question_proposition() -> None:
    text = "Сидячи на кормі, щулишся від ранкової прохолоди."
    sentence = AnchorSentence(
        "s-1",
        text,
        (
            AnchorToken(
                "s-1",
                "s-1:t-1",
                "Сидячи",
                0,
                len("Сидячи"),
                ({"pos": "verb", "raw": "verb:imperf:advp", "lemma": "сидіти"},),
            ),
            AnchorToken(
                "s-1",
                "s-1:t-2",
                "щулишся",
                text.index("щулишся"),
                text.index("щулишся") + len("щулишся"),
                (
                    {
                        "pos": "verb",
                        "raw": "verb:rev:imperf:pres:s:2",
                        "lemma": "щулитися",
                    },
                ),
            ),
            AnchorToken(
                "s-1",
                "s-1:t-3",
                "прохолоди",
                text.index("прохолоди"),
                text.index("прохолоди") + len("прохолоди"),
                (
                    {
                        "pos": "noun",
                        "raw": "noun:inanim:f:v_rod",
                        "lemma": "прохолода",
                    },
                ),
            ),
        ),
    )

    assert not _safe_source_proposition_carrier(sentence)


def test_anaphoric_pronoun_is_not_a_standalone_question_proposition() -> None:
    text = "На нього дивиться ранкове сонце."
    surfaces = (
        (
            "нього",
            ({"pos": "noun", "raw": "noun:m:v_rod:pron:pers:3", "lemma": "він"},),
        ),
        ("дивиться", ({"pos": "verb", "raw": "verb:rev:pres:s:3", "lemma": "дивитися"},)),
        ("ранкове", ({"pos": "adj", "raw": "adj:n:v_naz", "lemma": "ранковий"},)),
        ("сонце", ({"pos": "noun", "raw": "noun:inanim:n:v_naz", "lemma": "сонце"},)),
    )
    sentence = AnchorSentence(
        "s-1",
        text,
        tuple(
            AnchorToken(
                "s-1",
                f"s-1:t-{index}",
                surface,
                text.index(surface),
                text.index(surface) + len(surface),
                parses,
            )
            for index, (surface, parses) in enumerate(surfaces, start=1)
        ),
    )

    assert not _safe_source_proposition_carrier(sentence)


def test_imperative_target_cannot_supply_an_ambiguous_closed_choice_bank() -> None:
    token = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-1",
        surface="Візьміть",
        start_offset=0,
        end_offset=7,
        vesum_parses=(
            {"pos": "verb", "raw": "verb:perf:impr:p:2", "lemma": "взяти"},
        ),
    )

    assert _certified_choice_bank(token) is None


def test_short_writing_rejects_an_ambiguous_content_lemma() -> None:
    token = AnchorToken(
        sentence_id="s-1",
        token_id="s-1:t-1",
        surface="зв'язки",
        start_offset=0,
        end_offset=7,
        vesum_parses=(
            {"pos": "noun", "raw": "noun:inanim:p:v_naz", "lemma": "зв'язок"},
            {"pos": "noun", "raw": "noun:inanim:p:v_naz", "lemma": "зв'язка"},
        ),
    )
    sentence = AnchorSentence("s-1", "зв'язки допомагають.", (token,))

    assert _eligible_tokens("short-writing", sentence) == ()


def test_match_up_builder_accepts_only_certified_atlas_antonyms() -> None:
    plan = BUILDERS["match-up"](complete_inventory(), slot_id="P1-A1", phase=1)

    assert plan.disposition == "certified"
    assert all(unit.distinctness["pair"]["relation"] == "atlas_antonym.v1" for unit in plan.units)
    assert all(
        unit.allowed_forms[0].casefold() != unit.allowed_forms[1].casefold() for unit in plan.units
    )


def test_production_error_correction_changes_exactly_one_word_per_item() -> None:
    plan = _production_plan("error-correction")

    assert len({unit.distinctness["morphology_class"] for unit in plan.units}) >= 3
    assert sum(
        unit.distinctness["error_position"] == "sentence-initial" for unit in plan.units
    ) <= 4
    for unit in plan.units:
        assert unit.distinctness["frame_family"] == "contextual-mismatch.v1"
        assert unit.distinctness["semantic_warrant"]
        wrong_words = _WORD_RE.findall(unit.allowed_forms[0])
        source_words = _WORD_RE.findall(unit.rendering_surface or "")
        assert len(wrong_words) == len(source_words)
        assert (
            sum(
                wrong.casefold() != source.casefold()
                for wrong, source in zip(wrong_words, source_words, strict=True)
            )
            == 1
        )
        assert unit.expected_key_or_rule.certified_error_count == 1


@pytest.mark.parametrize(
    ("text", "target"),
    (
        ("Українці читають книги, бо працюють.", "працюють"),
        ("Українці читають і працюють.", "працюють"),
        ("Пристрій прочитає книги.", "прочитає"),
        ("Ми і студенти працюють.", "працюють"),
    ),
)
def test_contextual_error_correction_rejects_ambiguous_subject_licensers(
    text: str, target: str
) -> None:
    sentence = _sentences(text)[0]
    token = next(token for token in sentence.tokens if token.surface.casefold() == target)

    assert _error_replacement(sentence, token) is None


def test_contextual_error_correction_rejects_syncretic_alternative_forms() -> None:
    adjective_sentence = _sentences("Регулярне читання розвиває мозок.")[0]
    adjective = next(
        token for token in adjective_sentence.tokens if token.surface == "Регулярне"
    )
    adjective_rows = _contextual_error_replacements(adjective_sentence, adjective)
    assert all(form.casefold() != "регулярні" for form, _class, _warrant in adjective_rows)

    government_sentence = _sentences("Без книг читання допомагає мозку.")[0]
    governed = next(token for token in government_sentence.tokens if token.surface == "книг")
    government_rows = _contextual_error_replacements(government_sentence, governed)
    assert all(form.casefold() != "книги" for form, _class, _warrant in government_rows)


def test_contextual_error_correction_rejects_cross_clause_adjective_head() -> None:
    sentence = _sentences("Регулярне розвиває, читання допомагає.")[0]
    adjective = next(token for token in sentence.tokens if token.surface == "Регулярне")

    assert _contextual_error_replacements(sentence, adjective) == ()


def test_production_short_writing_uses_a_self_contained_source_prompt() -> None:
    plan = _production_plan("short-writing")
    prompt = plan.units[0].rendering_surface or ""

    assert plan.disposition == "certified"
    assert not re.match(
        r"^(?:так(?:ий|а|е|і)|це|цей|ця|ці|також|тому)\b",
        prompt,
        re.IGNORECASE,
    )
    assert not prompt.endswith("!")
    assert plan.units[0].allowed_forms[0] == (
        f"Спирайтеся на цю думку з тексту: «{prompt}»"
    )
    assert all("лем" not in marker.casefold() for marker in plan.units[0].allowed_forms)


def test_degree_writing_still_validates_non_lemma_source_constraints() -> None:
    inventory = complete_inventory()
    task = inventory.writing_tasks[0]
    constraints = (
        ConstraintSpec("contains_lemma_set", {"lemmas": DEGREE_WRITING_LEMMAS}),
        ConstraintSpec("word_count_range", {"minimum": 60, "maximum": 80}),
        ConstraintSpec("min_verb_count", {"minimum": 100}),
    )
    degree_task = replace(
        task,
        prompt=DEGREE_WRITING_SCENARIO,
        constraints=constraints,
        focus_alignment="degree-writing",
        attribute_warrants=DEGREE_WRITING_WARRANTS,
    )

    plan = build_short_writing(
        replace(inventory, writing_tasks=(degree_task,)),
        slot_id="P3-A1",
        phase=3,
    )

    assert plan.disposition == "unavailable"


def test_generic_error_correction_is_not_mislabelled_as_degree_reinforcement() -> None:
    plan = _production_plan(
        "error-correction",
        focus="ступені порівняння прикметників",
    )

    assert plan.disposition == "certified"
    assert all(unit.distinctness.get("focus_alignment") is None for unit in plan.units)


@pytest.mark.parametrize(
    ("duration", "marker"),
    ((45, "від 60 до 80 слів"), (60, "від 80 до 110 слів"), (90, "від 120 до 160 слів")),
)
def test_production_short_writing_uses_visible_duration_specific_range(
    duration: int, marker: str
) -> None:
    plan = _production_plan("short-writing", duration=duration)

    assert plan.disposition == "certified"
    assert marker in plan.units[0].allowed_forms
    assert "до 200 слів" not in plan.units[0].allowed_forms


def test_fill_in_builder_carries_exact_evidence_rendering_surfaces() -> None:
    inventory = complete_inventory()

    plan = BUILDERS["fill-in"](inventory, slot_id="P1-A3", phase=1)

    assert plan.disposition == "certified"
    assert all(unit.rendering_surface for unit in plan.units)
    assert {unit.rendering_surface for unit in plan.units} <= {
        candidate.literal_evidence
        for candidate in inventory.candidates
        if candidate.activity_type == "fill-in"
    }


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

    assert MUTATION_CATALOG_VERSION == "true-false-mutations.v2"
    assert statement is not None
    assert verify_false_statement("replace-one-surface.v1", binding, statement)
    assert construct_false_statement("free-form", binding) is None
    assert not verify_false_statement("free-form", binding, statement)


@pytest.mark.parametrize(
    ("literal", "surface", "start", "expected"),
    (
        (
            "Ночуватимуть тут же на березі, у наметі.",
            "Ночуватимуть",
            0,
            "Не ночуватимуть тут же на березі, у наметі.",
        ),
        (
            "Вона працює щодня до восьмої години вечора.",
            "працює",
            5,
            "Вона не працює щодня до восьмої години вечора.",
        ),
        (
            "— Ночуватимуть тут же на березі, у наметі.",
            "Ночуватимуть",
            2,
            "— Не ночуватимуть тут же на березі, у наметі.",
        ),
        (
            "А він сказав: — Ночуватимуть тут же.",
            "Ночуватимуть",
            16,
            "А він сказав: — Не ночуватимуть тут же.",
        ),
    ),
)
def test_true_false_negation_binds_one_exact_predicate_offset(
    literal: str, surface: str, start: int, expected: str
) -> None:
    binding = MutationBinding(
        sentence_id="s-1",
        literal_evidence=literal,
        source_surface=surface,
        replacement_surface=f"не {surface.casefold()}",
        source_start_offset=start,
        source_end_offset=start + len(surface),
    )

    statement = construct_false_statement("negate-asserted-predicate.v1", binding)

    assert statement == expected
    assert verify_false_statement("negate-asserted-predicate.v1", binding, statement)


@pytest.mark.parametrize(
    "truth_pattern",
    (
        (True, False) * 4,
        (False, True) * 4,
        (True,) * 4 + (False,) * 4,
        (False,) * 4 + (True,) * 4,
    ),
)
def test_true_false_answer_key_breaks_every_trivial_balanced_pattern(
    truth_pattern: tuple[bool, ...],
) -> None:
    facts = [
        TrueFalseFact(
            fact_id=f"tf-{index}",
            sentence_id=f"s-{index}",
            literal_evidence=f"Речення {index}.",
            source_surface="Речення",
            replacement_surface="Не речення",
            truth_value=truth_value,
            mutation_rule_id="negate-asserted-predicate.v1",
        )
        for index, truth_value in enumerate(truth_pattern, start=1)
    ]

    _break_trivial_truth_pattern(facts)

    resulting_pattern = tuple(fact.truth_value for fact in facts)
    assert resulting_pattern != truth_pattern
    assert Counter(resulting_pattern) == Counter(truth_pattern)


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
        "source_proposition",
        "target_case_usage",
        "word_count_range",
    }
    source_verifiable = tuple(spec for spec in task.constraints if spec.kind != "word_count_range")
    assert validate_constraints(source_verifiable, task.sample_tokens)
    assert validate_constraints((case_constraint,), task.sample_tokens)
    assert not validate_constraints((ConstraintSpec("llm_judgment", {}),), task.sample_tokens)


def test_short_writing_plan_carries_exact_learner_facing_constraint_fragments() -> None:
    task = complete_inventory().writing_tasks[0]

    plan = build_short_writing(complete_inventory(), slot_id="P3-A1", phase=3)

    assert plan.disposition == "certified"
    assert plan.units[0].allowed_forms == (
        f"Спирайтеся на цю думку з тексту: «{task.prompt}»",
        "мінімум 1 дієслово",
        "від 60 до 80 слів",
    )


def test_mark_the_words_plan_records_exact_certified_target_token_records() -> None:
    plan = build_mark_the_words(complete_inventory(), slot_id="P1-A1", phase=1)

    assert plan.disposition == "certified"
    assert len(plan.certified_target_tokens) >= floor_for("mark-the-words").minimum_units
    assert all(
        token.surface and token.end_offset > token.start_offset
        for token in plan.certified_target_tokens
    )


def test_comparison_mark_rejects_plain_compb_adjectives() -> None:
    sentences = []
    target_ids = []
    for sentence_number, text in enumerate(
        ("Теплий світлий тихий довгий.", "Новий добрий простий сильний."),
        start=1,
    ):
        sentence_id = f"s-{sentence_number}"
        tokens = []
        for token_number, match in enumerate(_WORD_RE.finditer(text), start=1):
            token_id = f"{sentence_id}:t-{token_number}"
            target_ids.append(token_id)
            tokens.append(
                AnchorToken(
                    sentence_id=sentence_id,
                    token_id=token_id,
                    surface=match.group(0),
                    start_offset=match.start(),
                    end_offset=match.end(),
                    vesum_parses=(
                        {"pos": "adj", "raw": "adj:m:v_naz:compb", "lemma": match.group(0)},
                    ),
                )
            )
        sentences.append(AnchorSentence(sentence_id=sentence_id, text=text, tokens=tuple(tokens)))
    inventory = CertificationInventory(
        source_id="comparison-base-only",
        sentences=tuple(sentences),
        mark_requests=(
            MarkTheWordsRequest(
                request_id="degree-comparison",
                sentence_ids=("s-1", "s-2"),
                criterion="degree=comparison",
                target_token_ids=tuple(target_ids),
            ),
        ),
    )

    plan = build_mark_the_words(inventory, slot_id="P2-A5", phase=2)

    assert plan.disposition == "unavailable"


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
                target_token_ids=request.target_token_ids[
                    : floor_for("mark-the-words").minimum_units - 1
                ],
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
