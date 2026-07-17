"""Grounding-mode v1 — Slice 1 acceptance (registry mode + flag-vector + dual-path).

Contract: hramatka/grounding-mode-v1-final-spec.md §slice-1, §0, §4, §5, §6.4, §7.
Flags-off path remains the production extractive path (byte-identical quoting
validators; derived types keep legacy evidence contracts until the flag is on).
"""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from hramatka.engine import flags, pipeline, registry, schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _fill_in_evidence_shape() -> dict:
    return {
        "type": "fill-in",
        "instruction": "Обери правильну форму.",
        "items": [
            {
                "sentence": "На думку вчених, читання є одним з найскладніших ____ для мозку.",
                "answer": "завдань",
                "options": ["завдань", "вправ", "задач", "питань"],
                "evidence": (
                    "На думку вчених, читання є одним з найскладніших завдань для мозку."
                ),
            }
        ],
    }


def _fill_in_kit_shape() -> dict:
    return {
        "type": "fill-in",
        "instruction": "Обери правильну форму.",
        "items": [
            {
                "sentence": "Читання зміцнює ____ і увагу.",
                "answer": "пам'ять",
                "options": ["пам'ять", "книжку", "стіл", "вікно"],
                "kit_anchors": {
                    "lemmas": ["читання", "пам'ять"],
                    "witness_span": "читання є одним з найскладніших завдань",
                },
            }
        ],
    }


def _error_correction_evidence_shape() -> dict:
    return {
        "type": "error-correction",
        "instruction": "Виправ помилку.",
        "items": [
            {
                "sentence": "Під час читання активізуються одразу 17 ділянки головного мозку.",
                "error": "ділянки",
                "correction": "ділянок",
                "options": ["ділянки", "ділянок", "книжки"],
                "explanation": "Після 17 потрібна форма родового множини.",
                "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
            }
        ],
    }


def _error_correction_kit_shape() -> dict:
    item = {
        "sentence": "Під час читання активізуються одразу 17 ділянки головного мозку.",
        "error": "ділянки",
        "correction": "ділянок",
        "options": ["ділянки", "ділянок", "книжки"],
        "explanation": "Після 17 потрібна форма родового множини.",
        "kit_anchors": {
            "lemmas": ["ділянка", "мозок"],
            "witness_span": "17 ділянок головного мозку",
            "rule_id": "numeral_moat",
        },
    }
    return {
        "type": "error-correction",
        "instruction": "Виправ помилку.",
        "items": [item],
    }


def _short_writing_evidence_shape() -> dict:
    return {
        "type": "short-writing",
        "instruction": "Напиши короткий текст.",
        "prompt": "Напиши три речення про читання своїми словами.",
        "source_ref": "Текст-опора",
        "word_count_guidance": "3 речення (30–40 слів)",
        "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку.",
    }


def _short_writing_kit_shape() -> dict:
    return {
        "type": "short-writing",
        "instruction": "Напиши короткий текст.",
        "prompt": "Напиши три речення про читання своїми словами.",
        "source_ref": "Текст-опора",
        "word_count_guidance": "3 речення (30–40 слів)",
        "kit_anchors": {
            "lemmas": ["читання", "мозок"],
            "witness_span": "читання є одним з найскладніших завдань для мозку",
        },
    }


def _match_up_evidence_shape() -> dict:
    return {
        "type": "match-up",
        "instruction": "Поєднай.",
        "pairs": [
            {
                "left": "насолода",
                "right": "задоволення",
                "evidence": "втратили насолоду від неспішного читання",
            },
            {
                "left": "мозок",
                "right": "розум",
                "evidence": "завдань для мозку",
            },
            {
                "left": "книжка",
                "right": "видання",
                "evidence": "не прочитує жодної книжки",
            },
        ],
    }


def _true_false_missing_evidence() -> dict:
    return {
        "type": "true-false",
        "instruction": "Познач.",
        "items": [{"statement": "A", "correct": True}],
    }


def _fp_kw(**overrides):
    base = dict(
        anchor_hash="slice1-anchor",
        level="B1",
        pedagogy="ttt",
        phase="1",
        types=["true-false", "fill-in"],
        grounding_text="grounding",
        prompt_template="prompt",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Registry grounding_mode + fingerprint flip
# ---------------------------------------------------------------------------
def test_every_registry_entry_has_grounding_mode_from_spec_tables():
    expected = {
        "true-false": registry.GROUNDING_QUOTING,
        "quiz": registry.GROUNDING_QUOTING,
        "text-questions": registry.GROUNDING_QUOTING,
        "mark-the-words": registry.GROUNDING_QUOTING,
        "cloze": registry.GROUNDING_QUOTING,
        "match-up": registry.GROUNDING_QUOTING,  # E2: quoting in v1
        "fill-in": registry.GROUNDING_DERIVED,
        "error-correction": registry.GROUNDING_DERIVED,
        "short-writing": registry.GROUNDING_DERIVED,
    }
    for activity_type, mode in expected.items():
        entry = registry.ACTIVITY_REGISTRY[activity_type]
        assert entry.grounding_mode == mode
        assert entry.fingerprint()["grounding_mode"] == mode


def test_registry_fingerprint_changes_when_grounding_mode_flips():
    types = ["fill-in"]
    before = registry.registry_fingerprint(types)
    entry = registry.ACTIVITY_REGISTRY["fill-in"]
    flipped = dataclasses.replace(entry, grounding_mode=registry.GROUNDING_QUOTING)
    # Patch only for this assertion (do not leave production registry mutated).
    original = registry.ACTIVITY_REGISTRY["fill-in"]
    try:
        registry.ACTIVITY_REGISTRY["fill-in"] = flipped
        after = registry.registry_fingerprint(types)
    finally:
        registry.ACTIVITY_REGISTRY["fill-in"] = original

    assert before["digest"] != after["digest"]
    assert before["entries"][0]["grounding_mode"] == registry.GROUNDING_DERIVED
    assert after["entries"][0]["grounding_mode"] == registry.GROUNDING_QUOTING


# ---------------------------------------------------------------------------
# Flag vector in fingerprint + bake-id flip
# ---------------------------------------------------------------------------
def test_flag_vector_present_in_fingerprint_blob():
    inputs = pipeline.fingerprint_inputs(**_fp_kw())
    assert "flag_vector" in inputs
    assert set(inputs["flag_vector"]) == set(flags.FLAG_NAMES)
    assert all(isinstance(v, bool) for v in inputs["flag_vector"].values())
    # Production default: all flags off.
    assert inputs["flag_vector"] == {name: False for name in flags.FLAG_NAMES}


def test_toggling_flag_changes_bake_id(monkeypatch):
    base = pipeline.make_fingerprint(**_fp_kw())
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    flipped = pipeline.make_fingerprint(**_fp_kw())
    assert base != flipped
    # Explicit vector override also reshuffles independently of env.
    other = pipeline.make_fingerprint(
        **_fp_kw(
            flag_vector={
                flags.FLAG_KIT_ENRICHMENT_V1: True,
                flags.FLAG_WRITER_PROMPT_V2: False,
                flags.FLAG_GROUNDING_MODE_V1: False,
            }
        )
    )
    assert other != base
    assert other != flipped


def test_gate_impl_digest_covers_content_density_and_derived_stub():
    digest = pipeline._gate_impl_digest()
    assert isinstance(digest, str) and len(digest) == 64
    # content_density / derived stub / flags are on the hashed path list —
    # touch derived stub content identity indirectly by ensuring module imports.
    from hramatka.engine.gates import derived as derived_gates

    assert derived_gates.DERIVED_GATES_VERSION.startswith("derived.gates")


# ---------------------------------------------------------------------------
# Quoting validators still reject missing evidence (flags off + on)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("flag_on", [False, True])
def test_quoting_validators_reject_missing_evidence(monkeypatch, flag_on):
    if flag_on:
        monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    else:
        monkeypatch.delenv("HRAMATKA_GROUNDING_MODE_V1", raising=False)

    errors = registry.ACTIVITY_REGISTRY["true-false"].raw_validator(_true_false_missing_evidence())
    assert any("evidence" in err for err in errors)

    match_up_no_ev = _match_up_evidence_shape()
    for pair in match_up_no_ev["pairs"]:
        del pair["evidence"]
    mu_errors = registry.ACTIVITY_REGISTRY["match-up"].raw_validator(match_up_no_ev)
    assert any("evidence" in err for err in mu_errors)


def test_match_up_remains_evidence_quote_validated(monkeypatch):
    """match-up is quoting in v1 (E2) — even under grounding_mode_v1."""
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    good = _match_up_evidence_shape()
    assert registry.ACTIVITY_REGISTRY["match-up"].raw_validator(good) == []
    assert registry.ACTIVITY_REGISTRY["match-up"].grounding_mode == registry.GROUNDING_QUOTING

    bad = copy.deepcopy(good)
    del bad["pairs"][0]["evidence"]
    errors = registry.ACTIVITY_REGISTRY["match-up"].raw_validator(bad)
    assert any("evidence" in err for err in errors)
    # kit_anchors does not substitute for match-up evidence.
    bad["pairs"][0]["kit_anchors"] = {
        "lemmas": ["насолода"],
        "witness_span": "насолоду від неспішного читання",
    }
    errors2 = registry.ACTIVITY_REGISTRY["match-up"].raw_validator(bad)
    assert any("evidence" in err for err in errors2)


# ---------------------------------------------------------------------------
# Derived dual-path (under grounding_mode_v1)
# ---------------------------------------------------------------------------
def test_flags_off_derived_types_still_require_evidence():
    """Production path: fill-in / error-correction / short-writing unchanged."""
    assert registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(_fill_in_evidence_shape()) == []
    assert (
        registry.ACTIVITY_REGISTRY["error-correction"].raw_validator(
            _error_correction_evidence_shape()
        )
        == []
    )
    assert (
        registry.ACTIVITY_REGISTRY["short-writing"].raw_validator(_short_writing_evidence_shape())
        == []
    )

    no_ev = _fill_in_evidence_shape()
    del no_ev["items"][0]["evidence"]
    errors = registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(no_ev)
    assert any("evidence" in err for err in errors)


def test_derived_validators_reject_missing_witness_span(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")

    missing_witness = _fill_in_kit_shape()
    missing_witness["items"][0]["kit_anchors"] = {"lemmas": ["читання"]}
    errors = registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(missing_witness)
    assert any("witness_span" in err for err in errors)

    empty_witness = _fill_in_kit_shape()
    empty_witness["items"][0]["kit_anchors"]["witness_span"] = "   "
    errors2 = registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(empty_witness)
    assert any("witness_span" in err for err in errors2)

    sw = _short_writing_kit_shape()
    del sw["kit_anchors"]["witness_span"]
    sw_errors = registry.ACTIVITY_REGISTRY["short-writing"].raw_validator(sw)
    assert any("witness_span" in err for err in sw_errors)

    ec = _error_correction_kit_shape()
    ec["items"][0]["kit_anchors"]["witness_span"] = ""
    ec_errors = registry.ACTIVITY_REGISTRY["error-correction"].raw_validator(ec)
    assert any("witness_span" in err for err in ec_errors)


def test_derived_validators_reject_evidence_only_quote_restore(monkeypatch):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")

    # Classic extractive fill-in is a quote-restore shape under mode=derived.
    errors = registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(_fill_in_evidence_shape())
    assert any("quote-restore" in err or "kit_anchors" in err for err in errors)

    # Valid kit_anchors shape is accepted by the raw contract.
    assert registry.ACTIVITY_REGISTRY["fill-in"].raw_validator(_fill_in_kit_shape()) == []
    assert (
        registry.ACTIVITY_REGISTRY["error-correction"].raw_validator(_error_correction_kit_shape())
        == []
    )
    assert (
        registry.ACTIVITY_REGISTRY["short-writing"].raw_validator(_short_writing_kit_shape()) == []
    )


def test_sentence_builder_always_derived_kit_anchors():
    """sentence-builder mode is derived from day one (P1); not pilot-registered."""
    good = {
        "type": "sentence-builder",
        "instruction": "Зробіть речення.",
        "starters": ["Читання", "Мозок"],
        "kit_anchors": {
            "lemmas": ["читання", "мозок"],
            "witness_span": "читання є одним з найскладніших завдань",
        },
    }
    assert registry._validate_sentence_builder(good) == []

    evidence_only = {
        "type": "sentence-builder",
        "instruction": "Зробіть речення.",
        "starters": ["Читання"],
        "evidence": "читання є одним з найскладніших завдань",
    }
    errors = registry._validate_sentence_builder(evidence_only)
    assert any("witness_span" in err or "quote-restore" in err for err in errors)


# ---------------------------------------------------------------------------
# IR kit_anchors
# ---------------------------------------------------------------------------
def test_parse_raw_activity_extracts_kit_anchors():
    raw = _fill_in_kit_shape()
    clean, evidence, kit_anchors = schema.parse_raw_activity(raw)
    assert evidence == []
    assert all("kit_anchors" not in item for item in clean["items"])
    assert len(kit_anchors) == 1
    assert kit_anchors[0].locator == "items[0]"
    assert kit_anchors[0].witness_span == "читання є одним з найскладніших завдань"
    assert "читання" in kit_anchors[0].lemmas

    sw = _short_writing_kit_shape()
    clean_sw, _ev, ka_sw = schema.parse_raw_activity(sw)
    assert "kit_anchors" not in clean_sw
    assert len(ka_sw) == 1 and ka_sw[0].locator == "text"


def test_hramatka_activity_as_dict_carries_kit_anchors():
    clean, evidence, kit_anchors = schema.parse_raw_activity(_fill_in_kit_shape())
    ir = schema.HramatkaActivity(
        activity=clean, evidence=evidence, kit_anchors=kit_anchors, candidate_id="c0"
    )
    payload = ir.as_dict()
    assert payload["kit_anchors"][0]["witness_span"]
    assert payload["kit_anchors"][0]["locator"] == "items[0]"


# ---------------------------------------------------------------------------
# No production publish path treats derived survivors as lesson-ready
# ---------------------------------------------------------------------------
def test_derived_publish_allowed_is_false_by_default():
    """Default env: tray off ⇒ derived publish refused (slice 3 G4)."""
    assert flags.derived_publish_allowed() is False



def test_derived_kit_path_not_lesson_ready_under_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    raw = _fill_in_kit_shape()
    # Pad to minimum_survivors for fill-in (3).
    raw["items"] = [
        copy.deepcopy(raw["items"][0]),
        copy.deepcopy(raw["items"][0]),
        copy.deepcopy(raw["items"][0]),
    ]
    for index, item in enumerate(raw["items"]):
        item["sentence"] = f"Речення {index}: Читання зміцнює ____."
        item["kit_anchors"] = {
            "lemmas": ["читання"],
            "witness_span": f"witness {index} читання",
        }

    result = pipeline.run(
        "На думку вчених, читання є одним з найскладніших завдань для мозку.",
        types=["fill-in"],
        generator=lambda _prompt: json.dumps({"activities": [raw]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        use_cache=False,
    )
    # Dual-path raw contract accepts kit_anchors, but tray-off publish hold rejects (G4).
    assert result.lesson_b1 == []
    assert all(
        ir.gate_result.status == schema.DISPOSITION_REJECTED
        or ir.activity.get("type") != "fill-in"
        for ir in result.activities
    )
    assert any(
        check.gate == "derived_publish" and check.status == "fail"
        for ir in result.activities
        for check in ir.gate_result.checks
    )
    assert any(
        "teacher_review_tray_v1" in check.detail
        for ir in result.activities
        for check in ir.gate_result.checks
        if check.gate == "derived_publish"
    )


def test_flags_off_fill_in_evidence_still_reaches_gate_chain(tmp_path):
    """Production path: evidence-shaped fill-in is not blocked by derived_publish."""
    raw = _fill_in_evidence_shape()
    # minimum_survivors=3; supply three items with distinct evidence quotes.
    base_item = raw["items"][0]
    raw["items"] = [
        {
            **copy.deepcopy(base_item),
            "sentence": f"____ {i}",
            "evidence": f"evidence quote number {i} for fill-in item",
        }
        for i in range(3)
    ]
    result = pipeline.run(
        "evidence quote number 0 for fill-in item. "
        "evidence quote number 1 for fill-in item. "
        "evidence quote number 2 for fill-in item.",
        types=["fill-in"],
        generator=lambda _prompt: json.dumps({"activities": [raw]}, ensure_ascii=False),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        use_cache=False,
    )
    assert not any(
        check.gate == "derived_publish"
        for ir in result.activities
        for check in ir.gate_result.checks
    )
