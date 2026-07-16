"""Bake-off 2026-07-10 (#54 item 2): match-up left sides arrived inflected.

The judge flagged «привітною», «розкачайте», «спливуть» — sentence forms where a
match-up board needs the dictionary's citation form.

Every form used here is a real VESUM row from the committed offline fixture
(`fixtures/vesum_forms.json`); none is invented. The fixture anchor supplies the
same shape as the bake-off defect: it contains «людей», so the citation form
«людина» is grounded but NOT verbatim — exactly the «привітною»/«привітний»
case.
"""

from __future__ import annotations

from hramatka.engine import fixtures, pipeline
from hramatka.engine.gates import matchup_lemma


def _anchor() -> str:
    return fixtures.load_anchor()


def _left_checks(left: str, evidence: str) -> list:
    """Run the real match-up gate and return only its `matchup_left` checks."""
    activity = {
        "type": "match-up",
        "instruction": "З'єднайте слово з опори з його значенням.",
        "pairs": [{"left": left, "right": "пристрій", "evidence": evidence}],
    }
    ir = pipeline.gate_activity(activity, _anchor(), {}, atlas_lookup={})
    return [c for c in ir.gate_result.checks if c.gate == "matchup_left"]


def test_inflected_left_side_warns_and_names_the_citation_form():
    """«третини» is in the anchor verbatim, but a board must cite «третина»."""
    verdict = matchup_lemma.check_left_side("третини", _anchor())

    assert verdict["status"] == "warn"
    assert "inflected form" in verdict["detail"]
    assert "'третина'" in verdict["detail"]


def test_citation_form_of_an_anchor_word_passes_though_not_verbatim():
    """The #54 fix: «людина» grounds via its anchor form «людей».

    Under a surface-verbatim rule this correct left side would warn, leaving the
    generator no way to satisfy both the lemma requirement and grounding.
    """
    anchor = _anchor()
    assert "людей" in anchor
    assert "людина" not in anchor  # not verbatim…

    assert matchup_lemma.check_left_side("людина", anchor)["status"] == "pass"


def test_citation_form_that_is_also_verbatim_passes():
    assert matchup_lemma.check_left_side("телевізор", _anchor())["status"] == "pass"


def test_left_side_absent_from_the_anchor_in_every_form_still_warns():
    """Grounding is loosened to lemma level, not abandoned."""
    anchor = _anchor()
    verdict = matchup_lemma.check_left_side("студент", anchor)

    assert verdict["status"] == "warn"
    assert "not an anchor word in any form" in verdict["detail"]


def test_form_vesum_cannot_resolve_warns():
    """The VESUM token gate covers only the RIGHT side of match-up pairs, so a
    fabricated left must warn here — nothing else ever judges it (PR #193
    cross-family review, blocker #1)."""
    verdict = matchup_lemma.check_left_side("фейкословоxx", _anchor())
    assert verdict["status"] == "warn"
    assert "no VESUM parse" in verdict["detail"]


def test_multi_word_left_side_is_not_judged():
    """Conservative by design — the gate only speaks where it has evidence,
    and says so honestly instead of claiming a citation-form check ran."""
    for left in ("головного мозку", ""):
        verdict = matchup_lemma.check_left_side(left, _anchor())
        assert verdict["status"] == "pass"
        assert verdict["detail"] == "Left side not judged (phrase or empty)."


# --- wiring: the real match-up gate, not just the helper ------------------


def test_gate_warns_on_an_inflected_left_side():
    checks = _left_checks("книжки", "не прочитує жодної книжки")

    assert [c.status for c in checks] == ["warn"]
    assert "citation form ('книжка')" in checks[0].detail


def test_gate_accepts_a_citation_form_that_is_not_verbatim_in_the_anchor():
    """Red before #54: `matchup_left` demanded a surface-verbatim left side, so
    «людина» warned even though it is the correct citation form of «людей».
    """
    assert _left_checks("людина", "Багато людей втратили") == []


def test_gate_fails_a_schema_token_on_the_right_side():
    """«true»/«false» must not ship as a match-up "gloss": the VESUM token gate
    is Latin-blind, so the right side needs the schema-token check too
    (PR #193 cross-family review, nit #1)."""
    activity = {
        "type": "match-up",
        "instruction": "З'єднайте слово з опори з його значенням.",
        "pairs": [
            {"left": "телевізор", "right": "true", "evidence": "час увімкнути телевізор"}
        ],
    }
    ir = pipeline.gate_activity(activity, _anchor(), {}, atlas_lookup={})
    checks = [c for c in ir.gate_result.checks if c.gate == "schema_tokens"]
    assert checks, "schema_tokens must judge the right side"
    assert checks[0].status == "fail"
