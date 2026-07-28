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
import threading
from pathlib import Path
from typing import Any

import uvicorn

from hramatka.api.app import create_app
from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.config import Settings
from hramatka.api.qualified_models import (
    DENSITY_CONTRACT_DIGEST,
    DENSITY_CONTRACT_VERSION,
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
from hramatka.engine import fixtures
from hramatka.engine.prompt_pack import PROMPT_PACK_VERSION
from hramatka.engine.providers import telemetry_ctx

LOOPBACK = "127.0.0.1"
OWNER_TOKEN_RE = re.compile(r"^[a-f0-9]{64}$")
EXPECTED_STATE_DIRECTORY = Path(__file__).resolve().parents[1] / ".real-e2e"
TEST_LOGICAL_MODEL_ID = "gemini-3.5-flash"
TEST_PROVIDER_ROUTE = QualifiedProviderRoute(
    "gemini-flash-ais", "google-ais", "google-ais/gemini-3.5-flash"
)


def _qualified_test_registry() -> QualifiedModelRegistry:
    """Return one explicit synthetic receipt; production defaults stay empty."""
    model = LogicalModelSpec(
        id=TEST_LOGICAL_MODEL_ID,
        label="Gemini 3.5 Flash",
        description_uk="Детермінована тестова модель.",
        provider_routes=(TEST_PROVIDER_ROUTE,),
    )
    receipt = QualificationReceipt(
        logical_model_id=model.id,
        provider_route=TEST_PROVIDER_ROUTE.id,
        provider_host=TEST_PROVIDER_ROUTE.host,
        provider_model_id=TEST_PROVIDER_ROUTE.model_id,
        registry_version=QUALIFIED_MODEL_REGISTRY_VERSION,
        prompt_pack_version=PROMPT_PACK_VERSION,
        prompt_sha256=PROMPT_SHA256,
        template_version=TEMPLATE_VERSION,
        template_sha256=TEMPLATE_SHA256,
        density_contract_version=DENSITY_CONTRACT_VERSION,
        density_contract_digest=DENSITY_CONTRACT_DIGEST,
        type_kit_identity=TYPE_KIT_IDENTITY,
        passed_anchors=QUALIFICATION_ANCHORS,
        passed=True,
    )
    return QualifiedModelRegistry(models=(model,), receipts=(receipt,))


class FixtureBaker(EngineLessonBaker):
    """Fast deterministic LessonBaker port implementation; it never calls a provider.

    It reuses the engine E2E fake-generator seam and its miniature digest-verified
    bundle. The submitted anchor is deliberately replaced with the synthetic
    fixture anchor inside the baker; API materialization still binds the actual
    browser paste to the durable lesson resource.
    """

    def __init__(self, runtime_dir: Path, *, logical_model_id: str | None = None) -> None:
        self._runtime_dir = runtime_dir

        def generator(prompt: str) -> str:
            ctx = telemetry_ctx.get()
            phase = int(ctx.phase) if ctx is not None and ctx.phase else 1
            activities = fixtures.e2e_activities_for_prompt(prompt, phase=phase)
            return json.dumps({"activities": activities}, ensure_ascii=False)

        super().__init__(
            generator=generator,
            bundle=fixtures._bundle_with_matchup_vocabulary(runtime_dir / "data"),
            cache_dir=runtime_dir / "cache",
            logical_model_id=logical_model_id,
        )

    def for_logical_model(self, logical_model_id: str | None) -> FixtureBaker:
        if logical_model_id != TEST_LOGICAL_MODEL_ID:
            raise ValueError("real-backend fixture requires its qualified logical model")
        routed = FixtureBaker.__new__(FixtureBaker)
        routed._runtime_dir = self._runtime_dir
        EngineLessonBaker.__init__(
            routed,
            generator=self._generator,
            bundle=self._resolved_bundle,
            cache_dir=self._cache_dir,
            logical_model_id=logical_model_id,
        )
        return routed

    def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]:
        del anchor
        return super().bake(fixtures.load_anchor(), duration, focus)


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
    payload = json.dumps({"pid": pid, "port": port}, separators=(",", ":")) + "\n"
    encoded = payload.encode("ascii")
    if len(encoded) > 256:
        raise RuntimeError("real-backend readiness payload is unexpectedly large")
    view = memoryview(encoded)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RuntimeError("could not publish real-backend readiness")
        view = view[written:]


def _watch_parent(parent_gone: threading.Event, server_holder: list[uvicorn.Server]) -> None:
    try:
        while os.read(0, 4096):
            pass
    except OSError:
        pass
    parent_gone.set()
    if server_holder:
        server_holder[0].should_exit = True


def main() -> None:
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
            baker=FixtureBaker(database_path.parent),
            model_registry=_qualified_test_registry(),
        )
        teacher = app.state.store.create_teacher("E2E викладач")
        _, invite_token = app.state.store.create_invite(teacher.id)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(invite_token, encoding="ascii")
        token_path.chmod(0o600)

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
