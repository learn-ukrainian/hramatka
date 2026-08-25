from __future__ import annotations

import json
import re

from hramatka.api.baking.engine_adapter_v3 import EngineLessonBaker
from hramatka.engine.sentence_segmentation_v1 import sentence_spans
from hramatka.qualification.harness import (
    _DeterministicRouteProvider,
    _qualification_fixture_bundle,
    deterministic_runtime_anchors,
)
from hramatka.qualification.receipts import RouteBinding


def test_real_baker_delivers_the_accepted_human_paced_lesson_shape(tmp_path) -> None:
    bundle = _qualification_fixture_bundle(tmp_path / "bundle")
    provider = _DeterministicRouteProvider(
        RouteBinding(
            route_id="subscription",
            host="antigravity-cli",
            model_id="gemini-3.7-flash-high",
        ),
        force_initial_shortfall=False,
    )
    anchors = deterministic_runtime_anchors()
    source = anchors["b1-narrative"].text
    baker = EngineLessonBaker(
        generator=provider,
        bundle=bundle,
        semantic_reviewer=provider,
        semantic_reviewer_route="subscription",
        logical_model_id="gemini-3.7-flash-high",
    )

    result = baker.bake(source, 45, None)

    blocks = result["blocks"]
    assert [(block["phase"], block["type"]) for block in blocks] == [
        (1, "match-up"),
        (1, "quiz"),
        (2, "fill-in"),
        (2, "error-correction"),
        (2, "mark-the-words"),
        (3, "cloze"),
    ]
    payloads = [block["activity"]["payload"] for block in blocks]
    assert [
        len(payloads[0]["pairs"]),
        len(payloads[1]["items"]),
        len(payloads[2]["items"]),
        len(payloads[3]["items"]),
        len(payloads[4]["target_words"]),
    ] == [8, 8, 7, 6, 10]
    quiz_items = blocks[1]["activity"]["payload"]["items"]
    assert all("___" not in item["question"] for item in quiz_items)
    cloze = blocks[-1]["activity"]["payload"]
    sentences = sentence_spans(cloze["text"])
    assert len(cloze["blanks"]) == len(sentences)
    reconstructed = cloze["text"]
    for blank in cloze["blanks"]:
        reconstructed = reconstructed.replace(f"{{{blank['id']}}}", blank["answer"])
    assert 350 <= len(re.findall(r"[А-Яа-яІіЇїЄєҐґ][А-Яа-яІіЇїЄєҐґ'’\-]*", reconstructed)) <= 450
    assert all(len(re.findall(r"\{[1-9]\d*\}", sentence)) == 1 for sentence in sentences)
    assert blocks[-1]["mode"] == "письмово"


def test_real_baker_compiles_truncated_model_cloze_from_certified_kit(tmp_path) -> None:
    bundle = _qualification_fixture_bundle(tmp_path / "bundle")
    provider = _DeterministicRouteProvider(
        RouteBinding(
            route_id="subscription",
            host="antigravity-cli",
            model_id="gemini-3.7-flash-high",
        ),
        force_initial_shortfall=False,
    )

    class TruncatedClozeProvider:
        _model = "gemini-3.7-flash-high"

        def for_bake(self):
            return self

        def __call__(self, prompt: str) -> str:
            parsed = json.loads(provider(prompt))
            slots = parsed.get("slots") if isinstance(parsed, dict) else None
            if (
                isinstance(slots, list)
                and len(slots) == 1
                and isinstance(slots[0], dict)
                and slots[0].get("type") == "cloze"
            ):
                record = slots[0]
                record["slot_id"] = "P1-A1"
                record["serialized_units"] = record["serialized_units"][:-2]
                record["activity"]["payload"]["text"] += " "
                record["activity"]["payload"]["blanks"] = record["activity"]["payload"]["blanks"][
                    :-2
                ]
                record["activity"]["answer_key"]["blanks"] = record["activity"]["answer_key"][
                    "blanks"
                ][:-2]
            return json.dumps(parsed, ensure_ascii=False)

    baker = EngineLessonBaker(
        generator=TruncatedClozeProvider(),
        bundle=bundle,
        semantic_reviewer=provider,
        semantic_reviewer_route="subscription",
        logical_model_id="gemini-3.7-flash-high",
    )

    result = baker.bake(deterministic_runtime_anchors()["b1-dialogue"].text, 45, None)

    cloze = result["blocks"][-1]["activity"]["payload"]
    sentences = sentence_spans(cloze["text"])
    assert len(cloze["blanks"]) == len(sentences)
    assert all(len(re.findall(r"\{[1-9]\d*\}", sentence)) == 1 for sentence in sentences)
