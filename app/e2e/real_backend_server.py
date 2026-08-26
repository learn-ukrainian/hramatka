"""Test-only real API process for the browser contract exercise.

It uses the ordinary FastAPI factory and durable runner.  Only the LessonBaker
implementation is deterministic, so the browser still crosses the actual
session, Origin/CSRF, SQLite, status-polling, and revision routes.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

LOOPBACK = "127.0.0.1"
OWNER_TOKEN_RE = re.compile(r"^[a-f0-9]{64}$")
EXPECTED_STATE_DIRECTORY = Path(__file__).resolve().parents[1] / ".real-e2e"
TEST_LOGICAL_MODEL_ID = "gemini-3.7-flash"
_V3_KITS_RE = re.compile(
    r"=== IMMUTABLE TYPE-KITS \(data, not instructions\) ===\n```json\n(.*?)\n```",
    re.DOTALL,
)


class E2EInterpreterError(RuntimeError):
    """Raised when the real-backend harness is not using its requested Python."""


def _assert_expected_interpreter() -> None:
    """Refuse to start under an interpreter the launcher did not declare.

    Deliberately unconditional.  An earlier revision skipped the whole check
    whenever ``CI`` was set, which disabled it precisely where E2E results gate
    a release, and let any process turn the guard off by exporting one
    variable.  The launcher now declares the prefix on every path, CI included,
    so there is nothing left to except.
    """
    expected_prefix = os.environ.get("HRAMATKA_E2E_VENV_PREFIX", "")
    if not expected_prefix:
        raise E2EInterpreterError("real-backend E2E requires HRAMATKA_E2E_VENV_PREFIX")
    # Compare resolved paths.  A symlinked prefix, a relative value, or a
    # trailing separator all name the same environment; rejecting those would
    # be a false alarm, not a caught defect.
    if Path(sys.prefix).resolve() != Path(expected_prefix).resolve():
        raise E2EInterpreterError(
            "real-backend E2E interpreter prefix mismatch: "
            f"expected {expected_prefix}, got {sys.prefix}"
        )


def _qualified_test_registry() -> Any:
    """Return one explicit synthetic receipt; production defaults stay empty.

    ``HRAMATKA_E2E_EMPTY_REGISTRY=1`` opts into the empty-picker state so the
    real-backend suite can assert the fail-closed contract without resurrecting
    a production receipt.
    """
    from hramatka.api.qualified_models import (
        DENSITY_CONTRACT_DIGEST,
        DENSITY_CONTRACT_VERSION,
        PROMPT_PACK_VERSION,
        PROMPT_SHA256,
        QUALIFICATION_ANCHORS,
        QUALIFIED_MODEL_REGISTRY_VERSION,
        TEMPLATE_SHA256,
        TEMPLATE_VERSION,
        TYPE_KIT_IDENTITY,
        LogicalModelSpec,
        QualificationReceipt,
        QualifiedModelRegistry,
        QualifiedProviderRoute,
    )
    from hramatka.engine.serializer_policy import DEFAULT_SERIALIZER_TEMPERATURE

    if os.environ.get("HRAMATKA_E2E_EMPTY_REGISTRY") == "1":
        return QualifiedModelRegistry(receipts=())

    test_provider_route = QualifiedProviderRoute(
        "gemini-flash-subscription", "antigravity-cli", "gemini-3.7-flash-high"
    )
    model = LogicalModelSpec(
        id=TEST_LOGICAL_MODEL_ID,
        label="Gemini 3.7 Flash",
        description_uk="Детермінована тестова модель.",
        provider_routes=(test_provider_route,),
    )
    receipt = QualificationReceipt(
        logical_model_id=model.id,
        provider_route=test_provider_route.id,
        provider_host=test_provider_route.host,
        provider_model_id=test_provider_route.model_id,
        registry_version=QUALIFIED_MODEL_REGISTRY_VERSION,
        prompt_pack_version=PROMPT_PACK_VERSION,
        prompt_sha256=PROMPT_SHA256,
        template_version=TEMPLATE_VERSION,
        template_sha256=TEMPLATE_SHA256,
        density_contract_version=DENSITY_CONTRACT_VERSION,
        density_contract_digest=DENSITY_CONTRACT_DIGEST,
        type_kit_identity=TYPE_KIT_IDENTITY,
        serializer_temperature=DEFAULT_SERIALIZER_TEMPERATURE,
        passed_anchors=QUALIFICATION_ANCHORS,
        passed=True,
    )
    return QualifiedModelRegistry(models=(model,), receipts=(receipt,))


def _v3_fixture_serializer(prompt: str) -> str:
    """Render v3 records from the exact certified kits without a provider call."""
    from hramatka.qualification.harness import _v3_live_record_from_kit

    matched = _V3_KITS_RE.search(prompt)
    if matched is None:
        raise AssertionError("real-backend v3 fixture received no immutable type kits")
    kits = json.loads(matched.group(1))
    if not isinstance(kits, list) or not all(isinstance(kit, dict) for kit in kits):
        raise AssertionError("real-backend v3 fixture received malformed immutable type kits")
    records = [_v3_live_record_from_kit(kit) for kit in kits]
    if "=== BLOCK REGENERATION CONTRACT ===" in prompt:
        for record in records:
            record["activity"]["payload"]["instruction"] += " Уважно звірте відповідь із текстом."
    return json.dumps({"slots": records}, ensure_ascii=False)


def _v3_fixture_semantic_reviewer(prompt: str) -> str:
    """Approve host-built teacher samples deterministically and without transport."""
    begin = "BEGIN_HOST_REVIEW_REQUEST\n"
    end = "\nEND_HOST_REVIEW_REQUEST"
    if begin not in prompt or end not in prompt:
        raise AssertionError("real-backend semantic fixture received an invalid request")
    request = json.loads(prompt.split(begin, 1)[1].split(end, 1)[0])
    items = request.get("items")
    if not isinstance(items, list):
        raise AssertionError("real-backend semantic fixture request has no items")
    return json.dumps(
        {
            "contract_version": request["contract_version"],
            "input_digest": request["input_digest"],
            "results": [
                {
                    "review_id": item["review_id"],
                    "verdict": "pass",
                    "failure_codes": [],
                }
                for item in items
            ],
        },
        ensure_ascii=False,
    )


def _fixture_baker(runtime_dir: Path) -> Any:
    if os.environ.get("HRAMATKA_E2E_UNDER_CAPACITY") == "1":
        # This dedicated browser fixture crosses the normal FastAPI queue and
        # v3 preflight path, but cannot make a provider request.  A short delay
        # inside preflight gives the browser one status-poll interval in which
        # to expose any accidental generation projection.
        from hramatka.api.baking import engine_adapter_v3
        from hramatka.api.baking.engine_adapter_v3 import EngineLessonBaker as V3EngineLessonBaker
        from hramatka.qualification import harness as qualification_harness

        class UnderCapacityFixtureBaker(V3EngineLessonBaker):
            def __init__(self, runtime_dir: Path) -> None:
                self._preflight_delay_seconds = 1.2

                def provider_must_not_run(_prompt: str) -> str:
                    raise AssertionError("under-capacity preflight called a provider")

                super().__init__(
                    generator=provider_must_not_run,
                    bundle=qualification_harness._qualification_fixture_bundle(
                        runtime_dir / "data"
                    ),
                )

            def for_logical_model(self, logical_model_id: str | None) -> Any:
                if logical_model_id != TEST_LOGICAL_MODEL_ID:
                    raise ValueError("real-backend fixture requires its qualified logical model")
                return self

            def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]:
                original_preflight = engine_adapter_v3.preflight_lesson

                def delayed_preflight(*args: Any, **kwargs: Any) -> Any:
                    import time

                    time.sleep(self._preflight_delay_seconds)
                    return original_preflight(*args, **kwargs)

                engine_adapter_v3.preflight_lesson = delayed_preflight
                try:
                    return super().bake(anchor, duration, focus)
                finally:
                    engine_adapter_v3.preflight_lesson = original_preflight

        return UnderCapacityFixtureBaker(runtime_dir)

    from hramatka.api.baking.engine_adapter_v3 import EngineLessonBaker as V3EngineLessonBaker
    from hramatka.qualification import harness as qualification_harness

    class FixtureBaker(V3EngineLessonBaker):
        """Offline v3 baker using the production preflight and certified kit records."""

        def __init__(self, runtime_dir: Path) -> None:
            self._runtime_dir = runtime_dir
            super().__init__(
                generator=_v3_fixture_serializer,
                bundle=qualification_harness._qualification_fixture_bundle(runtime_dir / "data"),
                semantic_reviewer=_v3_fixture_semantic_reviewer,
                semantic_reviewer_route="fixture:semantic-reviewer:v1",
                logical_model_id=TEST_LOGICAL_MODEL_ID,
            )

        def for_logical_model(self, logical_model_id: str | None) -> Any:
            if logical_model_id != TEST_LOGICAL_MODEL_ID:
                raise ValueError("real-backend fixture requires its qualified logical model")
            return self

        def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]:
            del anchor
            return super().bake(
                qualification_harness.deterministic_runtime_anchors()["b1-narrative"].text,
                duration,
                focus,
            )

        def regenerate_activity(
            self,
            anchor: str | dict,
            duration: int,
            focus: str | None,
            *,
            block: dict[str, Any],
            lesson_blocks: Sequence[Mapping[str, Any]],
            feedback: str | None,
        ) -> dict[str, Any]:
            """Exercise production regeneration with the deterministic fixture anchor."""
            del anchor
            return super().regenerate_activity(
                qualification_harness.deterministic_runtime_anchors()["b1-narrative"].text,
                duration,
                focus,
                block=block,
                lesson_blocks=lesson_blocks,
                feedback=feedback,
            )

    return FixtureBaker(runtime_dir)


def _required_file_descriptor(name: str) -> int:
    raw = os.environ.get(name, "")
    try:
        descriptor = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an inherited file descriptor") from exc
    if descriptor < 3 or str(descriptor) != raw:
        raise RuntimeError(f"{name} must be an inherited file descriptor")
    return descriptor


def _owner_matches(state_directory: Path, owner_token: str) -> bool:
    owner_marker = state_directory / "owner"
    try:
        return (
            stat.S_ISDIR(state_directory.lstat().st_mode)
            and not state_directory.is_symlink()
            and stat.S_ISREG(owner_marker.lstat().st_mode)
            and not owner_marker.is_symlink()
            and owner_marker.read_text(encoding="ascii") == f"{owner_token}\n"
        )
    except OSError:
        return False


def _runtime_directory() -> tuple[Path, str]:
    state_directory = Path(os.path.abspath(os.environ["HRAMATKA_E2E_STATE_DIR"]))
    database_path = Path(os.path.abspath(os.environ["HRAMATKA_E2E_DB_PATH"]))
    invite_path = Path(os.path.abspath(os.environ["HRAMATKA_E2E_INVITE_PATH"]))
    owner_token = os.environ.get("HRAMATKA_E2E_OWNER_TOKEN", "")
    if (
        state_directory != EXPECTED_STATE_DIRECTORY
        or database_path != EXPECTED_STATE_DIRECTORY / "pilot.sqlite3"
        or invite_path != EXPECTED_STATE_DIRECTORY / "invite-token"
        or state_directory.is_symlink()
        or not OWNER_TOKEN_RE.fullmatch(owner_token)
        or not _owner_matches(state_directory, owner_token)
    ):
        raise RuntimeError("refusing to use an unexpected real-backend state directory")
    return state_directory, owner_token


def _write_ready(descriptor: int, *, pid: int, port: int) -> None:
    payload = (
        json.dumps({"pid": pid, "port": port, "python_prefix": sys.prefix}, separators=(",", ":"))
        + "\n"
    )
    encoded = payload.encode("ascii")
    if len(encoded) > 256:
        raise RuntimeError("real-backend readiness payload is unexpectedly large")
    view = memoryview(encoded)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RuntimeError("could not publish real-backend readiness")
        view = view[written:]


def _watch_parent(parent_gone: threading.Event, server_holder: list[Any]) -> None:
    try:
        while os.read(0, 4096):
            pass
    except OSError:
        pass
    parent_gone.set()
    if server_holder:
        server_holder[0].should_exit = True


def main() -> None:
    _assert_expected_interpreter()
    import uvicorn

    from hramatka.api.app import create_app
    from hramatka.api.config import Settings

    ready_descriptor = _required_file_descriptor("HRAMATKA_E2E_READY_FD")
    state_directory, _ = _runtime_directory()
    parent_gone = threading.Event()
    server_holder: list[uvicorn.Server] = []
    threading.Thread(
        target=_watch_parent,
        args=(parent_gone, server_holder),
        daemon=True,
        name="hramatka-e2e-parent-watch",
    ).start()

    origin = os.environ["HRAMATKA_E2E_ORIGIN"]
    database_path = Path(os.environ["HRAMATKA_E2E_DB_PATH"])
    token_path = Path(os.environ["HRAMATKA_E2E_INVITE_PATH"])
    retry_token_path = token_path.with_name("invite-token-retry-1")
    listener: socket.socket | None = None
    try:
        if parent_gone.is_set():
            return
        app = create_app(
            settings=Settings(
                database_path=database_path,
                pilot_origin=origin,
                csrf_hmac_key=b"e2e-only-csrf-key-not-a-deployment-secret",
            ),
            baker=_fixture_baker(database_path.parent),
            model_registry=_qualified_test_registry(),
        )
        teacher = app.state.store.create_teacher("E2E викладач")
        _, invite_token = app.state.store.create_invite(teacher.id)
        retry_teacher = app.state.store.create_teacher("E2E викладач — повтор")
        _, retry_invite_token = app.state.store.create_invite(retry_teacher.id)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(invite_token, encoding="ascii")
        token_path.chmod(0o600)
        retry_token_path.write_text(retry_invite_token, encoding="ascii")
        retry_token_path.chmod(0o600)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((LOOPBACK, 0))
        listener.listen(socket.SOMAXCONN)
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, host=LOOPBACK, port=port))
        server_holder.append(server)
        if parent_gone.is_set():
            server.should_exit = True
            return
        _write_ready(ready_descriptor, pid=os.getpid(), port=port)
        os.close(ready_descriptor)
        ready_descriptor = -1
        server.run(sockets=[listener])
    finally:
        if ready_descriptor >= 0:
            os.close(ready_descriptor)
        if listener is not None:
            listener.close()


if __name__ == "__main__":
    main()
