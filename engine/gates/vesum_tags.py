"""Thin structured layer over `scripts.verification.vesum`.

VESUM tags are the verification SOURCE OF TRUTH for the numeral gate
(slice-1-build-plan.md §6c). pymorphy3 is never used here — only for
lemmatizing raw anchor prose elsewhere in the pipeline (out of scope for
this module), never for final case/number/animacy verification, because it
can disagree with VESUM (fleet finding A2).

Every lookup takes/uses an explicit `db_path` (`engine.paths.VESUM_DB`) —
never a cwd-relative default — so this module works whether the process is
launched from the repo root or from `.agent/tmp/hramatka/engine/` itself.
"""

from __future__ import annotations

import sys

from . import _bootstrap_sys_path

_bootstrap_sys_path()

from scripts.verification.vesum import verify_lemma, verify_word  # noqa: E402

# ---------------------------------------------------------------------------
# VESUM tag reference (confirmed live 2026-07-08 — see engine-integration-recon.md)
# ---------------------------------------------------------------------------
# Case codes: v_naz=nom v_rod=gen v_dav=dat v_zna=acc v_oru=instr v_mis=loc v_kly=voc
# `:p:` = plural (absence = singular). `anim`/`inanim` on nouns.
# For ANIMATE nouns, the accusative form == the genitive form (v_zna≡v_rod) —
# VESUM tags such forms with BOTH v_rod and v_zna, so a plain tag-membership
# check against either case already reflects that syncretism correctly.

CASE_CODES: dict[str, str] = {
    "v_naz": "nom",
    "v_rod": "gen",
    "v_dav": "dat",
    "v_zna": "acc",
    "v_oru": "instr",
    "v_mis": "loc",
    "v_kly": "voc",
}
CASE_LABELS: dict[str, str] = {
    "nom": "nominative",
    "gen": "genitive",
    "dat": "dative",
    "acc": "accusative",
    "instr": "instrumental",
    "loc": "locative",
    "voc": "vocative",
}
NUMBER_LABELS: dict[str, str] = {"sg": "singular", "pl": "plural"}

_GENDER_CODES = {"m", "f", "n"}


def parse_tag(tags: str) -> dict:
    """Parse one raw VESUM tag string into a structured dict.

    Returns: {pos, case, number('sg'|'pl'), animacy('anim'|'inanim'|None),
    gender('m'|'f'|'n'|None), numr_flag(bool), raw}.

    `pos` here is read from the tag string itself (`parts[0]`); callers that
    already have the authoritative `pos` column from a VESUM row (as
    `parse_word`/`verify_word` do) should prefer that value instead.
    `numr_flag` is VESUM's own trailing `:numr` marker — it appears on
    ordinal adjectives (`сімнадцятий` -> `adj:m:v_naz:numr`) and on
    "magnitude" counting nouns (`тисяча`/`мільйон` -> `noun:...:numr`) and
    is the tool-backed (not hand-curated) signal the numeral gate uses to
    recognize both classes.
    """
    parts = tags.split(":")
    pos = parts[0] if parts else None
    case = None
    for code, name in CASE_CODES.items():
        if code in parts:
            case = name
            break
    number = "pl" if "p" in parts else "sg"
    gender = next((p for p in parts if p in _GENDER_CODES), None)
    animacy = "anim" if "anim" in parts else ("inanim" if "inanim" in parts else None)
    return {
        "pos": pos,
        "case": case,
        "number": number,
        "gender": gender,
        "animacy": animacy,
        "numr_flag": "numr" in parts,
        "raw": tags,
    }


def parse_word(word: str, pos_filter: str | None = None, *, db_path=None) -> list[dict]:
    """Look up `word` in VESUM; return one structured parse per match.

    Tries the surface form as given, then a lower-cased fallback (VESUM
    forms are corpus-derived and normally lowercase; sentence-initial
    capitalization would otherwise miss).
    """
    from . import _vesum_db_path

    resolved_db = db_path if db_path is not None else _vesum_db_path()
    matches = verify_word(word, pos_filter=pos_filter, db_path=resolved_db)
    if not matches and word != word.lower():
        matches = verify_word(word.lower(), pos_filter=pos_filter, db_path=resolved_db)
    parsed = []
    for m in matches:
        p = parse_tag(m["tags"])
        p["pos"] = m["pos"]  # trust the DB column over tag-string parsing
        p["lemma"] = m["lemma"]
        parsed.append(p)
    return parsed


def form_has(
    word: str,
    case: str,
    number: str,
    animacy: str | None = None,
    pos_filter: str | None = None,
) -> bool:
    """True iff ANY VESUM parse of `word` matches the given case/number
    (and animacy, if given). `case`/`number` use the short codes from
    CASE_CODES.values() / NUMBER_LABELS.keys() (nom/gen/.../sg/pl) — not the
    raw `v_naz`-style VESUM codes.
    """
    for parsed in parse_word(word, pos_filter=pos_filter):
        if parsed["case"] != case or parsed["number"] != number:
            continue
        if animacy is not None and parsed["animacy"] != animacy:
            continue
        return True
    return False


def find_form(
    lemma: str,
    case: str,
    number: str,
    animacy: str | None = None,
    *,
    db_path=None,
) -> str | None:
    """Reverse lookup: the first surface form of `lemma` in VESUM matching
    case/number(/animacy). Best-effort — used only to build a human-readable
    'expected' hint in gate results, never for verification itself.
    """
    from . import _vesum_db_path

    resolved_db = db_path if db_path is not None else _vesum_db_path()
    for row in verify_lemma(lemma, db_path=resolved_db):
        parsed = parse_tag(row["tags"])
        if parsed["case"] != case or parsed["number"] != number:
            continue
        if animacy is not None and parsed["animacy"] != animacy:
            continue
        return row["word_form"]
    return None


def describe(case: str, number: str) -> str:
    """Human-readable 'genitive plural' style label for a (case, number) pair."""
    return f"{CASE_LABELS.get(case, case)} {NUMBER_LABELS.get(number, number)}"
