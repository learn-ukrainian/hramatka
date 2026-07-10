"""Anchor provenance and source-baseline diagnostics (Sol defect 7/8)."""

from __future__ import annotations

import hashlib
import json

from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.lesson import materialize_lesson
from hramatka.api.store import JobRecord, now_iso
from hramatka.engine import measure
from hramatka.engine.gates import vesum


def _job(anchor: str, *, source: str = "teacher-paste") -> JobRecord:
    timestamp = now_iso()
    return JobRecord(
        id="provenance-lesson",
        anchor_text=anchor,
        anchor_source=source,
        duration=45,
        focus=None,
        request_hash="test",
        status="baking",
        step="перевірка",
        last_error=None,
        lesson=None,
        warning_acknowledgements=frozenset(),
        created_at=timestamp,
        updated_at=timestamp,
        started_at=timestamp,
    )


def _single_true_false(anchor_sentence: str):
    def generator(_prompt: str) -> str:
        return json.dumps(
            {
                "activities": [
                    {
                        "type": "true-false",
                        "instruction": "Познач правильне твердження за текстом.",
                        "items": [
                            {
                                "statement": anchor_sentence,
                                "correct": True,
                                "evidence": anchor_sentence,
                            }
                        ],
                    }
                ]
            }
        )

    return generator


def test_anchor_baseline_marks_an_unverified_verbatim_form_not_clean():
    diagnostics = vesum.anchor_baseline_diagnostics("Учні кажуть здрастуйте.")

    diagnostic = next(item for item in diagnostics if item["form"].lower() == "здрастуйте")
    assert diagnostic["status"] == "flagged-not-verified"
    assert "не підтверджено" in diagnostic["reason"]
    assert "перевірена нормативна" in diagnostic["reason"]


def test_emitted_document_carries_anchor_source_fingerprint_and_diagnostic(tmp_path):
    sentence = "Учні кажуть здрастуйте."
    anchor = {"anchor_id": "source-quote", "body_uk": sentence, "source": "teacher-url"}
    baker = EngineLessonBaker(
        generator=_single_true_false(sentence), cache_dir=tmp_path / "cache"
    )

    template = baker.bake(anchor, duration=45, focus=None)
    document = materialize_lesson(template, _job(sentence, source="teacher-url"))

    emitted_anchor = document["anchor"]
    assert emitted_anchor["source"] == "teacher-url"
    assert emitted_anchor["fingerprint"] == hashlib.sha256(sentence.encode()).hexdigest()
    diagnostic = next(
        item for item in emitted_anchor["diagnostics"] if item["form"].lower() == "здрастуйте"
    )
    assert diagnostic == {
        "form": "здрастуйте",
        "status": "flagged-not-verified",
        "reason": (
            "Форму в опорному тексті не підтверджено VESUM; це цитата, "
            "а не перевірена нормативна форма."
        ),
    }


def test_c1_anchor_does_not_fail_b1_task_language_gate(tmp_path):
    # The first sentence is deliberately C1-class; the extractive B1 task
    # itself uses the second, simple sentence.  Source-baseline diagnostics
    # may flag unfamiliar anchor vocabulary, but they are not task-language
    # gate failures and therefore cannot suppress an otherwise valid bake.
    task_sentence = "Учні читають текст."
    anchor = (
        "Епістемологічна інтерсуб’єктивність дискурсивно переосмислює "
        "герменевтичну парадигму. "
        f"{task_sentence}"
    )
    baker = EngineLessonBaker(
        generator=_single_true_false(task_sentence), cache_dir=tmp_path / "cache"
    )

    template = baker.bake(anchor, duration=45, focus=None)

    assert template["blocks"]
    payload = template["blocks"][0]["activity"]["payload"]
    assert payload["items"][0]["statement"] == task_sentence
    # No C1 anchor form is converted into a failed task gate.
    assert template["rejected"] == []
    assert all(block["mark"] in {"ok", "warn"} for block in template["blocks"])


def test_measurement_report_surfaces_anchor_baseline_without_raw_ir(tmp_path):
    report = measure.measure(
        ["Учні кажуть здрастуйте."],
        generator=_single_true_false("Учні кажуть здрастуйте."),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    anchor = report["anchors"][0]
    assert anchor["anchor_provenance"]["source"] == "teacher-paste"
    assert any(
        diagnostic["status"] == "flagged-not-verified"
        for diagnostic in anchor["anchor_diagnostics"]
    )
    html = (tmp_path / "out" / "measure-report.html").read_text(encoding="utf-8")
    assert "Anchor baseline: forms not verified as normative" in html
    assert "flagged-not-verified" in html
