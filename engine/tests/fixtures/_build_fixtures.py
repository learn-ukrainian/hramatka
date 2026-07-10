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

import json
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent


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

    rc = pytest.main(["hramatka/engine/tests", "-q", "-p", "no:cacheprovider"])
    if rc != 0:
        raise SystemExit(f"suite failed under real data (rc={rc}); not writing fixtures")

    rows.sort(key=lambda r: (r["word_form"], r["pos"], r["tags"]))
    return rows


def _trim_payload(payload: dict) -> dict:
    """Keep only the fields `retrieval.build_atlas_lookup` actually reads, so the
    fixture stays tiny and carries no incidental public prose."""
    trimmed: dict = {"lemma": payload.get("lemma"), "pos": payload.get("pos")}
    enr = payload.get("enrichment")
    if isinstance(enr, dict):
        keep = {k: enr[k] for k in ("cefr", "heritage") if k in enr}
        if keep:
            trimmed["enrichment"] = keep
    secs = payload.get("sections")
    if isinstance(secs, dict) and isinstance(secs.get("synonyms"), dict):
        trimmed["sections"] = {"synonyms": secs["synonyms"]}
    for fallback in ("cefr", "heritage_status"):
        if fallback in payload:
            trimmed[fallback] = payload[fallback]
    return trimmed


def _extract_atlas_payloads(real_dir: Path) -> list[dict]:
    from hramatka.engine import data, retrieval

    data.set_active_bundle(data.resolve_bundle(data_dir=real_dir, verify=True, allow_drift=True))
    anchor = (HERE / "anchor01.txt").read_text(encoding="utf-8")
    needed = {lemma.lower() for lemma in retrieval.anchor_lemmas(anchor)} | {"книжка", "час"}

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
                kept[lemma.lower()] = _trim_payload(payload)
    finally:
        conn.close()
    return [kept[k] for k in sorted(kept)]


def main() -> None:
    real_dir = Path(os.environ["HRAMATKA_TEST_DATA_DIR"])
    vesum_rows = _record_vesum_rows()
    atlas_rows = _extract_atlas_payloads(real_dir)
    (HERE / "vesum_forms.json").write_text(
        json.dumps(vesum_rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (HERE / "atlas_rows.json").write_text(
        json.dumps(atlas_rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(vesum_rows)} vesum forms, {len(atlas_rows)} atlas payloads")


if __name__ == "__main__":
    main()
