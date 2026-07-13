"""HramatkaActivity IR + projection to a pure activities-b1 item + validation.

Slice-1 §2: the engine keeps a richer intermediate representation (IR) that
carries grounding metadata (evidence spans, provenance, gate results), and
PROJECTS a pure `activities-b1`-conformant object for rendering. The public
b1 schema (`schemas/activities-b1.schema.json`) sets `additionalProperties:
false` on every def, so evidence MUST be carried out-of-band and stripped
before validation — never added inline.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from functools import lru_cache

import jsonschema

from hramatka.contracts import PILOT_ACTIVITY_TYPES

from . import vendoring

# activities-b1 `type` const → its self-contained def name in the schema.
_TYPE_TO_DEF = {activity_type: f"{activity_type}-b1" for activity_type in PILOT_ACTIVITY_TYPES}


@lru_cache(maxsize=1)
def load_b1_schema() -> dict:
    """The pinned `lu.activity.v1` schema, read + digest-verified from the vendor dir."""
    return vendoring.read_json(vendoring.LU_ACTIVITY, "activities-b1.schema.json")


class B1ValidationError(ValueError):
    """Raised when a projected activity fails the activities-b1 schema."""


# ---------------------------------------------------------------------------
# IR dataclasses
# ---------------------------------------------------------------------------
@dataclass
class Evidence:
    """One anchor-derived evidence span. `char_start`/`char_end` are RECOMPUTED
    by the evidence-span gate (§6a), not trusted from the model.
    """

    quote: str
    locator: str  # e.g. "items[0]", "text", "pairs[2]"
    char_start: int | None = None
    char_end: int | None = None
    kind: str = "literal"  # literal | inference | absent


# Candidate disposition.  This is deliberately not a boolean: a candidate can
# be valid enough for a teacher to inspect while still being unsafe to place in
# a lesson automatically.  In particular, an extractive FALSE statement has
# no deterministic falsity proof and therefore belongs in the review tray.
DISPOSITION_READY = "ready"
DISPOSITION_REVIEW = "review_required"
DISPOSITION_REJECTED = "rejected"

# Compatibility names for the slice-1 gate API.  Consumers must use the
# disposition, not `passed`, for selection.  Keeping these aliases makes old
# IR readable while the versioned `extractive-v1` baseline remains available
# for measurements.
GATE_CLEAN = DISPOSITION_READY
GATE_REVIEW = DISPOSITION_REVIEW
GATE_FAILED = DISPOSITION_REJECTED

_GATE_RANK = {GATE_CLEAN: 0, GATE_REVIEW: 1, GATE_FAILED: 2}
_CHECK_TO_STATUS = {"pass": GATE_CLEAN, "warn": GATE_REVIEW, "fail": GATE_FAILED}


@dataclass
class GateCheck:
    gate: str
    status: str  # pass | warn | fail
    detail: str
    locator: str | None = None


@dataclass
class GateResult:
    # The authoritative tri-state verdict. `add()` escalates it monotonically as
    # a sensible default; the pipeline OVERRIDES it after per-item salvage (a
    # salvaged item's FAIL must not fail an activity that still ships).
    status: str = GATE_CLEAN
    checks: list[GateCheck] = field(default_factory=list)

    def add(self, gate: str, status: str, detail: str, locator: str | None = None) -> None:
        self.checks.append(GateCheck(gate=gate, status=status, detail=detail, locator=locator))
        mapped = _CHECK_TO_STATUS.get(status, GATE_CLEAN)
        if _GATE_RANK[mapped] > _GATE_RANK[self.status]:
            self.status = mapped

    @property
    def passed(self) -> bool:
        """Legacy convenience only; it is NOT an auto-inclusion decision."""
        return self.status != GATE_FAILED

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "passed": self.passed,
            "checks": [
                {"gate": c.gate, "status": c.status, "detail": c.detail, "locator": c.locator}
                for c in self.checks
            ],
        }


@dataclass
class HramatkaActivity:
    """Engine IR — the richer internal representation (slice-1 §2)."""

    activity: dict  # the projected activities-b1 item (evidence already stripped)
    evidence: list[Evidence] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    gate_result: GateResult = field(default_factory=GateResult)
    # Per-item verdicts: items/pairs a per-statement/per-pair gate FAILED, so
    # they were filtered OUT of the shipped `activity` but carried here for the
    # review sheet ("N items need teacher attention" — not silently deleted).
    flagged: list[dict] = field(default_factory=list)
    # The typed candidate contract is validated before evidence is stripped.
    # Keeping this private IR copy means rejected candidates retain the exact
    # raw material and reasons rather than disappearing during projection.
    raw_candidate: dict | None = None
    candidate_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "activity": self.activity,
            "evidence": [
                {
                    "quote": e.quote,
                    "locator": e.locator,
                    "char_start": e.char_start,
                    "char_end": e.char_end,
                    "kind": e.kind,
                }
                for e in self.evidence
            ],
            "provenance": self.provenance,
            "gate_result": self.gate_result.as_dict(),
            "flagged": self.flagged,
            "raw_candidate": self.raw_candidate,
            "candidate_id": self.candidate_id,
        }


# ---------------------------------------------------------------------------
# Evidence stripping / projection
# ---------------------------------------------------------------------------
def _pop_evidence(obj: dict) -> str | None:
    """Remove and return the 'evidence' key from a dict (None if absent)."""
    if isinstance(obj, dict) and "evidence" in obj:
        return obj.pop("evidence")
    return None


def parse_raw_activity(raw: dict) -> tuple[dict, list[Evidence]]:
    """Split a model-emitted SUPERSET activity into a clean activities-b1
    item plus a list of Evidence (with per-item locators).

    Evidence may sit at the activity level (cloze) or on each item/pair
    (true-false / match-up). All 'evidence' keys are removed so the returned
    activity is a pure b1 subset (`additionalProperties: false` safe).
    """
    activity = copy.deepcopy(raw)
    evidence: list[Evidence] = []

    top_ev = _pop_evidence(activity)
    if isinstance(top_ev, str) and top_ev.strip():
        evidence.append(Evidence(quote=top_ev, locator="text"))

    for coll_key in ("items", "pairs", "blanks"):
        coll = activity.get(coll_key)
        if not isinstance(coll, list):
            continue
        for i, element in enumerate(coll):
            ev = _pop_evidence(element) if isinstance(element, dict) else None
            if isinstance(ev, str) and ev.strip():
                evidence.append(Evidence(quote=ev, locator=f"{coll_key}[{i}]"))

    return activity, evidence


def project_to_b1(ir: HramatkaActivity) -> dict:
    """Return the pure activities-b1 item for the renderer — a defensive
    deep copy of `ir.activity` with any stray 'evidence' keys stripped.
    """
    clean, _ = parse_raw_activity(ir.activity)
    return clean


def validate_b1(obj: dict) -> None:
    """Validate a single projected item against activities-b1.schema.json.

    Raises B1ValidationError on failure (catches projection drift like
    `correct`→`isTrue`, `text`→`passage`, a leftover `evidence` key, etc.).
    Validates the one-item array against the top-level schema so the schema's
    own oneOf/`additionalProperties:false` rules apply exactly as in prod.
    """
    schema = load_b1_schema()
    t = obj.get("type")
    if t not in _TYPE_TO_DEF:
        raise B1ValidationError(
            f"Unknown/out-of-scope activity type {t!r} (slice-1 supports "
            f"{sorted(_TYPE_TO_DEF)})."
        )
    try:
        jsonschema.validate([obj], schema)
    except jsonschema.ValidationError as exc:
        raise B1ValidationError(
            f"activity type={t!r} failed activities-b1 schema: {exc.message}"
        ) from exc
