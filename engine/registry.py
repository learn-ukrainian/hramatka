"""Versioned activity registry for the typed candidate-bank pipeline.

Wave 0 deliberately registers only the three proven extractive activities.
Their prompt builder preserves the exact ``extractive-v1`` prompt so changing
the plumbing does not change generated exercise content.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import retrieval, schema
from .gates import evidence_span, matchup_semantics, numeral
from .gates import vesum as vesum_gate
from .prompts import load_extractive_template

REGISTRY_VERSION = "wave0.registry.v1"
EXTRACTIVE_PROMPT_VERSION = "extractive-v1:9b0c30a97f64"

PromptBuilder = Callable[[str, str, list[str], str], str]
EvidenceLocator = Callable[[dict[str, Any]], tuple[str, ...]]
RawValidator = Callable[[object], list[str]]
ActivityGate = Callable[
    [dict[str, Any], list[schema.Evidence], str, schema.GateResult, dict | None], None
]
EvidenceAnswerPairs = Callable[[schema.HramatkaActivity], list[tuple[str, str]]]
_SPACE_RE = re.compile(r"\s+")


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


def _validate_basics(raw: object, expected_type: str) -> list[str]:
    if not isinstance(raw, dict):
        return ["candidate must be a JSON object"]
    if raw.get("type") != expected_type:
        return [f"candidate type must be {expected_type!r}"]
    if not isinstance(raw.get("instruction"), str) or not raw["instruction"].strip():
        return ["candidate requires a non-empty Ukrainian instruction"]
    return []


def _validate_true_false(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "true-false"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        errors.append("candidate requires a non-empty items list")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"items[{index}] must be an object")
                continue
            if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                errors.append(f"items[{index}] requires a non-empty evidence quote")
            if not isinstance(item.get("statement"), str) or not item["statement"].strip():
                errors.append(f"items[{index}] requires a non-empty statement")
            if not isinstance(item.get("correct"), bool):
                errors.append(f"items[{index}] requires boolean correct")
    return errors


def _validate_cloze(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "cloze"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    if not isinstance(raw.get("evidence"), str) or not raw["evidence"].strip():
        errors.append("candidate requires a non-empty activity evidence quote")
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


def _validate_match_up(raw: object) -> list[str]:
    if errors := _validate_basics(raw, "match-up"):
        return errors
    assert isinstance(raw, dict)
    errors = []
    pairs = raw.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        errors.append("candidate requires a non-empty pairs list")
    else:
        for index, pair in enumerate(pairs):
            if not isinstance(pair, dict):
                errors.append(f"pairs[{index}] must be an object")
                continue
            if not isinstance(pair.get("evidence"), str) or not pair["evidence"].strip():
                errors.append(f"pairs[{index}] requires a non-empty evidence quote")
            for side in ("left", "right"):
                if not isinstance(pair.get(side), str) or not pair[side].strip():
                    errors.append(f"pairs[{index}] requires a non-empty {side}")
    return errors


def _item_locators(raw: dict[str, Any]) -> tuple[str, ...]:
    return tuple(f"items[{index}]" for index, _item in enumerate(raw.get("items", [])))


def _pair_locators(raw: dict[str, Any]) -> tuple[str, ...]:
    return tuple(f"pairs[{index}]" for index, _pair in enumerate(raw.get("pairs", [])))


def _text_locator(_raw: dict[str, Any]) -> tuple[str, ...]:
    return ("text",)


def _signature_value(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "").casefold()).strip()


def _true_false_evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    return [
        (
            _signature_value(evidence_by_locator.get(f"items[{index}]")),
            _signature_value(item.get("correct")),
        )
        for index, item in enumerate(ir.activity.get("items", []))
    ]


def _cloze_evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    answer = "|".join(
        _signature_value(blank.get("answer")) for blank in ir.activity.get("blanks", [])
    )
    return [(_signature_value(evidence_by_locator.get("text")), answer)]


def _match_up_evidence_answer_pairs(ir: schema.HramatkaActivity) -> list[tuple[str, str]]:
    evidence_by_locator = {e.locator: e.quote for e in ir.evidence}
    return [
        (
            _signature_value(evidence_by_locator.get(f"pairs[{index}]")),
            _signature_value(pair.get("right")),
        )
        for index, pair in enumerate(ir.activity.get("pairs", []))
    ]


def _numeral_phrases(text: str) -> list[str]:
    return [d["raw_span"] for d in retrieval.extract_numeral_inventory(text) if d["following_noun"]]


def _run_numeral_gate(text: str, gr: schema.GateResult, locator: str) -> None:
    for phrase in _numeral_phrases(text):
        result = numeral.check_numeral_government(phrase)
        status = result["status"]
        if status in ("fail", "warn"):
            gr.add(
                "numeral",
                status,
                f"[{result['rule']}] {result['detail']}"
                + (f" expected={result['expected']}" if result.get("expected") else ""),
                locator=locator,
            )


def _gate_true_false(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    ev_by_loc = {e.locator: e for e in evidence}
    for i, item in enumerate(activity.get("items", [])):
        loc = f"items[{i}]"
        statement = item.get("statement", "")
        ev = ev_by_loc.get(loc)
        verdict = evidence_span.check_evidence(ev.quote if ev else "", anchor_body)
        gr.add("evidence_span", verdict["status"], verdict["detail"], locator=loc)
        if ev is not None:
            ev.char_start = verdict["char_start"]
            ev.char_end = verdict["char_end"]
            ev.kind = verdict["kind"]
        token_verdicts = vesum_gate.check_tokens(
            vesum_gate.content_tokens(statement), anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator=loc)
        _run_numeral_gate(statement, gr, loc)
        if item.get("correct") is False:
            gr.add(
                "false_statement",
                "warn",
                "FALSE statement — deterministic falsity check out of scope; teacher-confirm.",
                locator=loc,
            )


def _gate_cloze(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    text = activity.get("text", "")
    ev = next((e for e in evidence if e.locator == "text"), None)
    verdict = evidence_span.check_evidence(ev.quote if ev else "", anchor_body)
    gr.add("evidence_span", verdict["status"], verdict["detail"], locator="text")
    if ev is not None:
        ev.char_start = verdict["char_start"]
        ev.char_end = verdict["char_end"]
        ev.kind = verdict["kind"]
    has_gap = "{gap}" in text or "{{" in text
    if not has_gap:
        gr.add("cloze_gap", "fail", "Cloze text has no {gap}/{{N}} marker.", locator="text")
    for blank in activity.get("blanks", []) or []:
        answer = blank.get("answer", "")
        options = blank.get("options", []) or []
        if answer and ev and answer not in ev.quote:
            gr.add(
                "cloze_answer",
                "fail",
                f"Cloze answer '{answer}' is not present in the source sentence.",
                locator="text",
            )
        if answer and answer not in options:
            gr.add(
                "cloze_answer",
                "fail",
                f"Cloze answer '{answer}' is not among its own options.",
                locator="text",
            )
        distractors = [o for o in options if o != answer]
        token_verdicts = vesum_gate.check_tokens(
            distractors, anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator="text")


def _gate_match_up(
    activity: dict[str, Any],
    evidence: list[schema.Evidence],
    anchor_body: str,
    gr: schema.GateResult,
    atlas_lookup: dict | None = None,
) -> None:
    ev_by_loc = {e.locator: e for e in evidence}
    for i, pair in enumerate(activity.get("pairs", [])):
        loc = f"pairs[{i}]"
        left = pair.get("left", "")
        right = pair.get("right", "")
        ev = ev_by_loc.get(loc)
        quote = ev.quote if ev else left
        verdict = evidence_span.check_evidence(quote, anchor_body)
        gr.add("evidence_span", verdict["status"], verdict["detail"], locator=loc)
        if ev is not None:
            ev.char_start = verdict["char_start"]
            ev.char_end = verdict["char_end"]
            ev.kind = verdict["kind"]
        token_verdicts = vesum_gate.check_tokens(
            vesum_gate.content_tokens(right), anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator=loc)
        semantic_verdict = matchup_semantics.check_pair(left, right, atlas_lookup=atlas_lookup)
        if semantic_verdict["status"] != "pass":
            gr.add(
                "matchup_semantics",
                semantic_verdict["status"],
                semantic_verdict["detail"],
                locator=loc,
            )


@dataclass(frozen=True)
class ActivityRegistryEntry:
    """Everything an activity type owns in the candidate-bank pipeline."""

    activity_type: str
    prompt_builder: PromptBuilder
    prompt_version: str
    raw_schema_version: str
    raw_validator: RawValidator
    evidence_locator: EvidenceLocator
    gate: ActivityGate
    evidence_answer_pairs: EvidenceAnswerPairs
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
            "raw_schema_version": self.raw_schema_version,
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
        raw_schema_version="true-false.raw.v1",
        raw_validator=_validate_true_false,
        evidence_locator=_item_locators,
        gate=_gate_true_false,
        evidence_answer_pairs=_true_false_evidence_answer_pairs,
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
        raw_schema_version="cloze.raw.v1",
        raw_validator=_validate_cloze,
        evidence_locator=_text_locator,
        gate=_gate_cloze,
        evidence_answer_pairs=_cloze_evidence_answer_pairs,
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
        raw_schema_version="match-up.raw.v1",
        raw_validator=_validate_match_up,
        evidence_locator=_pair_locators,
        gate=_gate_match_up,
        evidence_answer_pairs=_match_up_evidence_answer_pairs,
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
