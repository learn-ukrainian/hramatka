"""Grounding-mode v1 — Slice 3 acceptance (gate bifurcation + RULE_REGISTRY + tray).

Contract: hramatka/grounding-mode-v1-final-spec.md §slice-3, §5.4, §6.2–§6.5, §0.
Flags-off path remains byte-identical production extractive behavior.
"""

from __future__ import annotations

import copy
import json
import math

import pytest

from hramatka.engine import flags, pipeline, registry, retrieval, schema
from hramatka.engine.fixtures import load_anchor
from hramatka.engine.gates import derived, evidence_span, numeral


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _kit_from_lemmas(*lemmas: str, numerals: list[str] | None = None) -> dict:
    """Synthetic non-empty kit for unit gates (status=available)."""
    return {
        "version": "kit_enrichment_v1",
        "status": "available",
        "anchor_lemmas": sorted(lemmas),
        "facets": {
            "synonym_banks": "empty",
            "cefr_tags": "empty",
            "aspect_stress": "absent",
            "focus_citation_forms": "empty",
            "numeral_tuples": "available" if numerals else "empty",
        },
        "synonym_banks": [],
        "cefr_tags": [],
        "aspect_stress": {"status": "absent", "pairs": [], "stress": [], "reason": "test"},
        "focus_citation_forms": {"status": "empty", "forms": []},
        "numeral_tuples": [
            {
                "value": n,
                "case": ["v_naz"],
                "gender": [],
                "trigger": None,
                "noun_lemma": ["ділянка"],
                "witness_span": f"{n} ділянок",
            }
            for n in (numerals or [])
        ],
    }


def _fill_in_kit_item(
    *,
    sentence: str,
    answer: str,
    options: list[str],
    lemmas: list[str],
    witness: str,
    index: int = 0,
) -> dict:
    return {
        "sentence": sentence,
        "answer": answer,
        "options": options,
        "kit_anchors": {
            "lemmas": lemmas,
            "witness_span": witness,
        },
    }


def _error_correction_kit_item(
    *,
    sentence: str,
    error: str,
    correction: str,
    options: list[str],
    lemmas: list[str],
    witness: str,
    rule_id: str | None = "numeral_moat",
) -> dict:
    ka: dict = {
        "lemmas": lemmas,
        "witness_span": witness,
    }
    if rule_id is not None:
        ka["rule_id"] = rule_id
    return {
        "sentence": sentence,
        "error": error,
        "correction": correction,
        "options": options,
        "explanation": "Керування числівника.",
        "kit_anchors": ka,
    }


ANCHOR = (
    "На думку вчених, читання є одним з найскладніших завдань для мозку. "
    "Під час читання активізуються одразу 17 ділянок головного мозку."
)


# ---------------------------------------------------------------------------
# RULE_REGISTRY + numeral MOAT oracle (≥3 real government cases)
# ---------------------------------------------------------------------------
def test_rule_registry_numeral_moat_is_first_entry():
    assert derived.RULE_NUMERAL_MOAT in derived.RULE_REGISTRY
    assert list(derived.RULE_REGISTRY.keys())[0] == derived.RULE_NUMERAL_MOAT
    assert derived.RULE_REGISTRY[derived.RULE_NUMERAL_MOAT] is derived.verify_numeral_moat


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("двадцять одна книга", "pass"),  # ends_1 + f sg
        ("двадцять дві книги", "pass"),  # ends_2_4 + f pl
        ("двадцять п'ять книг", "pass"),  # ends_5_9_0 + gen pl
        ("двадцять дві книг", "fail"),  # wrong government
        ("двадцять п'ять книги", "fail"),
    ],
)
def test_numeral_moat_oracle_real_government_cases(phrase, expected):
    """Python MOAT decides; LLM judgment is never authoritative."""
    direct = numeral.check_numeral_government(phrase)
    via_registry = derived.verify_numeral_moat({"phrase": phrase})
    assert direct["status"] == expected
    if expected == "pass":
        assert via_registry["status"] == "pass"
    else:
        assert via_registry["status"] == "fail"
    # Registry path must not invent a pass when the oracle fails.
    assert (via_registry["status"] == "pass") == (direct["status"] == "pass")


def test_unknown_rule_id_always_rejects():
    result = derived.check_rule_id("made_up_agreement_rule")
    assert result["status"] == "fail"
    assert "Unknown rule_id" in result["detail"]
    missing = derived.check_rule_id(None)
    assert missing["status"] == "fail"


# ---------------------------------------------------------------------------
# Kit closure fail-closed
# ---------------------------------------------------------------------------
def test_empty_kit_fails_closed():
    result = derived.check_kit_closure(["читання зміцнює пам'ять"], kit=None)
    assert result["status"] == "fail"
    assert "Empty kit" in result["detail"] or "empty" in result["detail"].casefold()

    empty = {"status": "empty", "anchor_lemmas": []}
    assert retrieval.kit_is_empty(empty)
    result2 = derived.check_kit_closure(["читання"], kit=empty)
    assert result2["status"] == "fail"


def test_token_outside_kit_closure_fails():
    kit = _kit_from_lemmas("читання", "мозок", "завдання")
    # «телевізор» is real UA but not in this kit closure.
    result = derived.check_kit_closure(["читання і телевізор"], kit=kit)
    assert result["status"] == "fail"
    assert "kit_closure" in result["rule"]


def test_tokens_inside_kit_closure_pass():
    kit = _kit_from_lemmas("читання", "мозок", "завдання")
    result = derived.check_kit_closure(["читання завдання мозку"], kit=kit)
    assert result["status"] == "pass"


def test_kit_closure_uses_any_candidate_lemma_for_duzhe_duzhyi(monkeypatch):
    """#228: «дуже» may also parse as adjective lemma «дужий»."""
    monkeypatch.setattr(
        derived.retrieval,
        "lemmatize",
        lambda _text: {"дуже": {"дуже", "дужий"}},
    )
    monkeypatch.setattr(
        derived.vesum_tags,
        "parse_word",
        lambda _surface: [
            {"lemma": "дуже", "pos": "adv", "raw": "adv:compb"},
            {"lemma": "дужий", "pos": "adj", "raw": "adj:n:v_naz:compb"},
        ],
    )
    # Isolate the candidate-lemma rule from the #228 function-adverb exemption.
    monkeypatch.setattr(derived, "KIT_CLOSURE_FUNCTION_ADVERBS", frozenset(), raising=False)

    result = derived.check_kit_closure(["дуже"], _kit_from_lemmas("дуже"))

    assert result["status"] == "pass"


def test_kit_closure_allows_natural_sentence_with_closed_class_words(monkeypatch):
    """#228: closed-class words do not consume kit-closure vocabulary budget."""
    monkeypatch.setattr(
        derived.retrieval,
        "lemmatize",
        lambda _text: {
            "це": {"це", "цей"},
            "дуже": {"дуже", "дужий"},
            "важливо": {"важливо"},
        },
    )
    parses = {
        "це": [
            {"lemma": "це", "pos": "noun", "raw": "noun:inanim:n:v_naz:pron:dem"},
            {"lemma": "це", "pos": "part", "raw": "part"},
        ],
        "дуже": [
            {"lemma": "дуже", "pos": "adv", "raw": "adv:compb"},
            {"lemma": "дужий", "pos": "adj", "raw": "adj:n:v_naz:compb"},
        ],
        "важливо": [{"lemma": "важливо", "pos": "adv", "raw": "adv:compb:predic"}],
    }
    monkeypatch.setattr(derived.vesum_tags, "parse_word", lambda surface: parses[surface])

    result = derived.check_kit_closure(["Це дуже важливо…"], _kit_from_lemmas("важливо"))

    assert result["status"] == "pass"


@pytest.mark.parametrize(
    ("surface", "legacy_candidates", "parses"),
    [
        (
            "він",
            {"він"},
            [{"lemma": "він", "pos": "noun", "raw": "noun:unanim:m:v_naz:pron:pers:3"}],
        ),
        ("бо", {"бо"}, [{"lemma": "бо", "pos": "conj", "raw": "conj:subord"}]),
        ("від", {"від"}, [{"lemma": "від", "pos": "prep", "raw": "prep"}]),
        ("ой", {"ой"}, [{"lemma": "ой", "pos": "intj", "raw": "intj"}]),
        (
            "був",
            {"булий"},
            [{"lemma": "бути", "pos": "verb", "raw": "verb:imperf:past:m"}],
        ),
    ],
)
def test_kit_closure_exempts_closed_class_vesum_parses(
    monkeypatch, surface, legacy_candidates, parses
):
    """#228 exemption predicates use VESUM tags/lemma, never semantic guesses."""
    monkeypatch.setattr(
        derived.retrieval,
        "lemmatize",
        lambda _text: {surface: legacy_candidates},
    )
    monkeypatch.setattr(derived.vesum_tags, "parse_word", lambda _surface: parses)

    result = derived.check_kit_closure([surface], _kit_from_lemmas("важливо"))

    assert result["status"] == "pass"


@pytest.mark.parametrize(
    "surface",
    ["дуже", "також", "вже", "ще", "потім", "тепер", "тоді", "майже", "треба", "можна"],
)
def test_kit_closure_exempts_curated_function_adverbs(monkeypatch, surface):
    """#228 keeps the VESUM-verified surface allowlist deliberately small."""
    monkeypatch.setattr(
        derived.retrieval,
        "lemmatize",
        lambda _text: {surface: {f"outside-{surface}"}},
    )
    monkeypatch.setattr(derived.vesum_tags, "parse_word", lambda _surface: [])

    result = derived.check_kit_closure([surface], _kit_from_lemmas("важливо"))

    assert result["status"] == "pass"


# ---------------------------------------------------------------------------
# G5 diversity
# ---------------------------------------------------------------------------
def test_diversity_rejects_mono_cluster_batch():
    # ceil(6/3)=2; mono-cluster of 6 → reject
    clusters = ["читання"] * 6
    result = derived.check_batch_diversity(clusters)
    assert result["status"] == "fail"
    assert "diversity" in result["rule"]


def test_diversity_accepts_sufficient_clusters():
    # N=6 requires 2 clusters
    clusters = ["читання", "читання", "читання", "мозок", "мозок", "мозок"]
    assert derived.required_diversity_clusters(6) == 2
    result = derived.check_batch_diversity(clusters)
    assert result["status"] == "pass"


def test_diversity_ceil_formula():
    assert derived.required_diversity_clusters(1) == 1
    assert derived.required_diversity_clusters(3) == 1
    assert derived.required_diversity_clusters(4) == 2
    assert derived.required_diversity_clusters(9) == 3
    assert math.ceil(7 / 3) == derived.required_diversity_clusters(7)


# ---------------------------------------------------------------------------
# G1 entity/numeral bound
# ---------------------------------------------------------------------------
def test_g1_rejects_new_named_entity_outside_kit():
    kit = _kit_from_lemmas("читання", "мозок")
    # Альцгеймера is VESUM prop:lname in fixtures and not in this kit/anchor.
    result = derived.check_g1_entity_numeral_bound(
        ["читання і Альцгеймера"],
        kit,
        anchor_body="читання зміцнює мозок",
    )
    assert result["status"] == "fail"
    assert "named entities" in result["detail"]


def test_g1_rejects_numeral_outside_kit():
    kit = _kit_from_lemmas("читання", "ділянка")  # no numeral_tuples
    result = derived.check_g1_entity_numeral_bound(
        ["активізуються 99 ділянок"],
        kit,
        anchor_body="читання зміцнює мозок",
    )
    assert result["status"] == "fail"
    assert "numeral" in result["detail"].casefold()


def test_g1_allows_kit_numeral():
    kit = _kit_from_lemmas("читання", "ділянка", numerals=["17"])
    result = derived.check_g1_entity_numeral_bound(
        ["активізуються 17 ділянок"],
        kit,
        anchor_body="активізуються одразу 17 ділянок головного мозку",
    )
    assert result["status"] == "pass"


def test_g1_exempts_indefinite_quantifiers(monkeypatch):
    """#228: indefinite quantifiers are not verifiable fact-numerals."""
    parses = {
        "багато": [{"lemma": "багато", "pos": "numr", "raw": "numr:p:v_naz:pron:ind"}],
        "мало": [{"lemma": "мало", "pos": "adv", "raw": "adv:compb:predic"}],
        "кілька": [{"lemma": "кілька", "pos": "numr", "raw": "numr:p:v_naz:pron:ind"}],
        "декілька": [{"lemma": "декілька", "pos": "numr", "raw": "numr:p:v_naz:pron:ind"}],
        "чимало": [{"lemma": "чимало", "pos": "adv", "raw": "adv"}],
    }
    monkeypatch.setattr(derived.vesum_tags, "parse_word", lambda token: parses[token.casefold()])

    result = derived.check_g1_entity_numeral_bound(
        ["Багато мало кілька декілька чимало"],
        _kit_from_lemmas("читання"),
        anchor_body="читання зміцнює мозок",
    )

    assert result["status"] == "pass"


def test_g1_still_rejects_unlisted_word_numeral():
    result = derived.check_g1_entity_numeral_bound(
        ["двадцять ділянок"],
        _kit_from_lemmas("ділянка"),
        anchor_body="читання зміцнює мозок",
    )

    assert result["status"] == "fail"
    assert "двадцять" in result["detail"]


# ---------------------------------------------------------------------------
# Quoting evidence_span still fail-closed (no regression)
# ---------------------------------------------------------------------------
def test_quoting_evidence_span_still_fail_closed(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    missing = evidence_span.check_evidence("", ANCHOR)
    assert missing["status"] == "fail"
    absent = evidence_span.check_evidence("this quote is not in the anchor at all", ANCHOR)
    assert absent["status"] == "fail"

    # True-false still requires evidence under the flag.
    errors = registry.ACTIVITY_REGISTRY["true-false"].raw_validator(
        {
            "type": "true-false",
            "instruction": "Познач.",
            "items": [{"statement": "A", "correct": True}],
        }
    )
    assert any("evidence" in err for err in errors)


def test_flags_off_fill_in_still_requires_evidence_restore(monkeypatch):
    """Production path: flags off → extractive fill-in hardness unchanged."""
    monkeypatch.delenv("HRAMATKA_GROUNDING_MODE_V1", raising=False)
    monkeypatch.delenv("HRAMATKA_TEACHER_REVIEW_TRAY_V1", raising=False)
    gr = schema.GateResult()
    activity = {
        "type": "fill-in",
        "items": [
            {
                "sentence": "Читання є ____ для мозку.",
                "answer": "завданням",
                "options": ["завданням", "книжкою", "телевізором", "ризиком"],
            }
        ],
    }
    # No evidence → extractive path fails evidence_span.
    registry._gate_fill_in(activity, evidence=[], anchor_body=ANCHOR, gr=gr)
    assert any(c.gate == "evidence_span" and c.status == "fail" for c in gr.checks)


# ---------------------------------------------------------------------------
# Tray hard-gate on publish
# ---------------------------------------------------------------------------
def test_derived_publish_allowed_requires_tray(monkeypatch):
    monkeypatch.delenv("HRAMATKA_TEACHER_REVIEW_TRAY_V1", raising=False)
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    assert flags.teacher_review_tray_v1_operational() is False
    assert flags.derived_publish_allowed() is False

    monkeypatch.setenv("HRAMATKA_TEACHER_REVIEW_TRAY_V1", "1")
    assert flags.teacher_review_tray_v1_operational() is True
    assert flags.derived_publish_allowed() is True


def test_tray_off_publish_raises_dependency_error(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    monkeypatch.delenv("HRAMATKA_TEACHER_REVIEW_TRAY_V1", raising=False)
    with pytest.raises(flags.DerivedPublishDependencyError) as exc_info:
        flags.assert_derived_publish_allowed()
    assert "teacher_review_tray_v1" in str(exc_info.value)
    assert "dependency error" in str(exc_info.value).casefold()


def test_pipeline_refuses_derived_when_tray_off(monkeypatch, tmp_path):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    monkeypatch.setenv("HRAMATKA_KIT_ENRICHMENT_V1", "1")
    monkeypatch.delenv("HRAMATKA_TEACHER_REVIEW_TRAY_V1", raising=False)

    raw = {
        "type": "fill-in",
        "instruction": "Обери правильну форму.",
        "items": [
            _fill_in_kit_item(
                sentence=f"Читання є одним з найскладніших ____ для мозку ({i}).",
                answer="завдань",
                options=["завдань", "вправ", "задач", "питань"],
                lemmas=["читання", "мозок"],
                witness=f"witness {i} читання для мозку",
            )
            for i in range(3)
        ],
    }

    result = pipeline.run(
        load_anchor(),
        types=["fill-in"],
        generator=lambda _prompt: json.dumps({"activities": [raw]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        use_cache=False,
    )
    assert result.lesson_b1 == []
    assert any(
        check.gate == "derived_publish" and check.status == "fail"
        for ir in result.activities
        for check in ir.gate_result.checks
    )
    assert any(
        "teacher_review_tray_v1" in check.detail
        for ir in result.activities
        for check in ir.gate_result.checks
        if check.gate == "derived_publish"
    )


# ---------------------------------------------------------------------------
# E1: validators + gates agree on derived IR
# ---------------------------------------------------------------------------
def test_e1_validators_and_gates_agree_on_derived_ir(monkeypatch):
    """A well-formed derived item is accepted by the raw validator and fails
    the same structural IR checks at the gate layer when witness is missing;
    a complete kit_anchors IR is accepted by both layers for shape.
    """
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")

    good = {
        "type": "fill-in",
        "instruction": "Обери правильну форму.",
        "items": [
            _fill_in_kit_item(
                sentence="Читання є одним з найскладніших ____ для мозку.",
                answer="завдань",
                options=["завдань", "вправ", "задач", "питань"],
                lemmas=["читання", "мозок"],
                witness="читання є одним з найскладніших завдань для мозку",
            )
        ],
    }
    assert registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(good) == []
    clean, _ev, kit_anchors = schema.parse_raw_activity(good)
    assert len(kit_anchors) == 1
    assert kit_anchors[0].witness_span
    witness_gate = derived.check_witness_span(kit_anchors[0])
    assert witness_gate["status"] == "pass"

    # Evidence-only quote-restore: validator rejects; gate witness check fails.
    bad = {
        "type": "fill-in",
        "instruction": "Обери.",
        "items": [
            {
                "sentence": "Читання є одним з найскладніших ____ для мозку.",
                "answer": "завдань",
                "options": ["завдань", "вправ", "задач", "питань"],
                "evidence": "читання є одним з найскладніших завдань для мозку",
            }
        ],
    }
    val_errors = registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(bad)
    assert any("kit_anchors" in err or "quote-restore" in err for err in val_errors)
    # No kit_anchors → gate-level witness fails identically in spirit.
    _clean2, _ev2, ka2 = schema.parse_raw_activity(bad)
    assert ka2 == []
    assert derived.check_witness_span(None)["status"] == "fail"

    # Missing witness_span: both layers fail.
    missing = copy.deepcopy(good)
    missing["items"][0]["kit_anchors"] = {"lemmas": ["читання"]}
    val_missing = registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(missing)
    assert any("witness_span" in err for err in val_missing)
    _c3, _e3, ka3 = schema.parse_raw_activity(missing)
    assert ka3 and derived.check_witness_span(ka3[0])["status"] == "fail"


def test_derived_short_writing_gate_requires_constraints():
    """The derived gate protects direct callers that bypass pack validation."""
    activity = {
        "type": "short-writing",
        "instruction": "Напишіть короткий текст.",
        "prompt": "Читання розвиває мозок.",
        "source_ref": "Текст-опора",
        "word_count_guidance": "30–40 слів",
    }
    anchors = [
        schema.KitAnchors(
            lemmas=["читання", "мозок"],
            witness_span="читання є одним з найскладніших завдань для мозку",
            locator="text",
        )
    ]
    gr = schema.GateResult()
    derived.gate_derived_activity(
        activity,
        anchors,
        _kit_from_lemmas("читання", "мозок"),
        ANCHOR,
        gr,
    )
    assert any(
        check.gate == "short_writing_constraints" and check.status == "fail"
        for check in gr.checks
    )


def test_e1_error_correction_rule_id_validator_and_gate(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    item = _error_correction_kit_item(
        sentence="Під час читання активізуються одразу 17 ділянки головного мозку.",
        error="ділянки",
        correction="ділянок",
        options=["ділянки", "ділянок", "книжки", "завдання"],
        lemmas=["ділянка", "мозок"],
        witness="17 ділянок головного мозку",
        rule_id="numeral_moat",
    )
    raw = {
        "type": "error-correction",
        "instruction": "Виправ помилку.",
        "items": [item],
    }
    assert registry.ACTIVITY_REGISTRY["error-correction"].raw_validator(raw) == []
    _clean, _ev, kas = schema.parse_raw_activity(raw)
    assert kas[0].rule_id == "numeral_moat"
    assert derived.check_rule_id(kas[0].rule_id)["status"] == "pass"
    # Inventory span is the decidable MOAT unit (not full prose with leading tokens).
    moat = derived.verify_registered_rule(
        kas[0].rule_id,
        {"phrase": "17 ділянок"},
    )
    assert moat["status"] == "pass"
    # Error form fails the same oracle.
    assert derived.verify_numeral_moat({"phrase": "17 ділянки"})["status"] == "fail"

    unknown = copy.deepcopy(raw)
    unknown["items"][0]["kit_anchors"]["rule_id"] = "fantasy_rule"
    # Raw validator allows any non-empty rule_id string; gate rejects unknown.
    assert registry.ACTIVITY_REGISTRY["error-correction"].raw_validator(unknown) == []
    assert derived.check_rule_id("fantasy_rule")["status"] == "fail"


# ---------------------------------------------------------------------------
# Integration: derived without kit closure → rejected; mono-cluster; unknown rule
# ---------------------------------------------------------------------------
def test_gate_activity_rejects_without_kit_closure(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    monkeypatch.setenv("HRAMATKA_TEACHER_REVIEW_TRAY_V1", "1")

    raw = {
        "type": "fill-in",
        "instruction": "Обери.",
        "items": [
            _fill_in_kit_item(
                sentence=f"Читання є одним з найскладніших ____ для мозку ({i}).",
                answer="завдань",
                options=["завдань", "вправ", "задач", "питань"],
                lemmas=["читання", "мозок"],
                witness=f"witness {i} читання",
            )
            for i in range(3)
        ],
    }

    # Empty kit → fail closed even with tray on.
    ir = pipeline.gate_activity(
        raw,
        ANCHOR,
        provenance={"generator": "test"},
        kit=None,
        candidate_id="c0",
    )
    assert ir.gate_result.status == schema.DISPOSITION_REJECTED
    assert any(
        c.gate == "kit_closure" and c.status == "fail" for c in ir.gate_result.checks
    )


def test_gate_activity_rejects_unknown_rule_id(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    monkeypatch.setenv("HRAMATKA_TEACHER_REVIEW_TRAY_V1", "1")
    kit = _kit_from_lemmas(
        "читання",
        "ділянка",
        "мозок",
        "головний",
        "активізуватися",
        "одразу",
        numerals=["17"],
    )
    item = _error_correction_kit_item(
        sentence="Під час читання активізуються одразу 17 ділянки головного мозку.",
        error="ділянки",
        correction="ділянок",
        options=["ділянки", "ділянок", "книжки", "завдання"],
        lemmas=["ділянка"],
        witness="17 ділянок",
        rule_id="not_a_real_rule",
    )
    raw = {
        "type": "error-correction",
        "instruction": "Виправ.",
        "items": [copy.deepcopy(item), copy.deepcopy(item)],
    }
    for index, it in enumerate(raw["items"]):
        it["kit_anchors"]["witness_span"] = f"17 ділянок {index}"

    ir = pipeline.gate_activity(
        raw,
        ANCHOR,
        provenance={"generator": "test"},
        kit=kit,
        candidate_id="c1",
    )
    assert any(
        c.gate == "rule_registry" and c.status == "fail" for c in ir.gate_result.checks
    )


def test_gate_activity_rejects_mono_cluster_batch(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    monkeypatch.setenv("HRAMATKA_TEACHER_REVIEW_TRAY_V1", "1")
    kit = _kit_from_lemmas(
        "читання",
        "мозок",
        "завдання",
        "найскладніший",
    )
    # 4 items, all same core lemma cluster → ceil(4/3)=2 required.
    items = []
    for i in range(4):
        items.append(
            _fill_in_kit_item(
                sentence=f"Читання є одним з найскладніших ____ для мозку ({i}).",
                answer="завдань",
                options=["завдань", "вправ", "задач", "питань"],
                lemmas=["читання"],  # mono-cluster
                witness=f"читання witness {i}",
            )
        )
    raw = {
        "type": "fill-in",
        "instruction": "Обери.",
        "items": items,
    }
    ir = pipeline.gate_activity(
        raw,
        ANCHOR,
        provenance={"generator": "test"},
        kit=kit,
        candidate_id="c2",
    )
    assert any(
        c.gate == "diversity_g5" and c.status == "fail" for c in ir.gate_result.checks
    )


def test_derived_gates_version_and_digest_coverage():
    assert derived.DERIVED_GATES_VERSION.startswith("derived.gates.v1")
    digest = pipeline._gate_impl_digest()
    assert isinstance(digest, str) and len(digest) == 64


def test_reuse_scoped_to_quoting_under_flag(monkeypatch):
    """Derived candidates skip sentence-id distinctness under grounding_mode_v1."""
    from hramatka.engine import content_density

    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    ir = schema.HramatkaActivity(
        activity={"type": "fill-in", "items": [{}, {}, {}]},
        kit_anchors=[],
    )
    assert content_density.itemized_sentence_ids_are_distinct(ir, None) is True

    monkeypatch.delenv("HRAMATKA_GROUNDING_MODE_V1", raising=False)
    # Flags off: empty sentence ids → vacuously distinct (same as before).
    assert content_density.itemized_sentence_ids_are_distinct(ir, None) is True
