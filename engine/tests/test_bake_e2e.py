"""End-to-end bake: fixture anchor + FAKE generator -> schema-valid lu.lesson.v1.

Proves the full private path with NO network and NO public checkout:
  synthetic anchor -> engine pipeline (mock generator) -> gate chain ->
  projected lu.activity.v1 items -> EngineLessonBaker composes a lu.lesson.v1
  block template -> durable materialize -> validated against the VENDORED,
  digest-verified lu.lesson.v1 schema.
"""

from __future__ import annotations

import pytest

from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.baking.port import BakeError
from hramatka.engine import fixtures


def test_e2e_bake_refuses_to_repeat_thin_candidate_bank(tmp_path):
    anchor = fixtures.load_anchor()
    baker = EngineLessonBaker(generator=fixtures.mock_generator, cache_dir=tmp_path / "cache")

    with pytest.raises(BakeError, match="too few distinct"):
        baker.bake(anchor, duration=45, focus=None)


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

    # The legacy duration adapter must not repeat the lone clean cloze into
    # six slots; Wave 0 reports an honest shortfall until more types arrive.
    baker = EngineLessonBaker(generator=gen, cache_dir=tmp_path / "cache-bake")
    with pytest.raises(BakeError, match="too few distinct"):
        baker.bake(anchor, duration=45, focus=None)


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
