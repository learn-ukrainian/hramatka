"""Tests for retrieval.py — lemmatize (VESUM reverse), atlas {lemma→payload}
built once, numeral inventory, grounding pack size. Read-only DB access.
"""

from __future__ import annotations

from engine import retrieval as R
from engine.fixtures import load_anchor

ANCHOR = (
    "Третина українців за рік не прочитує жодної книжки, зате дві третини "
    "щодня знаходять час увімкнути телевізор. Активізуються 17 ділянок мозку."
)


def test_lemmatize_returns_vesum_lemmas():
    lem = R.lemmatize(ANCHOR)
    # книжки -> книжка, українців -> українець (VESUM reverse lookup)
    assert "книжка" in lem.get("книжки", set())
    assert "українець" in lem.get("українців", set())


def test_anchor_lemmas_is_flat_set():
    lemmas = R.anchor_lemmas(ANCHOR)
    assert "третина" in lemmas
    assert isinstance(lemmas, set)


def test_numeral_inventory_extraction():
    inv = R.extract_numeral_inventory(ANCHOR)
    spans = {d["raw_span"] for d in inv}
    assert "дві третини" in spans
    assert "17 ділянок" in spans
    # sorted by char offset
    offsets = [d["char_offset"] for d in inv]
    assert offsets == sorted(offsets)


def test_numeral_inventory_extends_across_mixed_fraction():
    # Regression: the raw_span for a spelled-out fraction must reach the real
    # governed noun ("рази"), not stop at the preposition "з" (which produced
    # "два з" -> no-noun-found in the gate).
    inv = R.extract_numeral_inventory("Ризик зменшився у два з половиною рази торік.")
    spans = {d["raw_span"] for d in inv}
    assert "два з половиною рази" in spans
    frac = next(d for d in inv if d["raw_span"] == "два з половиною рази")
    assert frac["following_noun"] == "рази"


def test_numeral_inventory_captures_date_span():
    # Digit date: the month is captured as the following noun.
    inv = R.extract_numeral_inventory("Свято відзначають 23 квітня щороку.")
    spans = {d["raw_span"] for d in inv}
    assert "23 квітня" in spans
    # Spelled-out date: the raw_span must reach the genitive month, not stop
    # at "двадцять третє" (which the gate would call a bare ordinal).
    inv2 = R.extract_numeral_inventory("Це сталося двадцять третє квітня минулого року.")
    assert "двадцять третє квітня" in {d["raw_span"] for d in inv2}


def test_numeral_inventory_does_not_over_slice_cardinal_before_month():
    # "двадцять хвилин лютого" is NOT a date (хвилин is not an ordinal-day
    # word) — the cardinal span must stay "двадцять хвилин", not swallow the month.
    inv = R.extract_numeral_inventory("Лишилось двадцять хвилин лютого місяця.")
    assert "двадцять хвилин" in {d["raw_span"] for d in inv}


def test_build_atlas_lookup_single_pass_needed_only():
    lookup = R.build_atlas_lookup({"книжка", "час"})
    # only requested lemmas returned, keyed lowercase
    assert set(lookup).issubset({"книжка", "час"})
    for rec in lookup.values():
        assert "lemma" in rec and "cefr" in rec and "synonyms" in rec


def test_build_atlas_lookup_empty_when_no_lemmas():
    assert R.build_atlas_lookup(set()) == {}


def test_grounding_pack_is_compact_and_structured():
    pack = R.build_grounding_pack(load_anchor(), "B1")
    assert "Рівень: B1." in pack["text"]
    assert "Числа в опорі" in pack["text"]
    # numeral inventory carries the anchor's real numerals
    spans = {d["raw_span"] for d in pack["numeral_inventory"]}
    assert "дві третини" in spans and "17 ділянок" in spans
    # comfortably under the ~1500-token budget (chars/4 heuristic)
    assert len(pack["text"]) // 4 < 1500
