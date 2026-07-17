"""Grounding-mode v1 — Slice 5 acceptance (writer_prompt_v2 mode-split authoring).

Contract: hramatka/grounding-mode-v1-final-spec.md §slice-5, §9, §7/E4.
E4 choice: writer_prompt_v2 alone → force extractive-v5 copy (not a boot error).
Flags-off path remains byte-identical production extractive / pack output.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from hramatka.engine import flags, pipeline, prompt_pack, registry, retrieval
from hramatka.engine.fixtures import load_anchor
from hramatka.engine.prompts import (
    load_active_writer_template,
    load_extractive_template,
    load_writer_prompt_v2_template,
    mode_split_authoring_active,
)


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clear_slice5_flags(monkeypatch) -> None:
    for env in (
        "HRAMATKA_WRITER_PROMPT_V2",
        "HRAMATKA_GROUNDING_MODE_V1",
        "HRAMATKA_KIT_ENRICHMENT_V1",
    ):
        monkeypatch.delenv(env, raising=False)


def _shared_kwargs(*, grounding: dict | None = None) -> dict:
    snapshot = pipeline.snapshot_anchor(load_anchor())
    pack = grounding or retrieval.build_grounding_pack(snapshot["body_uk"], "B1")
    return {
        "snapshot": snapshot,
        "grounding": pack,
        "duration_minutes": 45,
        "focus": "читання",
        "phase_count_plans": {1: {"true-false": 1, "fill-in": 1}},
        "visible_slots_by_phase": {1: 2},
    }


def _writer_prompt(types: list[str] | None = None) -> str:
    requested = types or ["true-false", "fill-in", "cloze", "short-writing"]
    return registry.build_extractive_v1_prompt(
        load_anchor(),
        "B1",
        requested,
        "",
        counts={activity_type: 1 for activity_type in requested},
    )


# ---------------------------------------------------------------------------
# Flags-off identity
# ---------------------------------------------------------------------------
def test_flags_off_writer_template_is_extractive_bytes(monkeypatch):
    _clear_slice5_flags(monkeypatch)
    assert mode_split_authoring_active() is False
    assert load_active_writer_template() == load_extractive_template()
    assert "ДВА РЕЖИМИ ОБҐРУНТУВАННЯ" not in load_active_writer_template()


def test_flags_off_prompt_pack_injection_identical(monkeypatch):
    _clear_slice5_flags(monkeypatch)
    first = prompt_pack.build_shared_input(**_shared_kwargs())
    second = prompt_pack.build_shared_input(**_shared_kwargs())
    assert first["provenance"]["injection_sha256"] == second["provenance"]["injection_sha256"]
    assert "authoring" not in first
    assert first["provenance"]["template_version"] == prompt_pack.TEMPLATE_VERSION


def test_flags_off_rendered_pack_prompt_has_no_mode_split(monkeypatch):
    _clear_slice5_flags(monkeypatch)
    shared = prompt_pack.build_shared_input(**_shared_kwargs())
    prompt = prompt_pack.render_phase_prompt(prompt_pack.phase_context(shared, phase=1))
    assert "РЕЖИМИ ОБҐРУНТУВАННЯ" not in prompt
    assert "never quote restore" not in prompt.casefold()
    assert "- true-false: 1" in prompt
    assert "[quoting]" not in prompt


# ---------------------------------------------------------------------------
# E4 matrix
# ---------------------------------------------------------------------------
def test_e4_writer_prompt_alone_forces_extractive_copy(monkeypatch):
    """writer_prompt_v2 ON ∧ grounding_mode_v1 OFF → extractive-v5 copy (E4)."""
    _clear_slice5_flags(monkeypatch)
    monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")

    assert mode_split_authoring_active() is False
    assert load_active_writer_template() == load_extractive_template()
    prompt = _writer_prompt()
    assert "ДВА РЕЖИМИ ОБҐРУНТУВАННЯ" not in prompt
    assert "grounding_mode=derived" not in prompt
    assert "НІКОЛИ не відновлення дослівного" not in prompt
    assert "частина правильні, частина неправильні" in prompt  # extractive T/F copy

    shared = prompt_pack.build_shared_input(**_shared_kwargs())
    assert "authoring" not in shared
    pack_prompt = prompt_pack.render_phase_prompt(prompt_pack.phase_context(shared, phase=1))
    assert "РЕЖИМИ ОБҐРУНТУВАННЯ" not in pack_prompt
    assert "[derived]" not in pack_prompt


def test_e4_both_on_emits_mode_split_authoring(monkeypatch):
    """writer_prompt_v2 ∧ grounding_mode_v1 → mode tables + P2/P3 lines."""
    _clear_slice5_flags(monkeypatch)
    monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")

    assert mode_split_authoring_active() is True
    template = load_active_writer_template()
    assert template == load_writer_prompt_v2_template()
    assert "ДВА РЕЖИМИ ОБҐРУНТУВАННЯ" in template

    prompt = _writer_prompt(["true-false", "fill-in", "cloze", "short-writing", "error-correction"])
    assert "true-false [quoting]" in prompt
    assert "fill-in [derived]" in prompt
    assert "cloze [quoting]" in prompt
    assert "НІКОЛИ не відновлення дослівного речення опори" in prompt  # P3
    assert "Лише SPAN ПРОПУСКУ" in prompt  # P2
    assert "спочатку оберіть ПРАВИЛЬНУ форму" in prompt  # correction-first
    assert "constraints[]" in prompt
    assert "Культурний апгрейд" in prompt
    assert "Анти-мета" in prompt


def test_e4_both_on_pack_carries_modes_and_empty_kit_notice(monkeypatch):
    _clear_slice5_flags(monkeypatch)
    monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")

    shared = prompt_pack.build_shared_input(**_shared_kwargs())
    assert shared["authoring"]["mode"] == "mode-split-v2"
    assert shared["authoring"]["grounding_modes"]["true-false"] == "quoting"
    assert shared["authoring"]["grounding_modes"]["fill-in"] == "derived"
    assert shared["provenance"]["template_version"].startswith(prompt_pack.TEMPLATE_VERSION + "+")
    assert "writer-prompt-v2:" in shared["provenance"]["template_version"]

    prompt = prompt_pack.render_phase_prompt(prompt_pack.phase_context(shared, phase=1))
    assert "РЕЖИМИ ОБҐРУНТУВАННЯ" in prompt
    assert "fill-in НІКОЛИ не quote-restore" in prompt
    assert "true-false [quoting]" in prompt
    assert "fill-in [derived]" in prompt
    # Kit enrichment flag off → explicit empty-kit authoring notice.
    assert "порожній або відсутній" in prompt


def test_mode_split_with_kit_enrichment_references_kit_banks(monkeypatch):
    _clear_slice5_flags(monkeypatch)
    monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    monkeypatch.setenv("HRAMATKA_KIT_ENRICHMENT_V1", "1")

    snapshot = pipeline.snapshot_anchor(load_anchor())
    grounding = retrieval.build_grounding_pack(snapshot["body_uk"], "B1", focus="читання")
    shared = prompt_pack.build_shared_input(**_shared_kwargs(grounding=grounding))
    assert shared["kit_enrichment"]["status"] == "available"
    prompt = prompt_pack.render_phase_prompt(prompt_pack.phase_context(shared, phase=1))
    assert "KIT ENRICHMENT" in prompt
    assert "anchor_lemmas" in prompt


def test_sentence_builder_instructions_when_type_requested(monkeypatch):
    _clear_slice5_flags(monkeypatch)
    monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")

    # sentence-builder is mode-derived in the prompt table (P1) even before
    # pilot registration; density scaling skips unregistered types.
    prompt = registry.build_extractive_v1_prompt(
        load_anchor(),
        "B1",
        ["sentence-builder", "fill-in"],
        "",
        counts={"sentence-builder": 1, "fill-in": 1},
    )
    assert "sentence-builder [derived]" in prompt
    assert "Зробіть речення" in prompt
    assert "starters" in prompt


# ---------------------------------------------------------------------------
# Prompt digest / bake-id honesty
# ---------------------------------------------------------------------------
def test_mode_split_changes_registry_fingerprint_and_bake_id(monkeypatch):
    _clear_slice5_flags(monkeypatch)
    types = ["true-false", "fill-in"]
    base_reg = registry.registry_fingerprint(types)
    base_fp = pipeline.make_fingerprint(
        anchor_hash="a" * 64,
        level="B1",
        pedagogy="TTT",
        phase=1,
        types=types,
        grounding_text="x",
        prompt_template=load_active_writer_template(),
        count_plan={"true-false": 1, "fill-in": 1},
    )

    monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    split_reg = registry.registry_fingerprint(types)
    split_fp = pipeline.make_fingerprint(
        anchor_hash="a" * 64,
        level="B1",
        pedagogy="TTT",
        phase=1,
        types=types,
        grounding_text="x",
        prompt_template=load_active_writer_template(),
        count_plan={"true-false": 1, "fill-in": 1},
    )

    assert base_reg["digest"] != split_reg["digest"]
    assert split_reg["entries"][0]["prompt_version"].startswith("writer-prompt-v2:")
    assert split_reg["entries"][0]["authoring_mode"] == "mode-split-v2"
    assert "authoring_mode" not in base_reg["entries"][0]
    assert base_fp != split_fp


def test_writer_prompt_alone_does_not_change_pack_injection(monkeypatch):
    """E4 partial-on: pack bytes stay extractive; flag vector alone may flip bake id."""
    _clear_slice5_flags(monkeypatch)
    off = prompt_pack.build_shared_input(**_shared_kwargs())
    off_prompt = prompt_pack.render_phase_prompt(prompt_pack.phase_context(off, phase=1))

    monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")
    alone = prompt_pack.build_shared_input(**_shared_kwargs())
    alone_prompt = prompt_pack.render_phase_prompt(prompt_pack.phase_context(alone, phase=1))

    assert alone["provenance"]["injection_sha256"] == off["provenance"]["injection_sha256"]
    assert alone_prompt == off_prompt
    assert _digest({k: alone[k] for k in alone if k != "provenance"}) == _digest(
        {k: off[k] for k in off if k != "provenance"}
    )


def test_mode_split_changes_pack_injection_sha(monkeypatch):
    _clear_slice5_flags(monkeypatch)
    off = prompt_pack.build_shared_input(**_shared_kwargs())

    monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")
    monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    on = prompt_pack.build_shared_input(**_shared_kwargs())

    assert on["provenance"]["injection_sha256"] != off["provenance"]["injection_sha256"]


@pytest.mark.parametrize(
    ("writer", "grounding", "expect_split"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, True),
    ],
)
def test_e4_composition_matrix(monkeypatch, writer, grounding, expect_split):
    _clear_slice5_flags(monkeypatch)
    if writer:
        monkeypatch.setenv("HRAMATKA_WRITER_PROMPT_V2", "1")
    if grounding:
        monkeypatch.setenv("HRAMATKA_GROUNDING_MODE_V1", "1")
    assert mode_split_authoring_active() is expect_split
    assert flags.writer_prompt_v2_enabled() is writer
    assert flags.grounding_mode_v1_enabled() is grounding
    template = load_active_writer_template()
    if expect_split:
        assert template == load_writer_prompt_v2_template()
    else:
        assert template == load_extractive_template()
