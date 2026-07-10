"""End-to-end bake: fixture anchor + FAKE generator -> schema-valid lu.lesson.v1.

Proves the full private path with NO network and NO public checkout:
  synthetic anchor -> engine pipeline (mock generator) -> gate chain ->
  projected lu.activity.v1 items -> EngineLessonBaker composes a lu.lesson.v1
  block template -> durable materialize -> validated against the VENDORED,
  digest-verified lu.lesson.v1 schema.
"""

from __future__ import annotations

import jsonschema
import pytest

from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.baking.port import BakeError
from hramatka.api.lesson import materialize_lesson
from hramatka.api.store import JobRecord, now_iso
from hramatka.api.validation import validate_lesson
from hramatka.engine import fixtures, vendoring


def _job(anchor: str, duration: int = 45) -> JobRecord:
    ts = now_iso()
    return JobRecord(
        id="e2e-lesson",
        anchor_text=anchor,
        anchor_source="teacher-paste",
        duration=duration,
        focus=None,
        request_hash="deadbeef",
        status="baking",
        step="перевірка",
        last_error=None,
        lesson=None,
        warning_acknowledgements=frozenset(),
        created_at=ts,
        updated_at=ts,
        started_at=ts,
    )


def _vendored_lesson_schema() -> dict:
    # Load + digest-verify the pinned lu.lesson.v1 schema through the loader.
    return vendoring.read_json(vendoring.LU_LESSON, "lu.lesson.v1.schema.json")


def test_e2e_bake_emits_schema_valid_lesson(tmp_path):
    anchor = fixtures.load_anchor()
    baker = EngineLessonBaker(generator=fixtures.mock_generator, cache_dir=tmp_path / "cache")

    template = baker.bake(anchor, duration=45, focus=None)
    lesson = materialize_lesson(template, _job(anchor))

    # 1) validates against the VENDORED, digest-verified lu.lesson.v1 schema
    schema = _vendored_lesson_schema()
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(lesson)
    # 2) and through the durable runner's validator (same pinned schema)
    validate_lesson(lesson)

    # shape the durable layer + pedagogy plan guarantee
    assert lesson["schema"] == "lu.lesson.v1"
    assert lesson["status"] == "ready"
    assert [b["phase"] for b in lesson["blocks"]] == [1, 1, 2, 2, 2, 3]
    assert lesson["rejected"] == []

    # 3) the block content is REAL engine output, not a canned fixture:
    tf_payload = next(
        b["activity"]["payload"] for b in lesson["blocks"] if b["type"] == "true-false"
    )
    statements = [it["statement"] for it in tf_payload["items"]]
    assert "Третина українців за рік не прочитує жодної книжки." in statements
    # 4) the out-of-band evidence quote never leaks onto the learner payload as
    #    a key (the "evidence_span" gate NAME in provenance is fine).
    import json as _json

    assert '"evidence":' not in _json.dumps(lesson, ensure_ascii=False)
    # 5) block mark carries the tri-state gate verdict end-to-end (Sol defect 1):
    #    the true-false ships review_required (a FALSE-statement warn) -> warn,
    #    the clean cloze/match-up -> ok. mark and external_options stay consistent
    #    with the vendored schema's "external_options => warn" rule.
    marks = {b["type"]: b["mark"] for b in lesson["blocks"]}
    assert marks["true-false"] == "warn"
    assert marks["cloze"] == "ok" and marks["match-up"] == "ok"
    for b in lesson["blocks"]:
        assert b["provenance"]["external_options"] is (b["mark"] == "warn")


def test_e2e_bake_raises_bakeerror_when_generator_unavailable(tmp_path):
    # A dead generator degrades to a safe, teacher-visible BakeError (no crash,
    # no anchor/engine text in the message).
    def dead(_prompt):
        from hramatka.engine.transport import GeneratorUnavailable

        raise GeneratorUnavailable("simulated")

    baker = EngineLessonBaker(generator=dead, cache_dir=tmp_path / "cache")
    with pytest.raises(BakeError) as exc:
        baker.bake(fixtures.load_anchor(), duration=45, focus=None)
    assert "generator is unavailable" in str(exc.value)


def _true_false_4good_1bad() -> dict:
    # 4 anchor-grounded items + 1 hallucinated (evidence absent) -> the bad item
    # is salvaged out, the 4 good siblings still ship.
    return {
        "type": "true-false",
        "instruction": "Познач правильні твердження за текстом.",
        "items": [
            {"statement": "Третина українців за рік не прочитує книжки.", "correct": True,
             "evidence": "Третина українців за рік не прочитує жодної книжки"},
            {"statement": "Дві третини щодня вмикають телевізор.", "correct": True,
             "evidence": "дві третини щодня знаходять час увімкнути телевізор"},
            {"statement": "Активізуються 17 ділянок мозку.", "correct": True,
             "evidence": "активізуються одразу 17 ділянок головного мозку"},
            {"statement": "Читання є одним з найскладніших завдань.", "correct": True,
             "evidence": "читання є одним з найскладніших завдань для мозку"},
            {"statement": "У тексті йдеться про ранкову пробіжку.", "correct": True,
             "evidence": "щоденна ранкова пробіжка корисна для серця"},
        ],
    }


def test_e2e_salvage_visibility_generated_equals_shipped_plus_flagged_plus_rejected(tmp_path):
    # Sol defect 3: partial bake results surface as shipped blocks + rejected[]
    # entries with gate-failed:<gate> reasons — never all-or-nothing, never
    # silent. One activity trips a single item (salvage), one fails wholly.
    from hramatka.api.baking.engine_adapter import bake_accounting, rejected_entries
    from hramatka.engine import pipeline

    cloze = next(a for a in fixtures.GOOD_ACTIVITIES if a["type"] == "cloze")

    def gen(_p):
        import json as _json

        return _json.dumps(
            {"activities": [cloze, _true_false_4good_1bad(), fixtures.HALLUCINATED_ACTIVITY]}
        )

    anchor = fixtures.load_anchor()
    result = pipeline.run(
        anchor, generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )

    # accounting is a strict partition of the generated activities
    acct = bake_accounting(result)
    assert acct == {"generated": 3, "shipped": 1, "flagged": 1, "rejected": 1}
    assert acct["generated"] == acct["shipped"] + acct["flagged"] + acct["rejected"]

    # the salvaged sibling still ships (4 of 5 items), not all-or-nothing
    partial = next(ir for ir in result.activities if ir.flagged)
    assert len(partial.activity["items"]) == 4

    # rejected[] discloses BOTH the dropped activity and the salvaged item, each
    # with a machine-readable gate-failed:<gate> reason
    rejected = rejected_entries(result.activities)
    assert len(rejected) == 2
    assert all(entry["reason"].startswith("gate-failed:") for entry in rejected)
    assert all(entry["type"] == "gate-failed" for entry in rejected)
    reasons = sorted(entry["reason"] for entry in rejected)
    assert reasons == ["gate-failed:evidence_span", "gate-failed:evidence_span"]

    # and the adapter emits that same rejected[] in the bake template
    baker = EngineLessonBaker(generator=gen, cache_dir=tmp_path / "cache-bake")
    template = baker.bake(anchor, duration=45, focus=None)
    assert len(template["rejected"]) == 2
    assert {e["reason"] for e in template["rejected"]} == {"gate-failed:evidence_span"}


def test_e2e_bake_wraps_data_bundle_errors_as_bakeerror(tmp_path, monkeypatch):
    # review-p46 nit 4: a misconfigured/drifted data bundle degrades to the same
    # safe, teacher-visible BakeError as any other failure — never a raw trace,
    # never a leaked path.
    from hramatka.engine import data, pipeline

    def drift(*_a, **_k):
        raise data.DataDriftError("vesum.db: sha256 mismatch at /srv/lu-data/secret/vesum.db")

    monkeypatch.setattr(pipeline, "run", drift)
    baker = EngineLessonBaker(generator=fixtures.mock_generator, cache_dir=tmp_path / "cache")
    with pytest.raises(BakeError) as exc:
        baker.bake(fixtures.load_anchor(), duration=45, focus=None)
    message = str(exc.value)
    assert "data bundle" in message
    assert "/srv/lu-data" not in message  # the raw path must not leak to teachers
