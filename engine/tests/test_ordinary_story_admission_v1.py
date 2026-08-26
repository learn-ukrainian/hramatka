from __future__ import annotations

import re
from pathlib import Path

from hramatka.engine import data
from hramatka.engine.anchor_inventory_v3 import inventory_from_anchor
from hramatka.engine.fixtures import _build_fixture_bundle
from hramatka.engine.lesson_capacity_v3 import AnchorParagraph, AnchorWindow, preflight_lesson
from hramatka.engine.lesson_profile_45_v1 import lesson_profile_45, slot_builders_45
from hramatka.engine.lesson_quality_v1 import validate_teacher_lesson_plan_quality_45
from hramatka.engine.lesson_workload_45_v1 import phase_minutes
from hramatka.engine.sentence_segmentation_v1 import sentence_spans

_SOURCE = Path(__file__).with_name("fixtures") / "ordinary_b1_story_565.txt"
_UKRAINIAN_WORD_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ][А-Яа-яІіЇїЄєҐґ'’\-]*")


def test_ordinary_teacher_story_qualifies_without_glossary_gaming(tmp_path: Path) -> None:
    """The deployed #565 reproduction must authorize a dense six-format lesson offline."""
    source = _SOURCE.read_text(encoding="utf-8")
    profile = lesson_profile_45()
    bundle_root = tmp_path / "ordinary-story-bundle"
    bundle_root.mkdir()
    bundle = _build_fixture_bundle(bundle_root, vesum_delta_names=("profile_45_v3",))
    previous_bundle = data._active
    data.set_active_bundle(bundle)
    try:
        inventory = inventory_from_anchor(
            source,
            scheduled_types=tuple(slot.requested_type for slot in profile.slots),
            replacement_types=tuple(
                replacement for slot in profile.slots for replacement in slot.replacement_types
            ),
            duration_minutes=45,
        )
    finally:
        data.set_active_bundle(previous_bundle)

    result = preflight_lesson(
        AnchorWindow(
            paragraphs=(AnchorParagraph("ordinary-story-565", inventory),),
            initial_start=0,
            initial_end=0,
        ),
        duration_minutes=45,
        slots=profile.slots,
        builders=slot_builders_45(),
    )

    assert len(_UKRAINIAN_WORD_RE.findall(source)) == 351
    assert len(sentence_spans(source)) == 24
    assert result.allocation is not None
    validate_teacher_lesson_plan_quality_45(result.allocation)

    allocated = {slot.slot_id: slot for slot in result.allocation.slots}
    assert allocated["P1-A1"].requested_type == "match-up"
    assert allocated["P1-A1"].scheduled_type == "true-false"
    assert allocated["P1-A1"].substitution_reason == "preflight_unavailable"
    assert [(slot.scheduled_type, len(slot.plan.units)) for slot in result.allocation.slots] == [
        ("true-false", 8),
        ("quiz", 8),
        ("fill-in", 8),
        ("error-correction", 8),
        ("mark-the-words", 10),
        ("cloze", 24),
    ]
    assert dict(phase_minutes(result.allocation.slots)) == {1: 8.0, 2: 19.0, 3: 15.0}
    assert {unit.distinctness["gap"]["sentence_id"] for unit in allocated["P3-A1"].plan.units} == {
        f"s-{index}" for index in range(1, 25)
    }
