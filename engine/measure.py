"""Step-7 measurement harness and rater-visible report.

The harness deliberately keeps measurement concerns in the engine lane.  It
does not tune generation or gates: it runs the existing pipeline, records the
tri-state disposition of every generated activity, and exposes enough
provenance for a rater to check the hard claims without opening raw IR.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import content_density, paths, pipeline, retrieval, schema
from .gates import numeral
from .generate import call_gemma

_DEFAULT_NUMERAL_BANK_PATH = Path(__file__).with_name("numeral-regression-bank.json")
_TRI_STATES = (schema.GATE_CLEAN, schema.GATE_REVIEW, schema.GATE_FAILED)
_REQUIRED_DETERMINISM_CLASSES = ("R", "T", "P", "A")
_HERITAGE_DETAIL = re.compile(r"heritage/russianism='(?P<heritage>[^']+)'", re.IGNORECASE)

# These regressions landed in #57/#61 after the original JSON corpus.  They
# live here rather than changing the gate-owned corpus file: this is the
# versioned measurement overlay that proves the real bake failures stay wired
# through the Step-7 bank.
_BAKE_REGRESSION_NUMERAL_BANK: tuple[dict[str, Any], ...] = (
    {
        "id": "r2-nested-magnitude",
        "class": "bake-regression",
        "phrase": "двадцять тисяч гривень",
        "accepted_statuses": ["pass"],
    },
    {
        "id": "r2-blyzko-nested-magnitude",
        "class": "bake-regression",
        "phrase": "близько двадцяти тисяч гривень",
        "accepted_statuses": ["pass"],
    },
    {
        "id": "r2-word-range",
        "class": "bake-regression",
        "phrase": "дві-три хвилини",
        "accepted_statuses": ["pass"],
    },
    {
        "id": "r2-collective-predicative",
        "class": "bake-regression",
        "phrase": "обоє працюємо",
        "accepted_statuses": ["pass"],
    },
    {
        "id": "r3-compound-final-head",
        "class": "bake-regression",
        "phrase": "двадцять тисяч п'ять гривень",
        "accepted_statuses": ["pass"],
    },
    {
        "id": "r3-range-magnitude",
        "class": "bake-regression",
        "phrase": "дві-три тисячі гривень",
        "accepted_statuses": ["pass"],
    },
    {
        "id": "r3-long-compound",
        "class": "bake-regression",
        "phrase": "одна тисяча двісті тридцять чотири книги",
        "accepted_statuses": ["pass"],
    },
)
_BAKE_REGRESSION_NEGATIVE_BANK: tuple[dict[str, Any], ...] = (
    {
        "id": "r2-blyzko-bad-government",
        "class": "bake-regression",
        "phrase": "Близько три годин",
        "accepted_statuses": ["fail"],
    },
)


def default_numeral_bank() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load the gate corpus with the #57/#61 bake-regression overlay.

    A positive row is accepted when it does not hard-fail.  Deliberately
    context-dependent legal constructions retain their documented ``warn``
    state; the report never relabels those as clean.
    """
    try:
        payload = json.loads(_DEFAULT_NUMERAL_BANK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Cannot load numeral regression bank at {_DEFAULT_NUMERAL_BANK_PATH}."
        ) from exc

    positive = payload.get("positive")
    negative = payload.get("negative")
    if not isinstance(positive, list) or not isinstance(negative, list):
        raise ValueError("Numeral regression bank must contain positive and negative lists.")
    return [*positive, *_BAKE_REGRESSION_NUMERAL_BANK], [
        *negative,
        *_BAKE_REGRESSION_NEGATIVE_BANK,
    ]


def _bank_entry(entry: object, *, positive: bool) -> dict[str, Any]:
    """Normalize legacy tuple probes and metadata-rich corpus entries."""
    default_statuses = ["pass"] if positive else ["fail"]
    if isinstance(entry, tuple) and len(entry) == 2:
        phrase, context_case = entry
        return {
            "phrase": phrase,
            "context_case": context_case,
            "accepted_statuses": default_statuses,
        }
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid numeral bank entry: {entry!r}")

    phrase = entry.get("phrase")
    context_case = entry.get("context_case")
    accepted_statuses = entry.get("accepted_statuses", default_statuses)
    if (
        not isinstance(phrase, str)
        or context_case is not None
        and not isinstance(context_case, str)
        or not isinstance(accepted_statuses, list)
        or not accepted_statuses
        or not all(isinstance(status, str) for status in accepted_statuses)
    ):
        raise ValueError(f"Invalid numeral bank entry: {entry!r}")
    return {
        "id": entry.get("id"),
        "class": entry.get("class"),
        "phrase": phrase,
        "context_case": context_case,
        "accepted_statuses": accepted_statuses,
    }


def _numeral_bank_recall(positive: list[object], negative: list[object]) -> dict[str, Any]:
    """Run the numeral moat corpus, retaining clean/warn/fail evidence."""
    pos_rows, neg_rows = [], []
    pos_ok = pos_clean = pos_warn = neg_ok = 0
    for entry in positive:
        probe = _bank_entry(entry, positive=True)
        verdict = numeral.check_numeral_government(probe["phrase"], probe["context_case"])
        ok = verdict["status"] in probe["accepted_statuses"]
        pos_ok += ok
        pos_clean += int(verdict["status"] == "pass")
        pos_warn += int(verdict["status"] == "warn")
        pos_rows.append(
            {
                **probe,
                "ctx": probe["context_case"],
                "status": verdict["status"],
                "rule": verdict["rule"],
                "ok": ok,
            }
        )
    for entry in negative:
        probe = _bank_entry(entry, positive=False)
        verdict = numeral.check_numeral_government(probe["phrase"], probe["context_case"])
        ok = verdict["status"] in probe["accepted_statuses"]
        neg_ok += ok
        neg_rows.append(
            {
                **probe,
                "ctx": probe["context_case"],
                "status": verdict["status"],
                "rule": verdict["rule"],
                "ok": ok,
            }
        )
    return {
        "positive_accept": pos_ok,
        "positive_total": len(positive),
        "positive_clean": pos_clean,
        "positive_review_required": pos_warn,
        "negative_reject": neg_ok,
        "negative_total": len(negative),
        "positive_rows": pos_rows,
        "negative_rows": neg_rows,
    }


def _empty_tri_state_counts() -> dict[str, int]:
    return {status: 0 for status in _TRI_STATES}


def _tri_state_counts(activities: list[Any]) -> dict[str, int]:
    counts = _empty_tri_state_counts()
    for ir in activities:
        counts[ir.gate_result.status] += 1
    return counts


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_sha() -> str:
    """Return the checked-out engine revision without making git a runtime dependency."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=paths.ENGINE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"
    return result.stdout.strip() or "unavailable"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1)
    return round(ordered[index], 6)


def _run_bake(
    anchor: str | dict[str, Any],
    *,
    generator: Callable[[str], str],
    out_dir: Path,
    cache_dir: Path | None,
    use_cache: bool,
) -> tuple[pipeline.PipelineResult, dict[str, Any]]:
    """Run one bake with wall-clock and retry accounting at the seam."""
    calls = 0

    def counted_generator(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return generator(prompt)

    started = time.perf_counter()
    result = pipeline.run(
        anchor,
        generator=counted_generator,
        out_dir=out_dir,
        cache_dir=cache_dir,
        use_cache=use_cache,
    )
    wall_clock_seconds = round(time.perf_counter() - started, 6)
    return result, {
        "wall_clock_seconds": wall_clock_seconds,
        # The locked AIS Gemma route is explicitly $0.  Providers expose no
        # token telemetry, so this is a declared route cost rather than a
        # fabricated estimate.
        "cost_usd": 0.0,
        "cost_basis": "locked google-ais/gemma-4-31b-it route ($0)",
        "generator_calls": calls,
        "retry_count": max(0, calls - 1),
        "cache_enabled": use_cache,
        "generation_error": result.generation_error,
    }


def _gate_checks(ir: Any) -> list[dict[str, Any]]:
    return [
        {
            "gate": check.gate,
            "status": check.status,
            "locator": check.locator,
            "detail": check.detail,
        }
        for check in ir.gate_result.checks
    ]


def _failed_gate(ir: Any) -> str:
    for check in ir.gate_result.checks:
        if check.status == "fail" and check.gate:
            return check.gate
    return "unknown"


_NOTE_REASONS = {
    "evidence_span": "опору не вдалося повністю підтвердити",
    "false_statement": "хибне твердження потребує підтвердження",
    "vesum_token": "є неперевірена або позначена словоформа",
    "numeral": "є попередження щодо числівника",
    "matchup_semantics": "зв’язок у парі потребує звірки",
    "matchup_left": "ліве слово не підтверджено опорою",
    "external_options": "є варіанти поза текстом опори",
    "cloze_answer": "відповідь у пропуску потребує звірки",
    "cloze_gap": "у вправі бракує коректного пропуску",
    "partition": "частину вправи вилучено після перевірки",
    "schema": "структура вправи потребує звірки",
}


def _teacher_note(ir: Any) -> str:
    status = ir.gate_result.status
    if status == schema.GATE_CLEAN:
        return "Згенеровано з опори; гейти чисті — звірте перед уроком."
    if status == schema.GATE_FAILED:
        return "Вправу не показано: автоматична перевірка її відхилила."
    gates: list[str] = []
    for check in ir.gate_result.checks:
        if check.status == "warn" and check.gate not in gates:
            gates.append(check.gate)
    for flag in ir.flagged:
        for reason in flag.get("reasons", []):
            gate = reason.get("gate")
            if isinstance(gate, str) and gate not in gates:
                gates.append(gate)
    reasons = "; ".join(
        _NOTE_REASONS.get(gate, "є попередження автоматичної перевірки") for gate in gates
    )
    return f"Згенеровано з опори; перевірте: {reasons or _NOTE_REASONS['partition']} — підтвердьте перед прийняттям."


def _evidence_rows(ir: Any) -> list[dict[str, Any]]:
    checks = _gate_checks(ir)
    rows = []
    for evidence in ir.evidence:
        row = {
            "locator": evidence.locator,
            "quote": evidence.quote,
            "char_start": evidence.char_start,
            "char_end": evidence.char_end,
            "kind": evidence.kind,
        }
        if evidence.char_start is None or evidence.char_end is None:
            failures = [
                check["detail"]
                for check in checks
                if check["gate"] == "evidence_span"
                and check["status"] == "fail"
                and check["locator"] == evidence.locator
            ]
            row["located_failure_reason"] = "; ".join(failures) or "evidence was not located"
        rows.append(row)
    # A model can omit an evidence field altogether.  Emit a rater-visible
    # absent row rather than making the only failure explanation live in an
    # unrelated gate table.
    covered = {row["locator"] for row in rows}
    for check in checks:
        if (
            check["gate"] == "evidence_span"
            and check["status"] == "fail"
            and check["locator"] not in covered
        ):
            rows.append(
                {
                    "locator": check["locator"],
                    "quote": None,
                    "char_start": None,
                    "char_end": None,
                    "kind": "absent",
                    "located_failure_reason": check["detail"],
                }
            )
    return rows


def _rejected_rows(ir: Any) -> list[dict[str, Any]]:
    if ir.gate_result.status == schema.GATE_FAILED:
        return [
            {
                "kind": "rejected",
                "locator": None,
                "machine_reason_class": f"gate-failed:{_failed_gate(ir)}",
                "content": ir.activity,
            }
        ]
    rows = []
    for flagged in ir.flagged:
        reasons = flagged.get("reasons") or []
        gate = next(
            (
                reason["gate"]
                for reason in reasons
                if isinstance(reason.get("gate"), str) and reason["gate"]
            ),
            "unknown",
        )
        rows.append(
            {
                "kind": "flagged",
                "locator": flagged.get("locator"),
                "machine_reason_class": f"gate-failed:{gate}",
                "content": flagged.get("item"),
                "reasons": reasons,
            }
        )
    return rows


def _activity_report(ir: Any, anchor: dict[str, Any]) -> dict[str, Any]:
    status = ir.gate_result.status
    gates_run = sorted({check.gate for check in ir.gate_result.checks}) or ["gated"]
    review = status == schema.GATE_REVIEW
    return {
        "activity": ir.activity,
        "gate_outcome": status,
        "gate_checks": _gate_checks(ir),
        "mark": "warn" if review else "ok" if status == schema.GATE_CLEAN else "blocked",
        "note": _teacher_note(ir),
        "block_provenance": {
            "anchor": {
                "anchor_id": anchor["anchor_id"],
                "source": anchor["source"],
                "content_fingerprint": anchor["content_fingerprint"],
            },
            "generated": {
                "generator": ir.provenance.get("generator"),
                "fingerprint": ir.provenance.get("fingerprint"),
            },
            "teacher": {"edited": False},
            "gates_run": gates_run,
        },
        "activity_provenance": {
            "generation_history": [dict(ir.provenance)],
            "external_options": review,
        },
        "evidence": _evidence_rows(ir),
        "rejected": _rejected_rows(ir),
        "rater_controls": ["accept", "edit-minor", "edit-content", "reject"],
    }


def _salvage_counts(activities: list[Any]) -> dict[str, int]:
    rejected = [ir for ir in activities if ir.gate_result.status == schema.GATE_FAILED]
    visible = [ir for ir in activities if ir.gate_result.status != schema.GATE_FAILED]
    flagged = [ir for ir in visible if ir.flagged]
    shipped = [ir for ir in visible if not ir.flagged]
    counts = {
        "generated": len(activities),
        "shipped": len(shipped),
        "flagged": len(flagged),
        "rejected": len(rejected),
    }
    assert counts["generated"] == sum(counts[name] for name in ("shipped", "flagged", "rejected"))
    return counts


def _mark_distribution(activities: list[Any]) -> dict[str, int]:
    """Count the teacher-visible marks only for activities that ship."""
    counts = {"ok": 0, "warn": 0}
    for ir in activities:
        if ir.gate_result.status == schema.GATE_CLEAN:
            counts["ok"] += 1
        elif ir.gate_result.status == schema.GATE_REVIEW:
            counts["warn"] += 1
    counts["shipped"] = counts["ok"] + counts["warn"]
    return counts


def _is_russianism_warning(check: Any) -> bool:
    """Do not relabel every VESUM warning as a Russianism or calque."""
    if check.gate != "vesum_token" or check.status != "warn":
        return False
    match = _HERITAGE_DETAIL.search(check.detail)
    return match is not None and match["heritage"].casefold() in {"russianism", "calque"}


def _russianism_counts(activities: list[Any]) -> dict[str, Any]:
    warned = 0
    for ir in activities:
        if any(_is_russianism_warning(check) for check in ir.gate_result.checks):
            warned += 1
    return {
        "warned_items": warned,
        "total_items": len(activities),
        "warn_rate": _rate(warned, len(activities)),
    }


def _iter_generated_units(ir: Any) -> list[tuple[str, Any]]:
    """Yield generated task units, including salvaged-out content exactly once."""
    activity = ir.activity
    a_type = activity.get("type")
    if a_type == "true-false":
        units = [(f"items[{index}]", item) for index, item in enumerate(activity.get("items", []))]
    elif a_type == "match-up":
        units = [(f"pairs[{index}]", pair) for index, pair in enumerate(activity.get("pairs", []))]
    else:
        units = [("activity", activity)]
    # A fully rejected activity still contains its original generated content.
    # Its flagged partition is diagnostic detail, not a second generated unit.
    if ir.gate_result.status != schema.GATE_FAILED:
        units.extend((str(flag.get("locator")), flag.get("item")) for flag in ir.flagged)
    return units


def _task_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for element in value for text in _task_strings(element)]
    if not isinstance(value, dict):
        return []
    return [
        text
        for key, element in value.items()
        if key not in {"evidence", "id"}
        for text in _task_strings(element)
    ]


def _numeral_transfer(activities: list[Any], anchor_id: str) -> dict[str, Any]:
    """Measure pass/warn/fail transfer on generated task language, not anchors."""
    counts = {"pass": 0, "warn": 0, "fail": 0}
    rows = []
    for activity_index, ir in enumerate(activities):
        for locator, unit in _iter_generated_units(ir):
            phrases = []
            for text in _task_strings(unit):
                for inventory in retrieval.extract_numeral_inventory(text):
                    phrase = inventory["raw_span"]
                    verdict = numeral.check_numeral_government(phrase)
                    phrases.append(
                        {
                            "phrase": phrase,
                            "status": verdict["status"],
                            "rule": verdict["rule"],
                        }
                    )
            if not phrases:
                continue
            status = max(
                phrases, key=lambda item: {"pass": 0, "warn": 1, "fail": 2}[item["status"]]
            )["status"]
            counts[status] += 1
            rows.append(
                {
                    "anchor_id": anchor_id,
                    "activity_index": activity_index,
                    "locator": locator,
                    "status": status,
                    "phrases": phrases,
                }
            )
    total = sum(counts.values())
    return {
        "numeral_bearing_items": total,
        **counts,
        "pass_rate": _rate(counts["pass"], total),
        "warn_rate": _rate(counts["warn"], total),
        "fail_rate": _rate(counts["fail"], total),
        "items": rows,
    }


def _merge_transfer(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"pass": 0, "warn": 0, "fail": 0}
    items = []
    for row in rows:
        for status in counts:
            counts[status] += row[status]
        items.extend(row["items"])
    total = sum(counts.values())
    return {
        "numeral_bearing_items": total,
        **counts,
        "pass_rate": _rate(counts["pass"], total),
        "warn_rate": _rate(counts["warn"], total),
        "fail_rate": _rate(counts["fail"], total),
        "items": items,
    }


def _fixture_generator(activity: dict[str, Any]) -> Callable[[str], str]:
    payload = json.dumps({"activities": [activity]}, ensure_ascii=False)
    return lambda _prompt: payload


def _russianism_fixture_specs() -> tuple[dict[str, Any], ...]:
    quoted_anchor = "Багато людей щомісяця отримують получку і телевізор."
    quoted = {
        "type": "true-false",
        "instruction": "Познач правильні твердження за текстом.",
        "items": [
            {
                "statement": "Багато людей отримують получку.",
                "correct": True,
                "evidence": "Багато людей щомісяця отримують получку",
            }
        ],
    }
    introduced_anchor = (
        "Третина українців за рік не прочитує жодної книжки, зате дві третини мають телевізор."
    )
    introduced = {
        "type": "true-false",
        "instruction": "Познач правильні твердження за текстом.",
        "items": [
            {
                "statement": "Третина українців втратили получку.",
                "correct": True,
                "evidence": "Третина українців за рік не прочитує жодної книжки",
            }
        ],
    }
    return (
        {
            "id": "quoted-anchor-russianism",
            "anchor": quoted_anchor,
            "generator": _fixture_generator(quoted),
            "generated_task_language": False,
        },
        {
            "id": "generated-task-russianism",
            "anchor": introduced_anchor,
            "generator": _fixture_generator(introduced),
            "generated_task_language": True,
        },
    )


def _russianism_fixture_report(
    *,
    out_dir: Path,
    cache_dir: Path | None,
    use_cache: bool,
) -> tuple[dict[str, Any], pipeline.PipelineResult | None]:
    rows = []
    first_result: pipeline.PipelineResult | None = None
    for index, fixture in enumerate(_russianism_fixture_specs()):
        result, bake = _run_bake(
            fixture["anchor"],
            generator=fixture["generator"],
            out_dir=out_dir / f"fixture-{index:02d}",
            cache_dir=cache_dir / f"fixture-{index:02d}" if cache_dir else None,
            use_cache=use_cache,
        )
        first_result = first_result or result
        caught = any(
            ir.gate_result.status == schema.GATE_REVIEW
            and any(_is_russianism_warning(check) for check in ir.gate_result.checks)
            for ir in result.activities
        )
        rows.append(
            {
                "id": fixture["id"],
                "generated_task_language": fixture["generated_task_language"],
                "caught": caught,
                "bake": bake,
                "items": [_activity_report(ir, result.anchor) for ir in result.activities],
            }
        )
    caught = sum(row["caught"] for row in rows)
    generated_task_fixture_caught = any(
        row["generated_task_language"] and row["caught"] for row in rows
    )
    return (
        {
            "offline_seed_required": True,
            "fixture_total": len(rows),
            "catch_count": caught,
            "uncaught_count": len(rows) - caught,
            "generated_task_fixture_caught": generated_task_fixture_caught,
            "hard_gate_met": caught >= 2 and generated_task_fixture_caught,
            "hard_gate_reason": (
                None
                if caught >= 2 and generated_task_fixture_caught
                else "Seeded russianism fixture data is unavailable or did not reach a russianism/calque warning."
            ),
            "fixtures": rows,
        },
        first_result,
    )


def _normalise_anchor(anchor: str | dict[str, Any]) -> tuple[str | dict[str, Any], str | None]:
    if isinstance(anchor, str):
        return anchor, None
    if not isinstance(anchor, dict):
        raise TypeError("measure anchors must be strings or anchor dictionaries")
    record = dict(anchor)
    anchor_class = record.pop("measurement_class", record.pop("anchor_class", None))
    if anchor_class is not None and not isinstance(anchor_class, str):
        raise ValueError("anchor measurement_class must be a string")
    return record, anchor_class


def _determinism_report(
    anchors: list[str | dict[str, Any]],
    classes: list[str | None],
    probes: Mapping[str, int] | None,
    *,
    generator: Callable[[str], str],
    out_dir: Path,
    runs: int,
) -> dict[str, Any]:
    if runs < 2:
        raise ValueError("determinism_runs must be at least 2")
    selected = dict(probes or {})
    if not selected:
        for required in _REQUIRED_DETERMINISM_CLASSES:
            try:
                selected[required] = classes.index(required)
            except ValueError:
                continue

    per_class = {}
    for anchor_class, index in selected.items():
        if index < 0 or index >= len(anchors):
            raise ValueError(f"determinism probe {anchor_class!r} points outside the anchor list")
        runs_report = []
        for run_index in range(runs):
            result, bake = _run_bake(
                anchors[index],
                generator=generator,
                out_dir=out_dir / anchor_class / f"run-{run_index + 1}",
                cache_dir=out_dir / "cache-disabled",
                use_cache=False,
            )
            ir_bytes = Path(result.out_files["lesson_ir"]).read_bytes()
            runs_report.append(
                {
                    "run": run_index + 1,
                    "fingerprint": result.fingerprint,
                    "ir_sha256": _sha256_bytes(ir_bytes),
                    "bake": bake,
                }
            )
        fingerprints = {run["fingerprint"] for run in runs_report}
        ir_digests = {run["ir_sha256"] for run in runs_report}
        per_class[anchor_class] = {
            "anchor_index": index,
            "runs": runs_report,
            "fingerprint_identical": len(fingerprints) == 1,
            "ir_byte_identical": len(ir_digests) == 1,
            # Full lesson assembly is not the live runtime path yet; the
            # item-level report makes this explicit instead of claiming it.
            "lesson_json_byte_identical": None,
            "stable": len(fingerprints) == 1 and len(ir_digests) == 1,
        }
    complete = all(name in per_class for name in _REQUIRED_DETERMINISM_CLASSES)
    return {
        "cache_enabled": False,
        "runs_per_anchor": runs,
        "required_classes": list(_REQUIRED_DETERMINISM_CLASSES),
        "classes": per_class,
        "complete": complete,
        "stable": complete
        and all(per_class[name]["stable"] for name in _REQUIRED_DETERMINISM_CLASSES),
        "lesson_assembly": {
            "measured": False,
            "reason": "the real assembler is not the live measurement runtime path",
        },
    }


def _report_header(
    result: pipeline.PipelineResult | None,
    *,
    cache_enabled: bool,
    expected_pins: Mapping[str, Any] | None,
) -> dict[str, Any]:
    inputs = result.fingerprint_inputs if result else {}
    header = {
        "report_contract": "step7-5b-rater-observability-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "engine_sha": _git_sha(),
        "engine_version": inputs.get("engine_version"),
        "model": inputs.get("model"),
        "package_versions": inputs.get("packages", {}),
        "vendor_artifact_digests": inputs.get("vendor", {}),
        "data_bundle_digests": inputs.get("data_bundle", {}),
        "cache_enabled_for_anchor_bakes": cache_enabled,
        # §5.1/E6: A/B consumers receive the frozen duration floor oracle,
        # rather than duplicating mutable values in a run notebook.
        "floor_oracle_v1": content_density.floor_oracle_record(),
    }
    pin_fields = (
        "engine_sha",
        "engine_version",
        "model",
        "package_versions",
        "vendor_artifact_digests",
        "data_bundle_digests",
    )
    expected = dict(expected_pins) if expected_pins is not None else None
    matches = (
        {field: header[field] == expected.get(field) for field in pin_fields}
        if expected is not None
        else {}
    )
    header["slate_pin_assertion"] = {
        "configured": expected is not None,
        "expected": expected,
        "matches": matches,
        "pins_match": all(matches.values()) if expected is not None else None,
    }
    return header


def measure(
    anchors: list[str | dict[str, Any]],
    *,
    generator: Callable[[str], str] = call_gemma,
    out_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    numeral_bank: tuple[list[object], list[object]] | None = None,
    determinism_probes: Mapping[str, int] | None = None,
    determinism_runs: int = 2,
    include_russianism_fixtures: bool = True,
    expected_pins: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure anchors and write JSON + HTML rater reports.

    Anchor dictionaries may carry ``measurement_class`` (``R``, ``T``, ``P``,
    or ``A``).  If all four are supplied, one anchor per class is automatically
    repeated ``determinism_runs`` times with cache disabled.  Callers can
    instead select explicit zero-based ``determinism_probes``.
    """
    out = Path(out_dir) if out_dir else (paths.ENGINE_DIR / ".out" / "measure")
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(cache_dir) if cache_dir else None
    cache_enabled = cache is not None

    normalised = [_normalise_anchor(anchor) for anchor in anchors]
    pipeline_anchors = [anchor for anchor, _anchor_class in normalised]
    anchor_classes = [anchor_class for _anchor, anchor_class in normalised]
    per_anchor = []
    all_activities: list[Any] = []
    transfer_rows = []
    bake_times = []
    header_result: pipeline.PipelineResult | None = None

    for index, anchor in enumerate(pipeline_anchors):
        result, bake = _run_bake(
            anchor,
            generator=generator,
            out_dir=out / f"anchor{index:02d}",
            cache_dir=cache / f"anchor{index:02d}" if cache else None,
            use_cache=cache_enabled,
        )
        header_result = header_result or result
        all_activities.extend(result.activities)
        transfer = _numeral_transfer(result.activities, result.anchor["anchor_id"])
        transfer_rows.append(transfer)
        bake_times.append(bake["wall_clock_seconds"])
        per_anchor.append(
            {
                "anchor_id": result.anchor["anchor_id"],
                "measurement_class": anchor_classes[index],
                "char_len": result.anchor["char_len"],
                "anchor_provenance": {
                    "source": result.anchor["source"],
                    "content_fingerprint": result.anchor["content_fingerprint"],
                },
                "anchor_diagnostics": result.anchor.get("diagnostics", []),
                "fingerprint": result.fingerprint,
                "fingerprint_inputs": result.fingerprint_inputs,
                "bake": bake,
                "tri_state_counts": _tri_state_counts(result.activities),
                "salvage_counts": _salvage_counts(result.activities),
                "mark_distribution": _mark_distribution(result.activities),
                "russianism": _russianism_counts(result.activities),
                "numeral_transfer": transfer,
                "items": [_activity_report(ir, result.anchor) for ir in result.activities],
            }
        )

    if numeral_bank is None:
        numeral_bank = default_numeral_bank()
    bank = _numeral_bank_recall(numeral_bank[0], numeral_bank[1])

    russianism: dict[str, Any]
    if include_russianism_fixtures:
        russianism, fixture_result = _russianism_fixture_report(
            out_dir=out / "russianism-e2e",
            cache_dir=cache / "russianism-e2e" if cache else None,
            use_cache=cache_enabled,
        )
        header_result = header_result or fixture_result
    else:
        russianism = {
            "offline_seed_required": True,
            "fixture_total": 0,
            "catch_count": 0,
            "uncaught_count": 0,
            "generated_task_fixture_caught": False,
            "hard_gate_met": False,
            "hard_gate_reason": "Russianism fixture execution was disabled by caller.",
            "fixtures": [],
            "not_run_reason": "disabled by caller",
        }

    determinism = _determinism_report(
        pipeline_anchors,
        anchor_classes,
        determinism_probes,
        generator=generator,
        out_dir=out / "determinism",
        runs=determinism_runs,
    )
    transfer = _merge_transfer(transfer_rows)
    tri_state_counts = _tri_state_counts(all_activities)
    report = {
        "header": _report_header(
            header_result,
            cache_enabled=cache_enabled,
            expected_pins=expected_pins,
        ),
        "anchors": per_anchor,
        "kpi": {
            "total_items": len(all_activities),
            "tri_state_counts": tri_state_counts,
            "tri_state_rates": {
                status: _rate(count, len(all_activities))
                for status, count in tri_state_counts.items()
            },
            "mark_distribution": _mark_distribution(all_activities),
            "russianism_real_anchors": _russianism_counts(all_activities),
            "numeral_transfer": transfer,
            "wall_clock_seconds": {
                "bake_count": len(bake_times),
                "p50": _percentile(bake_times, 0.5),
                "p95": _percentile(bake_times, 0.95),
            },
            "cost_usd": {"per_bake": 0.0, "total": 0.0},
        },
        "numeral_bank": bank,
        "russianism_e2e": russianism,
        "determinism": determinism,
    }

    html_path = out / "measure-report.html"
    json_path = out / "measure-report.json"
    report["out_files"] = {"html": str(html_path), "json": str(json_path)}
    html_path.write_text(render_html(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _json(value: Any) -> str:
    return _esc(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _render_gate_checks(checks: list[dict[str, Any]]) -> str:
    rows = []
    for check in checks:
        cls = {"pass": "ok", "warn": "warn", "fail": "bad"}.get(check["status"], "")
        rows.append(
            f"<tr class='{cls}'><td>{_esc(check['gate'])}</td><td>{_esc(check['status'])}</td>"
            f"<td>{_esc(check.get('locator') or '')}</td><td>{_esc(check['detail'])}</td></tr>"
        )
    return (
        "<table class='gates'><tr><th>gate</th><th>status</th><th>locator</th><th>detail</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _render_evidence(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "<p>No evidence spans were emitted.</p>"
    rows = []
    for row in evidence:
        location = f"[{_esc(row['char_start'])}:{_esc(row['char_end'])}]"
        failure = row.get("located_failure_reason")
        detail = f"; located-failure: {_esc(failure)}" if failure else ""
        rows.append(
            f"<li><code>{_esc(row['locator'])}</code> {location} ({_esc(row['kind'])}) — "
            f"{_esc(row['quote'])}{detail}</li>"
        )
    return "<ul>" + "".join(rows) + "</ul>"


def _rater_controls() -> str:
    return "☐ accept &nbsp; ☐ edit-minor &nbsp; ☐ edit-content &nbsp; ☐ reject"


def _render_rejected(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    parts = ["<details open><summary class='bad'>Rejected or flagged content</summary>"]
    for row in rows:
        parts.append(
            f"<div class='rejected'><p><b>{_esc(row['kind'])}:</b> "
            f"<code>{_esc(row['machine_reason_class'])}</code> "
            f"{_rater_controls()}</p><pre>{_json(row['content'])}</pre></div>"
        )
    parts.append("</details>")
    return "".join(parts)


def _render_activity(item: dict[str, Any]) -> str:
    status = item["gate_outcome"]
    badge_class = {
        schema.GATE_CLEAN: "ok",
        schema.GATE_REVIEW: "warn",
        schema.GATE_FAILED: "bad",
    }[status]
    return "".join(
        [
            "<div class='activity'>",
            f"<h3>{_esc(item['activity'].get('type'))} <span class='badge {badge_class}'>{_esc(status)}</span></h3>",
            f"<p><b>Teacher mark:</b> {_esc(item['mark'])} &nbsp; <b>Note:</b> {_esc(item['note'])}</p>",
            f"<p class='accept'><b>Rater verdict:</b> {_rater_controls()}</p>",
            "<details><summary>Block + activity provenance</summary>",
            f"<p><b>Block:</b></p><pre>{_json(item['block_provenance'])}</pre>",
            f"<p><b>Activity:</b></p><pre>{_json(item['activity_provenance'])}</pre></details>",
            f"<pre class='payload'>{_json(item['activity'])}</pre>",
            "<details open><summary>Evidence spans</summary>",
            _render_evidence(item["evidence"]),
            "</details>",
            _render_gate_checks(item["gate_checks"]),
            _render_rejected(item["rejected"]),
            "</div>",
        ]
    )


def _render_numeral_bank(bank: dict[str, Any]) -> str:
    parts = [
        "<h2>Numeral moat bank</h2>",
        "<table class='gates'><tr><th>side</th><th>class</th><th>phrase</th><th>ctx</th><th>status</th><th>rule</th><th>ok</th></tr>",
    ]
    for side, rows in (("positive", bank["positive_rows"]), ("negative", bank["negative_rows"])):
        for row in rows:
            cls = "ok" if row["ok"] else "bad"
            parts.append(
                f"<tr class='{cls}'><td>{side}</td><td>{_esc(row.get('class') or '')}</td>"
                f"<td>{_esc(row['phrase'])}</td><td>{_esc(row['ctx'])}</td>"
                f"<td>{_esc(row['status'])}</td><td>{_esc(row['rule'])}</td>"
                f"<td>{'✓' if row['ok'] else '✗'}</td></tr>"
            )
    parts.append("</table>")
    return "".join(parts)


def render_html(report: dict[str, Any]) -> str:
    kpi = report["kpi"]
    bank = report["numeral_bank"]
    russianism = report["russianism_e2e"]
    determinism = report["determinism"]
    parts = [
        "<meta charset='utf-8'>",
        "<title>Hramatka Step-7 measurement report</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;line-height:1.5}",
        ".badge{font-size:.8em;padding:.1em .5em;border-radius:.4em;color:#fff}.ok{background:#1a7f37}.warn{background:#9a6700}.bad{background:#cf222e}",
        "table.gates{border-collapse:collapse;width:100%;font-size:.85em;margin:.5rem 0}table.gates td,table.gates th{border:1px solid #ddd;padding:.2em .4em;text-align:left}",
        "tr.warn td{background:#fff8e1}tr.bad td{background:#ffebe9}tr.ok td{background:#f0fff4}.activity{border:1px solid #ccc;border-radius:.5em;padding:1rem;margin:1rem 0}",
        "pre{background:#f6f8fa;padding:.6rem;overflow-x:auto;font-size:.8em}.kpi{background:#f6f8fa;padding:1rem;border-radius:.5em}.accept{font-weight:600}.rejected{border-top:1px solid #ddd;padding:.5rem 0}",
        "</style>",
        "<h1>Hramatka Step-7 — measurement report</h1>",
        "<details open><summary>Report header and pinned runtime identity</summary>",
        f"<pre>{_json(report['header'])}</pre></details>",
        "<div class='kpi'>",
        f"<p><b>Tri-state activities:</b> {_esc(kpi['tri_state_counts'])} &nbsp; rates: {_esc(kpi['tri_state_rates'])}</p>",
        f"<p><b>Russianism real-anchor warn rate:</b> {_esc(kpi['russianism_real_anchors'])}</p>",
        f"<p><b>Numeral transfer:</b> {_esc(kpi['numeral_transfer']['pass'])} pass / {_esc(kpi['numeral_transfer']['warn'])} warn / {_esc(kpi['numeral_transfer']['fail'])} fail from {_esc(kpi['numeral_transfer']['numeral_bearing_items'])} numeral-bearing generated items</p>",
        f"<p><b>Wall-clock:</b> P50 {_esc(kpi['wall_clock_seconds']['p50'])}s; P95 {_esc(kpi['wall_clock_seconds']['p95'])}s; <b>cost:</b> $0 per bake</p>",
        f"<p><b>Russianism e2e fixtures:</b> {_esc(russianism['catch_count'])} caught / {_esc(russianism['uncaught_count'])} uncaught; generated-task case caught: {_esc(russianism['generated_task_fixture_caught'])}; hard gate: {_esc(russianism['hard_gate_met'])}</p>",
        f"<p><b>Shipped mark distribution:</b> {_esc(kpi['mark_distribution'])}</p>",
        f"<p><b>Determinism:</b> cache off, {_esc(determinism['runs_per_anchor'])} runs; complete={_esc(determinism['complete'])}; stable={_esc(determinism['stable'])}</p>",
        f"<p><b>Numeral moat:</b> {_esc(bank['positive_accept'])}/{_esc(bank['positive_total'])} positive accepted (clean {_esc(bank['positive_clean'])}; review-required {_esc(bank['positive_review_required'])}); {_esc(bank['negative_reject'])}/{_esc(bank['negative_total'])} negative rejected</p>",
        "</div>",
        "<h2>Determinism probes</h2>",
        f"<pre>{_json(determinism)}</pre>",
        "<h2>Russianism e2e fixtures</h2>",
        f"<pre>{_json(russianism)}</pre>",
        _render_numeral_bank(bank),
    ]
    for anchor in report["anchors"]:
        parts.append(
            f"<h2>Anchor {_esc(anchor['anchor_id'])} ({_esc(anchor['char_len'])} chars; class {_esc(anchor['measurement_class'])})</h2>"
        )
        parts.append(
            f"<p><b>Anchor provenance:</b> {_esc(anchor['anchor_provenance'])}</p>"
            f"<p><b>Bake:</b> {_esc(anchor['bake'])}</p>"
            f"<p><b>Tri-state:</b> {_esc(anchor['tri_state_counts'])} &nbsp; <b>Salvage:</b> {_esc(anchor['salvage_counts'])} &nbsp; <b>Shipped marks:</b> {_esc(anchor['mark_distribution'])}</p>"
        )
        diagnostics = anchor["anchor_diagnostics"]
        if diagnostics:
            parts.append(
                "<details open><summary class='warn'>Anchor baseline: forms not verified as normative "
                "(not engine-verified)</summary>"
                f"<pre>{_json(diagnostics)}</pre></details>"
            )
        for item in anchor["items"]:
            parts.append(_render_activity(item))
    return "\n".join(parts)
