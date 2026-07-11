"""Versioned activity registry for the typed candidate-bank pipeline.

Wave 0 deliberately registers only the three proven extractive activities.
Their prompt builder preserves the exact ``extractive-v1`` prompt so changing
the plumbing does not change generated exercise content.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import schema
from .prompts import load_extractive_template

REGISTRY_VERSION = "wave0.registry.v1"
EXTRACTIVE_PROMPT_VERSION = "extractive-v1:9b0c30a97f64"

PromptBuilder = Callable[[str, str, list[str], str], str]
EvidenceLocator = Callable[[dict[str, Any]], tuple[str, ...]]


def build_extractive_v1_prompt(
    anchor: str, level: str, _types: list[str], grounding_pack: str
) -> str:
    """The unmodified one-shot extractive-v1 prompt.

    ``types`` intentionally remains unused because the frozen prompt itself
    asks for all three legacy types.  The registry routes that one response
    into typed banks; it does not split the model call and risk drift.
    """
    return (
        f"{load_extractive_template()}\n\n"
        f"=== GROUNDING PACK ===\n{grounding_pack}\n\n"
        f"=== ТЕКСТ-ОПОРА (anchor, рівень {level}) ===\n{anchor}\n"
    )


@dataclass(frozen=True)
class RawCandidateSchema:
    """Small, explicit contract for the evidence-carrying model super-set."""

    version: str
    collection_key: str | None
    evidence_at_activity: bool = False

    def validate(self, raw: object, expected_type: str) -> list[str]:
        if not isinstance(raw, dict):
            return ["candidate must be a JSON object"]
        if raw.get("type") != expected_type:
            return [f"candidate type must be {expected_type!r}"]
        if not isinstance(raw.get("instruction"), str) or not raw["instruction"].strip():
            return ["candidate requires a non-empty Ukrainian instruction"]

        errors: list[str] = []
        if self.evidence_at_activity:
            if not isinstance(raw.get("evidence"), str) or not raw["evidence"].strip():
                errors.append("candidate requires a non-empty activity evidence quote")
        elif self.collection_key:
            collection = raw.get(self.collection_key)
            if not isinstance(collection, list) or not collection:
                errors.append(f"candidate requires a non-empty {self.collection_key} list")
            else:
                for index, item in enumerate(collection):
                    if not isinstance(item, dict):
                        errors.append(f"{self.collection_key}[{index}] must be an object")
                        continue
                    if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                        errors.append(
                            f"{self.collection_key}[{index}] requires a non-empty evidence quote"
                        )
                    if expected_type == "true-false":
                        if (
                            not isinstance(item.get("statement"), str)
                            or not item["statement"].strip()
                        ):
                            errors.append(f"items[{index}] requires a non-empty statement")
                        if not isinstance(item.get("correct"), bool):
                            errors.append(f"items[{index}] requires boolean correct")
                    if expected_type == "match-up":
                        for side in ("left", "right"):
                            if not isinstance(item.get(side), str) or not item[side].strip():
                                errors.append(f"pairs[{index}] requires a non-empty {side}")
        if expected_type == "cloze":
            if not isinstance(raw.get("text"), str) or not raw["text"].strip():
                errors.append("cloze requires non-empty text")
            blanks = raw.get("blanks")
            if not isinstance(blanks, list) or not blanks:
                errors.append("cloze requires a non-empty blanks list")
            else:
                for index, blank in enumerate(blanks):
                    if not isinstance(blank, dict):
                        errors.append(f"blanks[{index}] must be an object")
                        continue
                    if not isinstance(blank.get("id"), int):
                        errors.append(f"blanks[{index}] requires integer id")
                    if not isinstance(blank.get("answer"), str) or not blank["answer"].strip():
                        errors.append(f"blanks[{index}] requires a non-empty answer")
                    if not isinstance(blank.get("options"), list) or not blank["options"]:
                        errors.append(f"blanks[{index}] requires non-empty options")
        return errors


def _item_locators(raw: dict[str, Any]) -> tuple[str, ...]:
    return tuple(f"items[{index}]" for index, _item in enumerate(raw.get("items", [])))


def _pair_locators(raw: dict[str, Any]) -> tuple[str, ...]:
    return tuple(f"pairs[{index}]" for index, _pair in enumerate(raw.get("pairs", [])))


def _text_locator(_raw: dict[str, Any]) -> tuple[str, ...]:
    return ("text",)


@dataclass(frozen=True)
class ActivityRegistryEntry:
    """Everything an activity type owns in the candidate-bank pipeline."""

    activity_type: str
    prompt_builder: PromptBuilder
    prompt_version: str
    raw_candidate_schema: RawCandidateSchema
    evidence_locator: EvidenceLocator
    assessment_mode: str
    gate_chain: str
    gate_version: str
    partition_key: str | None
    minimum_survivors: int
    ttt_phases: tuple[int, ...]
    item_budget: int
    is_puzzle: bool
    public_projector: Callable[[schema.HramatkaActivity], dict]

    def fingerprint(self) -> dict[str, Any]:
        return {
            "type": self.activity_type,
            "prompt_version": self.prompt_version,
            "raw_schema_version": self.raw_candidate_schema.version,
            "gate_chain": self.gate_chain,
            "gate_version": self.gate_version,
            "assessment_mode": self.assessment_mode,
            "partition_key": self.partition_key,
            "minimum_survivors": self.minimum_survivors,
            "ttt_phases": self.ttt_phases,
            "item_budget": self.item_budget,
            "is_puzzle": self.is_puzzle,
        }


ACTIVITY_REGISTRY: dict[str, ActivityRegistryEntry] = {
    "true-false": ActivityRegistryEntry(
        activity_type="true-false",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_candidate_schema=RawCandidateSchema("true-false.raw.v1", "items"),
        evidence_locator=_item_locators,
        assessment_mode="auto_gradable",
        gate_chain="true-false.extractive.v1",
        gate_version="true-false.gates.v1",
        partition_key="items",
        minimum_survivors=1,
        ttt_phases=(1, 2, 3),
        item_budget=5,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "cloze": ActivityRegistryEntry(
        activity_type="cloze",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_candidate_schema=RawCandidateSchema("cloze.raw.v1", None, evidence_at_activity=True),
        evidence_locator=_text_locator,
        assessment_mode="auto_gradable",
        gate_chain="cloze.extractive.v1",
        gate_version="cloze.gates.v1",
        partition_key=None,
        minimum_survivors=1,
        ttt_phases=(1, 2, 3),
        item_budget=1,
        is_puzzle=False,
        public_projector=schema.project_to_b1,
    ),
    "match-up": ActivityRegistryEntry(
        activity_type="match-up",
        prompt_builder=build_extractive_v1_prompt,
        prompt_version=EXTRACTIVE_PROMPT_VERSION,
        raw_candidate_schema=RawCandidateSchema("match-up.raw.v1", "pairs"),
        evidence_locator=_pair_locators,
        assessment_mode="auto_gradable",
        gate_chain="match-up.extractive.v1",
        gate_version="match-up.gates.v1",
        partition_key="pairs",
        minimum_survivors=2,
        ttt_phases=(1, 2),
        item_budget=4,
        is_puzzle=True,
        public_projector=schema.project_to_b1,
    ),
}


def entries_for(types: list[str]) -> list[ActivityRegistryEntry]:
    unknown = sorted(set(types) - set(ACTIVITY_REGISTRY))
    if unknown:
        raise ValueError(f"Unsupported activity type(s): {', '.join(unknown)}")
    return [ACTIVITY_REGISTRY[activity_type] for activity_type in types]


def registry_fingerprint(types: list[str]) -> dict[str, Any]:
    entries = [entry.fingerprint() for entry in entries_for(types)]
    packed = json.dumps(entries, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "version": REGISTRY_VERSION,
        "entries": entries,
        "digest": hashlib.sha256(packed.encode("utf-8")).hexdigest(),
    }
