"""Pipeline orchestration (slice-1 §3): snapshot -> grounding-IN -> generate
-> per-type gate chain (§7) -> project + validate -> persist.

Emits two artifacts per run:
  lesson.b1.json  — selected READY activities projected to lu.activity.v1
  lesson.ir.json  — full candidate IR, including review/rejected reasons

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
from typing import Literal

from . import ENGINE_VERSION, data, paths, registry, retrieval, schema, selector, vendoring
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
    ready: list[schema.HramatkaActivity] = field(default_factory=list)
    review_required: list[schema.HramatkaActivity] = field(default_factory=list)
    rejected: list[schema.HramatkaActivity] = field(default_factory=list)
    selected: list[schema.HramatkaActivity] = field(default_factory=list)
    lesson_b1: list[dict] = field(default_factory=list)
    out_files: dict = field(default_factory=dict)
    generation_error: str | None = None
    regeneration_attempts: int = 0


# ---------------------------------------------------------------------------
# Snapshot + fingerprint
# ---------------------------------------------------------------------------
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: str | Path | None) -> str | None:
    """Content identity for an explicitly supplied database override."""
    if path is None:
        return None
    candidate = Path(path)
    h = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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
    sentence_matches = list(re.finditer(r"[^.!?…]+[.!?…]?", body))
    sentences = [
        {
            "id": f"sentence-{index + 1}",
            "text": match.group(0).strip(),
            "char_start": match.start(),
            "char_end": match.end(),
        }
        for index, match in enumerate(sentence_matches)
        if match.group(0).strip()
    ]
    terms = retrieval.tokenize(body)
    return {
        "anchor_id": anchor_id,
        "body_uk": body,
        "hash": _sha(body),
        "source": source,
        # This is content identity only — no external URL or teacher identity.
        "content_fingerprint": _sha(body),
        "char_len": len(body),
        # The snapshot is immutable input to generation.  Lemmas/numerals are
        # completed from the pinned grounding pack below; the remaining fields
        # require no model or mutable external state.
        "spans": [
            {
                "id": sentence["id"],
                "char_start": sentence["char_start"],
                "char_end": sentence["char_end"],
            }
            for sentence in sentences
        ],
        "sentences": sentences,
        "terms": sorted(set(terms)),
        "lemmas": [],
        "numerals": [],
        "candidate_facts": [sentence["text"] for sentence in sentences],
        "candidate_forms": sorted(set(terms)),
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
    """Content digest of every local generation, gate, and selection decision.

    A change to gate LOGIC must reshuffle the bake fingerprint even when every
    declared input is identical — otherwise cached raw generation would be
    silently reused under different decision code. Hashes the gate modules and
    Wave-0 orchestration modules by content, keyed by relative path for
    stability.
    """
    gate_files = [
        *(paths.ENGINE_DIR / "gates").glob("*.py"),
        paths.ENGINE_DIR / "retrieval.py",
        paths.ENGINE_DIR / "generate.py",
        paths.ENGINE_DIR / "pipeline.py",
        paths.ENGINE_DIR / "registry.py",
        paths.ENGINE_DIR / "selector.py",
    ]
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
    count_plan: dict[str, int] | None = None,
    registry_versions: dict | None = None,
    selector_policy: dict | None = None,
    max_regeneration_attempts: int = 0,
    atlas_override_digest: str | None = None,
    pipeline_mode: str = "candidate-bank.v1",
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
        "requested_type_count_plan": count_plan or {activity_type: 1 for activity_type in types},
        "grounding_digest": _sha(grounding_text),
        "prompt_digest": _sha(prompt_template),
        "registry": registry_versions or registry.registry_fingerprint(types),
        "selector_policy": selector_policy or selector.DEFAULT_POLICY.as_dict(),
        "max_regeneration_attempts": max_regeneration_attempts,
        "pipeline_mode": pipeline_mode,
        "gate_impl_digest": _gate_impl_digest(),
        "packages": _package_versions(),
        "vendor": vendoring.artifact_versions(),
        "data_bundle": data.active_bundle().digests(),
        "database_overrides": {"atlas_db_sha256": atlas_override_digest},
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


# Per-item (collection-index) gate locators look like "items[3]" / "pairs[1]".
# Everything else ("text", None) is an ACTIVITY-level locator.
_ITEM_LOCATOR_RE = re.compile(r"^(items|pairs)\[(\d+)\]$")

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


def _project_and_validate(
    activity: dict,
    gr: schema.GateResult,
    projector: Callable[[schema.HramatkaActivity], dict],
) -> dict | None:
    """Project + jsonschema-validate a (possibly filtered) activity. Returns
    the clean b1 item, or None (recording a 'schema' fail) on drift."""
    try:
        projected = projector(schema.HramatkaActivity(activity=activity))
        schema.validate_b1(projected)
        return projected
    except schema.B1ValidationError as exc:
        gr.add("schema", "fail", str(exc))
        return None


def reject_raw_candidate(
    raw_activity: object,
    provenance: dict,
    *,
    candidate_id: str,
    detail: str,
) -> schema.HramatkaActivity:
    """Retain an invalid raw candidate with a first-class rejection reason."""
    if isinstance(raw_activity, dict):
        clean, evidence = schema.parse_raw_activity(raw_activity)
        raw_copy = dict(raw_activity)
    else:
        clean, evidence, raw_copy = {"type": "unknown"}, [], {"value": raw_activity}
    ir = schema.HramatkaActivity(
        activity=clean,
        evidence=evidence,
        provenance=dict(provenance),
        raw_candidate=raw_copy,
        candidate_id=candidate_id,
    )
    ir.gate_result.add("raw_contract", "fail", detail)
    ir.gate_result.status = schema.DISPOSITION_REJECTED
    return ir


def gate_activity(
    raw_activity: dict,
    anchor_body: str,
    provenance: dict,
    atlas_lookup: dict | None = None,
    *,
    candidate_id: str | None = None,
) -> schema.HramatkaActivity:
    """Run the per-type gate chain, then PER-ITEM granularity (approved
    design): a per-statement/per-pair gate FAIL drops only that item/pair
    (carried in `ir.flagged` for the review sheet), not the whole activity.
    WARN items still ship (teacher-confirm). Activity-level gates (schema
    validity, cloze gap/answer structure) stay all-or-nothing; the whole
    activity is dropped only when an activity-level gate fails or too few
    items survive (match-up needs >=2). `gate_result.status` is the tri-state
    disposition: `rejected` == dropped; `ready` / `review_required` retain the
    (possibly-filtered) candidate for selection or the review tray.
    """
    activity_type = raw_activity.get("type") if isinstance(raw_activity, dict) else None
    entry = registry.ACTIVITY_REGISTRY.get(activity_type)
    if entry is None:
        return reject_raw_candidate(
            raw_activity,
            provenance,
            candidate_id=candidate_id or "candidate-unknown",
            detail=(
                f"Unsupported activity type {activity_type!r} for registry "
                f"{registry.REGISTRY_VERSION}."
            ),
        )
    contract_errors = entry.raw_validator(raw_activity)
    if contract_errors:
        return reject_raw_candidate(
            raw_activity,
            provenance,
            candidate_id=candidate_id or "candidate-unknown",
            detail="; ".join(contract_errors),
        )

    clean, evidence = schema.parse_raw_activity(raw_activity)
    if {evidence_item.locator for evidence_item in evidence} != set(
        entry.evidence_locator(raw_activity)
    ):
        return reject_raw_candidate(
            raw_activity,
            provenance,
            candidate_id=candidate_id or "candidate-unknown",
            detail="evidence locators do not match the registered raw contract",
        )
    ir = schema.HramatkaActivity(
        activity=clean,
        evidence=evidence,
        provenance=dict(provenance),
        raw_candidate=dict(raw_activity),
        candidate_id=candidate_id,
    )
    gr = ir.gate_result
    a_type = clean.get("type")
    entry.gate(clean, evidence, anchor_body, gr, atlas_lookup)
    per_item, activity_status = _locator_statuses(gr)

    # Activity-level FAIL (cloze text gates, etc.) -> drop the whole activity.
    if activity_status == "fail":
        gr.status = schema.GATE_FAILED
        return ir

    coll_key = entry.partition_key
    if coll_key is None:
        # Non-partitionable (cloze): all-or-nothing. No activity-level fail
        # above, so it ships; still project+validate for schema safety.
        projected = _project_and_validate(clean, gr, entry.public_projector)
        if projected is None:
            gr.status = schema.GATE_FAILED
            return ir
        ir.activity = projected
        gr.status = _ship_status(gr, None, False)
        return ir

    # Partition the collection: keep non-failing items, flag the failing ones.
    kept, kept_indices, flagged = [], [], []
    for i, item in enumerate(clean.get(coll_key, [])):
        loc = f"{coll_key}[{i}]"
        if per_item.get(loc) == "fail":
            flagged.append({"locator": loc, "reasons": _fail_reasons(gr, loc), "item": item})
        else:
            kept.append(item)
            kept_indices.append(i)
    ir.flagged = flagged

    min_items = entry.minimum_survivors
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
    # The public activity has compacted collection indices.  Keep evidence in
    # the same coordinate system and omit discarded-item spans so selector
    # coverage and evidence/answer de-duplication describe only survivors.
    remapped_evidence: list[schema.Evidence] = []
    original_to_filtered = {
        f"{coll_key}[{original}]": f"{coll_key}[{filtered_index}]"
        for filtered_index, original in enumerate(kept_indices)
    }
    for evidence_item in evidence:
        remapped_locator = original_to_filtered.get(evidence_item.locator)
        if remapped_locator is not None:
            evidence_item.locator = remapped_locator
            remapped_evidence.append(evidence_item)
        elif not _ITEM_LOCATOR_RE.match(evidence_item.locator):
            remapped_evidence.append(evidence_item)
    ir.evidence = remapped_evidence
    projected = _project_and_validate(filtered, gr, entry.public_projector)
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
def _run(
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
    count_plan: dict[str, int] | None = None,
    max_regeneration_attempts: int = 0,
    _candidate_generator: Callable[..., list[object]],
    _pipeline_mode: str,
) -> PipelineResult:
    """Run a deliberately selected pipeline mode.

    This is private so the production entrypoint cannot inject the measurement
    generator or its legacy selection mode.
    """
    types = list(types or EXTRACTIVE_TYPES)
    registry.entries_for(types)
    plan = {activity_type: int((count_plan or {}).get(activity_type, 1)) for activity_type in types}
    if any(count < 1 for count in plan.values()):
        raise ValueError("count_plan values must be positive integers")
    selection_policy = selector.SelectorPolicy(density_target=sum(plan.values()))
    atlas_override_digest = _file_sha256(atlas_db)
    snap = snapshot_anchor(anchor)
    grounding = retrieval.build_grounding_pack(snap["body_uk"], level, atlas_db=atlas_db)
    snap["lemmas"] = sorted(grounding["lemmas"])
    snap["numerals"] = grounding["numeral_inventory"]
    template = load_extractive_template()
    fp_inputs = fingerprint_inputs(
        anchor_hash=snap["hash"],
        level=level,
        pedagogy=pedagogy,
        phase=phase,
        types=types,
        grounding_text=grounding["text"],
        prompt_template=template,
        count_plan=plan,
        registry_versions=registry.registry_fingerprint(types),
        selector_policy=selection_policy.as_dict(),
        max_regeneration_attempts=max_regeneration_attempts,
        atlas_override_digest=atlas_override_digest,
        pipeline_mode=_pipeline_mode,
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

    # ---- generate typed candidates (cache-first) ---------------------
    raw_batches: list[tuple[list[str], list[object]]] = []
    if use_cache and cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(cached, list):  # extractive-v1 cache compatibility
            raw_batches = [(types, cached)]
        elif isinstance(cached, dict) and isinstance(cached.get("batches"), list):
            for batch in cached["batches"]:
                if not isinstance(batch, dict):
                    continue
                bank = batch.get("types")
                activities = batch.get("activities")
                if isinstance(bank, list) and isinstance(activities, list):
                    raw_batches.append(([str(activity_type) for activity_type in bank], activities))
    else:
        try:
            raw_batches = [(types, _candidate_generator(
                snap["body_uk"],
                level,
                types,
                generator=generator,
                grounding_pack=grounding["text"],
            ))]
        except (GeneratorUnavailable, GenerationUnparseable) as exc:
            result.generation_error = f"{type(exc).__name__}: {exc}"

    # ---- validate raw candidates, gate, and partition ----------------
    # Wire the grounding atlas lookup into every lexical gate (Sol defect 5),
    # extended to cover the model's INTRODUCED lexicon so a generated russianism
    # is caught too, not only one quoted from the anchor.
    def safe_for_grounding(raw: object) -> bool:
        if not isinstance(raw, dict):
            return False
        entry = registry.ACTIVITY_REGISTRY.get(raw.get("type"))
        return entry is not None and not entry.raw_validator(raw)

    raw_dicts = [
        raw
        for _bank, activities in raw_batches
        for raw in activities
        if safe_for_grounding(raw)
    ]
    atlas_lookup = retrieval.augmented_atlas_lookup(
        snap["body_uk"], raw_dicts, grounding["atlas_lookup"], atlas_db=atlas_db
    )
    # Treat the source as a quotation rather than a clean linguistic baseline.
    # These diagnostics belong in IR + the emitted private document, but do not
    # become task-language gate failures (Sol defect 7/8).
    snap["diagnostics"] = vesum_gate.anchor_baseline_diagnostics(
        snap["body_uk"], atlas_lookup=atlas_lookup
    )
    def append_candidate(
        raw: object,
        candidate_id: str,
        lookup: dict | None,
        expected_types: list[str],
    ) -> None:
        if not isinstance(raw, dict):
            ir = reject_raw_candidate(
                raw,
                provenance,
                candidate_id=candidate_id,
                detail="candidate must be a JSON object",
            )
        elif raw.get("type") not in expected_types:
            ir = reject_raw_candidate(
                raw,
                provenance,
                candidate_id=candidate_id,
                detail=(
                    f"candidate type {raw.get('type')!r} was emitted outside requested "
                    f"typed bank {expected_types!r}"
                ),
            )
        else:
            ir = gate_activity(
                raw,
                snap["body_uk"],
                provenance,
                atlas_lookup=lookup,
                candidate_id=candidate_id,
            )
        result.activities.append(ir)

    for batch_index, (bank, activities) in enumerate(raw_batches):
        for index, raw in enumerate(activities):
            append_candidate(raw, f"candidate-{batch_index}-{index:03d}", atlas_lookup, bank)
    result.regeneration_attempts = max(0, len(raw_batches) - 1)

    selector_phase = int(phase) if isinstance(phase, str) and phase.isdigit() else phase

    def partition_and_select() -> None:
        result.ready = [
            ir for ir in result.activities if ir.gate_result.status == schema.DISPOSITION_READY
        ]
        result.review_required = [
            ir
            for ir in result.activities
            if ir.gate_result.status == schema.DISPOSITION_REVIEW
        ]
        result.rejected = [
            ir
            for ir in result.activities
            if ir.gate_result.status == schema.DISPOSITION_REJECTED
        ]
        result.selected = selector.select_lesson(
            result.ready,
            count_plan=plan,
            phase=selector_phase if isinstance(selector_phase, int) else None,
            policy=selection_policy,
        )
        result.lesson_b1 = [ir.activity for ir in result.selected]

    partition_and_select()

    # Wave 0 keeps retries deliberately simple: callers opt in while later
    # waves acquire targeted per-type prompts.  Regenerated raw candidates are
    # still given stable ids, validated, and placed in the same partitions.
    for attempt in range(len(raw_batches), max_regeneration_attempts + 1):
        selected_counts = {
            activity_type: sum(
                ir.activity.get("type") == activity_type for ir in result.selected
            )
            for activity_type in types
        }
        unmet = [
            activity_type
            for activity_type, count in plan.items()
            if selected_counts[activity_type] < count
        ]
        if not unmet or result.generation_error:
            break
        try:
            regenerated = _candidate_generator(
                snap["body_uk"],
                level,
                unmet,
                generator=generator,
                grounding_pack=grounding["text"],
            )
        except (GeneratorUnavailable, GenerationUnparseable) as exc:
            result.generation_error = f"{type(exc).__name__}: {exc}"
            break
        raw_batches.append((unmet, regenerated))
        retry_dicts = [
            raw
            for _bank, activities in raw_batches
            for raw in activities
            if safe_for_grounding(raw)
        ]
        retry_lookup = retrieval.augmented_atlas_lookup(
            snap["body_uk"], retry_dicts, grounding["atlas_lookup"], atlas_db=atlas_db
        )
        for index, raw in enumerate(regenerated):
            append_candidate(raw, f"candidate-{attempt}-{index:03d}", retry_lookup, unmet)
        result.regeneration_attempts = attempt
        partition_and_select()

    if use_cache and not result.generation_error:
        cache_file.write_text(
            json.dumps(
                {
                    "format": "candidate-bank.v1",
                    "batches": [
                        {"types": bank, "activities": activities}
                        for bank, activities in raw_batches
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

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
                "dispositions": {
                    "ready": [ir.candidate_id for ir in result.ready],
                    "review_required": [ir.candidate_id for ir in result.review_required],
                    "rejected": [ir.candidate_id for ir in result.rejected],
                    "selected": [ir.candidate_id for ir in result.selected],
                },
                "regeneration_attempts": result.regeneration_attempts,
                "activities": [ir.as_dict() for ir in result.activities],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    result.out_files = {"lesson_b1": str(b1_path), "lesson_ir": str(ir_path)}
    return result


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
    count_plan: dict[str, int] | None = None,
    max_regeneration_attempts: int = 0,
) -> PipelineResult:
    """Run the production typed candidate-bank pipeline end-to-end.

    Only ``ready`` candidates reach ``lesson.b1.json``; review material remains
    in private IR and cannot be auto-included. The measurement baseline uses a
    separate, explicitly guarded entrypoint below.
    """
    return _run(
        anchor,
        level,
        types,
        pedagogy=pedagogy,
        phase=phase,
        generator=generator,
        out_dir=out_dir,
        atlas_db=atlas_db,
        use_cache=use_cache,
        cache_dir=cache_dir,
        count_plan=count_plan,
        max_regeneration_attempts=max_regeneration_attempts,
        _candidate_generator=generate,
        _pipeline_mode="candidate-bank.v1",
    )


def run_baseline_v1(
    anchor: str | dict,
    level: str = "B1",
    types: list[str] | None = None,
    *,
    measurement_only: Literal[True],
    **kwargs,
) -> PipelineResult:
    """Run the versioned extractive-v1 baseline for like-for-like measurement.

    This is intentionally not the production selection path: it recreates the
    pre-Wave-0 delivery rule in which every non-rejected activity entered the
    lesson. ``measurement_only=True`` is required because the result includes
    review-tray items that the normal ``run`` path can never assemble.
    """
    if measurement_only is not True:
        raise ValueError("run_baseline_v1 is restricted to measurement_only=True")
    from .generate import generate_baseline_v1

    result = _run(
        anchor,
        level,
        types,
        _candidate_generator=generate_baseline_v1,
        _pipeline_mode="extractive-v1.baseline",
        **kwargs,
    )
    result.selected = [ir for ir in result.activities if ir.gate_result.passed]
    result.lesson_b1 = [ir.activity for ir in result.selected]
    b1_path = Path(result.out_files["lesson_b1"])
    b1_path.write_text(json.dumps(result.lesson_b1, ensure_ascii=False, indent=2), encoding="utf-8")
    ir_path = Path(result.out_files["lesson_ir"])
    persisted = json.loads(ir_path.read_text(encoding="utf-8"))
    persisted["baseline_version"] = "extractive-v1"
    persisted["dispositions"]["selected"] = [ir.candidate_id for ir in result.selected]
    ir_path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
