"""Regression fixtures for Sol defect 6: MatchUp meaning, not just spelling.

The two decoys are deliberately valid Ukrainian words/phrases.  Their failure
mode is semantic: neither is an Atlas-sourced relation for the anchor term.
They must therefore be teacher-visible ``review_required`` rather than clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hramatka.engine import fixtures, pipeline, registry, schema
from hramatka.engine.gates import matchup_semantics

_FIXTURES = Path(__file__).with_name("fixtures")
_AGY_CALIBRATION_FIXTURE = _FIXTURES / "matchup_calibration_agy_2026-07-12.json"

# Compact real Atlas synonym facts used by the tests.  The offline fixture
# deliberately contains only production-extracted rows; VESUM response stubs
# below model the extra surface forms required to exercise them without adding
# corpus DBs to the repository.
_ATLAS = {
    "книжка": {"synonyms": ["книга"]},
    "головний": {"synonyms": ["основний"]},
    "ділянка": {"synonyms": ["відрізок"]},
}

_LEMMA_BY_FORM = {
    "книжка": "книжка",
    "книжки": "книжка",
    "книга": "книга",
    "головний": "головний",
    "основний": "основний",
    "ділянка": "ділянка",
    "пристрій": "пристрій",
    "велике": "великий",
    "задоволення": "задоволення",
}

_AGY_POS = {
    "балакати": "verb",
    "розмовляти": "verb",
    "бажати": "verb",
    "хотіти": "verb",
    "гасити": "verb",
    "тушити": "verb",
    "швидко": "adv",
    "хутко": "adv",
    "подавати": "verb",
    "давати": "verb",
    "начинка": "noun",
    "начинки": "noun",
    "фарш": "noun",
    "година": "noun",
    "час": "noun",
    "володіти": "verb",
    "мати": "verb",
}
_AGY_LEMMA_BY_FORM = {"начинки": "начинка"}

# This compact lookup reproduces the 2026-07-10 pinned-release baseline for
# the AGY calibration: two GOOD pairs are Atlas-passed, two GOOD pairs remain
# teacher-confirmed, and Atlas incorrectly passes three BAD pairs.
_AGY_ATLAS = {
    "балакати": {"synonyms": ["розмовляти"]},
    "бажати": {"synonyms": ["хотіти"]},
    "подавати": {"synonyms": ["давати"]},
    "начинка": {"synonyms": ["фарш"]},
    "година": {"synonyms": []},
    "швидко": {"synonyms": ["прожогом"]},
    "володіти": {"synonyms": ["мати"]},
}
_AGY_CURRENT_STATUS = {
    "balakaty-rozmovlyaty": "pass",
    "bazhaty-khotity": "pass",
    "hasyty-tushyty": "warn",
    "shvydko-khutko": "warn",
}


@pytest.fixture(scope="module")
def agy_calibration_pairs():
    return json.loads(_AGY_CALIBRATION_FIXTURE.read_text(encoding="utf-8"))["pairs"]


def _vesum_words(words, *, db_path):  # noqa: ARG001 - mirrors the adapter API
    return {
        word: [{"lemma": _LEMMA_BY_FORM[word], "pos": "noun"}]
        for word in words
        if word in _LEMMA_BY_FORM
    }


def _agy_vesum_words(words, *, db_path):  # noqa: ARG001 - mirrors the adapter API
    return {
        word: [{"lemma": _AGY_LEMMA_BY_FORM.get(word, word), "pos": _AGY_POS[word]}]
        for word in words
        if word in _AGY_POS
    }


def test_atlas_synonym_relation_verifies_a_positive_pair(monkeypatch):
    """An inflected anchor term becomes clean only through VESUM+Atlas evidence."""
    monkeypatch.setattr(matchup_semantics, "verify_words", _vesum_words)

    verdict = matchup_semantics.check_pair("книжки", "книга", atlas_lookup=_ATLAS)

    assert verdict["status"] == "pass"
    assert "Atlas synonym 'книга'" in verdict["detail"]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Concrete term → equally valid but unrelated object.
        ("ділянка", "пристрій"),
        # Adjective → valid noun phrase from another semantic field.
        ("головний", "велике задоволення"),
    ],
)
def test_valid_but_unrelated_decoy_is_not_a_clean_semantic_match(monkeypatch, left, right):
    """Step-7 §4.6 planted decoys: lexical validity cannot prove meaning."""
    monkeypatch.setattr(matchup_semantics, "verify_words", _vesum_words)

    verdict = matchup_semantics.check_pair(left, right, atlas_lookup=_ATLAS)

    assert verdict["status"] == "warn"
    assert "not semantic proof" in verdict["detail"]


def test_two_valid_decoys_make_matchup_review_required_not_clean():
    """The pipeline retains the pairs but blocks automatic acceptance honestly."""
    decoys = {
        "type": "match-up",
        "instruction": "З'єднай слово з опори з його значенням.",
        "pairs": [
            {
                "left": "ділянка",
                "right": "пристрій",
                "evidence": "17 ділянок головного мозку",
            },
            {
                "left": "головний",
                "right": "велике задоволення",
                "evidence": "ділянок головного мозку",
            },
        ],
    }

    ir = pipeline.gate_activity(decoys, fixtures.load_anchor(), {}, atlas_lookup=_ATLAS)

    assert ir.gate_result.status == schema.GATE_REVIEW
    assert ir.flagged == []  # warn is visible/reviewable rather than silently dropped
    semantic_checks = [c for c in ir.gate_result.checks if c.gate == "matchup_semantics"]
    assert len(semantic_checks) == 2
    assert all(check.status == "warn" for check in semantic_checks)
    assert not any(check.gate == "vesum_token" for check in ir.gate_result.checks)


def test_agy_good_pairs_do_not_regress_below_the_pinned_release_baseline(
    monkeypatch, agy_calibration_pairs
):
    monkeypatch.setattr(matchup_semantics, "verify_words", _agy_vesum_words)
    status_rank = {"warn": 0, "pass": 1}

    for pair in agy_calibration_pairs:
        if pair["classification"] != "good":
            continue
        verdict = matchup_semantics.check_pair(
            pair["left"], pair["right"], atlas_lookup=_AGY_ATLAS
        )
        assert status_rank[verdict["status"]] >= status_rank[_AGY_CURRENT_STATUS[pair["id"]]]


def test_agy_bad_pairs_never_auto_pass_and_hit_the_negative_tier(
    monkeypatch, agy_calibration_pairs
):
    monkeypatch.setattr(matchup_semantics, "verify_words", _agy_vesum_words)
    verdicts = {
        pair["id"]: matchup_semantics.check_pair(
            pair["left"], pair["right"], atlas_lookup=_AGY_ATLAS
        )
        for pair in agy_calibration_pairs
        if pair["classification"] == "bad"
    }

    assert all(verdict["status"] != "pass" for verdict in verdicts.values())
    assert all(verdict["status"] == "fail" for verdict in verdicts.values())
    assert "known-prefix verb form" in verdicts["podavaty-davaty"]["detail"]


def test_normalized_duplicate_pair_hits_the_negative_tier():
    verdict = matchup_semantics.check_pair("  КНИГА  ", "книга", atlas_lookup={})

    assert verdict["status"] == "fail"
    assert "normalize to the same text" in verdict["detail"]


def test_calibrated_refutation_applies_to_an_inflected_lemma(monkeypatch):
    monkeypatch.setattr(matchup_semantics, "verify_words", _agy_vesum_words)

    verdict = matchup_semantics.check_pair("начинки", "фарш", atlas_lookup=_AGY_ATLAS)

    assert verdict["status"] == "fail"
    assert "hypernym/hyponym culinary mismatch" in verdict["detail"]


def test_matchup_semantic_fail_is_flagged_and_removed_by_the_pipeline(monkeypatch):
    monkeypatch.setattr(matchup_semantics, "verify_words", _agy_vesum_words)
    monkeypatch.setattr(
        registry.vesum_gate,
        "check_tokens",
        lambda tokens, anchor_body, *, atlas_lookup: [
            {"token": token, "status": "pass", "detail": "test VESUM pass"} for token in tokens
        ],
    )
    activity = {
        "type": "match-up",
        "instruction": "З'єднай слово з опори з його значенням.",
        "pairs": [
            {"left": "подавати", "right": "давати", "evidence": "подавати"},
            {"left": "балакати", "right": "розмовляти", "evidence": "балакати"},
            {"left": "бажати", "right": "хотіти", "evidence": "бажати"},
        ],
    }

    ir = pipeline.gate_activity(
        activity,
        "подавати балакати бажати",
        {},
        atlas_lookup=_AGY_ATLAS,
    )

    assert ir.gate_result.status == schema.GATE_REVIEW
    assert [pair["left"] for pair in ir.activity["pairs"]] == ["балакати", "бажати"]
    assert ir.flagged[0]["locator"] == "pairs[0]"
    assert any(
        check.gate == "matchup_semantics" and check.status == "fail" and check.locator == "pairs[0]"
        for check in ir.gate_result.checks
    )
