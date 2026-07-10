"""Thin FastAPI surface for durable, poll-first Hramatka lesson bakes."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

from .baking.mock import MockLessonBaker
from .baking.port import LessonBaker
from .config import Settings
from .models import LessonCreate, StatusResponse
from .runner import BakeRunner
from .store import IdempotencyConflict, JobRecord, JobStore


def create_app(*, settings: Settings | None = None, baker: LessonBaker | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = JobStore(settings.database_path)
    store.initialize()
    baker = baker or MockLessonBaker(
        delay_seconds=settings.mock_delay_seconds,
        fail=settings.mock_fail,
    )
    runner = BakeRunner(store, baker, settings.bake_hard_timeout_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # A fresh process cannot safely claim an in-flight in-memory worker; fail honestly.
        store.recover_baking_jobs(settings.bake_hard_timeout_seconds)
        runner.start()
        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(title="Hramatka bake API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner

    def require_teacher(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not secrets.compare_digest(supplied, settings.teacher_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    def get_job_or_404(lesson_id: str) -> JobRecord:
        job = store.get(lesson_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")
        return job

    @app.post("/lessons", status_code=status.HTTP_202_ACCEPTED)
    def create_lesson(request: LessonCreate, _: None = Depends(require_teacher)) -> dict[str, str]:
        try:
            job, created = store.create_or_get(
                id=request.id,
                anchor_text=request.anchor.text,
                anchor_source=request.anchor.source,
                duration=request.duration,
                focus=request.focus,
            )
        except IdempotencyConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        if created and not runner.submit(job.id):
            store.fail_queued_drafts(
                "Bake worker is unavailable after a hard timeout; restart the API before retrying."
            )
            job = get_job_or_404(job.id)
        return {"id": job.id, "status": job.status}

    @app.get("/lessons/{lesson_id}/status", response_model=StatusResponse)
    def lesson_status(lesson_id: str, _: None = Depends(require_teacher)) -> StatusResponse:
        runner.expire_and_quarantine()
        job = get_job_or_404(lesson_id)
        return StatusResponse(
            id=job.id,
            status=job.status,
            step=job.step,
            last_error=job.last_error,
        )

    @app.get("/lessons/{lesson_id}")
    def get_lesson(lesson_id: str, _: None = Depends(require_teacher)) -> dict:
        job = get_job_or_404(lesson_id)
        if job.status != "ready" or job.lesson is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Lesson is not ready")
        return job.lesson

    @app.post("/lessons/{lesson_id}/blocks/{block_id}/accept")
    def accept_warning_block(
        lesson_id: str,
        block_id: str,
        _: None = Depends(require_teacher),
    ) -> dict[str, object]:
        try:
            job = store.acknowledge_warning(lesson_id, block_id)
        except KeyError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Lesson not found",
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return {"id": job.id, "acknowledged_warning_blocks": sorted(job.warning_acknowledgements)}

    @app.post("/lessons/{lesson_id}/accept")
    def accept_lesson(lesson_id: str, _: None = Depends(require_teacher)) -> dict:
        try:
            job = store.accept_lesson(lesson_id)
        except KeyError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Lesson not found",
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        assert job.lesson is not None  # established by the ready-state check above
        return job.lesson

    @app.post("/lessons/{lesson_id}/draft")
    def return_to_draft(lesson_id: str, _: None = Depends(require_teacher)) -> dict:
        try:
            job = store.return_to_draft(lesson_id)
        except KeyError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Lesson not found",
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        assert job.lesson is not None
        return job.lesson

    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
