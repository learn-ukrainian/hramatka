"""Measurement harness (slice-1 §10) — the point of the slice.

Runs the pipeline over anchors and emits `measure-report.html` (ai→human =
HTML, #M-2): each rendered item + its evidence + gate verdicts, for HUMAN
accept-scoring. Tracks the deterministic KPIs:
  - gate-pass rate (% items passing the whole chain clean)
  - russianism-warn rate + B1-B2 lexical-stretch signal (NON-green markers —
    gate-pass ≠ teacher-accept)
  - numeral differentiator: positive-probe pass + offline NEGATIVE-bank
    rejection recall (the moat must accept good and reject bad).

`would-accept-as-is` (the real KPI) is filled in by humans reading the HTML;
this harness only produces the deterministic scaffold + the sheet.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import paths, pipeline
from .gates import numeral
from .generate import call_gemma

_DEFAULT_NUMERAL_BANK_PATH = Path(__file__).with_name("numeral-regression-bank.json")


def default_numeral_bank() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load the versioned offline numeral regression corpus.

    A positive probe is accepted when it does not hard-fail. Some constructions
    are deliberately ``warn`` rather than ``pass``: decimal and mixed-fraction
    complements vary in real usage, while ordinal and ``з`` constructions need
    context that this narrow government gate does not infer. The corpus records
    that policy per row, and the report exposes clean versus review-required
    counts instead of relabelling warnings as clean passes.
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
    return positive, negative


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


def _numeral_bank_recall(positive: list[object], negative: list[object]) -> dict:
    """Run the moat corpus, retaining deliberately review-required positives.

    The positive-bank contract is "no valid form is rejected". Its ``pass``
    counter therefore includes entries whose explicitly documented gate policy
    is ``warn``; clean and warn counts remain separate in the report. Every
    negative bank entry must still hard-fail.
    """
    pos_rows, neg_rows = [], []
    pos_ok = pos_clean = pos_warn = neg_ok = 0
    for entry in positive:
        probe = _bank_entry(entry, positive=True)
        r = numeral.check_numeral_government(probe["phrase"], probe["context_case"])
        ok = r["status"] in probe["accepted_statuses"]
        pos_ok += ok
        pos_clean += int(r["status"] == "pass")
        pos_warn += int(r["status"] == "warn")
        pos_rows.append(
            {
                **probe,
                "ctx": probe["context_case"],
                "status": r["status"],
                "rule": r["rule"],
                "ok": ok,
            }
        )
    for entry in negative:
        probe = _bank_entry(entry, positive=False)
        r = numeral.check_numeral_government(probe["phrase"], probe["context_case"])
        ok = r["status"] in probe["accepted_statuses"]
        neg_ok += ok
        neg_rows.append(
            {
                **probe,
                "ctx": probe["context_case"],
                "status": r["status"],
                "rule": r["rule"],
                "ok": ok,
            }
        )
    return {
        "positive_pass": pos_ok,
        "positive_total": len(positive),
        "positive_clean": pos_clean,
        "positive_warn": pos_warn,
        "negative_reject": neg_ok,
        "negative_total": len(negative),
        "positive_rows": pos_rows,
        "negative_rows": neg_rows,
    }


def measure(
    anchors: list[str],
    *,
    generator: Callable[[str], str] = call_gemma,
    out_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    numeral_bank: tuple[list[object], list[object]] | None = None,
) -> dict:
    """Run the pipeline over `anchors`, aggregate KPIs, and write
    measure-report.html. Returns the report dict.
    """
    out = Path(out_dir) if out_dir else (paths.ENGINE_DIR / ".out" / "measure")
    out.mkdir(parents=True, exist_ok=True)

    per_anchor = []
    total_items = passed_items = russianism_warns = numeral_warns = 0
    for i, anchor in enumerate(anchors):
        res = pipeline.run(
            anchor,
            generator=generator,
            out_dir=out / f"anchor{i:02d}",
            cache_dir=cache_dir,
            use_cache=cache_dir is not None,
        )
        items = []
        for ir in res.activities:
            total_items += 1
            passed = ir.gate_result.passed
            passed_items += int(passed)
            for c in ir.gate_result.checks:
                if c.gate == "vesum_token" and c.status == "warn":
                    russianism_warns += 1
                if c.gate == "numeral" and c.status == "warn":
                    numeral_warns += 1
            items.append(ir.as_dict())
        per_anchor.append(
            {
                "anchor_id": res.anchor["anchor_id"],
                "char_len": res.anchor["char_len"],
                "anchor_provenance": {
                    "source": res.anchor["source"],
                    "content_fingerprint": res.anchor["content_fingerprint"],
                },
                # Source diagnostics are explicitly report-visible.  Raters
                # never have to inspect raw IR to learn that a verbatim form
                # was not verified as normative Ukrainian (Sol defect 7).
                "anchor_diagnostics": res.anchor.get("diagnostics", []),
                "generation_error": res.generation_error,
                "items": items,
                "lesson_b1_count": len(res.lesson_b1),
            }
        )

    if numeral_bank is None:
        numeral_bank = default_numeral_bank()
    bank = _numeral_bank_recall(numeral_bank[0], numeral_bank[1])

    report = {
        "anchors": per_anchor,
        "kpi": {
            "total_items": total_items,
            "gate_passed_items": passed_items,
            "gate_pass_rate": round(passed_items / total_items, 3) if total_items else 0.0,
            "russianism_warns": russianism_warns,
            "numeral_warns": numeral_warns,
        },
        "numeral_bank": bank,
    }

    html_path = out / "measure-report.html"
    html_path.write_text(render_html(report), encoding="utf-8")
    (out / "measure-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["out_files"] = {
        "html": str(html_path),
        "json": str(out / "measure-report.json"),
    }
    return report


def _esc(x) -> str:
    return html.escape(str(x), quote=True)


def _render_gate_checks(checks: list[dict]) -> str:
    rows = []
    for c in checks:
        cls = {"pass": "ok", "warn": "warn", "fail": "bad"}.get(c["status"], "")
        rows.append(
            f"<tr class='{cls}'><td>{_esc(c['gate'])}</td><td>{_esc(c['status'])}</td>"
            f"<td>{_esc(c.get('locator') or '')}</td><td>{_esc(c['detail'])}</td></tr>"
        )
    return "<table class='gates'><tr><th>gate</th><th>status</th><th>locator</th><th>detail</th></tr>" + "".join(rows) + "</table>"


def _render_activity(item: dict) -> str:
    act = item["activity"]
    gr = item["gate_result"]
    flagged = item.get("flagged") or []
    ships = gr["passed"]
    badge = "PASS" if ships else "FAIL"
    badge_cls = "ok" if ships else "bad"
    body = f"<h3>{_esc(act.get('type'))} <span class='badge {badge_cls}'>{badge}</span>"
    if flagged and ships:
        # partial-ship: good items shipped, N flagged for teacher attention
        body += f" <span class='badge warn'>{len(flagged)} need teacher attention</span>"
    body += "</h3>"
    body += f"<p class='instr'>{_esc(act.get('instruction',''))}</p>"
    body += "<pre class='payload'>" + _esc(json.dumps(act, ensure_ascii=False, indent=2)) + "</pre>"
    if flagged:
        rows = "".join(
            f"<li><code>{_esc(f.get('locator'))}</code> — "
            + "; ".join(
                _esc(r.get("gate")) + ": " + _esc(r.get("detail"))
                for r in f.get("reasons", [])
            )
            + "</li>"
            for f in flagged
        )
        body += (
            "<details open><summary class='bad'>Flagged items filtered out "
            f"(need teacher attention): {len(flagged)}</summary>"
            f"<ul>{rows}</ul></details>"
        )
    if item["evidence"]:
        ev = "".join(
            f"<li><code>{_esc(e['locator'])}</code> [{_esc(e['char_start'])}:{_esc(e['char_end'])}] "
            f"({_esc(e['kind'])}) — {_esc(e['quote'])}</li>"
            for e in item["evidence"]
        )
        body += f"<details><summary>Evidence</summary><ul>{ev}</ul></details>"
    body += _render_gate_checks(gr["checks"])
    body += "<p class='accept'>Would a teacher accept as-is? &nbsp; ☐ accept &nbsp; ☐ edit &nbsp; ☐ reject</p>"
    return f"<div class='activity'>{body}</div>"


def render_html(report: dict) -> str:
    kpi = report["kpi"]
    bank = report["numeral_bank"]
    parts = [
        "<meta charset='utf-8'>",
        "<title>Hramatka slice-1 measure report</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.5}",
        ".badge{font-size:.7em;padding:.1em .5em;border-radius:.4em;color:#fff}",
        ".ok{background:#1a7f37}.warn{background:#9a6700}.bad{background:#cf222e}",
        "table.gates{border-collapse:collapse;width:100%;font-size:.85em;margin:.5rem 0}",
        "table.gates td,table.gates th{border:1px solid #ddd;padding:.2em .4em;text-align:left}",
        "tr.warn td{background:#fff8e1}tr.bad td{background:#ffebe9}tr.ok td{background:#f0fff4}",
        ".activity{border:1px solid #ccc;border-radius:.5em;padding:1rem;margin:1rem 0}",
        "pre.payload{background:#f6f8fa;padding:.6rem;overflow-x:auto;font-size:.8em}",
        ".kpi{background:#f6f8fa;padding:1rem;border-radius:.5em}",
        ".accept{font-weight:600}",
        "</style>",
        "<h1>Hramatka slice-1 — measurement report</h1>",
        "<div class='kpi'>",
        f"<p><b>Gate-pass rate:</b> {kpi['gate_passed_items']}/{kpi['total_items']} "
        f"= {kpi['gate_pass_rate']}</p>",
        f"<p><b>Russianism warns:</b> {kpi['russianism_warns']} &nbsp; "
        f"<b>Numeral warns:</b> {kpi['numeral_warns']} "
        "(NON-green signals — gate-pass ≠ teacher-accept)</p>",
        f"<p><b>Numeral moat — positive probes accepted:</b> "
        f"{bank['positive_pass']}/{bank['positive_total']} &nbsp; "
        f"(clean: {bank['positive_clean']}; review-required: {bank['positive_warn']}) &nbsp; "
        f"<b>negative bank rejected:</b> {bank['negative_reject']}/{bank['negative_total']}</p>",
        "</div>",
    ]
    # numeral bank detail
    parts.append("<h2>Numeral moat bank</h2><table class='gates'><tr><th>side</th><th>class</th><th>phrase</th><th>ctx</th><th>status</th><th>rule</th><th>ok</th></tr>")
    for row in bank["positive_rows"]:
        cls = "ok" if row["ok"] else "bad"
        parts.append(f"<tr class='{cls}'><td>positive</td><td>{_esc(row.get('class') or '')}</td><td>{_esc(row['phrase'])}</td><td>{_esc(row['ctx'])}</td><td>{_esc(row['status'])}</td><td>{_esc(row['rule'])}</td><td>{'✓' if row['ok'] else '✗'}</td></tr>")
    for row in bank["negative_rows"]:
        cls = "ok" if row["ok"] else "bad"
        parts.append(f"<tr class='{cls}'><td>negative</td><td>{_esc(row.get('class') or '')}</td><td>{_esc(row['phrase'])}</td><td>{_esc(row['ctx'])}</td><td>{_esc(row['status'])}</td><td>{_esc(row['rule'])}</td><td>{'✓' if row['ok'] else '✗'}</td></tr>")
    parts.append("</table>")
    # per-anchor activities
    for a in report["anchors"]:
        parts.append(f"<h2>Anchor {_esc(a['anchor_id'])} ({_esc(a['char_len'])} chars, b1 items: {_esc(a['lesson_b1_count'])})</h2>")
        provenance = a.get("anchor_provenance", {})
        parts.append(
            "<p><b>Anchor provenance:</b> "
            f"{_esc(provenance.get('source', 'teacher-paste'))}; content fingerprint "
            f"<code>{_esc(provenance.get('content_fingerprint', ''))}</code></p>"
        )
        diagnostics = a.get("anchor_diagnostics") or []
        if diagnostics:
            rows = "".join(
                "<li><code>"
                + _esc(diagnostic.get("form"))
                + "</code> — <b>"
                + _esc(diagnostic.get("status"))
                + "</b>: "
                + _esc(diagnostic.get("reason"))
                + "</li>"
                for diagnostic in diagnostics
            )
            parts.append(
                "<details open><summary class='warn'>Anchor baseline: forms not "
                "verified as normative</summary><ul>"
                + rows
                + "</ul></details>"
            )
        if a["generation_error"]:
            parts.append(f"<p class='badge bad'>generation error: {_esc(a['generation_error'])}</p>")
        for item in a["items"]:
            parts.append(_render_activity(item))
    return "\n".join(parts)
