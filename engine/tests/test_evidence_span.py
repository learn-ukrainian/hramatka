"""Tests for gates/evidence_span.py — §6a span-REPAIR.

The three required cases: good offset (exact), bad/absent offsets but the
quote is present (repaired), and quote genuinely absent (fail for extractive).
"""

from __future__ import annotations

from hramatka.engine.gates import evidence_span as ES

ANCHOR = "Під час читання активізуються одразу 17 ділянок головного мозку."


def test_exact_quote_recomputes_offsets():
    v = ES.check_evidence("активізуються одразу 17 ділянок", ANCHOR)
    assert v["status"] == "pass"
    assert v["kind"] == "literal"
    # offsets are recomputed, and actually point at the quote
    assert ANCHOR[v["char_start"]:v["char_end"]] == "активізуються одразу 17 ділянок"


def test_model_offsets_ignored_quote_relocated():
    # Even if a caller had bogus offsets, the gate ignores them and locates
    # the quote by content — here via whitespace-collapsed matching.
    v = ES.check_evidence("активізуються   одразу  17   ділянок", ANCHOR)
    assert v["status"] == "pass"
    assert ANCHOR[v["char_start"]:v["char_end"]].startswith("активізуються")


def test_absent_quote_fails_for_extractive():
    v = ES.check_evidence("щоденна ранкова пробіжка корисна", ANCHOR)
    assert v["status"] == "fail"
    assert v["kind"] == "absent"


def test_absent_quote_warns_for_inferential():
    v = ES.check_evidence("щоденна ранкова пробіжка корисна", ANCHOR, extractive=False)
    assert v["status"] == "warn"


def test_empty_quote_fails():
    assert ES.check_evidence("", ANCHOR)["status"] == "fail"
    assert ES.check_evidence("   ", ANCHOR)["status"] == "fail"


def test_locate_quote_returns_none_when_absent():
    assert ES.locate_quote("немає такого", ANCHOR) is None
