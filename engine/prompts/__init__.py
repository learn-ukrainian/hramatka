"""Prompt templates for the Hramatka engine (slice 1)."""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_extractive_template() -> str:
    """The UA-first extractive-generation instruction block (SSOT)."""
    return (_PROMPTS_DIR / "extractive.md").read_text(encoding="utf-8")


def load_legacy_extractive_v4_template() -> str:
    """The immutable pre-count-plumbing prompt used only for measurement."""
    return (_PROMPTS_DIR / "extractive-v4.md").read_text(encoding="utf-8")
