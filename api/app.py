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
from datetime import datetime
from functools import cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Path, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from hramatka.engine import data
from hramatka.engine.providers import (
    configure_provider_concurrency,
    make_bake_generator,
    make_logical_model_generator,
)

from .agent_monitor import router as agent_monitor_router
from .baking.artifacts import configured_engine_out_dir
from .baking.engine_adapter_v3 import EngineLessonBaker
from .baking.port import LessonBaker
from .config import Settings
from .models import (
    ActivityReplacementMutation,
    BlockMoveMutation,
    DurationMutation,
    InviteRedeem,
    LessonCreate,
    RestoreRejectedMutation,
    RevisionMutation,
    TeacherPreferences,
    UrlImportRequest,
)
from .qualified_models import (
    LogicalModelUnavailable,
    QualifiedModelRegistry,
    default_model_registry,
)
from .review_attestation import ReviewAttestationError, ReviewAttestor
from .runner import BakeRunner
from .security import csrf_matches, csrf_token
from .store import (
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


def _status_payload(job: JobRecord) -> dict[str, object]:
    payload = {
        "id": job.id,
        "status": job.status,
        "step": job.step,
        "revision": job.revision,
        "failure_code": job.failure_code,
        "failure_message": job.failure_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    progress = _teacher_safe_progress(job.progress)
    if progress is not None:
        payload["progress"] = progress
    return payload


def _resource_payload(job: JobRecord) -> dict[str, object]:
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
        "logical_model_id": job.logical_model_id,
        # Keep the pinned lesson document valid while giving current clients
        # explicit names for the durable create-form choices.
        "methodology": job.methodology,
        "grammar_focus": job.grammar_focus,
        "lesson": lesson,
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

    baker = baker or EngineLessonBaker(
        store=store,
        generator=make_bake_generator(settings.bake_providers),
        engine_out_dir=configured_engine_out_dir(),
        logical_generator_factory=logical_generator,
    )
    runner = BakeRunner(
        store,
        baker,
        settings.bake_hard_timeout_seconds,
        worker_count=settings.bake_workers,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # In-process engine work is not resumable after a process exit.  Mark it
        # durably failed before this process claims any new aggregate.
        store.recover_baking_jobs(settings.bake_hard_timeout_seconds)
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
    async def invalid_input(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "code": "invalid_input",
                "message": "Запит містить помилку.",
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

    # The monitor router is intentionally dependency-free as a reusable unit,
    # but the live API must not disclose its lease tokens without a teacher
    # session.  The session dependency lives in this factory because it closes
    # over this application's store.
    app.include_router(agent_monitor_router, dependencies=[Depends(require_session)])

    def session_payload(session: AuthenticatedSession) -> dict[str, object]:
        return {
            "teacher": {
                "id": session.record.teacher_id,
                "display_name": session.record.teacher_display_name,
            },
            "expires_at": session.record.expires_at,
            "csrf_token": csrf_token(settings.csrf_hmac_key, session.raw_secret),
        }

    def owner_job(teacher_id: str, lesson_id: str) -> JobRecord:
        job = store.get(teacher_id, lesson_id)
        if job is None:
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.")
        return job

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

    @app.post("/api/session/redeem")
    def redeem_invite(
        request_body: InviteRedeem,
        _: None = Depends(require_json),
        __: None = Depends(require_origin),
    ) -> Response:
        try:
            redeemed = store.redeem_invite(request_body.token)
        except TokenFormatError as error:
            raise PilotError(422, "invalid_input", "Запит містить помилку.") from error
        except InviteUnavailable as error:
            raise PilotError(
                410, "invite_unavailable", "Це запрошення більше недоступне."
            ) from error
        session = AuthenticatedSession(record=redeemed.session, raw_secret=redeemed.raw_secret)
        response = JSONResponse(content=session_payload(session))
        response.headers.append(
            "Set-Cookie",
            f"{_SESSION_COOKIE}={_encode_opaque(redeemed.raw_secret)}; Path=/; Max-Age=604800; "
            "HttpOnly; Secure; SameSite=Lax",
        )
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
        return {"text": result.text, "source_url": result.source_url}

    @app.post("/api/lessons", status_code=status.HTTP_202_ACCEPTED)
    def create_lesson(
        request_body: LessonCreate,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_id = _lesson_id(request_body.id)
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
            job, created = store.create_or_get(
                session.teacher_id,
                lesson_id,
                anchor_text=request_body.anchor.text,
                anchor_source=request_body.anchor.source,
                anchor_source_url=request_body.anchor.source_url,
                level=request_body.level,
                duration=request_body.duration,
                focus=request_body.focus,
                methodology=request_body.methodology,
                grammar_focus=request_body.grammar_focus,
                logical_model_id=logical_model_id,
            )
        except IdempotencyConflict as error:
            raise PilotError(
                409,
                "idempotency_conflict",
                "Цей ідентифікатор уроку вже пов’язаний з іншими даними.",
                lesson_id=lesson_id,
            ) from error
        except SessionUnavailable as error:
            raise PilotError(401, "session_required", "Потрібна чинна сесія вчителя.") from error
        if created and not runner.submit(job.id):
            store.fail_queued_drafts(
                "Сервіс складання уроків недоступний. Спробуйте, будь ласка, ще раз.",
                failure_code="engine_unavailable",
            )
            job = owner_job(session.teacher_id, lesson_id)
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
        return _resource_payload(job)

    @app.delete("/api/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_lesson(
        lesson_id: UUID,
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> Response:
        lesson_key = _lesson_id(lesson_id)
        if not store.delete_lesson(session.teacher_id, lesson_key):
            raise PilotError(404, "lesson_not_found", "Урок не знайдено.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
            duration = request["duration"]
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
        if created and not runner.submit(job.id):
            store.fail_queued_drafts(
                "Сервіс складання уроків недоступний. Спробуйте, будь ласка, ще раз.",
                failure_code="engine_unavailable",
            )
            job = owner_job(session.teacher_id, new_lesson_id)
        return {"id": job.id, "status": job.status, "revision": job.revision, "reused": not created}

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
        return _resource_payload(job)

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
        return _resource_payload(job)

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
        return _resource_payload(job)

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
        return _resource_payload(job)

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
        return _resource_payload(job)

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
        return _resource_payload(job)

    @app.post("/api/lessons/{lesson_id}/duration")
    def select_duration(
        lesson_id: UUID,
        request_body: DurationMutation,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_key = _lesson_id(lesson_id)
        try:
            job = store.select_duration(
                session.teacher_id,
                lesson_key,
                duration=request_body.duration,
                expected_revision=request_body.expected_revision,
            )
        except (
            LessonNotFound,
            RevisionConflict,
            LessonStateConflict,
            ReviewMutationInvalid,
        ) as error:
            raise_review_mutation_error(error, lesson_key)
        return _resource_payload(job)

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
        return _resource_payload(job)

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
        return _resource_payload(job)

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
