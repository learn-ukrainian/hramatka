"""Tests for gates/vesum.py — §6b introduced-token validity.

All UA tokens VESUM-verified (#M-4). Anchor-verbatim tokens are trusted;
fabricated forms fail; the gate never calls the network.
"""

from __future__ import annotations

from engine.gates import vesum as VG

ANCHOR = "Під час читання активізуються одразу 17 ділянок головного мозку."


def test_anchor_verbatim_token_trusted():
    v = VG.check_tokens(["мозку", "читання"], ANCHOR)
    assert all(x["status"] == "pass" for x in v)
    assert all("anchor-verbatim" in x["detail"] for x in v)


def test_introduced_valid_tokens_pass():
    # серця / розуму are real VESUM forms, not in the anchor -> introduced+valid
    v = VG.check_tokens(["серця", "розуму"], ANCHOR)
    assert all(x["status"] == "pass" for x in v)
    assert VG.worst_status(v) == "pass"


def test_fabricated_token_fails():
    v = VG.check_tokens(["фейкословоxx"], ANCHOR)
    assert v[0]["status"] == "fail"
    assert VG.worst_status(v) == "fail"


def test_russianism_warns_when_atlas_flags_heritage():
    # a valid VESUM token whose atlas record flags a heritage/russianism ->
    # warn (teacher-confirm), never a hard fail in the MVP.
    atlas = {"розум": {"lemma": "розум", "heritage": "russianism"}}
    v = VG.check_tokens(["розуму"], ANCHOR, atlas_lookup=atlas)
    assert v[0]["status"] == "warn"
    assert VG.worst_status(v) == "warn"


def test_content_tokens_filters_short_and_nonword():
    toks = VG.content_tokens("Мозок — це орган, і 17 ділянок!")
    assert "Мозок" in toks and "орган" in toks and "ділянок" in toks
    assert "17" not in toks  # digits excluded
    assert "і" not in toks   # len<=1 excluded
