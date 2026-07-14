"""A bounded in-process durable bake worker pool per Uvicorn process."""

from __future__ import annotations

import threading

from jsonschema import ValidationError

from .baking.port import BakeError, LessonBaker, ProviderUnavailable
from .lesson import materialize_lesson
from .store import JobStore
from .validation import validate_lesson

_SAFE_FAILURE_MESSAGE = "Не вдалося скласти урок. Спробуйте, будь ласка, ще раз."
_PROVIDER_RETRY_DELAY_SECONDS = 0.25
_DEFAULT_WORKERS = 4
_MAX_WORKERS = 8


class BakeRunner:
    """Atomically claim jobs into a bounded pool and persist each result independently."""

    def __init__(
        self,
        store: JobStore,
        baker: LessonBaker,
        hard_timeout_seconds: int,
        worker_count: int = _DEFAULT_WORKERS,
    ) -> None:
        self._store = store
        self._baker = baker
        self._hard_timeout_seconds = hard_timeout_seconds
        self._worker_count = min(_MAX_WORKERS, max(1, worker_count))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._poisoned = threading.Event()
        self._state_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._watchdog: threading.Thread | None = None

    def start(self) -> None:
        """Start the configured independent workers plus a per-job timeout sweep."""
        with self._state_lock:
            if self._workers:
                return
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="hramatka-bake-watchdog"
            )
            try:
                for index in range(self._worker_count):
                    worker = threading.Thread(
                        target=self._work,
                        daemon=True,
                        name=f"hramatka-baker-{index + 1}",
                    )
                    worker.start()
                    self._workers.append(worker)
                self._watchdog.start()
            except RuntimeError:
                # Thread creation failure is process-wide, not a job fault.
                # Stop admitting drafts; a restart will sweep any in-flight rows.
                self.quarantine()
                raise

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def submit(self, lesson_id: str) -> bool:
        """Wake the shared pool; requests never create bake threads."""
        del lesson_id
        if self._poisoned.is_set() or self._stop.is_set():
            return False
        self._wake.set()
        return True

    def expire_and_quarantine(self) -> bool:
        """Durably fail every individually expired in-flight bake, never siblings.

        The compatibility name remains for callers, but an ordinary timeout is
        not a systemic outage and therefore must not quarantine the worker pool.
        """
        return self._store.sweep_expired_bakes(self._hard_timeout_seconds) > 0

    def quarantine(self) -> None:
        """Stop and fail queued work only for an explicit systemic condition."""
        self._poisoned.set()
        self._store.fail_queued_drafts(
            "Сервіс складання уроків недоступний. Спробуйте, будь ласка, ще раз.",
            failure_code="engine_unavailable",
        )
        self._wake.set()

    def _work(self) -> None:
        while not self._stop.is_set() and not self._poisoned.is_set():
            job = self._claim_next_job()
            if job is None:
                self._wake.wait(timeout=1)
                self._wake.clear()
                continue
            self._run_job(job)

    def _claim_next_job(self):
        """Retry optimistic CAS losers promptly so a wake fills the whole pool."""
        for _ in range(self._worker_count):
            job = self._store.claim_next_draft()
            if job is not None:
                return job
        return None

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
            self.expire_and_quarantine()
