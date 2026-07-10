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
    # 5) every block is a teacher-confirm warning (slice-1 honesty posture)
    assert all(b["mark"] == "warn" for b in lesson["blocks"])
    assert all(b["provenance"]["external_options"] is True for b in lesson["blocks"])


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
