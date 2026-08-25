"""Frozen same-origin FastAPI surface for the private teacher pilot."""
# ruff: noqa: B008

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Path, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse

from hramatka.engine import data
from hramatka.engine.providers import (
    GeneratorUnavailable,
    configure_provider_concurrency,
    make_logical_model_generator,
)

from .anchor_preparation import AnchorPreparationError, prepare_anchor_text
from .baking.artifacts import configured_engine_out_dir
from .baking.engine_adapter_v3 import (
    REGENERATION_PROMPT_SHA256,
    REGENERATION_PROMPT_VERSION,
    EngineLessonBaker,
)
from .baking.port import LessonBaker, ProviderUnavailable
from .config import Settings
from .models import (
    ActivityFeedbackMutation,
    ActivityRegenerationCreate,
    ActivityReplacementMutation,
    BlockMoveMutation,
    InviteRedeem,
    LessonCreate,
    RecoveryCodeRedeem,
    RestoreRejectedMutation,
    RevisionMutation,
    TeacherPreferences,
    UrlImportRequest,
    WebAuthnAssertion,
    WebAuthnCredential,
)
from .passkeys import assertion_options, enrollment_options, verify_assertion, verify_registration
from .qualified_models import (
    LogicalModelUnavailable,
    QualifiedModelRegistry,
    default_model_registry,
)
from .review_attestation import ReviewAttestationError, ReviewAttestor
from .runner import BakeRunner
from .security import csrf_matches, csrf_token
from .store import (
    ActivityRegenerationInProgress,
    ActivityRegenerationNotFound,
    ActivityRegenerationRecord,
    ActivityRegenerationStateConflict,
    AttemptQuiescing,
    FeedbackNotApplicable,
    IdempotencyConflict,
    InviteUnavailable,
    JobRecord,
    JobStore,
    LessonBlockNotFound,
    LessonNotFound,
    LessonStateConflict,
    PersistenceUnavailable,
    RejectedEntryNotFound,
    ReviewMutationInvalid,
    RevisionConflict,
    SessionUnavailable,
    TokenFormatError,
    WarningAcknowledgementsRequired,
    WarningBlockNotFound,
    canonical_request_json,
)
from .url_import import UrlImportError, fetch_url_text

_SESSION_COOKIE = "__Host-hramatka_session"
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
_TEACHER_PROGRESS_STEPS = frozenset({"generation", "gates", "assembly"})
_TEACHER_PROGRESS_FIELDS = (
    "phase",
    "phases_total",
    "step",
    "calls_done",
    "calls_planned",
    "updated_at",
)
_RFC3339_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _is_rfc3339_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _RFC3339_TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _session_cookie_max_age(expires_at: str) -> int:
    """Never keep a browser credential after its server-side absolute lifetime."""
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return max(0, int((expiry - datetime.now(UTC)).total_seconds()))


def _teacher_safe_progress(progress: object) -> dict[str, object] | None:
    """Project durable telemetry onto the small, browser-safe progress contract.

    Durable ``progress_json`` is operator telemetry and is intentionally
    extensible.  The teacher browser receives only the scalar fields it needs
    to render an honest bake-progress line; unknown keys and all nested data
    stay in SQLite for operator diagnosis.
    """
    if not isinstance(progress, dict) or any(
        field not in progress for field in _TEACHER_PROGRESS_FIELDS
    ):
        return None

    phase = progress.get("phase")
    phases_total = progress.get("phases_total")
    step = progress.get("step")
    calls_done = progress.get("calls_done")
    calls_planned = progress.get("calls_planned")
    updated_at = progress.get("updated_at")

    if type(phase) is not int or phase < 1:
        return None
    if type(phases_total) is not int or phases_total < 1:
        return None
    if step is not None and (not isinstance(step, str) or step not in _TEACHER_PROGRESS_STEPS):
        return None
    if calls_done is not None and (type(calls_done) is not int or calls_done < 0):
        return None
    if calls_planned is not None and (type(calls_planned) is not int or calls_planned < 0):
        return None
    if not _is_rfc3339_timestamp(updated_at):
        return None

    return {
        "phase": phase,
        "phases_total": phases_total,
        "step": step,
        "calls_done": calls_done,
        "calls_planned": calls_planned,
        "updated_at": updated_at,
    }


class PilotError(Exception):
    """A deliberately small error envelope with no implementation detail."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        lesson_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.lesson_id = lesson_id

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.lesson_id is not None:
            payload["lesson_id"] = self.lesson_id
        return payload


@dataclass(frozen=True)
class AuthenticatedSession:
    record: Any
    raw_secret: bytes

    @property
    def teacher_id(self) -> str:
        return self.record.teacher_id


def _encode_opaque(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_opaque(value: str | None) -> bytes | None:
    """Return only a canonical 32-byte base64url browser credential."""
    if value is None or not _OPAQUE_TOKEN_RE.fullmatch(value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=")
    except (ValueError, binascii.Error):
        return None
    if len(raw) != 32 or _encode_opaque(raw) != value:
        return None
    return raw


def _webauthn_challenge(credential: dict[str, object]) -> bytes:
    """Read only the ceremony challenge needed to match a persisted digest."""
    try:
        response = credential["response"]
        assert isinstance(response, dict)
        encoded = response["clientDataJSON"]
        assert isinstance(encoded, str)
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        challenge = json.loads(decoded)["challenge"]
        assert isinstance(challenge, str)
        return base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
    except (
        AssertionError,
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ) as error:
        raise PilotError(422, "invalid_input", "Запит містить помилку.") from error


def _status_payload(job: JobRecord) -> dict[str, object]:
    payload = {
        "id": job.id,
        "status": job.status,
        "step": job.step,
        "revision": job.revision,
        "attempt": job.attempt,
        "attempt_history": list(job.attempt_history),
        "failure_code": job.failure_code,
        "failure_message": job.failure_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    progress = _teacher_safe_progress(job.progress)
    if progress is not None:
        payload["progress"] = progress
    return payload


def _resource_payload(
    job: JobRecord,
    activity_feedback: dict[str, dict[str, object]] | None = None,
    activity_regenerations: list[ActivityRegenerationRecord] | None = None,
) -> dict[str, object]:
    if job.lesson is None:  # pragma: no cover - enforced by the ready-state check
        raise RuntimeError("A ready lesson aggregate must have a lesson document.")
    lesson = copy.deepcopy(job.lesson)
    # The stored document is valid against the digest-pinned public schema,
    # which requires these anchor provenance fields.  The frozen OpenAPI
    # browser contract deliberately narrows LessonAnchor to pasted text,
    # source, and character count.
    lesson.get("anchor", {}).pop("fingerprint", None)
    lesson.get("anchor", {}).pop("diagnostics", None)
    return {
        "lesson_id": job.id,
        "revision": job.revision,
        "accepted_at": job.accepted_at,
        "accepted_revision": job.accepted_revision,
        "warning_acknowledgements": sorted(job.warning_acknowledgements),
        # Applicable-only teacher verdicts on engine-flagged blocks (#402):
        # a row whose flag-time hash no longer matches the live block is
        # presented as "no feedback yet", never as a current judgment.
        "activity_feedback": dict(activity_feedback or {}),
        "activity_regenerations": [
            _activity_regeneration_payload(item) for item in (activity_regenerations or [])
        ],
        "logical_model_id": job.logical_model_id,
        # Keep the pinned lesson document valid while giving current clients
        # explicit names for the durable create-form choices.
        "methodology": job.methodology,
        "grammar_focus": job.grammar_focus,
        "lesson": lesson,
    }


def _activity_regeneration_payload(record: ActivityRegenerationRecord) -> dict[str, object]:
    """Return status/provenance only; block snapshots remain private durable history."""
    return {
        "id": record.id,
        "lesson_id": record.lesson_id,
        "block_id": record.block_id,
        "base_revision": record.base_revision,
        "status": record.status,
        "attempt": record.attempt,
        "failure_code": record.failure_code,
        "failure_message": record.failure_message,
        "prompt_version": record.prompt_version,
        "prompt_sha256": record.prompt_sha256,
        "old_block_hash": record.old_block_hash,
        "new_block_hash": record.new_block_hash,
        "applied_revision": record.applied_revision,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
    }


def _catalog_payload(jobs: list[Any]) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for job in jobs:
        items.append(
            {
                "id": job.id,
                "title": job.title,
                "status": job.status,
                "duration": job.duration,
                "focus": job.focus,
                "methodology": job.methodology,
                "grammar_focus": job.grammar_focus,
                "anchor_snippet": job.anchor_snippet,
                "level": job.level,
                "revision": job.revision,
                "accepted": job.accepted,
                "accepted_at": job.accepted_at,
                "accepted_revision": job.accepted_revision,
                "failure_code": job.failure_code,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
        )
    return {"lessons": items}


def _lesson_id(value: UUID) -> str:
    return str(value)


def _github_bearer_token(authorization: str | None) -> str:
    """Return one exact standard Bearer credential without retaining the header."""
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise ReviewAttestationError("github_token_required", status_code=401)
    token = authorization.removeprefix("Bearer ")
    if not token or any(character.isspace() for character in token) or "," in token:
        raise ReviewAttestationError("github_token_required", status_code=401)
    return token


def _refuse_model_less_generation(_prompt: str) -> str:
    """Stand in for the removed process-global generator (#563).

    ``_ProductionLessonBaker`` refuses a ``None`` logical-model route before
    ever selecting a generator, so this callable is unreachable in normal
    operation. It exists only so the top-level ``EngineLessonBaker`` always
    has *some* generator, without eagerly constructing a real provider port
    (and therefore without requiring a non-subscription credential, or an
    OpenRouter/Google-AIS route the operator may have deliberately left
    unconfigured) at process startup.
    """
    raise GeneratorUnavailable(
        "No process-global provider route is configured; every lesson bakes "
        "through a qualified per-lesson model."
    )


class _ProductionLessonBaker(EngineLessonBaker):
    """The auto-constructed production baker refuses model-less legacy jobs.

    Pre-#244 jobs and activity regenerations persisted no durable
    ``logical_model_id``. ``EngineLessonBaker.for_logical_model(None)``
    otherwise falls back to this instance's own process-global generator --
    previously a legacy Gemma/OpenRouter route the operator may not have
    (and, per #563, need not) configured.  Refuse that route explicitly here,
    before the runner's ``begin_provider_call`` bookkeeping and before any
    provider port is constructed, so a seeded legacy job reaches a safe
    terminal failure instead of silently reaching for a forbidden provider.

    This subclass exists only so tests that inject a plain
    ``EngineLessonBaker`` directly (to exercise its own preflight/generation
    behavior without qualified-model routing) keep their original semantics;
    only the app factory's own default baker refuses this way.
    """

    def for_logical_model(self, logical_model_id: str | None) -> EngineLessonBaker:
        if logical_model_id is None:
            raise ProviderUnavailable(
                "This job has no durable qualified-model route; legacy "
                "model-less baking is no longer supported.",
                retry_exhausted=True,
            )
        return super().for_logical_model(logical_model_id)


def create_app(
    *,
    settings: Settings | None = None,
    baker: LessonBaker | None = None,
    model_registry: QualifiedModelRegistry | None = None,
) -> FastAPI:
    """Create the one-process application; it deliberately exposes no bearer path."""
    settings = settings or Settings.from_env()
    production_routing_required = baker is None
    model_registry = model_registry or default_model_registry(
        required_provenance_by_route={
            "gemini-flash-subscription": settings.subscription_qualification_provenance_tier,
            "gemini-pro-subscription": settings.subscription_qualification_provenance_tier,
        }
    )
    store = JobStore(settings.database_path)
    store.initialize()
    configure_provider_concurrency(settings.max_provider_concurrency)

    @cache
    def logical_generator(logical_model_id: str):
        # Shared per logical identity so Gemma's internal primary selection
        # remains balanced across jobs rather than restarting at route one.
        model = model_registry.require_qualified(logical_model_id)
        return make_logical_model_generator(
            logical_model_id,
            settings.bake_providers,
            qualified_routes=model.provider_routes,
        )

    @cache
    def logical_semantic_reviewer(logical_model_id: str):
        # A separate selector guarantees a separate provider invocation and
        # prompt context from the lesson serializer while retaining the exact
        # route set covered by the logical model's qualification receipts.
        model = model_registry.require_qualified(logical_model_id)
        return make_logical_model_generator(
            logical_model_id,
            settings.bake_providers,
            qualified_routes=model.provider_routes,
        )

    baker = baker or _ProductionLessonBaker(
        store=store,
        generator=_refuse_model_less_generation,
        engine_out_dir=configured_engine_out_dir(),
        logical_generator_factory=logical_generator,
        semantic_reviewer_factory=logical_semantic_reviewer,
    )
    runner = BakeRunner(
        store,
        baker,
        settings.bake_hard_timeout_seconds,
        worker_count=settings.bake_workers,
    )

    def refuse_unclaimed_admission() -> None:
        # submit() returning false must close the pool before fail_queued_*
        # otherwise the 1s idle poll can claim the just-inserted draft and
        # leave the HTTP fallback projecting ``baking``.
        runner.close_admission()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # In-process engine work is not resumable after a process exit.  Mark it
        # durably failed before this process claims any new aggregate.
        store.recover_baking_jobs(settings.bake_hard_timeout_seconds)
        store.recover_activity_regenerations()
        runner.start()
        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(
        title="Hramatka teacher pilot API",
        version="1.0.0-pilot-frozen",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner
    app.state.baker = baker
    app.state.review_attestor = ReviewAttestor(settings)
    app.state.model_registry = model_registry

    @app.exception_handler(PilotError)
    async def pilot_error(_: Request, error: PilotError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.payload())

    @app.exception_handler(PersistenceUnavailable)
    async def persistence_error(_: Request, __: PersistenceUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "code": "persistence_unavailable",
                "message": "Не вдалося зберегти запит. Спробуйте, будь ласка, ще раз.",
                "retryable": True,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError) -> JSONResponse:
        # Most validation errors deliberately remain generic: a request can
        # contain sensitive pasted text. Duration is the bounded exception —
        # teachers need a clear scope explanation instead of a mysterious 422.
        duration_rejected = request.url.path in {
            "/api/lessons",
            "/api/teacher/preferences",
        } and any(
            item.get("loc", ())[-1:] in {("duration",), ("default_duration",)}
            for item in error.errors()
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "code": "invalid_input",
                "message": (
                    "Нові уроки наразі доступні лише у 45-хвилинному форматі, "
                    "доки цей формат проходить перевірку вчителями."
                    if duration_rejected
                    else "Запит містить помилку."
                ),
                "retryable": False,
            },
        )

    @app.exception_handler(ReviewAttestationError)
    async def review_attestation_error(_: Request, error: ReviewAttestationError) -> JSONResponse:
        # This endpoint accepts ephemeral machine credentials.  Its public
        # response is intentionally tiny and never includes token, provider,
        # GitHub, or OIDC validation detail.
        return JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "retryable": error.retryable},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, __: StarletteHTTPException) -> JSONResponse:
        # The frozen API never returns FastAPI's default ``detail`` envelope.
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "code": "lesson_not_found",
                "message": "Урок не знайдено.",
                "retryable": False,
            },
        )

    @app.middleware("http")
    async def sensitive_no_store(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if (
            request.url.path.startswith("/api/session")
            or request.url.path.startswith("/api/passkeys")
            or request.url.path.startswith("/api/recovery-codes")
            or request.url.path.startswith("/api/lessons")
            or request.url.path.startswith("/api/anchor")
            or request.url.path.startswith("/api/lesson-models")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_json(request: Request) -> None:
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].lower()
        if content_type != "application/json":
            raise PilotError(422, "invalid_input", "Запит містить помилку.")

    @app.post("/api/internal/review-attestations")
    async def create_review_attestation(
        request: Request,
        _: None = Depends(require_json),
        oidc_token: Annotated[str | None, Header(alias="X-GitHub-OIDC-Token")] = None,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, object]:
        content_length = request.headers.get("content-length")
        if content_length is not None and (
            not content_length.isdigit() or int(content_length) > 65_536
        ):
            raise ReviewAttestationError("invalid_request", status_code=422)
        body = await request.body()
        if len(body) > 65_536:
            raise ReviewAttestationError("invalid_request", status_code=422)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise ReviewAttestationError("invalid_request", status_code=422) from error
        attestor: ReviewAttestor = app.state.review_attestor
        github_token = _github_bearer_token(authorization)
        return await run_in_threadpool(
            attestor.attest,
            payload,
            oidc_token=oidc_token,
            github_token=github_token,
        )

    def require_origin(origin: Annotated[str | None, Header()] = None) -> None:
        if origin != settings.pilot_origin:
            raise PilotError(
                403, "csrf_rejected", "Запит не пройшов перевірку того самого походження."
            )

    def require_session(request: Request) -> AuthenticatedSession:
        raw_secret = _decode_opaque(request.cookies.get(_SESSION_COOKIE))
        if raw_secret is None:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.")
        record = store.lookup_session(raw_secret)
        if record is None:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.")
        return AuthenticatedSession(record=record, raw_secret=raw_secret)

    # Routers mounted during application construction cannot import this
    # closure: it is bound to this application's cookie decoder and JobStore.
    # Expose the dependency only on this app instance so they preserve the same
    # opaque-session proof rather than creating a parallel auth route.
    app.state.require_session = require_session

    def require_mutation_session(
        _: None = Depends(require_origin),
        session: AuthenticatedSession = Depends(require_session),
        supplied_csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> AuthenticatedSession:
        if not csrf_matches(settings.csrf_hmac_key, session.raw_secret, supplied_csrf):
            raise PilotError(
                403, "csrf_rejected", "Запит не пройшов перевірку того самого походження."
            )
        return session

    def session_payload(session: AuthenticatedSession) -> dict[str, object]:
        payload: dict[str, object] = {
            "teacher": {
                "id": session.record.teacher_id,
                "display_name": session.record.teacher_display_name,
            },
            "expires_at": session.record.expires_at,
            "csrf_token": csrf_token(settings.csrf_hmac_key, session.raw_secret),
        }
        if settings.local_static_teacher_enabled and session.record.auth_method == "local":
            payload["local_auth_disabled"] = True
        return payload

    def set_session_cookie(response: Response, raw_secret: bytes, expires_at: str) -> None:
        response.headers.append(
            "Set-Cookie",
            f"{_SESSION_COOKIE}={_encode_opaque(raw_secret)}; Path=/; "
            f"Max-Age={_session_cookie_max_age(expires_at)}; "
            "HttpOnly; Secure; SameSite=Lax",
        )

    def session_response(redeemed) -> Response:
        """Every entry door terminates in the one existing cookie session contract."""
        session = AuthenticatedSession(record=redeemed.session, raw_secret=redeemed.raw_secret)
        response = JSONResponse(content=session_payload(session))
        set_session_cookie(response, redeemed.raw_secret, redeemed.session.expires_at)
        return response

    def owner_job(teacher_id: str, lesson_id: str) -> JobRecord:
        job = store.get(teacher_id, lesson_id)
        if job is None:
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.")
        return job

    def lesson_resource(job: JobRecord) -> dict[str, object]:
        """The one lesson-resource assembly: feedback plus durable replacement state."""
        feedback: dict[str, dict[str, object]] = {}
        regenerations: list[ActivityRegenerationRecord] = []
        if job.status == "ready" and job.lesson is not None:
            feedback = store.applicable_activity_feedback(job.teacher_id, job.id)
            regenerations = store.list_activity_regenerations(job.teacher_id, job.id)
        return _resource_payload(
            job,
            activity_feedback=feedback,
            activity_regenerations=regenerations,
        )

    def raise_review_mutation_error(error: Exception, lesson_id: str) -> None:
        """Map every durable review-edit failure to the frozen error envelope."""
        if isinstance(error, LessonNotFound):
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.") from error
        if isinstance(error, LessonBlockNotFound):
            raise PilotError(404, "lesson_block_not_found", "Блок уроку не знайдено.") from error
        if isinstance(error, RejectedEntryNotFound):
            raise PilotError(
                404, "rejected_entry_not_found", "Відхилений блок не знайдено."
            ) from error
        if isinstance(error, RevisionConflict):
            raise PilotError(
                409,
                "revision_conflict",
                "Урок змінено; оновіть його перед повторною спробою.",
                lesson_id=lesson_id,
            ) from error
        if isinstance(error, LessonStateConflict):
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Стан уроку не дозволяє цю зміну.",
                lesson_id=lesson_id,
            ) from error
        if isinstance(error, ReviewMutationInvalid):
            raise PilotError(422, "invalid_input", "Запит містить помилку.") from error
        raise error

    def raise_regeneration_error(error: Exception, lesson_id: str) -> None:
        if isinstance(error, LessonNotFound):
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.") from error
        if isinstance(error, LessonBlockNotFound):
            raise PilotError(404, "lesson_block_not_found", "Блок уроку не знайдено.") from error
        if isinstance(error, ActivityRegenerationNotFound):
            raise PilotError(
                404, "regeneration_not_found", "Спробу створення варіанта не знайдено."
            ) from error
        if isinstance(error, RevisionConflict):
            raise PilotError(
                409,
                "revision_conflict",
                "Урок змінено; оновіть його перед повторною спробою.",
                lesson_id=lesson_id,
            ) from error
        if isinstance(error, ActivityRegenerationInProgress):
            raise PilotError(
                409,
                "regeneration_in_progress",
                "Для цього уроку вже створюється новий варіант вправи.",
                lesson_id=lesson_id,
            ) from error
        if isinstance(error, (LessonStateConflict, ActivityRegenerationStateConflict)):
            raise PilotError(
                409,
                "regeneration_state_conflict",
                "Стан уроку або блока не дозволяє створити цей варіант.",
                lesson_id=lesson_id,
            ) from error
        if isinstance(error, IdempotencyConflict):
            raise PilotError(
                409,
                "idempotency_conflict",
                "Цей ідентифікатор уже пов’язаний з іншою спробою.",
                lesson_id=lesson_id,
            ) from error
        if isinstance(error, (ReviewMutationInvalid, ValueError)):
            raise PilotError(422, "invalid_input", "Запит містить помилку.") from error
        raise error

    @app.post("/api/session/redeem")
    def redeem_invite(
        request_body: InviteRedeem,
        _: None = Depends(require_json),
        __: None = Depends(require_origin),
    ) -> Response:
        try:
            redeemed = store.redeem_invite(request_body.token, request_body.nonce)
        except TokenFormatError as error:
            raise PilotError(422, "invalid_input", "Запит містить помилку.") from error
        except InviteUnavailable as error:
            raise PilotError(
                410, "invite_unavailable", "Це запрошення більше недоступне."
            ) from error
        return session_response(redeemed)

    @app.post("/api/passkeys/enrollment/options")
    def passkey_enrollment_options(
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        # First binding is deliberately possible only from the invite-created
        # session: no name, email, or client-supplied identity can bind a key.
        if session.record.auth_method != "invite":
            raise PilotError(
                403, "passkey_enrollment_forbidden", "Потрібна первинна сесія за запрошенням."
            )
        challenge = store.issue_webauthn_challenge(
            kind="enrollment", teacher_id=session.teacher_id, session_id=session.record.id
        )
        return {
            "challenge_id": challenge.id,
            "publicKey": enrollment_options(
                origin=settings.pilot_origin,
                teacher_id=session.teacher_id,
                display_name=session.record.teacher_display_name,
                challenge=challenge.raw_challenge,
            ),
        }

    @app.post("/api/passkeys/enrollment/{challenge_id}")
    def finish_passkey_enrollment(
        challenge_id: UUID,
        request_body: WebAuthnCredential,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        if session.record.auth_method != "invite":
            raise PilotError(
                403, "passkey_enrollment_forbidden", "Потрібна первинна сесія за запрошенням."
            )
        challenge = _webauthn_challenge(request_body.credential)
        try:
            verified = verify_registration(
                credential=request_body.credential,
                challenge=challenge,
                origin=settings.pilot_origin,
            )
            if (
                store.consume_webauthn_challenge(
                    challenge_id=str(challenge_id),
                    raw_challenge=challenge,
                    kind="enrollment",
                    session_id=session.record.id,
                )
                != session.teacher_id
            ):
                raise PilotError(
                    410, "passkey_challenge_unavailable", "Перевірка ключа більше недоступна."
                )
            store.add_webauthn_credential(
                teacher_id=session.teacher_id,
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
            )
            recovery_codes = store.regenerate_recovery_codes(session.teacher_id)
        except (InvalidRegistrationResponse, ValueError, TypeError) as error:
            raise PilotError(422, "invalid_input", "Запит містить помилку.") from error
        return {"recovery_codes": recovery_codes}

    @app.post("/api/passkeys/authentication/options")
    def passkey_assertion_options(_: None = Depends(require_origin)) -> dict[str, object]:
        challenge = store.issue_webauthn_challenge(kind="assertion")
        return {
            "challenge_id": challenge.id,
            "publicKey": assertion_options(
                origin=settings.pilot_origin, challenge=challenge.raw_challenge
            ),
        }

    @app.post("/api/passkeys/authentication")
    def finish_passkey_assertion(
        request_body: WebAuthnAssertion,
        _: None = Depends(require_json),
        __: None = Depends(require_origin),
    ) -> Response:
        challenge = _webauthn_challenge(request_body.credential)
        try:
            credential_id = base64.urlsafe_b64decode(
                str(request_body.credential["rawId"])
                + "=" * (-len(str(request_body.credential["rawId"])) % 4)
            )
        except (KeyError, ValueError, binascii.Error) as error:
            raise PilotError(422, "invalid_input", "Запит містить помилку.") from error
        credential = store.lookup_webauthn_credential(credential_id)
        if credential is None:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.")
        try:
            verified = verify_assertion(
                credential=request_body.credential,
                challenge=challenge,
                origin=settings.pilot_origin,
                public_key=credential.public_key,
                sign_count=credential.sign_count,
            )
        except (InvalidAuthenticationResponse, ValueError, TypeError) as error:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.") from error
        if (
            store.consume_webauthn_challenge(
                challenge_id=str(request_body.challenge_id),
                raw_challenge=challenge,
                kind="assertion",
            )
            is not True
        ):
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.")
        teacher_id = store.record_webauthn_use(verified.credential_id, verified.new_sign_count)
        if teacher_id is None:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.")
        return session_response(store.mint_reentry_session(teacher_id, auth_method="passkey"))

    @app.post("/api/recovery-codes/regenerate")
    def regenerate_recovery_codes(
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        return {"recovery_codes": store.regenerate_recovery_codes(session.teacher_id)}

    @app.post("/api/recovery-codes/redeem")
    def redeem_recovery_code(
        request_body: RecoveryCodeRedeem,
        _: None = Depends(require_json),
        __: None = Depends(require_origin),
    ) -> Response:
        teacher_id = store.redeem_recovery_code(request_body.code)
        if teacher_id is None:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.")
        return session_response(store.mint_reentry_session(teacher_id, auth_method="recovery"))

    if settings.local_static_teacher_enabled:

        @app.get("/api/session/local-teacher")
        def establish_local_static_teacher_session() -> Response:
            """Establish a local-only teacher session, then enter the SPA."""
            try:
                established = store.create_local_static_session()
            except SessionUnavailable as error:
                raise PilotError(
                    401, "session_required", "Потрібна чинна сесія вчителя."
                ) from error
            response = RedirectResponse(url="/teacher/", status_code=status.HTTP_303_SEE_OTHER)
            set_session_cookie(response, established.raw_secret, established.session.expires_at)
            return response

    @app.get("/api/session")
    def get_session(session: AuthenticatedSession = Depends(require_session)) -> dict[str, object]:
        return session_payload(session)

    @app.delete("/api/session", status_code=status.HTTP_204_NO_CONTENT)
    def logout(session: AuthenticatedSession = Depends(require_mutation_session)) -> Response:
        store.logout_session(session.raw_secret)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.headers.append(
            "Set-Cookie", f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
        )
        return response

    @app.get("/api/teacher/preferences")
    def get_teacher_preferences(
        session: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, object]:
        """Owner-scoped read of persisted defaults (no CSRF needed for GET)."""
        dur = store.get_teacher_default_duration(session.teacher_id)
        return {"default_duration": dur}

    @app.get("/api/lesson-models")
    def get_lesson_models(
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, object]:
        """Expose qualified logical choices, never provider routing details."""
        if not production_routing_required:
            return model_registry.public_payload()
        operational: set[str] = set()
        for model in model_registry.qualified_models():
            try:
                logical_generator(model.id)
            except (LogicalModelUnavailable, ValueError):
                continue
            operational.add(model.id)
        return model_registry.public_payload(operational_model_ids=frozenset(operational))

    @app.put("/api/teacher/preferences")
    def put_teacher_preferences(
        request_body: TeacherPreferences,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        """Owner-scoped upsert; requires Origin + X-CSRF-Token (per #113 patterns)."""
        store.set_teacher_default_duration(session.teacher_id, request_body.default_duration)
        return {"default_duration": request_body.default_duration}

    @app.post("/api/anchor/import-url")
    def import_anchor_url(
        request_body: UrlImportRequest,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        try:
            result = fetch_url_text(request_body.url, teacher_id=session.teacher_id)
        except UrlImportError as error:
            if error.code == "url_rate_limited":
                status_code = status.HTTP_429_TOO_MANY_REQUESTS
            elif error.code == "url_fetch_failed":
                status_code = status.HTTP_502_BAD_GATEWAY
            else:
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            raise PilotError(
                status_code,
                error.code,
                error.message,
                retryable=error.retryable,
            ) from error
        try:
            prepared_text = prepare_anchor_text(result.text)
        except AnchorPreparationError as error:
            message = (
                "Виправте можливі помилки OCR у тексті: "
                + ", ".join(f"«{token}»" for token in error.suspicious_tokens)
                + "."
                if error.suspicious_tokens
                else "На сторінці не знайдено достатньо зв’язного українського тексту."
            )
            raise PilotError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "anchor_unusable",
                message,
            ) from error
        return {"text": prepared_text, "source_url": result.source_url}

    @app.post("/api/lessons", status_code=status.HTTP_202_ACCEPTED)
    def create_lesson(
        request_body: LessonCreate,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_id = _lesson_id(request_body.id)
        try:
            prepared_anchor_text = prepare_anchor_text(request_body.anchor.text)
        except AnchorPreparationError as error:
            message = (
                "Виправте можливі помилки OCR у тексті: "
                + ", ".join(f"«{token}»" for token in error.suspicious_tokens)
                + "."
                if error.suspicious_tokens
                else "У тексті не знайдено придатного українського уривка для уроку."
            )
            raise PilotError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "anchor_unusable",
                message,
            ) from error
        logical_model_id = request_body.logical_model_id
        if logical_model_id is None and production_routing_required:
            raise PilotError(
                409,
                "model_unavailable",
                "Оберіть доступну кваліфіковану модель.",
            )
        if logical_model_id is not None:
            try:
                model_registry.require_qualified(logical_model_id)
                if production_routing_required:
                    logical_generator(logical_model_id)
            except LogicalModelUnavailable as error:
                raise PilotError(
                    409,
                    "model_unavailable",
                    "Обрана модель зараз недоступна. Оновіть список моделей.",
                ) from error
            except ValueError as error:
                raise PilotError(
                    409,
                    "model_unavailable",
                    "Обрана модель не налаштована на цьому сервері. Оновіть список моделей.",
                ) from error
        try:
            with runner.hold_admission():
                job, created = store.create_or_get(
                    session.teacher_id,
                    lesson_id,
                    anchor_text=prepared_anchor_text,
                    anchor_source=request_body.anchor.source,
                    anchor_source_url=request_body.anchor.source_url,
                    level=request_body.level,
                    duration=request_body.duration,
                    focus=request_body.focus,
                    methodology=request_body.methodology,
                    grammar_focus=request_body.grammar_focus,
                    logical_model_id=logical_model_id,
                )
                if created and not runner.submit(job.id):
                    refuse_unclaimed_admission()
                    store.fail_queued_drafts(
                        "Сервіс складання уроків недоступний. Спробуйте, будь ласка, ще раз.",
                        failure_code="engine_unavailable",
                    )
                    job = owner_job(session.teacher_id, lesson_id)
        except IdempotencyConflict as error:
            raise PilotError(
                409,
                "idempotency_conflict",
                "Цей ідентифікатор уроку вже пов’язаний з іншими даними.",
                lesson_id=lesson_id,
            ) from error
        except SessionUnavailable as error:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.") from error
        return {"id": job.id, "status": job.status, "revision": job.revision, "reused": not created}

    @app.get("/api/lessons")
    def list_lessons(session: AuthenticatedSession = Depends(require_session)) -> dict[str, object]:
        return _catalog_payload(store.list_catalog(session.teacher_id))

    @app.get("/api/lessons/{lesson_id}/status")
    def lesson_status(
        lesson_id: UUID,
        session: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, object]:
        return _status_payload(owner_job(session.teacher_id, _lesson_id(lesson_id)))

    @app.get("/api/lessons/{lesson_id}")
    def get_lesson(
        lesson_id: UUID,
        session: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, object]:
        job = owner_job(session.teacher_id, _lesson_id(lesson_id))
        if job.status != "ready" or job.lesson is None:
            raise PilotError(409, "lesson_not_ready", "Урок ще не готовий.")
        return lesson_resource(job)

    @app.delete("/api/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_lesson(
        lesson_id: UUID,
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> Response:
        lesson_key = _lesson_id(lesson_id)
        job = owner_job(session.teacher_id, lesson_key)
        if job.status in {"draft", "baking"}:
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Спочатку скасуйте складання уроку; активне завдання не можна видалити.",
                lesson_id=lesson_key,
            )
        if job.status in {"cancelled", "failed"} and job.attempt_quiesced_at is None:
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Попередня спроба ще завершує запущену роботу; зачекайте перед видаленням.",
                lesson_id=lesson_key,
            )
        if not store.delete_lesson(session.teacher_id, lesson_key):
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/lessons/{lesson_id}/cancel")
    def cancel_lesson(
        lesson_id: UUID,
        request_body: RevisionMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job, _ = store.cancel(
                session.teacher_id,
                lesson_key,
                expected_revision=request_body.expected_revision,
            )
        except LessonNotFound as error:
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.") from error
        except RevisionConflict as error:
            raise PilotError(
                409,
                "revision_conflict",
                "Урок змінено; оновіть його перед повторною спробою.",
                lesson_id=lesson_key,
            ) from error
        except LessonStateConflict as error:
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Стан уроку не дозволяє скасування.",
                lesson_id=lesson_key,
            ) from error
        return _status_payload(job)

    @app.post("/api/lessons/{lesson_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_lesson(
        lesson_id: UUID,
        request_body: RevisionMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            with runner.hold_admission():
                job, queued = store.retry(
                    session.teacher_id,
                    lesson_key,
                    expected_revision=request_body.expected_revision,
                )
                if queued and not runner.submit(job.id):
                    refuse_unclaimed_admission()
                    store.fail_queued_drafts(
                        "Сервіс складання уроків недоступний. Спробуйте, будь ласка, ще раз.",
                        failure_code="engine_unavailable",
                    )
                    job = owner_job(session.teacher_id, lesson_key)
        except LessonNotFound as error:
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.") from error
        except RevisionConflict as error:
            raise PilotError(
                409,
                "revision_conflict",
                "Урок змінено; оновіть його перед повторною спробою.",
                lesson_id=lesson_key,
            ) from error
        except AttemptQuiescing as error:
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Скасування ще завершує запущену роботу; зачекайте перед повторною спробою.",
                lesson_id=lesson_key,
            ) from error
        except LessonStateConflict as error:
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Стан уроку не дозволяє повторне складання.",
                lesson_id=lesson_key,
            ) from error
        return _status_payload(job)

    @app.post("/api/lessons/{lesson_id}/recreate", status_code=status.HTTP_202_ACCEPTED)
    def recreate_lesson(
        lesson_id: UUID,
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        source_job = owner_job(session.teacher_id, _lesson_id(lesson_id))
        if not source_job.request_json.strip():
            raise PilotError(
                422,
                "invalid_input",
                "Немає збереженого запиту для повторного створення уроку.",
            )
        try:
            request = source_job.request
            anchor = request["anchor"]
            anchor_text = anchor["text"]
            anchor_source = anchor["source"]
            anchor_source_url = anchor.get("source_url")
            level = request["level"]
            # A stored 60/90-minute lesson remains readable, but recreating it
            # is a new generation and therefore enters only the qualified path.
            duration = 45
            focus = request["focus"]
            methodology = request.get("methodology", "ttt")
            grammar_focus = request.get("grammar_focus")
            logical_model_id = request.get("logical_model_id")
        except (KeyError, TypeError, ValueError):
            raise PilotError(
                422,
                "invalid_input",
                "Немає збереженого запиту для повторного створення уроку.",
            ) from None
        if not isinstance(anchor_text, str) or not anchor_text.strip():
            raise PilotError(
                422,
                "invalid_input",
                "Немає збереженого запиту для повторного створення уроку.",
            )
        try:
            canonical_request_json(
                anchor_text=anchor_text,
                anchor_source=anchor_source,
                anchor_source_url=anchor_source_url,
                level=level,
                duration=duration,
                focus=focus,
                methodology=methodology,
                grammar_focus=grammar_focus,
                logical_model_id=logical_model_id,
            )
        except ValueError:
            raise PilotError(422, "invalid_input", "Запит містить помилку.") from None
        if logical_model_id is None:
            raise PilotError(
                409,
                "model_unavailable",
                "Для старого уроку модель не збережено. Створіть новий урок.",
            )
        try:
            model_registry.require_qualified(logical_model_id)
            if production_routing_required:
                logical_generator(logical_model_id)
        except (LogicalModelUnavailable, ValueError) as error:
            raise PilotError(
                409,
                "model_unavailable",
                "Модель цього уроку більше не доступна. Створіть новий урок.",
            ) from error
        new_lesson_id = str(uuid.uuid4())
        try:
            with runner.hold_admission():
                job, created = store.create_or_get(
                    session.teacher_id,
                    new_lesson_id,
                    anchor_text=anchor_text,
                    anchor_source=anchor_source,
                    anchor_source_url=anchor_source_url,
                    level=level,
                    duration=duration,
                    focus=focus,
                    methodology=methodology,
                    grammar_focus=grammar_focus,
                    logical_model_id=logical_model_id,
                )
                if created and not runner.submit(job.id):
                    refuse_unclaimed_admission()
                    store.fail_queued_drafts(
                        "Сервіс складання уроків недоступний. Спробуйте, будь ласка, ще раз.",
                        failure_code="engine_unavailable",
                    )
                    job = owner_job(session.teacher_id, new_lesson_id)
        except IdempotencyConflict as error:
            raise PilotError(
                409,
                "idempotency_conflict",
                "Цей ідентифікатор уроку вже пов’язаний з іншими даними.",
                lesson_id=new_lesson_id,
            ) from error
        except SessionUnavailable as error:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.") from error
        except ValueError:
            raise PilotError(422, "invalid_input", "Запит містить помилку.") from None
        return {"id": job.id, "status": job.status, "revision": job.revision, "reused": not created}

    @app.post(
        "/api/lessons/{lesson_id}/blocks/{block_id}/regenerations",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_activity_regeneration(
        lesson_id: UUID,
        block_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
        ],
        request_body: ActivityRegenerationCreate,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        lesson_job = owner_job(session.teacher_id, lesson_key)
        logical_model_id = lesson_job.logical_model_id
        if logical_model_id is None:
            raise PilotError(
                409,
                "model_unavailable",
                "Для цього уроку не збережено модель. Створіть новий урок.",
            )
        try:
            model_registry.require_qualified(logical_model_id)
            if production_routing_required:
                logical_generator(logical_model_id)
        except (LogicalModelUnavailable, ValueError) as error:
            raise PilotError(
                409,
                "model_unavailable",
                "Модель цього уроку більше не доступна. Створіть новий урок.",
            ) from error
        try:
            with runner.hold_admission():
                regeneration, created = store.create_or_get_activity_regeneration(
                    session.teacher_id,
                    str(request_body.id),
                    lesson_id=lesson_key,
                    block_id=block_id,
                    expected_revision=request_body.expected_revision,
                    feedback=request_body.feedback,
                    prompt_version=REGENERATION_PROMPT_VERSION,
                    prompt_sha256=REGENERATION_PROMPT_SHA256,
                )
                if created and not runner.submit(regeneration.id):
                    refuse_unclaimed_admission()
                    store.fail_activity_regeneration(
                        session.teacher_id,
                        regeneration.id,
                        "engine_unavailable",
                        "Сервіс створення варіантів недоступний. Попередній блок збережено.",
                    )
                    regeneration = (
                        store.get_activity_regeneration(session.teacher_id, regeneration.id)
                        or regeneration
                    )
        except (
            LessonNotFound,
            LessonBlockNotFound,
            LessonStateConflict,
            RevisionConflict,
            ActivityRegenerationInProgress,
            IdempotencyConflict,
            ValueError,
        ) as error:
            raise_regeneration_error(error, lesson_key)
        return {**_activity_regeneration_payload(regeneration), "reused": not created}

    @app.get("/api/lessons/{lesson_id}/regenerations/{regeneration_id}")
    def get_activity_regeneration(
        lesson_id: UUID,
        regeneration_id: UUID,
        session: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        owner_job(session.teacher_id, lesson_key)
        regeneration = store.get_activity_regeneration(session.teacher_id, str(regeneration_id))
        if regeneration is None or regeneration.lesson_id != lesson_key:
            raise PilotError(
                404, "regeneration_not_found", "Спробу створення варіанта не знайдено."
            )
        return _activity_regeneration_payload(regeneration)

    @app.post(
        "/api/lessons/{lesson_id}/regenerations/{regeneration_id}/retry",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_activity_regeneration(
        lesson_id: UUID,
        regeneration_id: UUID,
        request_body: RevisionMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        regeneration = store.get_activity_regeneration(session.teacher_id, str(regeneration_id))
        if regeneration is None or regeneration.lesson_id != lesson_key:
            raise PilotError(
                404, "regeneration_not_found", "Спробу створення варіанта не знайдено."
            )
        try:
            model_registry.require_qualified(regeneration.logical_model_id)
            if production_routing_required:
                logical_generator(regeneration.logical_model_id)
        except (LogicalModelUnavailable, ValueError) as error:
            raise PilotError(
                409,
                "model_unavailable",
                "Модель цього уроку більше не доступна. Створіть новий урок.",
            ) from error
        try:
            with runner.hold_admission():
                retried = store.retry_activity_regeneration(
                    session.teacher_id,
                    str(regeneration_id),
                    expected_revision=request_body.expected_revision,
                    prompt_version=REGENERATION_PROMPT_VERSION,
                    prompt_sha256=REGENERATION_PROMPT_SHA256,
                )
                if not runner.submit(retried.id):
                    refuse_unclaimed_admission()
                    store.fail_activity_regeneration(
                        session.teacher_id,
                        retried.id,
                        "engine_unavailable",
                        "Сервіс створення варіантів недоступний. Попередній блок збережено.",
                    )
                    retried = (
                        store.get_activity_regeneration(session.teacher_id, retried.id) or retried
                    )
        except (
            ActivityRegenerationNotFound,
            ActivityRegenerationInProgress,
            ActivityRegenerationStateConflict,
            LessonNotFound,
            LessonBlockNotFound,
            LessonStateConflict,
            RevisionConflict,
            ValueError,
        ) as error:
            raise_regeneration_error(error, lesson_key)
        return _activity_regeneration_payload(retried)

    @app.post("/api/lessons/{lesson_id}/blocks/{block_id}/accept")
    def acknowledge_warning(
        lesson_id: UUID,
        block_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
        ],
        request_body: RevisionMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.acknowledge_warning(
                session.teacher_id,
                lesson_key,
                block_id=block_id,
                expected_revision=request_body.expected_revision,
            )
        except LessonNotFound as error:
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.") from error
        except RevisionConflict as error:
            raise PilotError(
                409,
                "revision_conflict",
                "Урок змінено; оновіть його перед повторною спробою.",
                lesson_id=lesson_key,
            ) from error
        except LessonStateConflict as error:
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Стан уроку не дозволяє цю зміну.",
                lesson_id=lesson_key,
            ) from error
        except WarningBlockNotFound as error:
            raise PilotError(
                404, "warning_block_not_found", "Блок-попередження не знайдено."
            ) from error
        return lesson_resource(job)

    @app.post("/api/lessons/{lesson_id}/blocks/{block_id}/move")
    def move_block(
        lesson_id: UUID,
        block_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
        ],
        request_body: BlockMoveMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.move_block(
                session.teacher_id,
                lesson_key,
                block_id=block_id,
                direction=request_body.direction,
                expected_revision=request_body.expected_revision,
            )
        except (
            LessonNotFound,
            LessonBlockNotFound,
            RevisionConflict,
            LessonStateConflict,
            ReviewMutationInvalid,
        ) as error:
            raise_review_mutation_error(error, lesson_key)
        return lesson_resource(job)

    @app.post("/api/lessons/{lesson_id}/blocks/{block_id}/remove")
    def remove_block(
        lesson_id: UUID,
        block_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
        ],
        request_body: RevisionMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.remove_block(
                session.teacher_id,
                lesson_key,
                block_id=block_id,
                expected_revision=request_body.expected_revision,
            )
        except (
            LessonNotFound,
            LessonBlockNotFound,
            RevisionConflict,
            LessonStateConflict,
            ReviewMutationInvalid,
        ) as error:
            raise_review_mutation_error(error, lesson_key)
        return lesson_resource(job)

    @app.post("/api/lessons/{lesson_id}/blocks/{block_id}/include")
    def include_reserve_block(
        lesson_id: UUID,
        block_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
        ],
        request_body: RevisionMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.include_reserve_block(
                session.teacher_id,
                lesson_key,
                block_id=block_id,
                expected_revision=request_body.expected_revision,
            )
        except (
            LessonNotFound,
            LessonBlockNotFound,
            RevisionConflict,
            LessonStateConflict,
            ReviewMutationInvalid,
        ) as error:
            raise_review_mutation_error(error, lesson_key)
        return lesson_resource(job)

    @app.put("/api/lessons/{lesson_id}/blocks/{block_id}/activity")
    def replace_block_activity(
        lesson_id: UUID,
        block_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
        ],
        request_body: ActivityReplacementMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.replace_block_activity(
                session.teacher_id,
                lesson_key,
                block_id=block_id,
                activity=request_body.activity,
                expected_revision=request_body.expected_revision,
            )
        except (
            LessonNotFound,
            LessonBlockNotFound,
            RevisionConflict,
            LessonStateConflict,
            ReviewMutationInvalid,
        ) as error:
            raise_review_mutation_error(error, lesson_key)
        return lesson_resource(job)

    @app.put("/api/lessons/{lesson_id}/blocks/{block_id}/feedback")
    def put_activity_feedback(
        lesson_id: UUID,
        block_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
        ],
        request_body: ActivityFeedbackMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.put_activity_feedback(
                session.teacher_id,
                lesson_key,
                slot_id=block_id,
                verdict=request_body.verdict,
                comment=request_body.comment,
            )
        except FeedbackNotApplicable as error:
            raise PilotError(
                409,
                "feedback_not_applicable",
                "Блок наразі не позначено двигуном.",
                lesson_id=lesson_key,
            ) from error
        except (LessonNotFound, LessonBlockNotFound, LessonStateConflict) as error:
            raise_review_mutation_error(error, lesson_key)
        return lesson_resource(job)

    @app.post("/api/lessons/{lesson_id}/rejected/{rejected_index}/restore")
    def restore_rejected_entry(
        lesson_id: UUID,
        rejected_index: Annotated[int, Path(ge=0)],
        request_body: RestoreRejectedMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.restore_rejected_entry(
                session.teacher_id,
                lesson_key,
                rejected_index=rejected_index,
                phase=request_body.phase,
                expected_revision=request_body.expected_revision,
            )
        except (
            LessonNotFound,
            RejectedEntryNotFound,
            RevisionConflict,
            LessonStateConflict,
            ReviewMutationInvalid,
        ) as error:
            raise_review_mutation_error(error, lesson_key)
        return lesson_resource(job)

    @app.post("/api/lessons/{lesson_id}/accept")
    def accept_lesson(
        lesson_id: UUID,
        request_body: RevisionMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.accept_lesson(
                session.teacher_id, lesson_key, expected_revision=request_body.expected_revision
            )
        except LessonNotFound as error:
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.") from error
        except RevisionConflict as error:
            raise PilotError(
                409,
                "revision_conflict",
                "Урок змінено; оновіть його перед повторною спробою.",
                lesson_id=lesson_key,
            ) from error
        except LessonStateConflict as error:
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Стан уроку не дозволяє прийняття.",
                lesson_id=lesson_key,
            ) from error
        except WarningAcknowledgementsRequired as error:
            raise PilotError(
                409,
                "warning_acknowledgements_required",
                "Підтвердьте всі видимі попередження перед прийняттям уроку.",
                lesson_id=lesson_key,
            ) from error
        return lesson_resource(job)

    @app.post("/api/lessons/{lesson_id}/draft")
    def return_to_draft(
        lesson_id: UUID,
        request_body: RevisionMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.return_to_draft(
                session.teacher_id, lesson_key, expected_revision=request_body.expected_revision
            )
        except LessonNotFound as error:
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.") from error
        except RevisionConflict as error:
            raise PilotError(
                409,
                "revision_conflict",
                "Урок змінено; оновіть його перед повторною спробою.",
                lesson_id=lesson_key,
            ) from error
        except LessonStateConflict as error:
            raise PilotError(
                409,
                "lesson_state_conflict",
                "Стан уроку не дозволяє цю зміну.",
                lesson_id=lesson_key,
            ) from error
        return lesson_resource(job)

    @app.get("/api/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/readyz")
    def readyz() -> dict[str, str]:
        if (
            settings.mock_mode
            or not callable(getattr(baker, "resolve_data_bundle", None))
            or not store.is_ready()
        ):
            raise PilotError(
                503,
                "service_not_ready",
                "Сервіс ще не готовий.",
                retryable=True,
            )
        try:
            baker.resolve_data_bundle()
        except (data.DataConfigError, data.DataDriftError, OSError, ValueError) as error:
            raise PilotError(
                503,
                "service_not_ready",
                "Сервіс ще не готовий.",
                retryable=True,
            ) from error
        return {"status": "ready"}

    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
