"""Regenerate the offline VESUM + Atlas JSON fixtures from the real corpus DBs.

NOT run in CI. A developer runs it once (with the protected DBs mounted) to
capture EXACTLY the rows the engine test suite queries, so the committed fixture
bundle reproduces real DB behaviour offline. It records every VESUM row the
suite looks up (by driving the actual suite) and every Atlas payload the anchor
lemmas need — nothing more, so the fixtures stay tiny.

    HRAMATKA_TEST_DATA_DIR=/path/to/real/corpus \
        .venv/bin/python -m hramatka.engine.tests.fixtures._build_fixtures

The output JSON contains only public linguistic reference data (word forms,
morphological tags, public Atlas payloads) — no teacher text, anchors, or
private corpus material.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
_AUGMENT_ONLY_ENV = "HRAMATKA_FIXTURE_AUGMENT_ONLY"
_AUGMENT_BASELINE_REF_ENV = "HRAMATKA_FIXTURE_AUGMENT_BASELINE_GIT_REF"
_AUGMENT_DELTA_NAME_ENV = "HRAMATKA_FIXTURE_AUGMENT_DELTA_NAME"
_QUALIFICATION_ASSET_ENV = "HRAMATKA_FIXTURE_WRITE_QUALIFICATION_ASSET"
_TEST_TARGET_ENV = "HRAMATKA_FIXTURE_TEST_TARGET"


def _baseline_rows(name: str) -> list[dict]:
    """Load the current file or an explicit committed baseline for clean augmentation."""
    return json.loads(_baseline_text(name))


def _baseline_text(name: str) -> str:
    """Load exact fixture bytes from the worktree or an explicit git baseline."""
    baseline_ref = os.environ.get(_AUGMENT_BASELINE_REF_ENV)
    path = HERE / name
    if not baseline_ref:
        return path.read_text(encoding="utf-8")
    repository = HERE.parents[3]
    relative = path.relative_to(repository)
    result = subprocess.run(
        ["git", "-C", str(repository), "show", f"{baseline_ref}:{relative}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _qualification_inventory_inputs(real_dir: Path) -> tuple[set[str], set[str]]:
    """Return exact VESUM surfaces and Atlas lemmas used by qualification."""
    from hramatka.api.baking.engine_adapter_v3 import (
        _inventory_candidate_types,
        _inventory_replacement_types,
        _lesson_slots,
    )
    from hramatka.engine import data
    from hramatka.engine.anchor_inventory_v3 import _TOKEN_RE, inventory_from_anchor

    data.set_active_bundle(data.resolve_bundle(data_dir=real_dir, verify=True))
    slots = _lesson_slots(45)
    surfaces: set[str] = set()
    atlas_lemmas: set[str] = set()
    anchor_path = HERE.parents[2] / "qualification" / "assets" / "b1-45m.anchors.json"
    rows = json.loads(anchor_path.read_text(encoding="utf-8"))["anchors"]
    for row in rows:
        source = row["text"]
        inventory = inventory_from_anchor(
            source,
            scheduled_types=_inventory_candidate_types(slots),
            replacement_types=_inventory_replacement_types(slots),
            duration_minutes=45,
        )
        surfaces.update(_TOKEN_RE.findall(source))
        surfaces.update(
            option
            for candidate in inventory.candidates
            for option in candidate.choice_bank
            if _TOKEN_RE.fullmatch(option)
        )
        for pair in inventory.atlas_pairs:
            surfaces.update((pair.left, pair.right))
            atlas_lemmas.add(pair.left.casefold())
    return surfaces, atlas_lemmas


def _qualification_vesum_rows(real_dir: Path) -> list[dict]:
    surfaces, _atlas_lemmas = _qualification_inventory_inputs(real_dir)
    connection = sqlite3.connect(real_dir / "vesum.db")
    rows: dict[tuple[str, str, str, str], dict] = {}
    try:
        for surface in sorted(surfaces, key=str.casefold):
            matches = connection.execute(
                "SELECT word_form, lemma, tags, pos FROM forms WHERE word_form = ?",
                (surface,),
            ).fetchall()
            if not matches and surface != surface.lower():
                matches = connection.execute(
                    "SELECT word_form, lemma, tags, pos FROM forms WHERE word_form = ?",
                    (surface.lower(),),
                ).fetchall()
            for word_form, lemma, tags, pos in matches:
                key = (word_form, lemma, tags, pos)
                rows[key] = {
                    "word_form": word_form,
                    "lemma": lemma,
                    "tags": tags,
                    "pos": pos,
                }
    finally:
        connection.close()
    return list(rows.values())


def _record_vesum_rows() -> list[dict]:
    """Wrap the linguistics query API to capture every row the suite reads."""
    import hramatka.engine.linguistics as L

    seen: set[tuple] = set()
    rows: list[dict] = []

    def _rec(word_form: str, lemma: str, tags: str, pos: str) -> None:
        key = (word_form, lemma, tags, pos)
        if key not in seen:
            seen.add(key)
            rows.append({"word_form": word_form, "lemma": lemma, "tags": tags, "pos": pos})

    _vw, _vws, _vl = L.verify_word, L.verify_words, L.verify_lemma

    def vw(word, pos_filter=None, db_path=None):
        res = _vw(word, pos_filter, db_path)
        for r in res:
            _rec(word, r["lemma"], r["tags"], r["pos"])
        return res

    def vws(words, pos_filter=None, db_path=None):
        res = _vws(words, pos_filter, db_path)
        for wf, matches in res.items():
            for r in matches:
                _rec(wf, r["lemma"], r["tags"], r["pos"])
        return res

    def vl(lemma, db_path=None):
        res = _vl(lemma, db_path)
        for r in res:
            _rec(r["word_form"], lemma, r["tags"], r["pos"])
        return res

    # Patch BEFORE the consumers (retrieval/gates) import the names.
    L.verify_word, L.verify_words, L.verify_lemma = vw, vws, vl

    import pytest

    test_target = os.environ.get(_TEST_TARGET_ENV, "hramatka/engine/tests")
    rc = pytest.main([test_target, "-q", "-p", "no:cacheprovider"])
    if rc != 0:
        raise SystemExit(f"suite failed under real data (rc={rc}); not writing fixtures")

    rows.sort(key=lambda r: (r["word_form"], r["pos"], r["tags"]))
    return rows


def _trim_payload(payload: dict, *, include_antonyms: bool = False) -> dict:
    """Keep only the fields `retrieval.build_atlas_lookup` actually reads, so the
    fixture stays tiny and carries no incidental public prose."""
    trimmed: dict = {"lemma": payload.get("lemma"), "pos": payload.get("pos")}
    enr = payload.get("enrichment")
    if isinstance(enr, dict):
        keep = {k: enr[k] for k in ("cefr", "heritage") if k in enr}
        if keep:
            trimmed["enrichment"] = keep
    secs = payload.get("sections")
    if isinstance(secs, dict):
        section_names = ("antonyms", "synonyms") if include_antonyms else ("synonyms",)
        kept_sections = {
            name: secs[name] for name in section_names if isinstance(secs.get(name), dict)
        }
        if kept_sections:
            trimmed["sections"] = kept_sections
    for fallback in ("cefr", "heritage_status"):
        if fallback in payload:
            trimmed[fallback] = payload[fallback]
    return trimmed


def _source_bundle_provenance(real_dir: Path) -> dict[str, object]:
    manifest_path = real_dir / "data-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    inputs = manifest.get("inputs", {})
    return {
        "version": manifest.get("version"),
        "content_sha256": manifest.get("release", {}).get("content_sha256"),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "vesum_sha256": inputs.get("vesum.db", {}).get("sha256"),
        "atlas_sha256": inputs.get("atlas.db", {}).get("sha256"),
    }


def _extract_atlas_payloads(real_dir: Path, *, include_qualification: bool = False) -> list[dict]:
    from hramatka.engine import data, retrieval

    data.set_active_bundle(data.resolve_bundle(data_dir=real_dir, verify=True))
    anchor = (HERE / "anchor01.txt").read_text(encoding="utf-8")
    qualification_lemmas: set[str] = set()
    if include_qualification:
        _surfaces, qualification_lemmas = _qualification_inventory_inputs(real_dir)
    needed = (
        {lemma.lower() for lemma in retrieval.anchor_lemmas(anchor)}
        | {"книжка", "час"}
        | qualification_lemmas
    )

    conn = sqlite3.connect(str(real_dir / "atlas.db"))
    kept: dict[str, dict] = {}
    try:
        for (payload_json,) in conn.execute(
            "SELECT payload_json FROM article_payloads WHERE is_public_route=1"
        ):
            try:
                payload = json.loads(payload_json)
            except (ValueError, TypeError):
                continue
            lemma = payload.get("lemma")
            if isinstance(lemma, str) and lemma.lower() in needed and lemma.lower() not in kept:
                kept[lemma.lower()] = _trim_payload(payload, include_antonyms=include_qualification)
    finally:
        conn.close()
    return [kept[k] for k in sorted(kept)]


def main() -> None:
    real_dir = Path(os.environ["HRAMATKA_TEST_DATA_DIR"])
    augment_only = os.environ.get(_AUGMENT_ONLY_ENV) == "1"
    qualification_asset = os.environ.get(_QUALIFICATION_ASSET_ENV) == "1"
    if qualification_asset:
        if not augment_only or not os.environ.get(_AUGMENT_BASELINE_REF_ENV):
            raise SystemExit(
                "qualification asset generation requires augmentation and an explicit baseline"
            )
        baseline_vesum = _baseline_rows("vesum_forms.json")
        baseline_vesum_keys = {
            (row["word_form"], row["lemma"], row["tags"], row["pos"]) for row in baseline_vesum
        }
        qualification_vesum = [
            row
            for row in _qualification_vesum_rows(real_dir)
            if (row["word_form"], row["lemma"], row["tags"], row["pos"]) not in baseline_vesum_keys
        ]
        baseline_atlas = _baseline_rows("atlas_rows.json")
        baseline_atlas_lemmas = {row.get("lemma") for row in baseline_atlas}
        qualification_atlas = [
            row
            for row in _extract_atlas_payloads(real_dir, include_qualification=True)
            if row.get("lemma") not in baseline_atlas_lemmas
        ]
        asset_path = HERE.parents[2] / "qualification" / "assets" / "b1-45m.linguistics.json"
        asset_path.write_text(
            json.dumps(
                {
                    "schema_version": "QualificationLinguistics.v1",
                    "source_bundle": _source_bundle_provenance(real_dir),
                    "vesum_forms": qualification_vesum,
                    "atlas_rows": qualification_atlas,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        (HERE / "vesum_forms.json").write_text(_baseline_text("vesum_forms.json"), encoding="utf-8")
        (HERE / "atlas_rows.json").write_text(_baseline_text("atlas_rows.json"), encoding="utf-8")
        print(
            f"wrote {len(qualification_vesum)} qualification VESUM forms and "
            f"{len(qualification_atlas)} Atlas payloads"
        )
        return
    if augment_only:
        vesum_rows = [*_baseline_rows("vesum_forms.json"), *_record_vesum_rows()]
    else:
        vesum_rows = _record_vesum_rows()
    if augment_only:
        vesum_rows = [
            {
                "word_form": word_form,
                "lemma": lemma,
                "tags": tags,
                "pos": pos,
            }
            for word_form, lemma, tags, pos in sorted(
                {(row["word_form"], row["lemma"], row["tags"], row["pos"]) for row in vesum_rows}
            )
        ]
    delta_name = os.environ.get(_AUGMENT_DELTA_NAME_ENV)
    if delta_name:
        if not augment_only or not os.environ.get(_AUGMENT_BASELINE_REF_ENV):
            raise SystemExit("delta generation requires augmentation and an explicit baseline")
        if Path(delta_name).name != delta_name or not (
            delta_name.startswith("vesum_forms.") and delta_name.endswith(".jsonl")
        ):
            raise SystemExit("delta name must match vesum_forms.<name>.jsonl")
        baseline_keys = {
            (row["word_form"], row["lemma"], row["tags"], row["pos"])
            for row in _baseline_rows("vesum_forms.json")
        }
        delta_rows = [
            row
            for row in vesum_rows
            if (row["word_form"], row["lemma"], row["tags"], row["pos"]) not in baseline_keys
        ]
        (HERE / delta_name).write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in delta_rows
            ),
            encoding="utf-8",
        )
        (HERE / "vesum_forms.json").write_text(_baseline_text("vesum_forms.json"), encoding="utf-8")
    atlas_rows = _extract_atlas_payloads(real_dir)
    if augment_only:
        current_atlas = _baseline_rows("atlas_rows.json")
        by_lemma = {row.get("lemma"): row for row in current_atlas}
        by_lemma.update({row.get("lemma"): row for row in atlas_rows})
        atlas_rows = [by_lemma[lemma] for lemma in sorted(by_lemma) if lemma]
    if not delta_name:
        (HERE / "vesum_forms.json").write_text(
            json.dumps(vesum_rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    (HERE / "atlas_rows.json").write_text(
        json.dumps(atlas_rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    if delta_name:
        print(
            f"wrote {len(delta_rows)} delta vesum forms to {delta_name}, "
            f"{len(atlas_rows)} atlas payloads"
        )
    else:
        print(f"wrote {len(vesum_rows)} vesum forms, {len(atlas_rows)} atlas payloads")


if __name__ == "__main__":
    main()
