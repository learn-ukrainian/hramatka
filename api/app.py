"""Frozen same-origin FastAPI surface for the private teacher pilot."""
# ruff: noqa: B008

from __future__ import annotations

import base64
import binascii
import copy
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Path, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from hramatka.engine import data

from .baking.engine_adapter import EngineLessonBaker
from .baking.port import LessonBaker
from .config import Settings
from .models import InviteRedeem, LessonCreate, RevisionMutation
from .runner import BakeRunner
from .security import csrf_matches, csrf_token
from .store import (
    IdempotencyConflict,
    InviteUnavailable,
    JobRecord,
    JobStore,
    LessonNotFound,
    LessonStateConflict,
    PersistenceUnavailable,
    RevisionConflict,
    SessionUnavailable,
    TokenFormatError,
    WarningAcknowledgementsRequired,
    WarningBlockNotFound,
)

_SESSION_COOKIE = "__Host-hramatka_session"
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")


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
    return {
        "id": job.id,
        "status": job.status,
        "step": job.step,
        "revision": job.revision,
        "failure_code": job.failure_code,
        "failure_message": job.failure_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


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


def create_app(*, settings: Settings | None = None, baker: LessonBaker | None = None) -> FastAPI:
    """Create the one-process application; it deliberately exposes no bearer path."""
    settings = settings or Settings.from_env()
    store = JobStore(settings.database_path)
    store.initialize()
    baker = baker or EngineLessonBaker()
    runner = BakeRunner(store, baker, settings.bake_hard_timeout_seconds)

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
        if request.url.path.startswith("/api/session") or request.url.path.startswith(
            "/api/lessons"
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_json(request: Request) -> None:
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].lower()
        if content_type != "application/json":
            raise PilotError(422, "invalid_input", "Запит містить помилку.")

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

    @app.post("/api/lessons", status_code=status.HTTP_202_ACCEPTED)
    def create_lesson(
        request_body: LessonCreate,
        _: None = Depends(require_json),
        session: AuthenticatedSession = Depends(require_mutation_session),
    ) -> dict[str, object]:
        lesson_id = _lesson_id(request_body.id)
        try:
            job, created = store.create_or_get(
                session.teacher_id,
                lesson_id,
                anchor_text=request_body.anchor.text,
                anchor_source=request_body.anchor.source,
                level=request_body.level,
                duration=request_body.duration,
                focus=request_body.focus,
            )
        except IdempotencyConflict as error:
            raise PilotError(
                409,
                "idempotency_conflict",
                "Цей ідентифікатор уроку вже пов’язаний з іншими даними.",
                lesson_id=lesson_id,
            ) from error
        except SessionUnavailable as error:
            raise PilotError(
                401, "session_required", "Потрібна чинна сесія вчителя."
            ) from error
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
        return {
            "lesson_id": job.id,
            "revision": job.revision,
            "warning_acknowledgements": sorted(job.warning_acknowledgements),
        }

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
        if settings.mock_mode or not isinstance(baker, EngineLessonBaker) or not store.is_ready():
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
