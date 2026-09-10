"""Grounding-IN (slice-1 §4 + §R B1/C1): reuse repo assets read-only.

Pipeline:
  anchor text -> lemmatize (VESUM reverse-lookup, deterministic) ->
  atlas {lemma -> payload} dict (ONE scan for the needed lemmas) +
  numeral inventory -> a compact, VERIFIED grounding pack for the prompt.

VESUM reverse-lookup is used for lemmatization (not pymorphy3): it is the
same source of truth the gates use and is fully deterministic, avoiding a
second morphology engine that could disagree with VESUM (§6c note). pymorphy3
remains permissible for lemmatization elsewhere but is unnecessary here.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from . import flags, paths
from .linguistics import verify_words

# Cyrillic word token (keeps apostrophe + soft signs; excludes digits).
_WORD_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’-]+", re.UNICODE)
_CONTENT_POS = {"noun", "adj", "verb", "adv"}

# Numeral surface forms worth flagging in the inventory (digits are matched
# separately). Kept small + deterministic — the moat gate does the real work.
# Include the VESUM-confirmed oblique forms needed by the case-trigger path:
# otherwise «близько двадцяти/трьох ...» never reaches the gate at all.
_NUMERAL_SURFACE = (
    r"нуль|один|одна|одне|два|дві|обидва|обидві|три|чотири|п'ять|шість|сім|"
    r"вісім|дев'ять|десять|одинадцять|дванадцять|тринадцять|чотирнадцять|"
    r"п'ятнадцять|шістнадцять|сімнадцять|вісімнадцять|дев'ятнадцять|двадцять|"
    r"двадцяти|трьох|тридцять|сорок|п'ятдесят|шістдесят|сімдесят|вісімдесят|"
    r"дев'яносто|сто|двоє|троє|четверо|п'ятеро|обоє|обидвоє|півтора|півтори|"
    r"тисяч[аіуео]?|мільйон\w*"
)
_NUMERAL_WORD_RE = re.compile(r"\b(" + _NUMERAL_SURFACE + r")\b", re.UNICODE | re.IGNORECASE)
_DIGIT_NUM_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")

_WORD_SURFACE = r"[А-ЯҐЄІЇа-яґєіїʼ'’-]+"
_MAGNITUDE_SURFACE = r"тисяч[аіуео]?|мільйон\w*|мільярд\w*"

# Nested magnitude: the magnitude word itself is governed by the outer
# cardinal, then in turn governs its complement. Preserve all three pieces so
# the numeral gate sees «двадцять тисяч гривень», not two broken overlaps.
_MAGNITUDE_TAIL_RE = re.compile(
    r"\s+(?P<magnitude>" + _MAGNITUDE_SURFACE + r")\s+(?P<noun>" + _WORD_SURFACE + r")",
    re.UNICODE | re.IGNORECASE,
)

# A range can reach the inventory as one token («дві-три») or as a split
# sequence («дві -три»). Restrict the right endpoint to another recognized
# numeral; a glued numeral+noun token such as «17-ділянок» is not a range.
_RANGE_TAIL_RE = re.compile(
    r"\s*[-–]\s*(?P<last>(?:" + _NUMERAL_SURFACE + r")|\d+(?:[.,]\d+)?)\s+"
    r"(?P<noun>" + _WORD_SURFACE + r")",
    re.UNICODE | re.IGNORECASE,
)

# Preserve a nearby curated case trigger in the extracted span. The numeral
# gate owns case semantics; this only prevents truncation from erasing the
# essential «близько» in «близько трьох годин».
_LEADING_TRIGGER_RE = re.compile(
    r"(?P<trigger>більше\s+ніж|менше\s+ніж|більш\s+ніж|менш\s+ніж|"
    r"близько|коло|до|від|завдяки|понад|під|з|із|зі)\s+$",
    re.UNICODE | re.IGNORECASE,
)

# Mixed-fraction continuation right after a cardinal: "<numeral> з половиною|
# чвертю|третиною <noun>" (spelled-out decimal, e.g. «два з половиною рази»).
# Captured so the raw_span reaches the real governed noun instead of stopping
# at the preposition «з» (which otherwise made the gate report no-noun-found).
_FRACTION_TAIL_RE = re.compile(
    r"\s+(?:з|із|зі)\s+(?:половиною|чвертю|третиною)\s+(?P<noun>" + _WORD_SURFACE + r")",
    re.UNICODE | re.IGNORECASE,
)

# DATE construction after a number: "<number> [<ordinal-day-word>] <genitive-
# month>" (23 квітня, двадцять третє квітня). Captured so the raw_span reaches
# the genitive month instead of mis-slicing the date into a bogus cardinal
# span. The optional intervening word must END in е/є (a neuter ordinal like
# «третє»/«двадцяте»/«перше») so real cardinals («двадцять хвилин лютого»)
# are NOT mis-captured as dates.
_MONTHS_GEN = (
    "січня|лютого|березня|квітня|травня|червня|"
    "липня|серпня|вересня|жовтня|листопада|грудня"
)
_DATE_TAIL_RE = re.compile(
    r"\s+(?:[А-ЯҐЄІЇа-яґєіїʼ'’-]+[еє]\s+)?(?P<month>" + _MONTHS_GEN + r")\b",
    re.UNICODE | re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?…]")


def _vesum_noun_words(text: str) -> list[re.Match[str]]:
    """The word tokens in *text* which VESUM parses as nouns.

    The inventory normally stays regex-only, but a magnitude/range tail can
    put another numeral where its ``_WORD_SURFACE`` placeholder expects the
    governed noun. Use one batched, noun-filtered VESUM lookup to distinguish
    that placeholder from the actual head noun deterministically.
    """
    words = list(_WORD_RE.finditer(text))
    if not words:
        return []
    forms = sorted({word.group(0) for word in words})
    matches = verify_words(forms, pos_filter="noun", db_path=paths.vesum_db())
    return [word for word in words if matches.get(word.group(0))]


def _extend_tail_to_head_noun(tail: str, match: re.Match[str]) -> tuple[str, int]:
    """Return the genuine governed noun and end offset for a matched tail.

    ``_MAGNITUDE_TAIL_RE`` and ``_RANGE_TAIL_RE`` must initially capture a
    word surface to recognize their shape. That surface may be a following
    cardinal (``тисяч п'ять``), or a magnitude that itself still needs a
    complement (``дві-три тисячі``). In either case, continue *after* the
    regex match to the first VESUM-confirmed noun in the same sentence. A
    genuine non-magnitude noun already captured by the tail remains the head,
    preventing a normal span such as ``двадцять тисяч гривень у банку`` from
    swallowing ``банку``.
    """
    following = match.group("noun")
    tail_end = match.end()
    sentence_end = _SENTENCE_END_RE.search(tail)
    sentence_tail = tail[: sentence_end.start()] if sentence_end else tail
    noun_words = _vesum_noun_words(sentence_tail)
    captured_start = match.start("noun")
    captured_is_noun = any(word.start() == captured_start for word in noun_words)
    captured_is_magnitude = re.fullmatch(
        _MAGNITUDE_SURFACE, following, re.IGNORECASE
    ) is not None
    if captured_is_noun and not captured_is_magnitude:
        return following, tail_end

    head = next((word for word in noun_words if word.start() >= tail_end), None)
    if head is None:
        return following, tail_end
    return head.group(0), head.end()


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# ---------------------------------------------------------------------------
# Lemmatization (VESUM reverse-lookup)
# ---------------------------------------------------------------------------
def tokenize(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall(nfc(text)) if len(t) > 1]


def lemmatize(text: str) -> dict[str, set[str]]:
    """Return {surface_lower -> {lemma, ...}} for every content-word token,
    via a single batched VESUM reverse-lookup. Non-content POS and unknown
    tokens are dropped.
    """
    surfaces = sorted({t.lower() for t in tokenize(text)})
    if not surfaces:
        return {}
    matches = verify_words(surfaces, db_path=paths.vesum_db())
    out: dict[str, set[str]] = {}
    for surface, rows in matches.items():
        lemmas = {r["lemma"] for r in rows if r["pos"] in _CONTENT_POS}
        if lemmas:
            out[surface] = lemmas
    return out


def anchor_lemmas(text: str) -> set[str]:
    """Flat set of all content lemmas occurring in the anchor."""
    lemmas: set[str] = set()
    for lset in lemmatize(text).values():
        lemmas |= lset
    return lemmas


# ---------------------------------------------------------------------------
# atlas {lemma -> payload}: ONE scan, filtered to the needed lemmas (§R C1 —
# no full-table-scan per lemma)
# ---------------------------------------------------------------------------
def _cefr_level(payload: dict) -> str | None:
    enr = payload.get("enrichment", {})
    if isinstance(enr, dict):
        cefr = enr.get("cefr")
        if isinstance(cefr, dict) and cefr.get("level"):
            return cefr["level"]
        if isinstance(cefr, str):
            m = re.search(r"\b([ABC][12])\b", cefr)
            if m:
                return m.group(1)
    top = payload.get("cefr")
    if isinstance(top, str):
        m = re.search(r"\b([ABC][12])\b", top)
        if m:
            return m.group(1)
    return None


def _synonyms(payload: dict) -> list[str]:
    secs = payload.get("sections", {})
    if isinstance(secs, dict):
        syn = secs.get("synonyms")
        if isinstance(syn, dict) and isinstance(syn.get("items"), list):
            return [s for s in syn["items"] if isinstance(s, str)]
    return []


def _heritage(payload: dict) -> str | None:
    enr = payload.get("enrichment", {})
    if isinstance(enr, dict):
        h = enr.get("heritage")
        if isinstance(h, dict):
            return h.get("classification") or h.get("status")
    h = payload.get("heritage_status")
    if isinstance(h, dict):
        return h.get("classification") or h.get("status")
    if isinstance(h, str):
        return h
    return None


def build_atlas_lookup(needed_lemmas: set[str], db_path=None) -> dict[str, dict]:
    """{lemma_lower -> compact atlas record} built in ONE table scan, keeping
    only rows whose lemma is in `needed_lemmas`. Never scans per-lemma.
    """
    db = str(db_path or paths.atlas_db())
    needed_lower = {lemma.lower() for lemma in needed_lemmas}
    if not needed_lower:
        return {}
    # Read-only URI open. The bundle atlas.db ships in WAL mode and root-owned
    # 0644 on deploy hosts; WAL needs -wal/-shm creation even for SELECTs, so a
    # plain connect dies with "attempt to write a readonly database" for the
    # unprivileged service user. mode=ro&immutable=1 never creates or locks
    # anything (bug from engine recon #2528; hit live on the pilot host 2026-07-13).
    conn = sqlite3.connect(f"file:{quote(db)}?mode=ro&immutable=1", uri=True)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM article_payloads WHERE is_public_route=1"
        )
        lookup: dict[str, dict] = {}
        import json

        for (payload_json,) in rows:
            try:
                payload = json.loads(payload_json)
            except (ValueError, TypeError):
                continue
            lemma = payload.get("lemma")
            if not isinstance(lemma, str):
                continue
            key = lemma.lower()
            if key in needed_lower and key not in lookup:
                lookup[key] = {
                    "lemma": lemma,
                    "pos": payload.get("pos"),
                    "cefr": _cefr_level(payload),
                    "synonyms": _synonyms(payload),
                    "heritage": _heritage(payload),
                }
        return lookup
    finally:
        conn.close()


def _activity_content_strings(act: dict) -> list[str]:
    """The learner-visible text fields a lexical gate inspects, per type — the
    same surfaces `pipeline._gate_*` tokenize (statements, cloze text + options,
    match-up left/right)."""
    out: list[str] = []
    for it in act.get("items", []) or []:
        if isinstance(it, dict) and isinstance(it.get("statement"), str):
            out.append(it["statement"])
    if isinstance(act.get("text"), str):
        out.append(act["text"])
    for b in act.get("blanks", []) or []:
        if isinstance(b, dict):
            out.extend(o for o in (b.get("options") or []) if isinstance(o, str))
    for p in act.get("pairs", []) or []:
        if isinstance(p, dict):
            out.extend(p[s] for s in ("left", "right") if isinstance(p.get(s), str))
    return out


def augmented_atlas_lookup(
    anchor_body: str,
    raw_activities: list[dict],
    base_lookup: dict[str, dict],
    *,
    atlas_db=None,
) -> dict[str, dict]:
    """Extend the anchor's atlas lookup to also cover the content lemmas the
    model INTRODUCED (tokens not present verbatim in the anchor), so a
    russianism the generator invents — not only one quoted from the anchor — is
    also checked against atlas heritage (Sol defect 5: the lexical gate must see
    the grounding lookup for generated language too, not just anchor language).

    One extra atlas scan for the union of introduced lemmas; anchor entries win
    on a key collision.
    """
    anchor_lower = nfc(anchor_body).lower()
    introduced: set[str] = set()
    for act in raw_activities:
        for text in _activity_content_strings(act):
            for tok in tokenize(text):
                low = tok.lower()
                if low not in anchor_lower:
                    introduced.add(low)
    if not introduced:
        return dict(base_lookup)
    matches = verify_words(sorted(introduced), db_path=paths.vesum_db())
    lemmas = {r["lemma"].lower() for rows in matches.values() for r in rows}
    extra = build_atlas_lookup(lemmas, db_path=atlas_db) if lemmas else {}
    return {**extra, **base_lookup}  # anchor entries win


# ---------------------------------------------------------------------------
# Numeral inventory
# ---------------------------------------------------------------------------
def extract_numeral_inventory(text: str) -> list[dict]:
    """Every numeral (digit or spelled-out) + its governed-noun candidate.

    Returns [{raw_span, char_offset, numeral, following_noun}] — feeds both
    the grounding pack and the numeral gate (which does the verification).
    """
    body = nfc(text)
    candidates: list[tuple[int, int, dict]] = []
    seen: set[tuple[int, str]] = set()
    for rx in (_DIGIT_NUM_RE, _NUMERAL_WORD_RE):
        for m in rx.finditer(body):
            start = m.start()
            numeral = m.group(0)
            tail = body[m.end():]
            frac_match = _FRACTION_TAIL_RE.match(tail)
            date_match = _DATE_TAIL_RE.match(tail)
            magnitude_match = _MAGNITUDE_TAIL_RE.match(tail)
            range_match = _RANGE_TAIL_RE.match(tail)
            if date_match:
                # Extend across "[<ordinal-day>] <genitive-month>" so the gate
                # sees the whole date and recognizes it (date-not-cardinal),
                # instead of "двадцять третє" -> a spurious cardinal span.
                following = date_match.group("month")
                tail_end = date_match.end()
            elif frac_match:
                # Extend the span across "з половиною/чвертю/третиною <noun>"
                # so the gate sees the whole mixed fraction, not just "два з".
                following = frac_match.group("noun")
                tail_end = frac_match.end()
            elif magnitude_match:
                # The outer numeral classifies the magnitude word, while the
                # magnitude word governs its complement. The first surface
                # after it can be another numeral, so extend to the genuine
                # VESUM noun rather than truncating a compound cardinal.
                following, tail_end = _extend_tail_to_head_noun(tail, magnitude_match)
            elif range_match:
                # Range agreement belongs to its last endpoint. Preserve the
                # range and its real head noun together even when punctuation
                # tokenizes it as «дві -три» or its next word is a magnitude.
                following, tail_end = _extend_tail_to_head_noun(tail, range_match)
            else:
                noun_match = _WORD_RE.search(tail)
                following = noun_match.group(0) if noun_match else None
                tail_end = noun_match.end() if noun_match else 0

            trigger_match = _LEADING_TRIGGER_RE.search(body[:start])
            span_start = trigger_match.start() if trigger_match else start
            span_end = m.end() + tail_end
            raw_span = body[span_start:m.end()] + tail[:tail_end]
            key = (start, numeral)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                (
                    start,
                    span_end,
                    {
                    "raw_span": raw_span,
                    "char_offset": start,
                    "numeral": numeral,
                    "following_noun": following,
                    },
                )
            )

    # Nested magnitudes and ranges have an inner numeral-looking token. Keep
    # only the earliest enclosing candidate, so the gate receives one complete
    # unit instead of a valid span plus a false-positive fragment.
    inventory: list[dict] = []
    covered_until = -1
    for start, end, item in sorted(candidates, key=lambda candidate: (candidate[0], -candidate[1])):
        if start < covered_until:
            continue
        inventory.append(item)
        covered_until = end
    inventory.sort(key=lambda d: d["char_offset"])
    return inventory


# ---------------------------------------------------------------------------
# Grounding pack
# ---------------------------------------------------------------------------
_KIT_VERSION = "kit_enrichment_v1"
_KIT_EMPTY = "empty"
_KIT_AVAILABLE = "available"


def _verified_synonym_banks(lookup: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic, disjoint Atlas+VESUM synonym banks.

    Atlas is the verified source for the semantic relation; VESUM confirms the
    emitted synonym is an attested Ukrainian form.  A synonym may belong to at
    most one anchor lemma, and anchor lemmas themselves never spill into a
    synonym bank.  This is deliberately a compact closed substrate, never a
    lookup fallback over the Atlas lexicon.  If VESUM rejects a lexical form,
    it is intentionally omitted from both the bank and any non-kit fallback text;
    verified re-introduction is not a behavior this subsystem supports.
    """
    anchor_lemma_keys = set(lookup)
    assigned: set[str] = set()
    banks: list[dict[str, Any]] = []
    for key in sorted(lookup):
        record = lookup[key]
        raw = record.get("synonyms") or []
        candidates = sorted(
            {
                synonym.strip()
                for synonym in raw
                if isinstance(synonym, str)
                and re.fullmatch(_WORD_SURFACE, synonym.strip()) is not None
            },
            key=str.casefold,
        )
        attested = verify_words(candidates, db_path=paths.vesum_db()) if candidates else {}
        synonyms = [
            synonym
            for synonym in candidates
            if attested.get(synonym)
            and synonym.casefold() not in anchor_lemma_keys
            and synonym.casefold() not in assigned
        ]
        assigned.update(synonym.casefold() for synonym in synonyms)
        banks.append(
            {
                "anchor_lemma": record["lemma"],
                "synonyms": synonyms,
                "status": _KIT_AVAILABLE if synonyms else _KIT_EMPTY,
            }
        )
    return banks


def _focus_citation_forms(
    anchor_body: str,
    anchor_lemma_set: set[str],
    focus: str | None,
) -> dict[str, Any]:
    """Return VESUM citation forms for focus terms actually anchored in text."""
    if not isinstance(focus, str) or not focus.strip():
        return {"status": "not-requested", "forms": []}
    focus_lemmas = anchor_lemmas(focus) & anchor_lemma_set
    if not focus_lemmas:
        return {"status": _KIT_EMPTY, "forms": []}

    # The lemma comes from a VESUM reverse lookup of literal anchor forms, so
    # it is a verified citation form rather than a model-normalized spelling.
    forms = sorted(
        {
            lemma
            for lemmas in lemmatize(anchor_body).values()
            for lemma in lemmas
            if lemma in focus_lemmas
        },
        key=str.casefold,
    )
    return {
        "status": _KIT_AVAILABLE if forms else _KIT_EMPTY,
        "forms": forms,
    }


def _numeral_tuples(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project anchor numeral inventory into source-attested MOAT tuples.

    ``value`` and ``trigger`` remain literal anchor strings.  Case/gender are
    represented as VESUM possibilities (not guessed single values), which lets
    Slice 3's Python oracle make the final decision without a hidden model
    inference step.
    """
    tuples: list[dict[str, Any]] = []
    for item in inventory:
        noun = item.get("following_noun")
        numeral = item.get("numeral")
        raw_span = item.get("raw_span")
        if not all(isinstance(value, str) and value for value in (noun, numeral, raw_span)):
            continue
        noun_rows = verify_words([noun], pos_filter="noun", db_path=paths.vesum_db()).get(noun, [])
        if not noun_rows:
            continue
        trigger, _, _ = raw_span.partition(numeral)
        cases: set[str] = set()
        genders: set[str] = set()
        lemmas: set[str] = set()
        for row in noun_rows:
            lemmas.add(row["lemma"])
            parts = row["tags"].split(":")
            cases.update(
                code
                for code in ("v_naz", "v_rod", "v_dav", "v_zna", "v_oru", "v_mis", "v_kly")
                if code in parts
            )
            genders.update(gender for gender in ("m", "f", "n") if gender in parts)
        tuples.append(
            {
                "value": numeral,
                "case": sorted(cases),
                "gender": sorted(genders),
                "trigger": trigger.strip() or None,
                "noun_lemma": sorted(lemmas, key=str.casefold),
                "witness_span": raw_span,
            }
        )
    return tuples


def build_enrichment_kit(
    anchor_body: str,
    *,
    lemmas: set[str],
    atlas_lookup: Mapping[str, Mapping[str, Any]],
    numeral_inventory: list[dict[str, Any]],
    focus: str | None = None,
) -> dict[str, Any]:
    """Build the deterministic Slice-2 feed-forward substrate.

    Every facet declares ``available`` or ``empty``.  An empty kit is an
    explicit state, not permission to substitute a broader lexicon.  Aspect
    pairs/stress remain explicitly absent until a verified stress source lands
    (#5368); no stress marks or aspect partners are fabricated here.
    """
    synonym_banks = _verified_synonym_banks(atlas_lookup)
    cefr_tags = [
        {"lemma": record["lemma"], "cefr": record["cefr"]}
        for _key, record in sorted(atlas_lookup.items())
        if isinstance(record.get("cefr"), str) and record["cefr"]
    ]
    focus_forms = _focus_citation_forms(anchor_body, lemmas, focus)
    numeral_tuples = _numeral_tuples(numeral_inventory)
    facets = {
        "synonym_banks": _KIT_AVAILABLE
        if any(bank["synonyms"] for bank in synonym_banks)
        else _KIT_EMPTY,
        "cefr_tags": _KIT_AVAILABLE if cefr_tags else _KIT_EMPTY,
        # #5368: no verified stress tool/source exists yet.  Do not invent
        # aspect partners or stress marks from morphology heuristics.
        "aspect_stress": "absent",
        "focus_citation_forms": focus_forms["status"],
        "numeral_tuples": _KIT_AVAILABLE if numeral_tuples else _KIT_EMPTY,
    }
    usable = any(
        status == _KIT_AVAILABLE
        for name, status in facets.items()
        if name != "focus_citation_forms" or status != "not-requested"
    )
    return {
        "version": _KIT_VERSION,
        "status": _KIT_AVAILABLE if usable else _KIT_EMPTY,
        "anchor_lemmas": sorted(lemmas, key=str.casefold),
        "facets": facets,
        "synonym_banks": synonym_banks,
        "cefr_tags": cefr_tags,
        "aspect_stress": {
            "status": "absent",
            "pairs": [],
            "stress": [],
            "reason": "No verified aspect-pair/stress source is available (#5368).",
        },
        "focus_citation_forms": focus_forms,
        "numeral_tuples": numeral_tuples,
    }


def kit_is_empty(kit: Mapping[str, Any] | None) -> bool:
    """Return whether a Slice-2 kit is explicitly empty.

    This is the later-gate API: absent, malformed, or disabled kit material is
    *not* treated as a populated kit.  It deliberately has no lexicon fallback.
    """
    return not isinstance(kit, Mapping) or kit.get("status") != _KIT_AVAILABLE


def _render_grounding_text(
    lookup: Mapping[str, Mapping[str, Any]],
    inventory: list[dict[str, Any]],
    *,
    level: str,
    max_lemmas: int,
    include_legacy_synonyms: bool,
) -> str:
    """Render the legacy grounding fields, optionally without synonym payloads.

    A populated enrichment kit is the single source for verified synonym
    banks.  Its non-kit prefix keeps the lemma, CEFR, and numeral fields, but
    omits the older Atlas synonym list so that the same relation has no second
    injection route.  The flag-off caller always passes ``True`` to preserve
    the historical bytes exactly.
    """
    lex_lines: list[str] = []
    for lemma in sorted(lookup)[:max_lemmas]:
        rec = lookup[lemma]
        parts = [rec["lemma"]]
        if rec.get("cefr"):
            parts.append(rec["cefr"])
        syns = rec.get("synonyms") or []
        if include_legacy_synonyms and syns:
            parts.append("синоніми: " + ", ".join(syns[:4]))
        lex_lines.append(" — ".join(parts))

    num_lines = [
        f"{d['numeral']} → {d['following_noun']}" if d["following_noun"] else d["numeral"]
        for d in inventory
    ]

    text_blocks = [f"Рівень: {level}."]
    if lex_lines:
        heading = (
            "Перевірена лексика опори (лема — рівень CEFR — синоніми):"
            if include_legacy_synonyms
            else "Перевірена лексика опори (лема — рівень CEFR):"
        )
        text_blocks.append(heading + "\n" + "\n".join(lex_lines))
    if num_lines:
        text_blocks.append(
            "Числа в опорі (для звірки керування числівника):\n" + "\n".join(num_lines)
        )
    return "\n\n".join(text_blocks)


def build_grounding_pack(
    anchor_body: str,
    level: str = "B1",
    *,
    atlas_db=None,
    max_lemmas: int = 40,
    focus: str | None = None,
) -> dict:
    """Assemble the compact grounding pack (verified lexicon + numeral
    inventory). Returns {text, lemmas, atlas_lookup, numeral_inventory}. Only
    lemmas actually occurring in this anchor are included (never the whole
    atlas); the rendered text stays well under ~1500 tokens.
    """
    lemmas = anchor_lemmas(anchor_body)
    lookup = build_atlas_lookup(lemmas, db_path=atlas_db)
    inventory = extract_numeral_inventory(anchor_body)

    legacy_pack = {
        "text": _render_grounding_text(
            lookup,
            inventory,
            level=level,
            max_lemmas=max_lemmas,
            include_legacy_synonyms=True,
        ),
        "lemmas": lemmas,
        "atlas_lookup": lookup,
        "numeral_inventory": inventory,
    }
    # The default path must remain byte-identical: do not add keys, query
    # additional sources, or alter rendered context unless the Slice-2 flag is
    # explicitly enabled.
    if not flags.kit_enrichment_v1_enabled():
        return legacy_pack

    kit = build_enrichment_kit(
        anchor_body,
        lemmas=lemmas,
        atlas_lookup=lookup,
        numeral_inventory=inventory,
        focus=focus,
    )
    kit_text = json.dumps(kit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    non_kit_text = (
        legacy_pack["text"]
        if kit_is_empty(kit)
        else _render_grounding_text(
            lookup,
            inventory,
            level=level,
            max_lemmas=max_lemmas,
            include_legacy_synonyms=False,
        )
    )
    return {
        **legacy_pack,
        "text": non_kit_text + "\n\n=== KIT ENRICHMENT V1 ===\n" + kit_text,
        "kit": kit,
    }
