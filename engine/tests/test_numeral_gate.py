"""Tests for gates/numeral.py — THE MOAT (§6c of slice-1-build-plan.md).

Every Ukrainian numeral/noun form used below was verified live against
VESUM via `scripts.verification.vesum.verify_word` (or `verify_lemma`)
before being written into a fixture — per repo rule #M-4
(deterministic-over-hallucination). See the build report for the raw tool
evidence dump. No form here is invented.

Layout: one POSITIVE (accept) + one NEGATIVE (reject) case per §6c numeral
class, plus the WARN classes (decimal, ordinal, ambiguous preposition,
context-undetermined) and a few structural edge cases.
"""

from __future__ import annotations

from engine.gates.numeral import check_numeral_government


def _rule(result: dict) -> str:
    return result["rule"]


# ---------------------------------------------------------------------------
# ends_1 (один/одна/одне — SINGULAR agreement, incl. oblique)
# ---------------------------------------------------------------------------
def test_ends_1_digit_nominative_accept():
    # 21 -> last digit 1, not 11 -> ends_1 -> nom SG "студент"
    result = check_numeral_government("21 студент")
    assert result["status"] == "pass"


def test_ends_1_oblique_locative_accept_with_explicit_context():
    # Fully-declined compound cardinal in locative — every component declines:
    # VESUM: "двадцяти" = numr:p:v_mis, "одному" = numr:{m,n}:v_mis,
    # "поверсі" = noun:inanim:m:v_mis. (The build-plan §6c example wrote the
    # prefix un-declined as "двадцять одному"; the compound-prefix check
    # correctly flags that — see test_ends_1_undeclined_prefix_warns + the
    # build report's spec-vs-VESUM note.)
    result = check_numeral_government("на двадцяти одному поверсі", context_case="loc")
    assert result["status"] == "pass"


def test_ends_1_oblique_accepts_raw_vesum_case_code_alias():
    # context_case also accepts the raw VESUM code spelling (v_mis), not
    # just the short code ("loc")
    result = check_numeral_government("на двадцяти одному поверсі", context_case="v_mis")
    assert result["status"] == "pass"


def test_ends_1_undeclined_prefix_warns():
    # Cross-family review blocker #5: an un-declined compound prefix in an
    # oblique context must never PASS. "на двадцять одному поверсі" leaves
    # "двадцять" in the nominative where locative "двадцяти" is required —
    # WARN (the gate can't confirm the whole numeral is correctly governed).
    result = check_numeral_government("на двадцять одному поверсі", context_case="loc")
    assert result["status"] == "warn"
    assert _rule(result) == "compound-prefix-unverified"


def test_ends_1_without_context_warns_undetermined():
    # "на" is not in the curated §6c trigger lexicon — the gate must be
    # honest that it can't auto-determine locative here (documented limit).
    result = check_numeral_government("на двадцяти одному поверсі")
    assert result["status"] == "warn"
    assert _rule(result) == "context-undetermined"


def test_ends_1_negative_number_mismatch():
    # 21 студенти (plural) should FAIL — ends_1 requires nom SG "студент"
    result = check_numeral_government("21 студенти")
    assert result["status"] == "fail"
    assert _rule(result) == "gov-ends_1"
    assert "singular" in result["expected"]


# ---------------------------------------------------------------------------
# ends_2_4 (два/три/чотири — nom-pl / acc-animate-gen-pl / oblique-pl)
# ---------------------------------------------------------------------------
def test_ends_2_4_nominative_inanimate_accept():
    result = check_numeral_government("два столи")
    assert result["status"] == "pass"


def test_ends_2_4_nominative_feminine_noun_accept():
    result = check_numeral_government("дві третини")
    assert result["status"] == "pass"


def test_ends_2_4_transparent_trigger_ponad_accept():
    # Fleet correction: "понад" is TRANSPARENT (does not force genitive).
    # VESUM: "роки" = noun:inanim:p:v_naz (no v_rod at all) -> nom-pl, not gen.
    result = check_numeral_government("понад два роки")
    assert result["status"] == "pass"
    assert "ends_2_4" in _rule(result)


def test_ends_2_4_accusative_animate_accept_gen_form():
    # "бачу двох студентів" — acc-animate: numeral+noun both take the
    # genitive-plural FORM (VESUM: студентів carries both v_rod and v_zna).
    # The governing verb ("бачу") is outside gate scope — the caller
    # supplies context_case="acc" explicitly; the gate skips the
    # unrecognized leading verb token because context_case was given.
    result = check_numeral_government("бачу двох студентів", context_case="acc")
    assert result["status"] == "pass"

    # Same phrase with the verb already stripped by the caller — equivalent.
    result2 = check_numeral_government("двох студентів", context_case="acc")
    assert result2["status"] == "pass"


def test_ends_2_4_negative_plain_nominative_wrong_case():
    # "два студентів" in plain nominative (no trigger) should FAIL —
    # expected nom-pl "студенти", not the genitive-plural form "студентів".
    result = check_numeral_government("два студентів")
    assert result["status"] == "fail"
    assert _rule(result) == "gov-ends_2_4"
    assert "nominative" in result["expected"]
    assert "студенти" in result["expected"]


def test_ends_2_4_negative_singular_noun():
    # "два стіл" — singular noun where nom-pl "столи" is required.
    result = check_numeral_government("два стіл")
    assert result["status"] == "fail"
    assert _rule(result) == "gov-ends_2_4"


# ---------------------------------------------------------------------------
# ends_5_9_0 (п'ять..десять, 11-19, tens/hundreds, 0 — genitive plural)
# ---------------------------------------------------------------------------
def test_ends_5_9_0_word_form_nominative_accept():
    result = check_numeral_government("п'ять столів")
    assert result["status"] == "pass"


def test_ends_5_9_0_digit_nominative_accept():
    result = check_numeral_government("17 ділянок")
    assert result["status"] == "pass"


def test_ends_5_9_0_digit_11_14_exception_accept():
    # 11 is the exception range (11-14 always genitive-plural class, despite
    # ending in "1").
    result = check_numeral_government("11 днів")
    assert result["status"] == "pass"


def test_ends_5_9_0_overriding_genitive_trigger_accept():
    # "близько" forces genitive regardless of numeral class.
    result = check_numeral_government("близько ста людей")
    assert result["status"] == "pass"


def test_ends_5_9_0_overriding_dative_trigger_accept():
    # "завдяки" forces dative; numeral AND noun both decline to dative
    # plural in the oblique branch.
    result = check_numeral_government("завдяки п'яти студентам")
    assert result["status"] == "pass"


def test_ends_5_9_0_negative_nominative_plural_instead_of_genitive():
    # "п'ять студенти" should FAIL — expected gen-pl "студентів".
    result = check_numeral_government("п'ять студенти")
    assert result["status"] == "fail"
    assert _rule(result) == "gov-ends_5_9_0"
    assert "genitive" in result["expected"]
    assert "студентів" in result["expected"]


def test_ends_5_9_0_negative_digit_11_14_singular_noun():
    # 13 (in the 11-14 exception range) requires gen-pl; a singular noun fails.
    result = check_numeral_government("13 студент")
    assert result["status"] == "fail"
    assert _rule(result) == "gov-ends_5_9_0"


# ---------------------------------------------------------------------------
# collective (двоє/троє/... — genitive plural ALWAYS, never the 2-4 nom-pl rule)
# ---------------------------------------------------------------------------
def test_collective_accept():
    result = check_numeral_government("двоє студентів")
    assert result["status"] == "pass"
    assert _rule(result) == "gov-collective"


def test_collective_negative_nominative_plural_instead_of_genitive():
    result = check_numeral_government("двоє студенти")
    assert result["status"] == "fail"
    assert _rule(result) == "gov-collective"
    assert "genitive" in result["expected"]


# ---------------------------------------------------------------------------
# half (півтора/півтори — genitive SINGULAR)
# ---------------------------------------------------------------------------
def test_half_accept():
    result = check_numeral_government("півтора року")
    assert result["status"] == "pass"


def test_half_negative_genitive_plural_instead_of_singular():
    # "півтора років" — genitive PLURAL used where genitive SINGULAR ("року") required.
    result = check_numeral_government("півтора років")
    assert result["status"] == "fail"
    assert _rule(result) == "gov-half"
    assert "singular" in result["expected"]


# ---------------------------------------------------------------------------
# magnitude (тисяча/мільйон/мільярд — nested: outer numeral classifies the
# magnitude word; the magnitude word ALWAYS governs its complement as gen-pl)
# ---------------------------------------------------------------------------
def test_magnitude_accept():
    # VESUM: "тисячі" = noun:inanim:p:v_naz:numr (nom-pl, matches outer "дві"
    # ends_2_4 nom-pl rule); "людей" = noun:anim:p:v_rod (genitive plural).
    result = check_numeral_government("дві тисячі людей")
    assert result["status"] == "pass"
    assert _rule(result) == "nested-magnitude"


def test_magnitude_negative_wrong_complement_case():
    # "дві тисячі людина" — complement must be gen-pl "людей", not nom-sg.
    result = check_numeral_government("дві тисячі людина")
    assert result["status"] == "fail"
    assert _rule(result) == "nested-magnitude-complement"
    assert "людей" in result["expected"]


def test_magnitude_negative_wrong_magnitude_word_form():
    # "п'ять тисячі людей" — outer numeral is ends_5_9_0, which requires the
    # magnitude word itself in genitive plural ("тисяч"), not "тисячі"
    # (VESUM: "тисячі" has NO p:v_rod tag at all — only f:v_rod, singular).
    result = check_numeral_government("п'ять тисячі людей")
    assert result["status"] == "fail"
    assert _rule(result) == "nested-magnitude-form"
    assert "тисяч" in result["expected"]


# ---------------------------------------------------------------------------
# decimal / fraction — WARN, never hard-fail
# ---------------------------------------------------------------------------
def test_decimal_warns_never_fails():
    # VESUM: both "рази" and "раза" are valid forms of "раз" — government is
    # genuinely ambiguous for decimals; the gate must WARN, not fail.
    result = check_numeral_government("2,5 рази")
    assert result["status"] == "warn"
    assert _rule(result) == "decimal-fraction"


# ---------------------------------------------------------------------------
# Mixed-fraction "X з половиною/чвертю/третиною <noun>" — the spelled-out
# decimal (real Gemma output «два з половиною рази» for «2,5 рази»). Government
# variable like a decimal -> WARN with the noun located, NOT no-noun-found.
# ---------------------------------------------------------------------------
def test_mixed_fraction_polovynoyu_warns_with_noun_located():
    # Regression: the tokenizer used to take "два", see "з", and report
    # no-noun-found on 'два з' — a false-fail that killed the whole activity.
    result = check_numeral_government("два з половиною рази")
    assert result["status"] == "warn"
    assert _rule(result) == "fraction-variable-government"
    assert "рази" in result["detail"]  # governed noun located past the fraction


def test_mixed_fraction_three_and_half_years_warns():
    result = check_numeral_government("три з половиною роки")
    assert result["status"] == "warn"
    assert _rule(result) == "fraction-variable-government"
    assert "роки" in result["detail"]


def test_mixed_fraction_chvertyu_warns():
    result = check_numeral_government("два з чвертю метри")
    assert result["status"] == "warn"
    assert _rule(result) == "fraction-variable-government"
    assert "метри" in result["detail"]


def test_mixed_fraction_without_noun_still_no_noun_found():
    # Fraction words but no counted noun after them -> genuine no-noun-found.
    result = check_numeral_government("два з половиною")
    assert result["status"] == "fail"
    assert _rule(result) == "no-noun-found"


def test_dangling_numeral_preposition_still_fails():
    # "два з" with nothing after remains a genuine no-noun-found fail (not a
    # fraction: "з" is not followed by половиною/чвертю/третиною).
    result = check_numeral_government("два з")
    assert result["status"] == "fail"
    assert _rule(result) == "no-noun-found"


# ---------------------------------------------------------------------------
# DATE construction "<number> <genitive-month>" — the day is ordinal, the
# month is genitive; correct by construction, NOT cardinal government.
# ---------------------------------------------------------------------------
def test_date_digit_day_genitive_month_passes():
    # Regression: «23 квітня» used to false-fail as gov-ends_2_4 (expected
    # nom-pl «квітні»). VESUM: «квітня» = noun:inanim:m:v_rod (genitive «of
    # April»). It is a date -> pass.
    result = check_numeral_government("23 квітня")
    assert result["status"] == "pass"
    assert _rule(result) == "date-not-cardinal"


def test_date_first_january_passes():
    result = check_numeral_government("1 січня")
    assert result["status"] == "pass"
    assert _rule(result) == "date-not-cardinal"


def test_date_eighth_march_passes():
    result = check_numeral_government("8 березня")
    assert result["status"] == "pass"
    assert _rule(result) == "date-not-cardinal"


def test_date_spelled_out_ordinal_day_passes():
    # «двадцять третє квітня» — spelled-out ordinal day + genitive month.
    result = check_numeral_government("двадцять третє квітня")
    assert result["status"] == "pass"
    assert _rule(result) == "date-not-cardinal"


def test_date_with_trailing_punctuation_passes():
    result = check_numeral_government("23 квітня, хочеться")
    assert result["status"] == "pass"
    assert _rule(result) == "date-not-cardinal"


def test_nominative_month_after_number_is_not_a_date():
    # «23 квітень» (nominative month) is NOT a date construction — the month
    # must be genitive. Falls through to (correctly failing) cardinal government.
    result = check_numeral_government("23 квітень")
    assert result["status"] == "fail"


def test_real_cardinal_with_common_noun_unaffected_by_date_rule():
    # «п'ять днів» is a genuine cardinal (gen pl) — the date rule must not
    # touch it (день is not a month).
    result = check_numeral_government("п'ять днів")
    assert result["status"] == "pass"
    assert _rule(result) == "gov-ends_5_9_0"


# ---------------------------------------------------------------------------
# ordinal — WARN/skip (adjectival agreement out of cardinal-moat scope)
# ---------------------------------------------------------------------------
def test_ordinal_warns_skip():
    # VESUM-confirmed: "сімнадцятий" tags carry the adjective `:numr` flag
    # (adj:m:v_naz:numr, ...) — the tool-backed ordinal signal this gate uses.
    result = check_numeral_government("сімнадцятий поверх")
    assert result["status"] == "warn"
    assert _rule(result) == "ordinal-skip"


# ---------------------------------------------------------------------------
# ambiguous preposition (з/із/зі) — WARN, never force a case
# ---------------------------------------------------------------------------
def test_ambiguous_preposition_warns():
    result = check_numeral_government("з двома студентами")
    assert result["status"] == "warn"
    assert _rule(result) == "ambiguous-preposition"


def test_ambiguous_preposition_resolved_by_explicit_context():
    # If the caller already knows it's instrumental (comitative "with"),
    # passing context_case bypasses the ambiguity warning entirely.
    result = check_numeral_government("з двома студентами", context_case="instr")
    assert result["status"] == "pass"


# ---------------------------------------------------------------------------
# Structural edge cases — the gate must never raise, always return a Result
# ---------------------------------------------------------------------------
def test_empty_phrase_fails_cleanly():
    result = check_numeral_government("")
    assert result["status"] == "fail"
    assert _rule(result) == "empty-phrase"


def test_no_numeral_found_fails_cleanly():
    result = check_numeral_government("студенти прийшли")
    assert result["status"] == "fail"
    assert _rule(result) == "no-numeral-found"


def test_no_noun_found_fails_cleanly():
    result = check_numeral_government("два")
    assert result["status"] == "fail"
    assert _rule(result) == "no-noun-found"


def test_invalid_context_case_fails_cleanly():
    result = check_numeral_government("два столи", context_case="not-a-case")
    assert result["status"] == "fail"
    assert _rule(result) == "invalid-context-case"


def test_result_shape_always_has_four_keys():
    for phrase, ctx in (
        ("два столи", None),
        ("2,5 рази", None),
        ("п'ять студенти", None),
        ("", None),
    ):
        result = check_numeral_government(phrase, context_case=ctx)
        assert set(result.keys()) == {"status", "rule", "expected", "detail"}
        assert result["status"] in ("pass", "warn", "fail")


# ===========================================================================
# Cross-family code review (codex + grok, 2026-07-08) — regression fixtures.
# Each of the coordinator's probe phrases is asserted here to its corrected
# verdict. All UA forms VESUM-verified before use (#M-4).
# ===========================================================================

# --- Blocker 1: trailing punctuation + head-noun selection -----------------
def test_trailing_punctuation_stripped_before_lookup():
    # "17 ділянок." must strip the period before the VESUM lookup, not look
    # up 'ділянок.' (this exact phrase is in the real anchor).
    result = check_numeral_government("17 ділянок.")
    assert result["status"] == "pass"
    assert _rule(result) == "gov-ends_5_9_0"


def test_trailing_verb_not_taken_as_head_noun():
    # "Два студенти прийшли." — the head noun is "студенти" (nom pl), NOT the
    # trailing verb "прийшли". Context nom supplied explicitly.
    result = check_numeral_government("Два студенти прийшли.", context_case="nom")
    assert result["status"] == "pass"
    assert _rule(result) == "gov-ends_2_4"


# --- Blocker 2: два/дві/обидва/обидві surface-gender agreement -------------
def test_gender_dvi_with_masculine_noun_fails():
    # "дві столи" — fem paucal form with a masc noun (столи, lemma стіл = m).
    result = check_numeral_government("дві столи")
    assert result["status"] == "fail"
    assert _rule(result) == "gender-ends_2_4"


def test_gender_dva_with_feminine_noun_fails():
    # "два книги" — masc paucal form with a fem noun (книги, lemma книга = f).
    result = check_numeral_government("два книги")
    assert result["status"] == "fail"
    assert _rule(result) == "gender-ends_2_4"


def test_gender_obydva_masculine_accept():
    # VESUM: обидва/обидві both -> lemma обидва, genderless numr:p tags; the
    # surface form is the only agreement signal. обидва + столи (m) -> OK.
    result = check_numeral_government("обидва столи")
    assert result["status"] == "pass"


def test_gender_obydvi_feminine_accept():
    result = check_numeral_government("обидві книги")
    assert result["status"] == "pass"


def test_gender_obydvi_masculine_noun_fails():
    result = check_numeral_government("обидві столи")
    assert result["status"] == "fail"
    assert _rule(result) == "gender-ends_2_4"


def test_gender_three_four_not_gender_checked():
    # три/чотири have NO surface gender variants (VESUM-invariant) — must not
    # be gender-checked. три + столи (m) and три + книги (f) both accept.
    assert check_numeral_government("три столи")["status"] == "pass"
    assert check_numeral_government("три книги")["status"] == "pass"


def test_gender_nested_magnitude_outer_paucal():
    # "два мільйони людей": outer "два" (m) agrees with мільйон (m) -> pass.
    assert check_numeral_government("два мільйони людей")["status"] == "pass"
    # "дві мільйони людей": fem "дві" with masc мільйон -> gender fail.
    bad = check_numeral_government("дві мільйони людей")
    assert bad["status"] == "fail"
    assert _rule(bad) == "nested-magnitude-gender"


# --- Blocker 3: нуль as a cardinal -----------------------------------------
def test_zero_cardinal_accept():
    # "нуль студентів" — нуль is VESUM-tagged a plain noun; the gate treats
    # it as a §6c cardinal (gen-pl government): студентів = gen pl anim.
    result = check_numeral_government("нуль студентів")
    assert result["status"] == "pass"
    assert _rule(result) == "gov-ends_5_9_0"


def test_zero_cardinal_negative_wrong_noun_case():
    # "нуль студенти" — nom-pl where gen-pl "студентів" is required.
    result = check_numeral_government("нуль студенти")
    assert result["status"] == "fail"
    assert _rule(result) == "gov-ends_5_9_0"


# --- Blocker 4: POS-filter the head noun -----------------------------------
def test_possessive_not_taken_as_head_noun():
    # "два мої" — "мої" is a possessive adjective (VESUM adj:pron:pos), NOT a
    # noun, so there is no governed noun -> no-noun-found, never a false pass.
    result = check_numeral_government("два мої")
    assert result["status"] == "fail"
    assert _rule(result) == "no-noun-found"


# --- Blocker 5: compound-prefix declension in oblique/override contexts -----
def test_compound_prefix_undeclined_override_warns():
    # "до сто двадцять одного року": "до" forces genitive; the prefix
    # "сто двадцять" is left nominative (should be "ста двадцяти") -> WARN,
    # never a false PASS on the strength of only the last token.
    result = check_numeral_government("до сто двадцять одного року")
    assert result["status"] == "warn"
    assert _rule(result) == "compound-prefix-unverified"


def test_compound_prefix_fully_declined_override_accept():
    # "до ста двадцяти одного року" — every component declined to genitive:
    # ста = numr:p:v_rod, двадцяти = numr:p:v_rod, одного = numr:m:v_rod,
    # року = noun:inanim:m:v_rod (gen sg, required by ends_1).
    result = check_numeral_government("до ста двадцяти одного року")
    assert result["status"] == "pass"


# --- Should-fix 6: mixed digit+word compounds ------------------------------
def test_malformed_compound_digit_then_word():
    result = check_numeral_government("20 два студенти")
    assert result["status"] == "fail"
    assert _rule(result) == "malformed-compound"


def test_malformed_compound_word_then_digit():
    result = check_numeral_government("два 3 студенти")
    assert result["status"] == "fail"
    assert _rule(result) == "malformed-compound"


def test_digit_plus_magnitude_word_is_wellformed():
    # "20 тисяч людей" — the ONE legal digit+word compound (digits + a
    # magnitude word). тисяч = gen pl (20 -> ends_5_9_0), людей = gen pl.
    result = check_numeral_government("20 тисяч людей")
    assert result["status"] == "pass"
    assert _rule(result) == "nested-magnitude"


# --- Should-fix 7: hyphenated tokens ---------------------------------------
def test_hyphen_glued_number_noun_warns():
    result = check_numeral_government("17-ділянок")
    assert result["status"] == "warn"
    assert _rule(result) == "hyphenated-token"


def test_hyphen_word_range_warns():
    result = check_numeral_government("два-три роки")
    assert result["status"] == "warn"
    assert _rule(result) == "hyphenated-token"


def test_hyphen_digit_range_warns():
    result = check_numeral_government("2-3 рази")
    assert result["status"] == "warn"
    assert _rule(result) == "hyphenated-token"
