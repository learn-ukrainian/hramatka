"""Regression fixtures for Sol defect 6: MatchUp meaning, not just spelling.

The two decoys are deliberately valid Ukrainian words/phrases.  Their failure
mode is semantic: neither is an Atlas-sourced relation for the anchor term.
They must therefore be teacher-visible ``review_required`` rather than clean.
"""

from __future__ import annotations

import pytest

from hramatka.engine import fixtures, pipeline, schema
from hramatka.engine.gates import matchup_semantics

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


def _vesum_words(words, *, db_path):  # noqa: ARG001 - mirrors the adapter API
    return {
        word: [{"lemma": _LEMMA_BY_FORM[word], "pos": "noun"}]
        for word in words
        if word in _LEMMA_BY_FORM
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
