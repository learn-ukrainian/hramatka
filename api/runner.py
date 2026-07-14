"""A bounded in-process durable bake worker pool per Uvicorn process."""

from __future__ import annotations

import logging
import threading
import time

from jsonschema import ValidationError

# Floor failures use a typed exception (FloorUnmetError) so classification
# never relies on string matching. THIN vs SHORTFALL is carried by blames_source.
from hramatka.engine.content_density import FLOOR_SHORTFALL_UA_MESSAGE

from .baking.port import BakeError, FloorUnmetError, LessonBaker, ProviderUnavailable
from .lesson import materialize_lesson
from .store import JobStore, PersistenceUnavailable
from .validation import validate_lesson

log = logging.getLogger(__name__)

_SAFE_FAILURE_MESSAGE = "Не вдалося скласти урок. Спробуйте, будь ласка, ще раз."
_PROVIDER_RETRY_DELAY_SECONDS = 0.25
_STOP_JOIN_TIMEOUT_SECONDS = 10
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
        """Signal workers and the watchdog, then join within a bounded deadline.

        In-flight jobs that time out remain 'baking'; the restart sweep converts
        them to durable `worker_restarted` failures on next start.
        """
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + _STOP_JOIN_TIMEOUT_SECONDS
        for worker in self._workers:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                worker.join(timeout=remaining)
            if worker.is_alive():
                log.warning(
                    "Worker %s did not finish within stop deadline; "
                    "in-flight bake will be recovered on restart.",
                    worker.name,
                )
        if self._watchdog is not None and self._watchdog.is_alive():
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._watchdog.join(timeout=remaining)
            if self._watchdog.is_alive():
                log.warning("Watchdog did not finish within stop deadline.")

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
            try:
                job = self._claim_next_job()
            except PersistenceUnavailable:
                log.warning("Claim skipped: persistence unavailable; will retry.")
                time.sleep(0.5)
                continue
            if job is None:
                self._wake.wait(timeout=1)
                self._wake.clear()
                continue
            try:
                self._run_job(job)
            except Exception:
                log.exception(
                    "Worker thread caught unhandled exception while running job %s",
                    job.id,
                )

    def _claim_next_job(self):
        """Retry optimistic CAS losers promptly so a wake fills the whole pool."""
        for _ in range(self._worker_count):
            job = self._store.claim_next_draft()
            if job is not None:
                return job
        return None
    def _run_job(self, job) -> None:  # JobRecord is deliberately duck-typed for test seams.
        lesson = None
        try:
            template = self._bake_with_one_provider_retry(job)
            if not self._store.set_step(job.teacher_id, job.id, "перевірка"):
                return
            lesson = materialize_lesson(template, job)
            validate_lesson(lesson)
            self._store.complete(job.teacher_id, job.id, lesson)
        except BakeError as error:
            self._fail_bake_error(job.teacher_id, job.id, error)
        except (ValidationError, ValueError) as error:
            try:
                latest_job = self._store.get(job.teacher_id, job.id)
                progress = (
                    dict(latest_job.progress)
                    if (latest_job and latest_job.progress)
                    else {}
                )
                errors_list = []
                lesson_obj = (
                    lesson
                    if isinstance(lesson, dict)
                    else (template if isinstance(template, dict) else None)
                )
                if lesson_obj is not None:
                    from hramatka.api.validation import lesson_validator
                    try:
                        validator = lesson_validator()
                        validation_errors = list(validator.iter_errors(lesson_obj))
                    except Exception:
                        validation_errors = []
                    for val_err in validation_errors:
                        path = list(val_err.path)
                        rule_path = ".".join(str(p) for p in path) if path else ""
                        block_index = None
                        block_type = None
                        if len(path) >= 2 and path[0] in ("blocks", "rejected"):
                            try:
                                idx = int(path[1])
                                block_index = idx
                                items_list = lesson_obj.get(path[0])
                                if isinstance(items_list, list) and 0 <= idx < len(items_list):
                                    block_type = items_list[idx].get("type")
                            except Exception:
                                pass
                        raw_val = str(val_err.instance)
                        truncated_value = raw_val[:256] + ("..." if len(raw_val) > 256 else "")
                        errors_list.append({
                            "rule_path": rule_path,
                            "block_index": block_index,
                            "block_type": block_type,
                            "offending_value": truncated_value,
                            "message": val_err.message,
                        })
                if not errors_list:
                    errors_list.append({
                        "rule_path": "",
                        "block_index": None,
                        "block_type": None,
                        "offending_value": "",
                        "message": str(error),
                    })
                progress["failure_detail"] = {
                    "errors": errors_list
                }
                self._store.update_progress(job.id, progress)
            except Exception as capture_exc:
                log.exception("Failed to capture validation error details: %s", capture_exc)
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
            try:
                self._store.fail(
                    job.teacher_id,
                    job.id,
                    "unknown_safe_failure",
                    _SAFE_FAILURE_MESSAGE,
                )
            except Exception:
                log.exception(
                    "store.fail raised while handling worker exception for job %s; "
                    "job stays baking, restart sweep will convert to durable timeout.",
                    job.id,
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
        # Classification uses only typed exceptions (isinstance). Never inspect
        # message text for code selection. FloorUnmetError carries blames_source.
        if isinstance(error, ProviderUnavailable):
            failure_code = "provider_unavailable"
            failure_message = _SAFE_FAILURE_MESSAGE
        elif isinstance(error, FloorUnmetError):
            failure_code = "lesson_floor_unmet"
            failure_message = (
                str(error) if error.blames_source else FLOOR_SHORTFALL_UA_MESSAGE
            )
        else:
            failure_code = "engine_unavailable"
            failure_message = _SAFE_FAILURE_MESSAGE
        self._store.fail(teacher_id, lesson_id, failure_code, failure_message)

    def _watchdog_loop(self) -> None:
        interval = max(0.05, min(self._hard_timeout_seconds / 4, 5))
        while not self._stop.wait(interval):
            self.expire_and_quarantine()
