"""Exactly one in-process durable bake runner per Uvicorn process."""

from __future__ import annotations

import threading

from jsonschema import ValidationError

from .baking.port import BakeError, LessonBaker, ProviderUnavailable
from .lesson import materialize_lesson
from .store import JobStore
from .validation import validate_lesson

_SAFE_FAILURE_MESSAGE = "Не вдалося скласти урок. Спробуйте, будь ласка, ще раз."
_PROVIDER_RETRY_DELAY_SECONDS = 0.25


class BakeRunner:
    """Claim at most one SQLite job and never report an undurable result as ready."""

    def __init__(self, store: JobStore, baker: LessonBaker, hard_timeout_seconds: int) -> None:
        self._store = store
        self._baker = baker
        self._hard_timeout_seconds = hard_timeout_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._poisoned = threading.Event()
        self._state_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None

    def start(self) -> None:
        """Start the sole worker plus a watchdog for honest timeout failure."""
        with self._state_lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._work, daemon=True, name="hramatka-baker")
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="hramatka-bake-watchdog"
            )
            try:
                self._worker.start()
                self._watchdog.start()
            except RuntimeError:
                self.quarantine()
                raise

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def submit(self, lesson_id: str) -> bool:
        """Wake the one worker; no request is allowed to spawn a bake thread."""
        del lesson_id
        if self._poisoned.is_set() or self._stop.is_set():
            return False
        self._wake.set()
        return True

    def expire_and_quarantine(self) -> bool:
        """Expose a non-cancellable in-process bake as durable timeout failure."""
        if self._store.sweep_expired_bakes(self._hard_timeout_seconds) == 0:
            return False
        self.quarantine()
        return True

    def quarantine(self) -> None:
        self._poisoned.set()
        self._store.fail_queued_drafts(
            "Сервіс складання уроків недоступний. Спробуйте, будь ласка, ще раз.",
            failure_code="engine_unavailable",
        )
        self._wake.set()

    def _work(self) -> None:
        while not self._stop.is_set() and not self._poisoned.is_set():
            job = self._store.claim_next_draft()
            if job is None:
                self._wake.wait(timeout=1)
                self._wake.clear()
                continue
            self._run_job(job)

    def _run_job(self, job) -> None:  # JobRecord is deliberately duck-typed for test seams.
        try:
            template = self._bake_with_one_provider_retry(job)
            if not self._store.set_step(job.teacher_id, job.id, "перевірка"):
                return
            lesson = materialize_lesson(template, job)
            validate_lesson(lesson)
            self._store.complete(job.teacher_id, job.id, lesson)
        except BakeError as error:
            self._fail_bake_error(job.teacher_id, job.id, error)
        except (ValidationError, ValueError):
            self._store.fail(
                job.teacher_id,
                job.id,
                "lesson_schema_invalid",
                _SAFE_FAILURE_MESSAGE,
            )
        except Exception:
            # Never persist an exception, trace, provider response, original
            # anchor, or filesystem path.  The durable aggregate carries only a
            # frozen allowlisted code and teacher-safe wording.
            self._store.fail(
                job.teacher_id,
                job.id,
                "unknown_safe_failure",
                _SAFE_FAILURE_MESSAGE,
            )

    def _bake_with_one_provider_retry(self, job) -> dict:  # JobRecord is deliberately duck-typed.
        request = {
            "anchor_id": job.id,
            "body_uk": job.anchor_text,
            "source": job.anchor_source,
        }
        for attempt in range(2):
            try:
                return self._baker.bake(request, job.duration, job.focus)
            except ProviderUnavailable:
                if attempt == 1 or self._stop.wait(_PROVIDER_RETRY_DELAY_SECONDS):
                    raise
        raise AssertionError("Provider retry loop must return or raise.")  # pragma: no cover

    def _fail_bake_error(self, teacher_id: str, lesson_id: str, error: BakeError) -> None:
        # Retry classification is a typed engine-boundary signal. Never inspect
        # arbitrary provider text when choosing a frozen durable machine code.
        failure_code = (
            "provider_unavailable"
            if isinstance(error, ProviderUnavailable)
            else "engine_unavailable"
        )
        self._store.fail(teacher_id, lesson_id, failure_code, _SAFE_FAILURE_MESSAGE)

    def _watchdog_loop(self) -> None:
        interval = max(0.05, min(self._hard_timeout_seconds / 4, 5))
        while not self._stop.wait(interval):
            if self.expire_and_quarantine():
                return
