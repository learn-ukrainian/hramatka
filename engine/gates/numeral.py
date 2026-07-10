"""The numeral case-government gate — THE MOAT.

Implements `hramatka/slice-1-build-plan.md` §6c exactly. Pure,
deterministic, no network, no LLM judge. Verification uses VESUM tags
DIRECTLY (source of truth); pymorphy3 is never used here.

Public API:
    check_numeral_government(phrase: str, context_case: str | None = None) -> Result
    Result = {"status": "pass"|"warn"|"fail", "rule": str,
              "expected": str | None, "detail": str}

Scope (locked by the build brief): CLASSIFY + VERIFY existing numeral+noun
phrases. This is NOT a generative digit-to-words renderer (deferred to the
numeral-rewrite slice) — it never composes a numeral form, only checks one.

Hardening (cross-family review, 2026-07-08): punctuation-robust tokenizing,
POS-filtered head-noun selection (skips trailing verbs/possessives),
два/дві/обидва/обидві surface-gender agreement, нуль as a cardinal, oblique
compound-prefix declension verification, and deliberate handling of mixed
digit+word compounds and hyphenated tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import vesum_tags as vt

# ---------------------------------------------------------------------------
# Case constants (short codes — see vesum_tags.CASE_CODES for the VESUM
# v_naz-style codes these map to)
# ---------------------------------------------------------------------------
NOM, GEN, DAT, ACC, INSTR, LOC, VOC = "nom", "gen", "dat", "acc", "instr", "loc", "voc"
_OBLIQUE = {GEN, DAT, INSTR, LOC}
_KNOWN_CASES = {NOM, GEN, DAT, ACC, INSTR, LOC, VOC}

# Accept either short codes ('gen') or raw VESUM codes ('v_rod') as
# `context_case` input, since callers (recon doc, pipeline, tests) use both
# spellings interchangeably.
_CASE_ALIASES: dict[str, str] = {**vt.CASE_CODES, **{c: c for c in _KNOWN_CASES}}

# Leading/trailing punctuation stripped BEFORE every VESUM lookup / form_has /
# lemma read (cross-family review: "17 ділянок." must not look up 'ділянок.').
_STRIP_CHARS = ".,;:!?»«\"'`()[]…—–"


def _strip(token: str) -> str:
    return token.strip(_STRIP_CHARS)


def _normalize_case(case: str) -> str | None:
    return _CASE_ALIASES.get(case)


# ---------------------------------------------------------------------------
# §6c Step 1 — trigger lexicon (case-overriding / transparent / ambiguous)
# ---------------------------------------------------------------------------
_OVERRIDING_GEN_SINGLE = {"близько", "коло", "до", "від"}
_OVERRIDING_GEN_PHRASES = {"більше ніж", "менше ніж", "більш ніж", "менш ніж"}
_OVERRIDING_DAT_SINGLE = {"завдяки"}
_TRANSPARENT_SINGLE = {"понад", "під"}
_AMBIGUOUS_SINGLE = {"з", "із", "зі"}

# Mixed-fraction continuation after a cardinal: "<numeral> з половиною|чвертю|
# третиною <noun>" — the spelled-out equivalent of a decimal like "2,5"
# (real Gemma output: «у два з половиною рази» for «в 2,5 рази»). Government is
# VARIABLE, exactly like a decimal (VESUM: both `рази` and `раза` valid).
_FRACTION_PREPS = {"з", "із", "зі"}
_FRACTION_WORDS = {"половиною", "чвертю", "третиною"}

# DATE construction: "<number> <genitive-month>" (23 квітня, двадцять третє
# квітня, 23-го квітня). The number is an ordinal DAY and the month is
# genitive ("of April") — correct by construction, NOT cardinal government.
# All 12 forms are VESUM-confirmed genitive month nouns (noun:...:m:v_rod).
_GENITIVE_MONTHS = {
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
}


@dataclass(frozen=True)
class _Trigger:
    word: str
    kind: str  # "gen" | "dat" | "ambiguous" | "transparent"


def _strip_trigger(tokens: list[str]) -> tuple[str, list[str], _Trigger | None]:
    """Detect + strip a leading trigger from the CURATED §6c lexicon only.

    Returns (auto_context_case, remaining_tokens, trigger_or_None). Leading
    words NOT in this lexicon (e.g. "на", "у", a governing verb like "бачу")
    are NOT touched here — see the leading-word handling in
    `check_numeral_government` for how those are skipped when the caller
    supplies an explicit `context_case`.
    """
    if not tokens:
        return NOM, tokens, None
    lowered = [_strip(t).lower() for t in tokens]
    if len(lowered) >= 2:
        two = f"{lowered[0]} {lowered[1]}"
        if two in _OVERRIDING_GEN_PHRASES:
            return GEN, tokens[2:], _Trigger(two, "gen")
    w = lowered[0]
    if w in _OVERRIDING_GEN_SINGLE:
        return GEN, tokens[1:], _Trigger(w, "gen")
    if w in _OVERRIDING_DAT_SINGLE:
        return DAT, tokens[1:], _Trigger(w, "dat")
    if w in _AMBIGUOUS_SINGLE:
        return NOM, tokens[1:], _Trigger(w, "ambiguous")
    if w in _TRANSPARENT_SINGLE:
        # Transparent: does NOT override government — base (nominative-like)
        # counting government still applies, same as if the trigger were
        # absent (fleet correction: the old понад→genitive mapping was wrong;
        # VESUM confirms "понад два роки" = nom-pl "роки").
        return NOM, tokens[1:], _Trigger(w, "transparent")
    return NOM, tokens, None


# ---------------------------------------------------------------------------
# §6c Step 2 — numeral classes
# ---------------------------------------------------------------------------
ENDS_1 = "ends_1"
ENDS_2_4 = "ends_2_4"
ENDS_5_9_0 = "ends_5_9_0"
COLLECTIVE = "collective"
HALF = "half"
MAGNITUDE = "magnitude"
ORDINAL = "ordinal"
DECIMAL = "decimal"
HYPHENATED = "hyphenated"

# A range may use an ASCII hyphen or Ukrainian typography's en dash.  Both
# spellings are intentionally sent to teacher review rather than treated as a
# single cardinal.  (The latter was absent from the original regression bank,
# so ``1939–1940`` could fall through as "no numeral found".)
_RANGE_SEPARATORS = "-–"

# Lemma → class, for cardinal numeral WORDS (VESUM pos == "numr"). Digits are
# classified arithmetically in `_classify_token` instead (VESUM has no
# entries for bare digit strings like "17").
_LEMMA_CLASS: dict[str, str] = {
    "один": ENDS_1,
    "два": ENDS_2_4,
    "обидва": ENDS_2_4,
    "три": ENDS_2_4,
    "чотири": ENDS_2_4,
    "п'ять": ENDS_5_9_0,
    "шість": ENDS_5_9_0,
    "сім": ENDS_5_9_0,
    "вісім": ENDS_5_9_0,
    "дев'ять": ENDS_5_9_0,
    "десять": ENDS_5_9_0,
    "одинадцять": ENDS_5_9_0,
    "дванадцять": ENDS_5_9_0,
    "тринадцять": ENDS_5_9_0,
    "чотирнадцять": ENDS_5_9_0,
    "п'ятнадцять": ENDS_5_9_0,
    "шістнадцять": ENDS_5_9_0,
    "сімнадцять": ENDS_5_9_0,
    "вісімнадцять": ENDS_5_9_0,
    "дев'ятнадцять": ENDS_5_9_0,
    "двадцять": ENDS_5_9_0,
    "тридцять": ENDS_5_9_0,
    "сорок": ENDS_5_9_0,
    "п'ятдесят": ENDS_5_9_0,
    "шістдесят": ENDS_5_9_0,
    "сімдесят": ENDS_5_9_0,
    "вісімдесят": ENDS_5_9_0,
    "дев'яносто": ENDS_5_9_0,
    "сто": ENDS_5_9_0,
    "двісті": ENDS_5_9_0,
    "триста": ENDS_5_9_0,
    "чотириста": ENDS_5_9_0,
    "п'ятсот": ENDS_5_9_0,
    "шістсот": ENDS_5_9_0,
    "сімсот": ENDS_5_9_0,
    "вісімсот": ENDS_5_9_0,
    "дев'ятсот": ENDS_5_9_0,
    "нуль": ENDS_5_9_0,
    "двоє": COLLECTIVE,
    "троє": COLLECTIVE,
    "четверо": COLLECTIVE,
    "п'ятеро": COLLECTIVE,
    "шестеро": COLLECTIVE,
    "семеро": COLLECTIVE,
    "восьмеро": COLLECTIVE,
    "дев'ятеро": COLLECTIVE,
    "десятеро": COLLECTIVE,
    "обоє": COLLECTIVE,
    "обидвоє": COLLECTIVE,
    "півтора": HALF,
    "півтори": HALF,
    "тисяча": MAGNITUDE,
    "мільйон": MAGNITUDE,
    "мільярд": MAGNITUDE,
}

_MAGNITUDE_LEMMAS = {"тисяча", "мільйон", "мільярд"}

# Surface-gender variants of the paucal numerals два / обидва. VESUM tags do
# NOT distinguish gender for these (both "два" and "дві" resolve to the same
# lemma with identical genderless `numr:p:*` tags), so agreement can only be
# checked from the SURFACE form. три/чотири have no gender variants at all
# (VESUM-invariant) and are therefore excluded from the surface check.
_TWO_FORMS_F = {"дві", "обидві"}
_TWO_FORMS_MN = {"два", "обидва"}

_DIGIT_RE = re.compile(r"^\d+([.,]\d+)?$")


@dataclass(frozen=True)
class _TokenInfo:
    token: str
    class_: str | None
    lemma: str | None
    is_digit: bool = False


def _classify_digit(token: str) -> _TokenInfo:
    core = _strip(token)
    if "," in core or "." in core:
        return _TokenInfo(token=token, class_=DECIMAL, lemma=core, is_digit=True)
    n = int(core)
    last_two = n % 100
    last_one = n % 10
    if 11 <= last_two <= 14:
        cls = ENDS_5_9_0
    elif last_one == 1:
        cls = ENDS_1
    elif last_one in (2, 3, 4):
        cls = ENDS_2_4
    else:
        cls = ENDS_5_9_0
    return _TokenInfo(token=token, class_=cls, lemma=core, is_digit=True)


def _looks_numeral(seg: str) -> bool:
    """Cheap 'is this segment numeral-ish' test (digit or VESUM cardinal),
    used only to recognize hyphenated ranges like '2-3' / 'два-три'.
    """
    seg = seg.lower()
    if _DIGIT_RE.match(seg):
        return True
    return any(m["pos"] == "numr" for m in vt.parse_word(seg))


def _classify_token(token: str) -> _TokenInfo | None:
    """Classify one token as a numeral (returning its §6c class) or None if
    it is not part of the numeral phrase (i.e. it's the noun, or unrelated).

    Recognition is entirely VESUM-tag-driven:
    - a hyphenated token whose first segment is numeral-ish is a HYPHENATED
      range/glued token (deliberate WARN — never silently misinterpreted);
    - a bare digit/decimal string is classified arithmetically;
    - a VESUM `pos == "numr"` match resolves via the `_LEMMA_CLASS` lexicon;
    - a VESUM `pos == "adj"` match carrying VESUM's own trailing `:numr`
      flag is an ORDINAL (`перший`, `сімнадцятий`, ... — tool-confirmed
      2026-07-08, not a hand-curated lemma list);
    - a VESUM `pos == "noun"` match whose lemma is a magnitude word
      (тисяча/мільйон/мільярд) is MAGNITUDE; лемма == "нуль" is a cardinal
      (нуль is VESUM-tagged as a plain noun, so it needs an explicit hook).
    """
    core = _strip(token)
    if not core:
        return None
    if any(separator in core for separator in _RANGE_SEPARATORS) and _looks_numeral(
        re.split(f"[{_RANGE_SEPARATORS}]", core, maxsplit=1)[0]
    ):
        return _TokenInfo(token=token, class_=HYPHENATED, lemma=core)
    if _DIGIT_RE.match(core):
        return _classify_digit(token)

    matches = vt.parse_word(core)
    numr_matches = [m for m in matches if m["pos"] == "numr"]
    if numr_matches:
        lemma = numr_matches[0]["lemma"]
        return _TokenInfo(token=token, class_=_LEMMA_CLASS.get(lemma), lemma=lemma)

    adj_matches = [m for m in matches if m["pos"] == "adj" and m["numr_flag"]]
    if adj_matches:
        return _TokenInfo(token=token, class_=ORDINAL, lemma=adj_matches[0]["lemma"])

    noun_magnitude = [
        m for m in matches if m["pos"] == "noun" and m["lemma"] in _MAGNITUDE_LEMMAS
    ]
    if noun_magnitude:
        return _TokenInfo(token=token, class_=MAGNITUDE, lemma=noun_magnitude[0]["lemma"])

    noun_zero = [m for m in matches if m["pos"] == "noun" and m["lemma"] == "нуль"]
    if noun_zero:
        return _TokenInfo(token=token, class_=ENDS_5_9_0, lemma="нуль")

    return None


# ---------------------------------------------------------------------------
# §6c Step 2 — required {case, number} for the governed NOUN and for the
# numeral's OWN surface form, given the numeral class + effective context case
# ---------------------------------------------------------------------------
def _required_forms(
    class_: str, effective_case: str, animate: bool
) -> tuple[str, str, str, str]:
    """Returns (noun_case, noun_number, numeral_case, numeral_number)."""
    if class_ == ENDS_1:
        return effective_case, "sg", effective_case, "sg"
    if class_ == ENDS_2_4:
        if effective_case == NOM:
            case_num = (NOM, "pl")
        elif effective_case == ACC:
            case_num = (GEN, "pl") if animate else (NOM, "pl")
        else:
            case_num = (effective_case, "pl")
        return case_num[0], case_num[1], case_num[0], case_num[1]
    if class_ == ENDS_5_9_0:
        if effective_case in (NOM, ACC):
            return GEN, "pl", NOM, "pl"
        return effective_case, "pl", effective_case, "pl"
    if class_ == COLLECTIVE:
        if effective_case in (NOM, ACC):
            return GEN, "pl", NOM, "pl"
        return effective_case, "pl", effective_case, "pl"
    if class_ == HALF:
        return GEN, "sg", effective_case, "sg"
    raise ValueError(f"No §6c government rule for numeral class {class_!r}")


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------
def _make(status: str, rule: str, detail: str, expected: str | None = None) -> dict:
    return {"status": status, "rule": rule, "expected": expected, "detail": detail}


def _pass(rule: str, detail: str) -> dict:
    return _make("pass", rule, detail)


def _warn(rule: str, detail: str, expected: str | None = None) -> dict:
    return _make("warn", rule, detail, expected)


def _fail(rule: str, detail: str, expected: str | None = None) -> dict:
    return _make("fail", rule, detail, expected)


def _expected_str(case: str, number: str, lemma: str | None) -> str:
    label = vt.describe(case, number)
    if lemma:
        form = vt.find_form(lemma, case, number)
        if form:
            return f"{label} (e.g. '{form}')"
    return label


def _noun_sg_gender(core: str) -> str | None:
    """Singular gender of a governed noun, with a pl-tantum-shaped fallback.

    A plural SURFACE form (e.g. "столи", "книги") carries no gender in its
    own VESUM tags, so we fall back to parsing the noun's LEMMA for its
    singular gender (стіл -> m, книга -> f). Returns None only when even the
    lemma has no gendered singular parse.
    """
    parses = vt.parse_word(core, pos_filter="noun")
    for p in parses:
        if p["number"] == "sg" and p["gender"]:
            return p["gender"]
    if parses:
        lemma = parses[0]["lemma"]
        for p in vt.parse_word(lemma, pos_filter="noun"):
            if p["number"] == "sg" and p["gender"]:
                return p["gender"]
    return None


def _surface_gender_issue(numeral_token: str, noun_sg_gender: str | None) -> str | None:
    """§6c fix: два/дві/обидва/обидві must surface-agree with the counted
    noun's gender (nominative paucal only). Returns an error detail string
    on mismatch, else None. Oblique two-forms (двох/двом/двома) and три/
    чотири/digits are not checked (no surface gender contrast).
    """
    core = _strip(numeral_token).lower()
    claims_f = core in _TWO_FORMS_F
    claims_mn = core in _TWO_FORMS_MN
    if not (claims_f or claims_mn) or noun_sg_gender is None:
        return None
    if claims_f and noun_sg_gender in ("m", "n"):
        return (
            f"'{numeral_token}' is the feminine paucal form but the noun is "
            f"gender '{noun_sg_gender}' — expected the masculine/neuter form "
            "(два/обидва)."
        )
    if claims_mn and noun_sg_gender == "f":
        return (
            f"'{numeral_token}' is the masculine/neuter paucal form but the "
            "noun is feminine — expected the feminine form (дві/обидві)."
        )
    return None


def _verify_numeral_form(
    token: str,
    class_: str,
    expected_case: str,
    expected_number: str,
    required_gender: str | None,
) -> tuple[bool, str]:
    """§6c Step 3: the numeral's own form must be VESUM-valid for the
    required case/number, and (only where VESUM's tags actually carry
    gender for this word — один/одна/одне and півтора/півтори) gender-agree
    with the noun. VESUM does NOT encode a gender distinction in the tags
    for два/дві/три/чотири (both "два" and "дві" resolve to the same lemma
    with identical, genderless tags) — surface-gender agreement for the
    paucal two-forms is handled separately in `_surface_gender_issue`.
    """
    core = _strip(token)
    if _DIGIT_RE.match(core):
        # Digits aren't VESUM-checkable word forms; trust the arithmetic
        # classification already performed.
        return True, ""
    parses = vt.parse_word(core)
    numr_matches = [m for m in parses if m["pos"] == "numr"]
    if not numr_matches:
        # нуль is a §6c cardinal but VESUM tags it as a plain NOUN (no numr
        # entry). It declines noun-like (singular), so verify CASE only
        # against its noun parses — number is irrelevant for this lexeme.
        zero_matches = [m for m in parses if m["pos"] == "noun" and m["lemma"] == "нуль"]
        if zero_matches:
            if any(m["case"] == expected_case for m in zero_matches):
                return True, ""
            found = sorted({m["case"] for m in zero_matches})
            return False, (
                f"'{token}' (нуль) has no VESUM form for "
                f"{vt.CASE_LABELS.get(expected_case, expected_case)} (found: {found})."
            )
        return False, f"'{token}' is not a valid numeral form in VESUM."
    case_number_matches = [
        m for m in numr_matches if m["case"] == expected_case and m["number"] == expected_number
    ]
    if not case_number_matches:
        found = sorted({(m["case"], m["number"]) for m in numr_matches})
        return False, (
            f"'{token}' has no VESUM numeral form for "
            f"{vt.describe(expected_case, expected_number)} (found: {found})."
        )
    if required_gender is not None and class_ in (ENDS_1, HALF):
        gender_ok = any(m["gender"] == required_gender for m in case_number_matches)
        if not gender_ok:
            return False, (
                f"'{token}' does not agree in gender with the noun "
                f"(needs gender={required_gender})."
            )
    return True, ""


def _verify_compound_prefix(
    prefix_infos: list[_TokenInfo], effective_case: str
) -> str | None:
    """In oblique / override contexts every WORD component of a compound
    numeral must itself decline (e.g. до -> genitive: 'ста двадцяти одного',
    not 'сто двадцять одного'). Returns the first component token that has
    no VESUM numeral parse in `effective_case`, else None.

    Digit components can't carry a surface case, so they're not verifiable
    here and are skipped (the caller downgrades to WARN, never PASS).
    """
    for info in prefix_infos:
        if info.is_digit or info.class_ == HYPHENATED:
            continue
        core = _strip(info.token)
        numr_matches = [m for m in vt.parse_word(core) if m["pos"] == "numr"]
        if not numr_matches:
            return info.token
        if not any(m["case"] == effective_case for m in numr_matches):
            return info.token
    return None


def _find_head_noun(post_numeral_tokens: list[str]) -> str | None:
    """Rightmost token after the numeral that VESUM parses as a NOUN.

    Skips trailing punctuation-only tokens, trailing verbs ('прийшли'),
    possessives/adjectives ('мої'), etc. — the governed head noun is the
    last genuine noun, not merely the last token. Returns the stripped core
    (ready for VESUM lookups) or None.
    """
    for token in reversed(post_numeral_tokens):
        core = _strip(token)
        if not core:
            continue
        if vt.parse_word(core, pos_filter="noun"):
            return core
    return None


# Sentinel distinguishing "not a fraction construction" from "a fraction whose
# governed noun is missing" (which is a genuine no-noun-found).
_NO_FRACTION = object()


def _fraction_continuation(post_numeral_tokens: list[str]):
    """Detect the mixed-fraction continuation "з половиною|чвертю|третиною"
    immediately after a cardinal. If matched, return the governed noun core
    located AFTER the fraction words (or None when no noun follows). Return
    the `_NO_FRACTION` sentinel when it is not a fraction construction.

    "половиною"/"чвертю"/"третиною" are themselves nouns, so the naive
    rightmost-noun scan would otherwise stop the phrase at the fraction word
    (or, when the preposition intervenes, report no-noun-found on 'два з').
    """
    if (
        len(post_numeral_tokens) >= 2
        and _strip(post_numeral_tokens[0]).lower() in _FRACTION_PREPS
        and _strip(post_numeral_tokens[1]).lower() in _FRACTION_WORDS
    ):
        return _find_head_noun(post_numeral_tokens[2:])
    return _NO_FRACTION


def check_numeral_government(phrase: str, context_case: str | None = None) -> dict:
    """THE MOAT. Pure, deterministic. Never raises — always returns a Result.

    `context_case` accepts either the short codes (nom/gen/dat/acc/instr/
    loc/voc) or raw VESUM codes (v_naz/v_rod/...). When given, it is trusted
    over auto-detection (the caller — e.g. a known governing verb like
    "бачу" -> accusative, or a preposition outside the curated §6c trigger
    lexicon like "на" -> locative — has already resolved case externally;
    §6c step 1 explicitly scopes verb-governed/free-prose case detection out
    of this gate). When absent, only the curated trigger lexicon is used;
    anything else is either genitive-forcing/dative-forcing/ambiguous/
    transparent per §6c, or defaults to nominative.
    """
    raw_tokens = phrase.strip().split()
    if not raw_tokens:
        return _fail("empty-phrase", "Empty phrase supplied to the numeral gate.")

    normalized_context: str | None = None
    if context_case is not None:
        normalized_context = _normalize_case(context_case)
        if normalized_context is None:
            return _fail(
                "invalid-context-case",
                f"Unrecognized context_case={context_case!r}.",
            )

    auto_case, tokens, trigger = _strip_trigger(raw_tokens)

    if trigger is not None and trigger.kind == "ambiguous" and normalized_context is None:
        return _warn(
            "ambiguous-preposition",
            f"Trigger '{trigger.word}' is case-ambiguous (genitive/ablative "
            "vs instrumental/comitative — same surface form, different "
            "government) and cannot be auto-resolved without external "
            "context. Pass context_case explicitly if known.",
        )

    effective_case = normalized_context if normalized_context is not None else auto_case

    # Find the first token (if any) that's recognizable as part of the
    # numeral. If NONE of the tokens classify as a numeral, this is simply
    # not a numeral phrase — "no-numeral-found", regardless of context_case.
    first_numeral_idx = next(
        (i for i, t in enumerate(tokens) if _classify_token(t) is not None), None
    )
    if first_numeral_idx is None:
        return _fail("no-numeral-found", f"No numeral token found in {phrase!r}.")

    # A leading word BEFORE the numeral that's outside the curated trigger
    # lexicon (a preposition like "на"/"у", or a governing verb like
    # "бачу") is only skipped when the caller has already supplied
    # context_case explicitly (meaning it was resolved externally).
    # Otherwise, an unrecognized leading word before the numeral means the
    # case genuinely can't be determined here — warn, per §6c step 1's
    # documented limit, rather than silently defaulting.
    if first_numeral_idx > 0:
        if normalized_context is None:
            return _warn(
                "context-undetermined",
                f"Leading token '{tokens[0]}' in {phrase!r} is not a "
                "recognized numeral or §6c case-government trigger — cannot "
                "auto-determine the required case (e.g. a governing verb or "
                "an untracked preposition). Pass context_case explicitly if "
                "known.",
            )
        tokens = tokens[first_numeral_idx:]

    numeral_infos: list[_TokenInfo] = []
    idx = 0
    while idx < len(tokens):
        info = _classify_token(tokens[idx])
        if info is None:
            break
        numeral_infos.append(info)
        idx += 1

    post_numeral_tokens = tokens[idx:]
    last = numeral_infos[-1]

    # -----------------------------------------------------------------
    # DATE construction "<number> <genitive-month>" (23 квітня / двадцять
    # третє квітня / 23-го квітня) — the number is an ordinal DAY and the
    # month is genitive; correct by construction, NOT cardinal government.
    # Short-circuits BEFORE any cardinal/ordinal rule. A month word only
    # triggers this because a numeral precedes it (we are in the gate); a
    # nominative month ("23 квітень") does NOT match and falls through to
    # normal (correctly failing) government.
    # -----------------------------------------------------------------
    if post_numeral_tokens and _strip(post_numeral_tokens[0]).lower() in _GENITIVE_MONTHS:
        month = _strip(post_numeral_tokens[0]).lower()
        return _pass(
            "date-not-cardinal",
            f"'{phrase}' is a date (<число> {month}) — the day is ordinal and "
            "the month is genitive; correct by construction, not cardinal "
            "government.",
        )

    # -----------------------------------------------------------------
    # Deliberate WARN/FAIL for token shapes we won't silently misread
    # -----------------------------------------------------------------
    if any(i.class_ == HYPHENATED for i in numeral_infos):
        return _warn(
            "hyphenated-token",
            f"Hyphenated numeral token in {phrase!r} (range like '2-3'/"
            "'два-три' or a glued '17-ділянок') — government is ambiguous / "
            "the token is malformed; deferring rather than guessing.",
        )

    if len(numeral_infos) > 1:
        has_digit = any(i.is_digit for i in numeral_infos)
        has_word = any(not i.is_digit for i in numeral_infos)
        # The only legal digit+word compound is digits followed by a
        # magnitude WORD ("20 тисяч"). Anything else ("20 два", "два 3")
        # is a malformed mix.
        digit_word_ok = last.class_ == MAGNITUDE and all(
            i.is_digit for i in numeral_infos[:-1]
        )
        if has_digit and has_word and not digit_word_ok:
            return _fail(
                "malformed-compound",
                f"Mixed digit+word numeral compound in {phrase!r} is "
                "malformed (only all-digit, all-word, or 'digits + magnitude "
                "word' compounds are well-formed).",
            )

    if last.class_ == DECIMAL:
        return _warn(
            "decimal-fraction",
            f"Decimal/fraction numeral '{last.token}' — government is "
            "genuinely variable in modern usage (VESUM confirms both "
            "singular and paucal complement forms attested); deferring to "
            "teacher review rather than hard-failing.",
        )
    if last.class_ == ORDINAL:
        return _warn(
            "ordinal-skip",
            f"Ordinal numeral '{last.token}' (lemma={last.lemma}) — "
            "adjectival gender/case agreement is out of the cardinal-moat "
            "scope for slice 1.",
        )
    if last.class_ is None:
        return _warn(
            "unclassified-numeral",
            f"Numeral token '{last.token}' (lemma={last.lemma}) has no "
            "known §6c government class.",
        )

    # Mixed-fraction continuation "<cardinal> з половиною/чвертю/третиною
    # <noun>" (spelled-out decimal, e.g. «два з половиною рази» = «2,5 рази»).
    # Skip the fraction words to reach the real governed noun; government is
    # variable (like a decimal — VESUM: both 'рази'/'раза' valid) -> WARN. A
    # genuine dangling numeral with no noun after the fraction still fails.
    fraction_noun = _fraction_continuation(post_numeral_tokens)
    if fraction_noun is not _NO_FRACTION:
        if fraction_noun is None:
            return _fail(
                "no-noun-found",
                f"No governed noun found after the mixed fraction in {phrase!r}.",
            )
        return _warn(
            "fraction-variable-government",
            f"Mixed fraction (<числівник> з половиною/чвертю/третиною) in "
            f"{phrase!r} — government is variable (VESUM confirms both e.g. "
            f"'рази'/'раза'); governed noun '{fraction_noun}' located; "
            "deferring to teacher review rather than failing.",
        )

    # Governed head noun = rightmost genuine NOUN after the numeral (skips
    # trailing verbs/possessives/punctuation).
    head_noun = _find_head_noun(post_numeral_tokens)
    if head_noun is None:
        return _fail(
            "no-noun-found",
            f"No governed noun found after the numeral in {phrase!r}.",
        )

    noun_matches = vt.parse_word(head_noun, pos_filter="noun")
    if not noun_matches:
        return _fail(
            "noun-not-in-vesum", f"'{head_noun}' was not found in VESUM — cannot verify."
        )
    noun_animate = any(m["animacy"] == "anim" for m in noun_matches)
    noun_lemma = noun_matches[0]["lemma"]

    # -----------------------------------------------------------------
    # Nested magnitude phrase (тисяча/мільйон/мільярд) — §6c Step 2
    # -----------------------------------------------------------------
    if last.class_ == MAGNITUDE:
        outer_infos = numeral_infos[:-1]
        outer_last = outer_infos[-1] if outer_infos else None
        outer_class = outer_last.class_ if outer_last else ENDS_1  # bare "тисяча" = "one thousand"
        if outer_class in (DECIMAL, ORDINAL, HYPHENATED, None):
            return _warn(
                "nested-magnitude-outer-unclear",
                f"Outer numeral before magnitude word '{last.token}' in "
                f"{phrase!r} has no clear §6c class.",
            )
        magnitude_case, magnitude_number, magnitude_num_case, magnitude_num_number = (
            _required_forms(outer_class, effective_case, animate=False)
        )
        magnitude_ok = vt.form_has(
            _strip(last.token), case=magnitude_case, number=magnitude_number, pos_filter="noun"
        )
        if not magnitude_ok:
            expected = _expected_str(magnitude_case, magnitude_number, last.lemma)
            return _fail(
                "nested-magnitude-form",
                f"'{last.token}' is not a VESUM form of '{last.lemma}' matching "
                f"{vt.describe(magnitude_case, magnitude_number)} (required by "
                f"outer numeral class {outer_class}).",
                expected=expected,
            )
        # Outer paucal (дві тисячі vs *два тисячі): the outer два/дві must
        # surface-agree with the magnitude word's own gender.
        if outer_last is not None and outer_class == ENDS_2_4 and effective_case == NOM:
            magnitude_gender = _noun_sg_gender(_strip(last.token))
            gender_issue = _surface_gender_issue(outer_last.token, magnitude_gender)
            if gender_issue:
                return _fail("nested-magnitude-gender", gender_issue)
        if outer_last is not None:
            outer_ok, outer_detail = _verify_numeral_form(
                outer_last.token, outer_class, magnitude_num_case, magnitude_num_number, None
            )
            if not outer_ok:
                return _fail("nested-magnitude-outer-numeral", outer_detail)
        # Magnitude word ALWAYS governs its complement as genitive plural,
        # regardless of the magnitude word's own case (§6c: "дві тисячі
        # людей" / "мільйона доларів" both keep the complement gen-pl).
        if not vt.form_has(head_noun, case=GEN, number="pl", pos_filter="noun"):
            expected = _expected_str(GEN, "pl", noun_lemma)
            return _fail(
                "nested-magnitude-complement",
                f"'{head_noun}' is not a genitive-plural VESUM form, but "
                f"'{last.token}' ({last.lemma}) always governs its complement "
                "as genitive plural.",
                expected=expected,
            )
        # Un-declined outer prefix in oblique/override context → never PASS.
        if effective_case in _OBLIQUE and len(outer_infos) > 1:
            bad = _verify_compound_prefix(outer_infos[:-1], effective_case)
            if bad is not None:
                return _warn(
                    "compound-prefix-unverified",
                    f"Compound numeral prefix token '{bad}' in {phrase!r} is "
                    f"not declined to the required "
                    f"{vt.CASE_LABELS.get(effective_case, effective_case)} case — "
                    "cannot confirm the whole numeral is correctly governed.",
                )
        return _pass(
            "nested-magnitude",
            f"'{phrase}' — '{last.token}' ({vt.describe(magnitude_case, magnitude_number)}) "
            f"+ complement '{head_noun}' (genitive plural) verified against VESUM.",
        )

    # -----------------------------------------------------------------
    # Collective / half / ends-1 / ends-2-4 / ends-5-9-0
    # -----------------------------------------------------------------
    noun_case, noun_number, numeral_case, numeral_number = _required_forms(
        last.class_, effective_case, animate=noun_animate
    )

    animacy_check = "anim" if noun_animate else None
    if not vt.form_has(
        head_noun, case=noun_case, number=noun_number, animacy=animacy_check, pos_filter="noun"
    ):
        expected = _expected_str(noun_case, noun_number, noun_lemma)
        return _fail(
            f"gov-{last.class_}",
            f"'{head_noun}' has no VESUM form matching "
            f"{vt.describe(noun_case, noun_number)}"
            + (" (animate)" if noun_animate else "")
            + f", required by numeral class {last.class_} in context case "
            f"'{effective_case}'.",
            expected=expected,
        )

    required_gender = _noun_sg_gender(head_noun) if last.class_ in (ENDS_1, HALF) else None
    numeral_ok, numeral_detail = _verify_numeral_form(
        last.token, last.class_, numeral_case, numeral_number, required_gender
    )
    if not numeral_ok:
        return _fail(f"numeral-form-{last.class_}", numeral_detail)

    # Surface-gender agreement for the paucal two-forms (два/дві/обидва/
    # обидві) in the nominative — VESUM tags can't back this, the surface
    # form is the only signal.
    if last.class_ == ENDS_2_4 and effective_case == NOM:
        gender_issue = _surface_gender_issue(last.token, _noun_sg_gender(head_noun))
        if gender_issue:
            return _fail("gender-ends_2_4", gender_issue)

    # Un-declined compound prefix in oblique/override context → never PASS a
    # nominative prefix that should have declined (e.g. до ста двадцяти ...).
    if effective_case in _OBLIQUE and len(numeral_infos) > 1:
        bad = _verify_compound_prefix(numeral_infos[:-1], effective_case)
        if bad is not None:
            return _warn(
                "compound-prefix-unverified",
                f"Compound numeral prefix token '{bad}' in {phrase!r} is not "
                f"declined to the required "
                f"{vt.CASE_LABELS.get(effective_case, effective_case)} case — "
                "cannot confirm the whole numeral is correctly governed.",
            )

    return _pass(
        f"gov-{last.class_}",
        f"'{phrase}' — '{last.token}' ({last.class_}) + '{head_noun}' "
        f"({vt.describe(noun_case, noun_number)}) verified against VESUM "
        f"(context case: {effective_case}).",
    )
