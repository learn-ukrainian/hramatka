from __future__ import annotations

from hramatka.api.baking import engine_adapter_v3
from hramatka.api.baking.engine_adapter_v3 import _lesson_slots, _mode
from hramatka.engine.lesson_profile_45_v1 import lesson_profile_45


def test_profile_is_the_immutable_45_minute_adapter_authority() -> None:
    profile = lesson_profile_45()

    assert _lesson_slots(45, "компаратив") == profile.slots
    assert [
        (slot.slot_id, slot.phase, slot.requested_type, slot.replacement_types)
        for slot in profile.slots
    ] == [
        ("P1-A1", 1, "match-up", ()),
        ("P1-A2", 1, "quiz", ()),
        ("P2-A1", 2, "fill-in", ()),
        ("P2-A2", 2, "error-correction", ()),
        ("P2-A3", 2, "mark-the-words", ()),
        ("P3-A1", 3, "cloze", ()),
    ]
    assert profile.digest == "8fdd9b4bdbc362099e64aba8324944a7443a2b7dabd28cb76917db2e882791be"
    assert profile.group_number_for("P2-A1", "fill-in") == 1
    assert profile.group_number_for("P3-A1", "cloze") == 1
    assert not profile.fallback_to_group_one("P2-A1", "fill-in")


def test_adapter_delegates_45_minute_builders_to_profile(monkeypatch) -> None:
    profile = lesson_profile_45()
    sentinel = {"profile-owned": object()}
    monkeypatch.setattr(engine_adapter_v3, "slot_builders_45", lambda: sentinel)
    assert engine_adapter_v3._slot_builders(profile.slots) is sentinel
    assert engine_adapter_v3._slot_builders(_lesson_slots(60)) is not sentinel
    assert engine_adapter_v3._slot_builders(_lesson_slots(90)) is not sentinel


def test_only_the_final_long_text_is_written_during_the_lesson() -> None:
    assert _mode(3, "cloze") == "письмово"
    assert _mode(3, "short-writing") == "вдома"
    assert _mode(2, "fill-in") == "усно"
