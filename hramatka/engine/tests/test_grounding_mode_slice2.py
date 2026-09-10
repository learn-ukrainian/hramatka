"""Slice-2 kit feed-forward contract (grounding-mode v1 §8 / §11)."""

from __future__ import annotations

import hashlib
import json
import os

from hramatka.engine import pipeline, prompt_pack, registry, retrieval
from hramatka.engine.fixtures import load_anchor


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_kit_enrichment_off_preserves_legacy_grounding_pack(monkeypatch):
    monkeypatch.delenv("HRAMATKA_KIT_ENRICHMENT_V1", raising=False)

    pack = retrieval.build_grounding_pack(load_anchor(), "B1", focus="читання")

    assert set(pack) == {"text", "lemmas", "atlas_lookup", "numeral_inventory"}
    assert "KIT ENRICHMENT V1" not in pack["text"]
    if os.environ.get("HRAMATKA_TEST_DATA_DIR"):
        assert pack["text"].startswith("Рівень: B1.")
        assert "Перевірена лексика опори (лема — рівень CEFR — синоніми):" in pack["text"]
        assert "Числа в опорі (для звірки керування числівника):" in pack["text"]
        assert pack["lemmas"] and pack["atlas_lookup"] and pack["numeral_inventory"]
    else:
        assert hashlib.sha256(pack["text"].encode("utf-8")).hexdigest() == (
            "49a553151bd308b31cd3119201f62929a84776c6f153cb8fba1c123212942ffe"
        )


def test_known_anchor_has_deterministic_preverified_kit(
    monkeypatch, active_matchup_vocabulary_bundle
):
    """Same fixture anchor must produce the exact same closed substrate twice."""
    monkeypatch.setenv("HRAMATKA_KIT_ENRICHMENT_V1", "1")

    first = retrieval.build_grounding_pack(load_anchor(), "B1", focus="читання")
    second = retrieval.build_grounding_pack(load_anchor(), "B1", focus="читання")
    kit = first["kit"]

    assert _digest(first["kit"]) == _digest(second["kit"])
    assert kit["status"] == "available"
    assert not retrieval.kit_is_empty(kit)
    assert "читання" in kit["anchor_lemmas"]
    assert kit["focus_citation_forms"] == {"status": "available", "forms": ["читання"]}
    assert kit["cefr_tags"]
    assert kit["numeral_tuples"]
    assert any(bank["synonyms"] for bank in kit["synonym_banks"])
    assert kit["aspect_stress"]["status"] == "absent"
    assert "#5368" in kit["aspect_stress"]["reason"]
    assert "=== KIT ENRICHMENT V1 ===" in first["text"]


def test_populated_kit_removes_legacy_synonym_payload_from_non_kit_text(
    monkeypatch, active_matchup_vocabulary_bundle
):
    """A populated kit is the only synonym-bank injection route.

    Legacy synonym strings not verified for VESUM attestation are intentionally
    dropped; they are not reintroduced as fallback prose when a verified kit is
    available.
    """
    monkeypatch.setenv("HRAMATKA_KIT_ENRICHMENT_V1", "1")

    pack = retrieval.build_grounding_pack(load_anchor(), "B1", focus="читання")
    prefix, marker, rendered_kit = pack["text"].partition("\n\n=== KIT ENRICHMENT V1 ===\n")

    assert pack["kit"]["status"] == "available"
    assert marker
    assert "синоніми" not in prefix
    assert "Рівень: B1." in prefix
    assert "книжка — A1" in prefix
    assert "Числа в опорі" in prefix
    assert rendered_kit == json.dumps(
        pack["kit"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    bank_synonyms = {
        synonym for bank in pack["kit"]["synonym_banks"] for synonym in bank["synonyms"]
    }
    assert bank_synonyms
    assert all(synonym not in prefix for synonym in bank_synonyms)


def test_kit_on_prompt_preserves_certified_matchup_pairs_without_second_synonym_route(
    monkeypatch, active_matchup_vocabulary_bundle
):
    """#226's effective modes retain certified pairs while the kit owns synonyms."""
    monkeypatch.setenv("HRAMATKA_KIT_ENRICHMENT_V1", "1")
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    snapshot = pipeline.snapshot_anchor(load_anchor())
    grounding = retrieval.build_grounding_pack(snapshot["body_uk"], "B1", focus="читання")
    shared = prompt_pack.build_shared_input(
        snapshot=snapshot,
        grounding=grounding,
        duration_minutes=45,
        focus="читання",
        phase_count_plans={1: {"match-up": 1, "fill-in": 1}},
        visible_slots_by_phase={1: 2},
    )
    context = prompt_pack.phase_context(shared, phase=1)
    matchup_kit = next(kit for kit in context["type_kits"] if kit["type"] == "match-up")
    prompt = registry.build_extractive_v1_prompt(
        snapshot["body_uk"], "B1", ["match-up", "fill-in"], grounding["text"]
    )

    assert shared["authoring"]["grounding_modes"] == {
        "match-up": "quoting",
        "fill-in": "derived",
    }
    assert [pair["atlas_lemma_pair"] for pair in matchup_kit["pairs"]] == [
        ["думка", "дума"],
        ["думка", "гадка"],
        ["думка", "судження"],
        ["думка", "мисль"],
    ]
    assert all(pair["semantic_status"] == "pass" for pair in matchup_kit["pairs"])
    assert "синоніми:" not in prompt
    assert prompt.count("=== KIT ENRICHMENT V1 ===") == 1


def test_empty_kit_is_explicit_and_never_spills_to_full_lexicon(monkeypatch):
    monkeypatch.setenv("HRAMATKA_KIT_ENRICHMENT_V1", "1")

    pack = retrieval.build_grounding_pack("Незрозуміле слово.", "B1")
    kit = pack["kit"]

    assert retrieval.kit_is_empty(kit)
    assert kit["status"] == "empty"
    assert kit["anchor_lemmas"] == []
    assert kit["synonym_banks"] == []
    assert kit["cefr_tags"] == []
    assert kit["numeral_tuples"] == []
    assert pack["atlas_lookup"] == {}
    assert '"status":"empty"' in pack["text"]


def test_prompt_pack_carries_kit_only_when_enrichment_is_enabled(monkeypatch):
    snapshot = pipeline.snapshot_anchor(load_anchor())
    grounding = retrieval.build_grounding_pack(snapshot["body_uk"], "B1")
    kwargs = {
        "snapshot": snapshot,
        "grounding": grounding,
        "duration_minutes": 45,
        "focus": "читання",
        "phase_count_plans": {1: {"true-false": 1}},
        "visible_slots_by_phase": {1: 1},
    }
    monkeypatch.delenv("HRAMATKA_KIT_ENRICHMENT_V1", raising=False)
    legacy = prompt_pack.build_shared_input(**kwargs)
    assert "kit_enrichment" not in legacy

    monkeypatch.setenv("HRAMATKA_KIT_ENRICHMENT_V1", "1")
    enriched_grounding = retrieval.build_grounding_pack(snapshot["body_uk"], "B1", focus="читання")
    enriched = prompt_pack.build_shared_input(**{**kwargs, "grounding": enriched_grounding})
    assert enriched["kit_enrichment"] == enriched_grounding["kit"]
