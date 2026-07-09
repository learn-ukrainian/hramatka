"""Tests for gates/vesum_tags.py — the thin structured layer over VESUM.

Every Ukrainian word form referenced here was verified live against VESUM
via `scripts.verification.vesum.verify_word` before being used (see the
build report for the raw tool evidence) — per repo rule #M-4
(deterministic-over-hallucination), no form is invented.
"""

from __future__ import annotations

from engine import paths
from engine.gates import vesum_tags as vt


# ---------------------------------------------------------------------------
# paths.py sanity — the engine must resolve real absolute DB paths regardless
# of cwd (build brief §R path-robustness requirement)
# ---------------------------------------------------------------------------
def test_project_root_and_dbs_resolve():
    assert (paths.PROJECT_ROOT / ".git").exists() or (
        paths.PROJECT_ROOT / "pyproject.toml"
    ).exists()
    assert paths.ATLAS_DB.is_absolute()
    assert paths.VESUM_DB.is_absolute()
    assert paths.ATLAS_DB.exists()
    assert paths.VESUM_DB.exists()


def test_preflight_passes_in_this_environment():
    # Confirms opencode + the google-ais key + both DBs are present, i.e.
    # the preflight function itself is wired correctly (not just "returns").
    paths.preflight()


# ---------------------------------------------------------------------------
# parse_tag — raw VESUM tag string -> structured dict
# ---------------------------------------------------------------------------
def test_parse_tag_noun_animate_genitive_plural():
    # VESUM-confirmed: verify_word("студентів") -> noun:anim:p:v_rod (+ v_zna)
    parsed = vt.parse_tag("noun:anim:p:v_rod")
    assert parsed["case"] == "gen"
    assert parsed["number"] == "pl"
    assert parsed["animacy"] == "anim"
    assert parsed["gender"] is None  # VESUM does not tag gender in plural


def test_parse_tag_noun_inanimate_singular_locative():
    # VESUM-confirmed: verify_word("поверсі") -> noun:inanim:m:v_mis
    parsed = vt.parse_tag("noun:inanim:m:v_mis")
    assert parsed["case"] == "loc"
    assert parsed["number"] == "sg"
    assert parsed["animacy"] == "inanim"
    assert parsed["gender"] == "m"


def test_parse_tag_magnitude_noun_numr_flag():
    # VESUM-confirmed: verify_word("тисячі") includes noun:inanim:p:v_naz:numr
    parsed = vt.parse_tag("noun:inanim:p:v_naz:numr")
    assert parsed["case"] == "nom"
    assert parsed["number"] == "pl"
    assert parsed["numr_flag"] is True


def test_parse_tag_ordinal_adjective_numr_flag():
    # VESUM-confirmed: verify_word("сімнадцятий") -> adj:m:v_naz:numr (+ others)
    parsed = vt.parse_tag("adj:m:v_naz:numr")
    assert parsed["case"] == "nom"
    assert parsed["gender"] == "m"
    assert parsed["numr_flag"] is True


def test_parse_tag_numeral_odyn_gender_present():
    # "один" DOES carry gender in VESUM tags (unlike два/дві — see numeral.py
    # docstring on the spec-vs-VESUM gap)
    parsed = vt.parse_tag("numr:n:v_dav")
    assert parsed["pos"] == "numr"
    assert parsed["case"] == "dat"
    assert parsed["gender"] == "n"
    assert parsed["number"] == "sg"


# ---------------------------------------------------------------------------
# parse_word / form_has — live VESUM lookups
# ---------------------------------------------------------------------------
def test_form_has_animate_accusative_equals_genitive():
    # tool evidence: verify_word("студентів") ->
    #   [{'lemma': 'студент', 'pos': 'noun', 'tags': 'noun:anim:p:v_rod'},
    #    {'lemma': 'студент', 'pos': 'noun', 'tags': 'noun:anim:p:v_zna'}]
    assert vt.form_has("студентів", case="gen", number="pl", animacy="anim")
    assert vt.form_has("студентів", case="acc", number="pl", animacy="anim")
    assert not vt.form_has("студентів", case="nom", number="pl")


def test_form_has_nominative_plural_studenty():
    # tool evidence: verify_word("студенти") ->
    #   noun:anim:p:v_naz, noun:anim:p:v_zna:rare, noun:anim:p:v_kly
    assert vt.form_has("студенти", case="nom", number="pl")
    assert not vt.form_has("студенти", case="gen", number="pl")


def test_form_has_roky_nominative_plural_not_genitive():
    # tool evidence: verify_word("роки") -> noun:inanim:p:v_naz/v_zna/v_kly
    # (no v_rod at all) — confirms the fleet correction that "понад два
    # роки" is NOT genitive.
    assert vt.form_has("роки", case="nom", number="pl")
    assert not vt.form_has("роки", case="gen", number="pl")


def test_form_has_magnitude_word_singular_vs_plural_genitive():
    # tool evidence: verify_word("тисяч") -> noun:inanim:p:v_rod:numr (gen pl)
    # verify_word("тисячі") has NO p:v_rod tag (only f:v_rod = gen SG) —
    # this is the nested-magnitude gate's negative fixture in numeral.py.
    assert vt.form_has("тисяч", case="gen", number="pl")
    assert not vt.form_has("тисячі", case="gen", number="pl")
    assert vt.form_has("тисячі", case="gen", number="sg")


def test_parse_word_unknown_form_returns_empty():
    assert vt.parse_word("асдфасдф") == []


# ---------------------------------------------------------------------------
# find_form — reverse lookup for human-readable 'expected' hints
# ---------------------------------------------------------------------------
def test_find_form_genitive_plural_stil():
    # tool evidence: verify_lemma("стіл") includes столів -> noun:inanim:p:v_rod
    assert vt.find_form("стіл", case="gen", number="pl") == "столів"


def test_find_form_returns_none_when_lemma_unknown():
    assert vt.find_form("асдфасдфлемма", case="gen", number="pl") is None


def test_describe_labels():
    assert vt.describe("gen", "pl") == "genitive plural"
    assert vt.describe("nom", "sg") == "nominative singular"
