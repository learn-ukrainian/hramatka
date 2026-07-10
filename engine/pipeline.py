"""Pipeline orchestration (slice-1 §3): snapshot -> grounding-IN -> generate
-> per-type gate chain (§7) -> project + validate -> persist.

Emits two artifacts per run:
  lesson.b1.json  — array of pure activities-b1 items (gate-passing, schema-valid)
  lesson.ir.json  — full IR (evidence + gate results + provenance) for measure/review

Idempotency (§R): the cache key is a fingerprint over ALL inputs (anchor hash
+ grounding-pack hash + prompt-template hash + model id + db fingerprint), so
an identical re-run reuses the cached raw generation instead of re-calling the
generator.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from . import ENGINE_VERSION, data, paths, retrieval, schema, vendoring
from .gates import evidence_span, matchup_semantics, numeral
from .gates import vesum as vesum_gate
from .generate import (
    GEMMA_MODEL,
    GenerationUnparseable,
    GeneratorUnavailable,
    call_gemma,
    generate,
)
from .prompts import load_extractive_template

EXTRACTIVE_TYPES = ("true-false", "cloze", "match-up")


@dataclass
class PipelineResult:
    anchor: dict
    fingerprint: str
    fingerprint_inputs: dict = field(default_factory=dict)
    activities: list[schema.HramatkaActivity] = field(default_factory=list)
    lesson_b1: list[dict] = field(default_factory=list)
    out_files: dict = field(default_factory=dict)
    generation_error: str | None = None


# ---------------------------------------------------------------------------
# Snapshot + fingerprint
# ---------------------------------------------------------------------------
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_anchor(anchor: str | dict) -> dict:
    if isinstance(anchor, dict):
        body = anchor.get("body_uk") or anchor.get("body") or ""
        anchor_id = anchor.get("anchor_id") or _sha(body)[:12]
        source = anchor.get("source", "teacher-paste")
    else:
        body = anchor
        anchor_id = _sha(body)[:12]
        source = "teacher-paste"
    body = retrieval.nfc(body)
    if source not in {"teacher-paste", "teacher-url"}:
        raise ValueError("anchor source must be teacher-paste or teacher-url")
    return {
        "anchor_id": anchor_id,
        "body_uk": body,
        "hash": _sha(body),
        "source": source,
        # This is content identity only — no external URL or teacher identity.
        "content_fingerprint": _sha(body),
        "char_len": len(body),
    }


# Packages whose version can change bake output (parsers/validators). Vendored
# artifacts (schema + linguistics) are covered separately via their manifest
# digests, so they are not duplicated here.
_FINGERPRINT_PACKAGES = ("jsonschema",)


def _package_versions() -> dict:
    versions: dict[str, str | None] = {"python": platform.python_version()}
    for pkg in _FINGERPRINT_PACKAGES:
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:  # pragma: no cover - dep always present
            versions[pkg] = None
    return versions


def _gate_impl_digest() -> str:
    """Content digest of the gate implementations + retrieval (review-p46 nit 2).

    A change to gate LOGIC must reshuffle the bake fingerprint even when every
    declared input is identical — otherwise cached raw generation would be
    silently reused under different gating code. Hashes `gates/*.py` plus
    `retrieval.py` by content, keyed by their relative path for stability.
    """
    gate_files = [*(paths.ENGINE_DIR / "gates").glob("*.py"), paths.ENGINE_DIR / "retrieval.py"]
    h = hashlib.sha256()
    for fp in sorted(gate_files, key=lambda p: p.relative_to(paths.ENGINE_DIR).as_posix()):
        h.update(fp.relative_to(paths.ENGINE_DIR).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(fp.read_bytes()).digest())
    return h.hexdigest()


def fingerprint_inputs(
    *,
    anchor_hash: str,
    level: str,
    pedagogy: str | None,
    phase: str | None,
    types: list[str],
    grounding_text: str,
    prompt_template: str,
) -> dict:
    """The FULL bake-identity input set (Sol defect #4 / review item 4).

    Covers anchor + level + pedagogy + phase + requested types + prompt digest +
    package versions + engine version + model + gate-implementation digest +
    vendored-artifact digests + data-bundle content digests. DBs are identified
    by CONTENT (sha256 from the pinned data manifest), never size/mtime — so a
    re-run on the same inputs is genuinely idempotent and any input change (data,
    prompt, package, OR gate code) reshuffles the fingerprint.
    """
    return {
        "engine_version": ENGINE_VERSION,
        "model": GEMMA_MODEL,
        "anchor_hash": anchor_hash,
        "level": level,
        "pedagogy": pedagogy,
        "phase": phase,
        "types": sorted(types),
        "grounding_digest": _sha(grounding_text),
        "prompt_digest": _sha(prompt_template),
        "gate_impl_digest": _gate_impl_digest(),
        "packages": _package_versions(),
        "vendor": vendoring.artifact_versions(),
        "data_bundle": data.active_bundle().digests(),
    }


def make_fingerprint(**inputs) -> str:
    """Stable sha256 over the canonical (sorted-key) JSON of `fingerprint_inputs`."""
    blob = json.dumps(
        fingerprint_inputs(**inputs),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _sha(blob)


# ---------------------------------------------------------------------------
# Per-type gate chains (§7)
# ---------------------------------------------------------------------------
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
    activity: dict,
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
        # introduced-token validity + heritage on the statement
        token_verdicts = vesum_gate.check_tokens(
            vesum_gate.content_tokens(statement), anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator=loc)
        # numeral government on any numeral phrase in the statement
        _run_numeral_gate(statement, gr, loc)
        # FALSE statements: falsity can't be verified deterministically -> warn
        if item.get("correct") is False:
            gr.add(
                "false_statement",
                "warn",
                "FALSE statement — deterministic falsity check out of scope; teacher-confirm.",
                locator=loc,
            )


def _gate_cloze(
    activity: dict,
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
    # answer removed from the exact source span (pre-gap span rule, §R)
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
        # distractors (introduced tokens) VESUM-valid + heritage-checked
        distractors = [o for o in options if o != answer]
        token_verdicts = vesum_gate.check_tokens(
            distractors, anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator="text")


def _gate_match_up(
    activity: dict,
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
        # left should be anchor-derived
        quote = ev.quote if ev else left
        verdict = evidence_span.check_evidence(quote, anchor_body)
        gr.add("evidence_span", verdict["status"], verdict["detail"], locator=loc)
        if ev is not None:
            ev.char_start = verdict["char_start"]
            ev.char_end = verdict["char_end"]
            ev.kind = verdict["kind"]
        # right side (gloss) VESUM-valid + heritage-checked; a fabricated gloss
        # FAILs (drops the pair), a russianism gloss WARNs (teacher-confirm).
        token_verdicts = vesum_gate.check_tokens(
            vesum_gate.content_tokens(right), anchor_body, atlas_lookup=atlas_lookup
        )
        worst = vesum_gate.worst_status(token_verdicts)
        if worst != "pass":
            bad = [v for v in token_verdicts if v["status"] == worst]
            gr.add("vesum_token", worst, "; ".join(v["detail"] for v in bad), locator=loc)
        semantic_verdict = matchup_semantics.check_pair(
            left, right, atlas_lookup=atlas_lookup
        )
        if semantic_verdict["status"] != "pass":
            gr.add(
                "matchup_semantics",
                semantic_verdict["status"],
                semantic_verdict["detail"],
                locator=loc,
            )


_GATE_CHAINS: dict[str, Callable] = {
    "true-false": _gate_true_false,
    "cloze": _gate_cloze,
    "match-up": _gate_match_up,
}


# Per-item (collection-index) gate locators look like "items[3]" / "pairs[1]".
# Everything else ("text", None) is an ACTIVITY-level locator.
_ITEM_LOCATOR_RE = re.compile(r"^(items|pairs)\[(\d+)\]$")

# Minimum surviving items per type after per-item filtering (schema minItems).
_MIN_ITEMS = {"true-false": 1, "match-up": 2}
# The collection key each partitionable type filters on.
_COLL_KEY = {"true-false": "items", "match-up": "pairs"}


def _status_rank(status: str) -> int:
    return {"pass": 0, "warn": 1, "fail": 2}[status]


def _locator_statuses(gr: schema.GateResult) -> tuple[dict[str, str], str]:
    """Fold the gate checks into (per-item worst status, activity-level worst
    status). Item-level = "items[i]"/"pairs[i]"; everything else is
    activity-level (cloze "text" gates, schema, type)."""
    per_item: dict[str, str] = {}
    activity = "pass"
    for c in gr.checks:
        loc = c.locator or ""
        if _ITEM_LOCATOR_RE.match(loc):
            if _status_rank(c.status) > _status_rank(per_item.get(loc, "pass")):
                per_item[loc] = c.status
        elif _status_rank(c.status) > _status_rank(activity):
            activity = c.status
    return per_item, activity


def _fail_reasons(gr: schema.GateResult, locator: str) -> list[dict]:
    return [
        {"gate": c.gate, "detail": c.detail}
        for c in gr.checks
        if c.locator == locator and c.status == "fail"
    ]


def _ship_status(
    gr: schema.GateResult, kept_locators: set[str] | None, flagged_present: bool
) -> str:
    """Tri-state verdict for an activity that SHIPS (Sol defect 1).

    `clean` iff no surviving check warns and nothing was salvaged; otherwise
    `review_required` (a warn or a dropped item must block automatic accept).
    Only SURVIVING checks count — activity-level checks always, item-level
    checks only for kept items (a flagged item's own warn is irrelevant once it
    is dropped; the salvage itself already forces review).
    """
    if flagged_present:
        return schema.GATE_REVIEW
    for c in gr.checks:
        if c.status != "warn":
            continue
        loc = c.locator or ""
        if _ITEM_LOCATOR_RE.match(loc):
            if kept_locators is None or loc in kept_locators:
                return schema.GATE_REVIEW
        else:
            return schema.GATE_REVIEW
    return schema.GATE_CLEAN


def _project_and_validate(activity: dict, gr: schema.GateResult) -> dict | None:
    """Project + jsonschema-validate a (possibly filtered) activity. Returns
    the clean b1 item, or None (recording a 'schema' fail) on drift."""
    try:
        projected = schema.project_to_b1(schema.HramatkaActivity(activity=activity))
        schema.validate_b1(projected)
        return projected
    except schema.B1ValidationError as exc:
        gr.add("schema", "fail", str(exc))
        return None


def gate_activity(
    raw_activity: dict,
    anchor_body: str,
    provenance: dict,
    atlas_lookup: dict | None = None,
) -> schema.HramatkaActivity:
    """Run the per-type gate chain, then PER-ITEM granularity (approved
    design): a per-statement/per-pair gate FAIL drops only that item/pair
    (carried in `ir.flagged` for the review sheet), not the whole activity.
    WARN items still ship (teacher-confirm). Activity-level gates (schema
    validity, cloze gap/answer structure) stay all-or-nothing; the whole
    activity is dropped only when an activity-level gate fails or too few
    items survive (match-up needs >=2). `gate_result.status` is the tri-state
    verdict: `failed` == dropped, `clean`/`review_required` == this
    (possibly-filtered) activity ships.
    """
    clean, evidence = schema.parse_raw_activity(raw_activity)
    ir = schema.HramatkaActivity(
        activity=clean, evidence=evidence, provenance=dict(provenance)
    )
    gr = ir.gate_result
    a_type = clean.get("type")
    chain = _GATE_CHAINS.get(a_type)
    if chain is None:
        gr.add("type", "fail", f"Unsupported activity type {a_type!r} for slice 1.")
        gr.status = schema.GATE_FAILED
        return ir

    chain(clean, evidence, anchor_body, gr, atlas_lookup)
    per_item, activity_status = _locator_statuses(gr)

    # Activity-level FAIL (cloze text gates, etc.) -> drop the whole activity.
    if activity_status == "fail":
        gr.status = schema.GATE_FAILED
        return ir

    coll_key = _COLL_KEY.get(a_type)
    if coll_key is None:
        # Non-partitionable (cloze): all-or-nothing. No activity-level fail
        # above, so it ships; still project+validate for schema safety.
        projected = _project_and_validate(clean, gr)
        if projected is None:
            gr.status = schema.GATE_FAILED
            return ir
        ir.activity = projected
        gr.status = _ship_status(gr, None, False)
        return ir

    # Partition the collection: keep non-failing items, flag the failing ones.
    kept, flagged = [], []
    for i, item in enumerate(clean.get(coll_key, [])):
        loc = f"{coll_key}[{i}]"
        if per_item.get(loc) == "fail":
            flagged.append({"locator": loc, "reasons": _fail_reasons(gr, loc), "item": item})
        else:
            kept.append(item)
    ir.flagged = flagged

    min_items = _MIN_ITEMS.get(a_type, 1)
    if len(kept) < min_items:
        gr.add(
            "partition",
            "fail",
            f"Only {len(kept)} item(s) survived per-item gating; {a_type} "
            f"requires >= {min_items} — dropping the whole activity.",
        )
        gr.status = schema.GATE_FAILED
        return ir

    filtered = dict(clean)
    filtered[coll_key] = kept
    projected = _project_and_validate(filtered, gr)
    if projected is None:
        gr.status = schema.GATE_FAILED
        return ir
    ir.activity = projected
    kept_locators = {
        f"{coll_key}[{i}]"
        for i, _ in enumerate(clean.get(coll_key, []))
        if per_item.get(f"{coll_key}[{i}]") != "fail"
    }
    gr.status = _ship_status(gr, kept_locators, bool(flagged))
    return ir


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(
    anchor: str | dict,
    level: str = "B1",
    types: list[str] | None = None,
    *,
    pedagogy: str | None = None,
    phase: str | None = None,
    generator: Callable[[str], str] = call_gemma,
    out_dir: str | Path | None = None,
    atlas_db=None,
    use_cache: bool = True,
    cache_dir: str | Path | None = None,
) -> PipelineResult:
    """Run the slice-1 pipeline end-to-end. `generator` is injectable (tests
    pass a mock; the real run uses `call_gemma`). All inputs resolve through the
    pinned vendor + data indirections, so the run is cwd- and checkout-independent.

    `pedagogy`/`phase` are carried into the bake fingerprint even though slice-1
    generation does not yet branch on them, so the identity is correct the moment
    a pedagogy/phase layer lands (Sol defect #4).

    `cache_dir` isolates the raw-generation cache. Mock/offline callers MUST
    pass their own tmp `cache_dir` so fixture output never lands in the
    default `engine/.cache/` that the real-Gemma run reads (the fingerprint
    is generator-agnostic by design, so a shared cache would otherwise
    let a mock run poison a real one).
    """
    types = list(types or EXTRACTIVE_TYPES)
    snap = snapshot_anchor(anchor)
    grounding = retrieval.build_grounding_pack(snap["body_uk"], level, atlas_db=atlas_db)
    template = load_extractive_template()
    fp_inputs = fingerprint_inputs(
        anchor_hash=snap["hash"],
        level=level,
        pedagogy=pedagogy,
        phase=phase,
        types=types,
        grounding_text=grounding["text"],
        prompt_template=template,
    )
    fingerprint = _sha(
        json.dumps(fp_inputs, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )

    out_dir = Path(out_dir) if out_dir else (paths.DEFAULT_OUT_DIR / snap["anchor_id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = Path(cache_dir) if cache_dir else paths.CACHE_DIR
    cache_path.mkdir(parents=True, exist_ok=True)
    cache_file = cache_path / f"{fingerprint}.raw.json"

    provenance = {
        "anchor_id": snap["anchor_id"],
        "anchor_hash": snap["hash"],
        "anchor_source": snap["source"],
        "anchor_content_fingerprint": snap["content_fingerprint"],
        "level": level,
        "pedagogy": pedagogy,
        "phase": phase,
        "generator": GEMMA_MODEL,
        "fingerprint": fingerprint,
    }

    result = PipelineResult(
        anchor=snap, fingerprint=fingerprint, fingerprint_inputs=fp_inputs
    )

    # ---- generate (cache-first) --------------------------------------
    raw_activities: list[dict] = []
    if use_cache and cache_file.exists():
        raw_activities = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        try:
            raw_activities = generate(
                snap["body_uk"],
                level,
                types,
                generator=generator,
                grounding_pack=grounding["text"],
            )
            cache_file.write_text(
                json.dumps(raw_activities, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (GeneratorUnavailable, GenerationUnparseable) as exc:
            result.generation_error = f"{type(exc).__name__}: {exc}"

    # ---- gate + project ----------------------------------------------
    # Wire the grounding atlas lookup into every lexical gate (Sol defect 5),
    # extended to cover the model's INTRODUCED lexicon so a generated russianism
    # is caught too, not only one quoted from the anchor.
    atlas_lookup = retrieval.augmented_atlas_lookup(
        snap["body_uk"], raw_activities, grounding["atlas_lookup"], atlas_db=atlas_db
    )
    # Treat the source as a quotation rather than a clean linguistic baseline.
    # These diagnostics belong in IR + the emitted private document, but do not
    # become task-language gate failures (Sol defect 7/8).
    snap["diagnostics"] = vesum_gate.anchor_baseline_diagnostics(
        snap["body_uk"], atlas_lookup=atlas_lookup
    )
    for raw in raw_activities:
        ir = gate_activity(raw, snap["body_uk"], provenance, atlas_lookup=atlas_lookup)
        result.activities.append(ir)

    result.lesson_b1 = [
        ir.activity for ir in result.activities if ir.gate_result.passed
    ]

    # ---- persist ------------------------------------------------------
    b1_path = out_dir / "lesson.b1.json"
    ir_path = out_dir / "lesson.ir.json"
    b1_path.write_text(
        json.dumps(result.lesson_b1, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ir_path.write_text(
        json.dumps(
            {
                "anchor": snap,
                "fingerprint": fingerprint,
                "fingerprint_inputs": fp_inputs,
                "generation_error": result.generation_error,
                "activities": [ir.as_dict() for ir in result.activities],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    result.out_files = {"lesson_b1": str(b1_path), "lesson_ir": str(ir_path)}
    return result
