"""Anchor provenance and source-baseline diagnostics (Sol defect 7/8)."""

from __future__ import annotations

import hashlib
import json

from hramatka.engine import measure, pipeline
from hramatka.engine.gates import vesum


def _single_true_false(*anchor_sentences: str):
    """Build one deliverable true/false block from source-backed statements."""
    if not anchor_sentences:
        raise ValueError("At least one source sentence is required.")
    statements = tuple(anchor_sentences)
    if len(statements) == 1:
        statements *= 4

    def generator(_prompt: str) -> str:
        return json.dumps(
            {
                "activities": [
                    {
                        "type": "true-false",
                        "instruction": "Познач правильне твердження за текстом.",
                        "items": [
                            {
                                "statement": sentence,
                                "correct": True,
                                "evidence": sentence,
                            }
                            for sentence in statements
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


def test_pipeline_carries_anchor_source_fingerprint_and_diagnostic(tmp_path):
    sentence = "Учні кажуть здрастуйте."
    anchor = {"anchor_id": "source-quote", "body_uk": sentence, "source": "teacher-url"}
    result = pipeline.run(
        anchor,
        generator=_single_true_false(sentence),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    emitted_anchor = result.anchor
    assert emitted_anchor["source"] == "teacher-url"
    assert emitted_anchor["content_fingerprint"] == hashlib.sha256(sentence.encode()).hexdigest()
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
    task_sentences = (
        "Учні читають текст.",
        "Учні обговорюють текст.",
        "Учні відповідають на запитання.",
        "Учні пояснюють свою думку.",
    )
    anchor = (
        "Епістемологічна інтерсуб’єктивність дискурсивно переосмислює "
        "герменевтичну парадигму. "
        + " ".join(task_sentences)
    )
    result = pipeline.run(
        anchor,
        generator=_single_true_false(*task_sentences),
        out_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
    )

    assert result.lesson_b1
    payload = result.lesson_b1[0]
    assert [item["statement"] for item in payload["items"]] == list(task_sentences)
    # No C1 anchor form is converted into a failed task gate.
    assert result.rejected == []


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
