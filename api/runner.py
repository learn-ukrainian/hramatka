"""One local worker thread per process, executing at most one mock bake at a time."""

from __future__ import annotations

import threading

from .baking.port import BakeError, LessonBaker
from .lesson import materialize_lesson
from .store import JobStore
from .validation import validate_lesson


class BakeRunner:
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
        """Start exactly one worker and one non-baking timeout watchdog."""
        with self._state_lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._work, daemon=True)
            self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
            try:
                self._worker.start()
                self._watchdog.start()
            except RuntimeError:
                self.quarantine("Bake worker could not start; restart the API before retrying.")
                raise

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def submit(self, lesson_id: str) -> bool:
        """Wake the single worker without creating a thread for this job."""
        del lesson_id
        if self._poisoned.is_set() or self._stop.is_set():
            return False
        self._wake.set()
        return True

    def expire_and_quarantine(self) -> bool:
        """Make a hung in-process baker explicit: fail queued work and require restart."""
        if self._store.sweep_expired_bakes(self._hard_timeout_seconds) == 0:
            return False
        self.quarantine(
            "Bake worker was quarantined after a hard timeout; restart the API before retrying."
        )
        return True

    def quarantine(self, error: str) -> None:
        self._poisoned.set()
        self._store.fail_queued_drafts(error)
        self._wake.set()

    def _work(self) -> None:
        while not self._stop.is_set() and not self._poisoned.is_set():
            job = self._store.claim_next_draft()
            if job is None:
                self._wake.wait(timeout=1)
                self._wake.clear()
                continue
            self._run_job(job)

    def _run_job(self, job) -> None:
        try:
            # Preserve anchor provenance across the durable-job / engine seam.
            # Mock bakers still receive a compatible value and intentionally
            # ignore it; the real baker forwards it to the engine snapshot.
            template = self._baker.bake(
                {
                    "anchor_id": job.id,
                    "body_uk": job.anchor_text,
                    "source": job.anchor_source,
                },
                job.duration,
                job.focus,
            )
            if not self._store.set_step(job.id, "перевірка"):
                return
            lesson = materialize_lesson(template, job)
            validate_lesson(lesson)
            self._store.complete(job.id, lesson)
        except BakeError as error:
            self._store.fail(job.id, str(error))
        except Exception:
            # Do not persist the anchor, engine response, token, or traceback.
            self._store.fail(
                job.id,
                "Bake failed before a valid lesson document was produced; retry after review.",
            )

    def _watchdog_loop(self) -> None:
        interval = max(0.05, min(self._hard_timeout_seconds / 4, 5))
        while not self._stop.wait(interval):
            if self.expire_and_quarantine():
                return
