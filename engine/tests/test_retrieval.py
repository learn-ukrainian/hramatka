"""Tests for retrieval.py — lemmatize (VESUM reverse), atlas {lemma→payload}
built once, numeral inventory, grounding pack size. Read-only DB access.
"""

from __future__ import annotations

from hramatka.engine import retrieval as R
from hramatka.engine.fixtures import load_anchor
from hramatka.engine.gates.numeral import check_numeral_government

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


def test_bake_off_numeral_spans_reach_the_gate_intact():
    """#52 full bake strings: extraction and gate must agree end-to-end."""
    cases = (
        ("Ціна першої квартири була двадцять тисяч гривень.", "двадцять тисяч гривень", "pass"),
        (
            "Оренда коштувала близько двадцяти тисяч гривень на місяць.",
            "близько двадцяти тисяч гривень",
            "pass",
        ),
        ("зачекайте ще дві-три хвилини", "дві-три хвилини", "pass"),
        ("Ми обоє працюємо з дому.", "обоє працюємо", "pass"),
        ("близько трьох годин", "близько трьох годин", "pass"),
        ("Близько три годин тривала дорога.", "Близько три годин", "fail"),
    )
    for text, expected_span, expected_status in cases:
        inventory = R.extract_numeral_inventory(text)
        span = next(item for item in inventory if item["raw_span"] == expected_span)
        assert check_numeral_government(span["raw_span"])["status"] == expected_status


def test_compound_cardinal_spans_reach_the_real_head_noun_and_gate():
    """#58: no numeral/magnitude fragment may replace the governed noun."""
    cases = (
        ("двадцять тисяч п'ять гривень", "pass"),
        ("двадцять тисяч п'ять гривні", "fail"),
        ("дві-три тисячі хвилин", "pass"),
        ("дві-три тисячі хвилини", "fail"),
        ("одна тисяча двісті тридцять чотири книги", "pass"),
        ("одна тисяча двісті тридцять чотири книг", "fail"),
    )
    for phrase, expected_status in cases:
        inventory = R.extract_numeral_inventory(phrase)
        assert [item["raw_span"] for item in inventory] == [phrase]
        assert inventory[0]["following_noun"] == phrase.split()[-1]
        assert check_numeral_government(inventory[0]["raw_span"])["status"] == expected_status


def test_compound_cardinal_never_borrows_a_noun_from_the_next_sentence():
    # A dangling compound must not reach past its sentence terminator to make
    # itself look grammatical using the next sentence's noun.
    text = "двадцять тисяч п'ять. гривень немає."
    inventory = R.extract_numeral_inventory(text)
    assert [item["raw_span"] for item in inventory] == ["двадцять тисяч п'ять"]
    assert check_numeral_government(inventory[0]["raw_span"])["status"] == "fail"


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
