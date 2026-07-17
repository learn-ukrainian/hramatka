"""Slice-2 kit feed-forward contract (grounding-mode v1 §8 / §11)."""

from __future__ import annotations

import hashlib
import json

from hramatka.engine import pipeline, prompt_pack, retrieval
from hramatka.engine.fixtures import load_anchor


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_kit_enrichment_off_preserves_legacy_grounding_pack(monkeypatch):
    monkeypatch.delenv("HRAMATKA_KIT_ENRICHMENT_V1", raising=False)

    pack = retrieval.build_grounding_pack(load_anchor(), "B1", focus="читання")

    assert set(pack) == {"text", "lemmas", "atlas_lookup", "numeral_inventory"}
    assert "KIT ENRICHMENT V1" not in pack["text"]


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
