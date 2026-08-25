"""Acceptance-focused tests for the private teacher-sample semantic boundary."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from hramatka.api.baking.engine_adapter_v3 import EngineLessonBaker
from hramatka.api.baking.port import GenerationFailed, ProviderUnavailable
from hramatka.engine.semantic_review_v1 import (
    SEMANTIC_REVIEW_VERSION,
    SemanticReviewAmbiguous,
    SemanticReviewCache,
    SemanticReviewConfigurationError,
    SemanticReviewMalformed,
    SemanticReviewRejected,
    build_review_request,
    render_review_prompt,
    review_teacher_samples,
    runtime_review_provenance,
)
from hramatka.engine.transport import GeneratorUnavailable, generator_route_identity

_PROVENANCE = {
    "adapter_source_sha256": "a" * 64,
    "projection_source_sha256": "b" * 64,
    "provider_source_sha256": "f" * 64,
    "review_source_sha256": "c" * 64,
    "serializer_template_sha256": "d" * 64,
    "serializer_template_version": "fixture-template.v1",
    "transport_source_sha256": "9" * 64,
}


def _row(
    *,
    activity_type: str = "text-questions",
    category: str = "comprehension",
    evidence: str = "Марія повернулася додому через сильний дощ.",
    question: str = "Чому Марія повернулася додому?",
    sample: str = "Марія повернулася додому, бо почався сильний дощ.",
) -> dict[str, object]:
    return {
        "activity_type": activity_type,
        "category": category,
        "evidence_segments": [evidence],
        "item_index": 0,
        "learner_prompt": question,
        "regeneration_lens": None,
        "review_id": "P2-A3:0",
        "source_context": None,
        "teacher_sample": sample,
    }


def test_source_quiz_answer_can_be_reviewed_against_certified_evidence() -> None:
    approval = review_teacher_samples(
        [
            _row(
                activity_type="quiz",
                question="Чому Марія повернулася додому?",
                sample="Через сильний дощ.",
            )
        ],
        reviewer=lambda prompt: _result(prompt, "pass", []),
        cache=SemanticReviewCache(),
        code_provenance=_PROVENANCE,
        explicit_route="fixture:reviewer:v1",
    )

    assert approval is not None


def _request_from_prompt(prompt: str) -> dict:
    return json.loads(
        prompt.split("BEGIN_HOST_REVIEW_REQUEST\n", 1)[1].split("\nEND_HOST_REVIEW_REQUEST", 1)[0]
    )


def _result(prompt: str, verdict: str, failure_codes: list[str]) -> str:
    request = _request_from_prompt(prompt)
    return json.dumps(
        {
            "contract_version": SEMANTIC_REVIEW_VERSION,
            "input_digest": request["input_digest"],
            "results": [
                {
                    "failure_codes": failure_codes,
                    "review_id": item["review_id"],
                    "verdict": verdict,
                }
                for item in request["items"]
            ],
        }
    )


@pytest.mark.parametrize(
    "failure_code",
    (
        "unsupported_actor",
        "unsupported_motive",
        "unsupported_event",
        "unsupported_cause",
        "unsupported_emotion",
        "unsupported_description",
    ),
)
def test_each_unsupported_claim_class_is_a_closed_rejection(failure_code: str) -> None:
    with pytest.raises(SemanticReviewRejected) as rejection:
        review_teacher_samples(
            [_row()],
            reviewer=lambda prompt: _result(prompt, "fail", [failure_code]),
            cache=SemanticReviewCache(),
            code_provenance=_PROVENANCE,
            explicit_route="fixture:reviewer:v1",
        )

    assert rejection.value.failure_codes == (failure_code,)


def test_supported_paraphrase_and_new_personal_episode_can_pass_together() -> None:
    rows = [
        _row(),
        {
            **_row(
                category="anchored_application",
                evidence="Олег спершу склав список справ, тому встиг усе завершити.",
                question="Коли планування допомогло вам завершити важливу справу?",
                sample=(
                    "Перед університетським проєктом я розподілила завдання на три дні "
                    "і здала роботу вчасно."
                ),
            ),
            "item_index": 1,
            "review_id": "P2-A3:1",
        },
    ]

    approval = review_teacher_samples(
        rows,
        reviewer=lambda prompt: _result(prompt, "pass", []),
        cache=SemanticReviewCache(),
        code_provenance=_PROVENANCE,
        explicit_route="fixture:reviewer:v1",
    )

    assert approval is not None
    assert approval.reviewer_route == "fixture:reviewer:v1"


class _QualificationBoundReviewer:
    def __init__(self, *, actual_route: tuple[str, str] = ("review-host", "review-model")):
        self.actual_route = actual_route

    @staticmethod
    def semantic_review_route_identity() -> tuple[str, str, str]:
        return ("review-route", "review-host", "review-model")

    def __call__(self, prompt: str) -> str:
        generator_route_identity.set(self.actual_route)
        return _result(prompt, "pass", [])


def test_qualification_bound_route_is_used_without_attribute_sniffing() -> None:
    approval = review_teacher_samples(
        [_row()],
        reviewer=_QualificationBoundReviewer(),
        cache=SemanticReviewCache(),
        code_provenance=_PROVENANCE,
    )

    assert approval is not None
    assert approval.reviewer_route == "review-route:review-host:review-model"


def test_static_route_attributes_and_post_call_route_drift_fail_closed() -> None:
    class StaticOnlyReviewer:
        _model = "review-model"
        host = "review-host"

        def __call__(self, prompt: str) -> str:
            return _result(prompt, "pass", [])

    with pytest.raises(SemanticReviewConfigurationError, match="qualification-bound"):
        review_teacher_samples(
            [_row()],
            reviewer=StaticOnlyReviewer(),
            cache=SemanticReviewCache(),
            code_provenance=_PROVENANCE,
        )

    with pytest.raises(SemanticReviewConfigurationError, match="did not use"):
        review_teacher_samples(
            [_row()],
            reviewer=_QualificationBoundReviewer(actual_route=("fallback-host", "review-model")),
            cache=SemanticReviewCache(),
            code_provenance=_PROVENANCE,
        )


def test_transplanted_personal_scene_is_not_treated_as_entailment() -> None:
    with pytest.raises(SemanticReviewRejected) as rejection:
        review_teacher_samples(
            [
                _row(
                    category="anchored_application",
                    evidence="Мандрівники заночували біля річки й розклали намет.",
                    question="Розкажіть про власне рішення під час подорожі.",
                    sample=("Ми заночували біля тієї самої річки й розклали такий самий намет."),
                )
            ],
            reviewer=lambda prompt: _result(prompt, "fail", ["personal_transplant"]),
            cache=SemanticReviewCache(),
            code_provenance=_PROVENANCE,
            explicit_route="fixture:reviewer:v1",
        )

    assert rejection.value.failure_codes == ("personal_transplant",)


@pytest.mark.parametrize("failure_mode", ("missing", "timeout", "malformed", "ambiguous"))
def test_unavailable_or_uncertain_review_never_authorizes_a_sample(
    failure_mode: str,
) -> None:
    if failure_mode == "missing":
        reviewer = None
        expected = SemanticReviewConfigurationError
    elif failure_mode == "timeout":

        def unavailable(_prompt: str) -> str:
            raise GeneratorUnavailable("review timeout", retry_exhausted=True)

        reviewer = unavailable
        expected = GeneratorUnavailable
    elif failure_mode == "malformed":

        def malformed(_prompt: str) -> str:
            return "not json"

        reviewer = malformed
        expected = SemanticReviewMalformed
    else:

        def ambiguous(prompt: str) -> str:
            return _result(prompt, "ambiguous", [])

        reviewer = ambiguous
        expected = SemanticReviewAmbiguous

    with pytest.raises(expected):
        review_teacher_samples(
            [_row()],
            reviewer=reviewer,
            cache=SemanticReviewCache(),
            code_provenance=_PROVENANCE,
            explicit_route="fixture:reviewer:v1",
        )


def test_adapter_preserves_nonretryable_reviewer_outage_and_missing_configuration_types() -> None:
    missing = EngineLessonBaker(generator=lambda _prompt: "{}")
    with pytest.raises(GenerationFailed) as unconfigured:
        missing._review_teacher_samples([_row()])
    assert unconfigured.value.generation_error_type == "SemanticReviewConfigurationError"

    def timeout(_prompt: str) -> str:
        raise GeneratorUnavailable("review timeout", retry_exhausted=True)

    unavailable = EngineLessonBaker(
        generator=lambda _prompt: "{}",
        semantic_reviewer=timeout,
        semantic_reviewer_route="fixture:reviewer:v1",
    )
    with pytest.raises(ProviderUnavailable) as outage:
        unavailable._review_teacher_samples([_row()])
    assert not outage.value.retry_exhausted


def test_exact_input_cache_calls_the_reviewer_once_and_binds_every_identity_field() -> None:
    calls = 0

    def reviewer(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _result(prompt, "pass", [])

    cache = SemanticReviewCache()
    kwargs = {
        "reviewer": reviewer,
        "cache": cache,
        "code_provenance": _PROVENANCE,
        "explicit_route": "fixture:reviewer:v1",
    }
    first = review_teacher_samples([_row()], **kwargs)
    second = review_teacher_samples([_row()], **kwargs)

    assert first == second
    assert calls == 1

    base_request, base_digest = build_review_request(
        [_row()], reviewer_route="fixture:reviewer:v1", code_provenance=_PROVENANCE
    )
    mutations = []
    for field, replacement in (
        ("evidence_segments", ["Інший доказ."]),
        ("learner_prompt", "Інше питання?"),
        ("teacher_sample", "Інший зразок."),
    ):
        changed = deepcopy(base_request["items"])
        changed[0][field] = replacement
        mutations.append((changed, "fixture:reviewer:v1", _PROVENANCE))
    mutations.append((base_request["items"], "fixture:reviewer:v2", _PROVENANCE))
    changed_provenance = dict(_PROVENANCE)
    changed_provenance["review_source_sha256"] = "e" * 64
    mutations.append((base_request["items"], "fixture:reviewer:v1", changed_provenance))

    assert all(
        build_review_request(items, reviewer_route=route, code_provenance=provenance)[1]
        != base_digest
        for items, route, provenance in mutations
    )
    assert (
        build_review_request(
            base_request["items"],
            reviewer_route="fixture:reviewer:v1",
            code_provenance=_PROVENANCE,
            rubric_version="teacher-sample-rubric.b1.v2",
        )[1]
        != base_digest
    )


def test_reviewer_cannot_replace_host_evidence_or_return_conflicting_rows() -> None:
    request, _digest = build_review_request(
        [_row()], reviewer_route="fixture:reviewer:v1", code_provenance=_PROVENANCE
    )
    prompt = render_review_prompt(request)
    assert _request_from_prompt(prompt)["items"][0]["evidence_segments"] == [
        "Марія повернулася додому через сильний дощ."
    ]

    def conflicting(raw_prompt: str) -> str:
        host_request = _request_from_prompt(raw_prompt)
        row = {
            "failure_codes": [],
            "review_id": host_request["items"][0]["review_id"],
            "verdict": "pass",
        }
        return json.dumps(
            {
                "contract_version": SEMANTIC_REVIEW_VERSION,
                "input_digest": host_request["input_digest"],
                "results": [row, {**row, "verdict": "ambiguous"}],
            }
        )

    with pytest.raises(SemanticReviewMalformed):
        review_teacher_samples(
            [_row()],
            reviewer=conflicting,
            cache=SemanticReviewCache(),
            code_provenance=_PROVENANCE,
            explicit_route="fixture:reviewer:v1",
        )


def test_runtime_provenance_hashes_the_exact_review_projection_adapter_and_template() -> None:
    from hramatka.api.baking import engine_adapter_v3

    provenance = runtime_review_provenance(adapter_path=Path(engine_adapter_v3.__file__))

    assert set(provenance) == set(_PROVENANCE)
    assert all(len(value) == 64 for key, value in provenance.items() if key.endswith("_sha256"))
