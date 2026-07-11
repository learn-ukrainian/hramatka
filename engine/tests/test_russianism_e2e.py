"""Russianism gate END-TO-END through the pipeline (Sol defect 5).

Proves the grounding/atlas lookup is wired into every lexical gate call in
`pipeline.run` — the fix for "retrieval returns atlas_lookup, the vesum gate can
use it, but pipeline omits it". Two offline integration fixtures, both against
the seeded-russianism bundle rows (`fixtures/seeded_russianism.json`):

  (a) a russianism QUOTED from the anchor — kept, but NOT badged verified: the
      diagnostic reaches the IR as a warn;
  (b) a russianism the model INTRODUCES into task language — caught as a warn,
      and the gate name reaches the block's `provenance.gates`.

`получка` is a lexical russianism for питомий `зарплата/платня`, VESUM-attested
as a form yet atlas-flagged `heritage=russianism` (see the seed file).
"""

from __future__ import annotations

import json
import os

import pytest

from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.baking.port import BakeError
from hramatka.engine import pipeline, schema

# The seed only exists in the offline fixture bundle; in real-data mode the
# corpus may not flag `получка`, so these fixtures are offline-only.
pytestmark = pytest.mark.skipif(
    bool(os.environ.get("HRAMATKA_TEST_DATA_DIR")),
    reason="russianism e2e depends on the seeded offline fixture bundle",
)

RUSSIANISM = "получку"  # acc sg of получка


def _vesum_warns(ir) -> list:
    return [
        c
        for c in ir.gate_result.checks
        if c.gate == "vesum_token" and c.status == "warn"
    ]


def test_russianism_quoted_from_anchor_is_flagged_not_verified(tmp_path):
    # (a) The russianism lives in the ANCHOR; a true-false statement quotes it
    # verbatim. It must NOT be dropped and NOT be badged engine-verified — the
    # atlas heritage diagnostic reaches the IR as a warn.
    anchor = "Багато людей щомісяця отримують получку і телевізор."
    statement = "Багато людей отримують получку."
    evidence = "Багато людей щомісяця отримують получку"  # verbatim anchor span

    def gen(_p):
        return json.dumps(
            {
                "activities": [
                    {
                        "type": "true-false",
                        "instruction": "Познач правильні твердження за текстом.",
                        "items": [
                            {"statement": statement, "correct": True, "evidence": evidence}
                        ],
                    }
                ]
            }
        )

    res = pipeline.run(
        anchor, generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    tf = res.activities[0]

    # ships (quoted source stays), but as review_required — never auto-clean
    assert tf.gate_result.status == schema.GATE_REVIEW
    assert tf.flagged == []
    assert RUSSIANISM in tf.activity["items"][0]["statement"]

    warns = _vesum_warns(tf)
    assert warns, "the atlas russianism diagnostic must reach the IR"
    detail = warns[0].detail
    assert RUSSIANISM in detail
    assert "russianism" in detail
    # explicitly NOT badged as engine-verified
    assert "quoted verbatim" in detail and "NOT engine-verified" in detail


def test_russianism_introduced_in_generated_task_language_is_caught(tmp_path):
    # (b) The anchor has NO russianism; the model INTRODUCES `получку` into a
    # statement. The augmented atlas lookup (anchor ∪ generated lexicon) catches
    # it as a warn, and the gate name reaches the block's provenance.gates.
    anchor = "Третина українців за рік не прочитує жодної книжки, зате дві третини мають телевізор."
    statement = "Третина українців втратили получку."
    evidence = "Третина українців за рік не прочитує жодної книжки"  # verbatim anchor span

    def gen(_p):
        return json.dumps(
            {
                "activities": [
                    {
                        "type": "true-false",
                        "instruction": "Познач правильні твердження за текстом.",
                        "items": [
                            {"statement": statement, "correct": True, "evidence": evidence}
                        ],
                    }
                ]
            }
        )

    # 1) pipeline: introduced russianism -> vesum_token WARN (caught, not passed)
    res = pipeline.run(
        anchor, generator=gen, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"
    )
    tf = res.activities[0]
    assert tf.gate_result.status == schema.GATE_REVIEW
    warns = _vesum_warns(tf)
    assert warns and RUSSIANISM in warns[0].detail and "russianism" in warns[0].detail

    # 2) adapter: review-required material cannot become an auto-included
    # block.  With no other ready candidate, the bake safely reports shortfall.
    baker = EngineLessonBaker(generator=gen, cache_dir=tmp_path / "cache-bake")
    with pytest.raises(BakeError, match="no automatically includable"):
        baker.bake(anchor, duration=45, focus=None)


def test_augmentation_is_required_grounding_alone_would_miss_it(tmp_path):
    # Guard the wiring itself: WITHOUT the augmented lookup (anchor-only atlas),
    # an introduced russianism absent from the anchor would slip through as a
    # plain VESUM pass. This asserts the augmentation actually changes the verdict.
    from hramatka.engine import retrieval
    from hramatka.engine.gates import vesum as vesum_gate

    anchor = "Третина українців за рік не прочитує жодної книжки."
    raw = [{"type": "true-false", "items": [{"statement": "Люди втратили получку."}]}]
    anchor_only = retrieval.build_grounding_pack(anchor, "B1")["atlas_lookup"]
    augmented = retrieval.augmented_atlas_lookup(anchor, raw, anchor_only)

    tokens = ["получку"]
    assert vesum_gate.worst_status(
        vesum_gate.check_tokens(tokens, anchor, atlas_lookup=anchor_only)
    ) == "pass"
    assert vesum_gate.worst_status(
        vesum_gate.check_tokens(tokens, anchor, atlas_lookup=augmented)
    ) == "warn"
