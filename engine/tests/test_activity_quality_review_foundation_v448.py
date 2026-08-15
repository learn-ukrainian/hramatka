"""Focused contract tests for the dormant #448 activity-quality reviewer."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from hramatka.engine.activity_quality_review_v1 import (
    ACTIVITY_QUALITY_PROMPT_VERSION,
    ACTIVITY_QUALITY_REVIEW_VERSION,
    ACTIVITY_QUALITY_RUBRIC_VERSION,
    ActivityQualityReviewCache,
    ActivityQualityReviewRejected,
    _Decision,
    activity_quality_review_inputs,
    build_review_request,
    item_approval_digest,
    review_activity_quality,
    runtime_activity_review_provenance,
)
from hramatka.engine.semantic_review_v1 import (
    SemanticReviewAmbiguous,
    SemanticReviewMalformed,
)


def _provenance() -> dict[str, str]:
    return {
        "activity_review_source_sha256": "a" * 64,
        "adapter_source_sha256": "b" * 64,
        "provider_source_sha256": "c" * 64,
        "serializer_template_sha256": "d" * 64,
        "serializer_template_version": "test-template.v1",
        "transport_source_sha256": "e" * 64,
    }


def _review_item(review_id: str = "activity-0") -> dict[str, Any]:
    return {
        "activity_type": "quiz",
        "certified_answer": "answer",
        "certified_target": "meaning:contrast",
        "evidence_segments": ["A short certified evidence segment."],
        "intended_cognitive_operation": "interpret the stated contrast",
        "item_index": 0,
        "learner_context": "Choose the answer that fits the meaning.",
        "options": ["answer", "near-one", "near-two"],
        "proof_obligations": ["the answer is uniquely supported"],
        "review_id": review_id,
    }


def _request_from_prompt(prompt: str) -> dict[str, Any]:
    start = "BEGIN_HOST_ACTIVITY_REVIEW_REQUEST\n"
    end = "\nEND_HOST_ACTIVITY_REVIEW_REQUEST"
    return json.loads(prompt.split(start, 1)[1].split(end, 1)[0])


def _response_for(
    prompt: str, *, verdict: str = "pass", failure_codes: list[str] | None = None
) -> str:
    request = _request_from_prompt(prompt)
    return json.dumps(
        {
            "contract_version": ACTIVITY_QUALITY_REVIEW_VERSION,
            "input_digest": request["input_digest"],
            "results": [
                {
                    "review_id": item["review_id"],
                    "verdict": verdict,
                    "failure_codes": failure_codes or [],
                }
                for item in request["items"]
            ],
        }
    )


def test_projection_requires_complete_bound_quiz_and_produces_item_local_request() -> None:
    activity = {
        "payload": {
            "type": "quiz",
            "items": [
                {
                    "question": "Choose the source-supported response.",
                    "options": ["answer", "near-one", "near-two"],
                }
            ],
        },
        "answer_key": {"items": [{"correct": 0}]},
    }
    kit = {
        "certified_units": [
            {
                "rendering_surface": "A certified source surface.",
                "expected_key_or_rule": {"value": "answer"},
                "distinctness": {
                    "semantic_target": "meaning:contrast",
                    "semantic_warrant": "A short certified warrant.",
                    "intended_cognitive_operation": "interpret contrast",
                    "proof_obligations": ["unique answer"],
                },
            }
        ]
    }

    projected = activity_quality_review_inputs(activity, kit)

    assert projected == (
        {
            "activity_type": "quiz",
            "certified_answer": "answer",
            "certified_target": "meaning:contrast",
            "evidence_segments": [
                "A certified source surface.",
                "A short certified warrant.",
            ],
            "intended_cognitive_operation": "interpret contrast",
            "item_index": 0,
            "learner_context": "Choose the source-supported response.",
            "options": ["answer", "near-one", "near-two"],
            "proof_obligations": ["unique answer"],
        },
    )

    activity["answer_key"]["items"][0]["correct"] = 1
    with pytest.raises(ValueError, match="answer binding"):
        activity_quality_review_inputs(activity, kit)


def test_request_and_item_digests_bind_every_identity_dimension() -> None:
    item = _review_item()
    provenance = _provenance()
    request, digest = build_review_request(
        [item], reviewer_route="fixture-route", code_provenance=provenance
    )
    assert request["input_digest"] == digest

    base_item_digest = item_approval_digest(
        item, reviewer_route="fixture-route", code_provenance=provenance
    )
    mutations: list[dict[str, Any]] = []
    for key in item:
        changed = deepcopy(item)
        if key == "activity_type":
            changed[key] = "cloze"
        elif key == "certified_answer":
            changed[key] = "near-one"
        elif key == "item_index":
            changed[key] = 1
        elif key == "options":
            changed[key] = ["answer", "other-one", "other-two"]
        elif key == "evidence_segments":
            changed[key] = ["Changed evidence."]
        elif key == "proof_obligations":
            changed[key] = ["Changed obligation."]
        else:
            changed[key] = f"changed-{key}"
        mutations.append(changed)
    for changed in mutations:
        assert (
            item_approval_digest(
                changed, reviewer_route="fixture-route", code_provenance=provenance
            )
            != base_item_digest
        )

    for key in provenance:
        changed_provenance = dict(provenance)
        changed_provenance[key] = "f" * 64 if key.endswith("sha256") else "other-template.v1"
        assert (
            item_approval_digest(
                item,
                reviewer_route="fixture-route",
                code_provenance=changed_provenance,
            )
            != base_item_digest
        )
    assert item_approval_digest(
        item, reviewer_route="different-route", code_provenance=provenance
    ) != base_item_digest
    assert item_approval_digest(
        item,
        reviewer_route="fixture-route",
        code_provenance=provenance,
        contract_version=f"{ACTIVITY_QUALITY_REVIEW_VERSION}.changed",
    ) != base_item_digest
    assert item_approval_digest(
        item,
        reviewer_route="fixture-route",
        code_provenance=provenance,
        prompt_version=f"{ACTIVITY_QUALITY_PROMPT_VERSION}.changed",
    ) != base_item_digest
    assert item_approval_digest(
        item,
        reviewer_route="fixture-route",
        code_provenance=provenance,
        rubric_version=f"{ACTIVITY_QUALITY_RUBRIC_VERSION}.changed",
    ) != base_item_digest


def test_runtime_provenance_is_complete_and_byte_bound() -> None:
    provenance = runtime_activity_review_provenance(
        adapter_path=Path("hramatka/api/app.py")
    )

    assert set(provenance) == set(_provenance())
    assert all(
        len(value) == 64
        for key, value in provenance.items()
        if key.endswith("_sha256")
    )
    assert provenance["serializer_template_version"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(input_digest="wrong-digest"),
        lambda payload: payload["results"].append(deepcopy(payload["results"][0])),
        lambda payload: payload["results"][0].update(review_id="wrong-review-id"),
        lambda payload: payload["results"][0].update(failure_codes=["unknown-code"]),
        lambda payload: payload["results"][0].update(
            verdict="ambiguous", failure_codes=["insufficient_context"]
        ),
        lambda payload: payload["results"][0].update(unexpected="field"),
    ],
)
def test_reviewer_envelope_mutations_fail_closed(mutate: Any) -> None:
    def malformed_reviewer(prompt: str) -> str:
        payload = json.loads(_response_for(prompt))
        mutate(payload)
        return json.dumps(payload)

    with pytest.raises(SemanticReviewMalformed):
        review_activity_quality(
            [_review_item()],
            reviewer=malformed_reviewer,
            cache=ActivityQualityReviewCache(),
            code_provenance=_provenance(),
            explicit_route="fixture-route",
        )


def test_cache_is_content_free_and_never_reuses_an_unsafe_outcome() -> None:
    calls = 0

    def reviewer(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _response_for(prompt)

    cache = ActivityQualityReviewCache()
    first = review_activity_quality(
        [_review_item()],
        reviewer=reviewer,
        cache=cache,
        code_provenance=_provenance(),
        explicit_route="fixture-route",
    )
    second = review_activity_quality(
        [_review_item()],
        reviewer=reviewer,
        cache=cache,
        code_provenance=_provenance(),
        explicit_route="fixture-route",
    )

    assert calls == 1
    assert first == second
    assert list(cache._entries.values()) == [_Decision((("activity-0", "pass", ()),))]
    assert list(cache._item_approvals.values()) == [None]
    assert all("evidence" not in repr(value) for value in cache._entries.values())

    def rejected_reviewer(prompt: str) -> str:
        return _response_for(
            prompt, verdict="fail", failure_codes=["morphology_only"]
        )

    rejected_cache = ActivityQualityReviewCache()
    with pytest.raises(ActivityQualityReviewRejected) as error:
        review_activity_quality(
            [_review_item("rejected")],
            reviewer=rejected_reviewer,
            cache=rejected_cache,
            code_provenance=_provenance(),
            explicit_route="fixture-route",
        )
    assert error.value.rejections == (("rejected", ("morphology_only",)),)
    assert not rejected_cache._item_approvals

    ambiguous_cache = ActivityQualityReviewCache()
    with pytest.raises(SemanticReviewAmbiguous, match="ambiguous decision"):
        review_activity_quality(
            [_review_item("ambiguous")],
            reviewer=lambda prompt: _response_for(prompt, verdict="ambiguous"),
            cache=ambiguous_cache,
            code_provenance=_provenance(),
            explicit_route="fixture-route",
        )
    assert not ambiguous_cache._item_approvals


def test_cache_conflict_is_ambiguous_not_last_write_wins() -> None:
    cache = ActivityQualityReviewCache()
    cache.put("same-input", _Decision((("one", "pass", ()),)))

    with pytest.raises(SemanticReviewAmbiguous, match="conflicting"):
        cache.put(
            "same-input",
            _Decision((("one", "fail", ("morphology_only",)),)),
        )
