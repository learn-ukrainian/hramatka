"""Black-box behaviour tests for the frozen Hramatka pilot API.

These deliberately exercise the browser contract rather than implementation
details.  The only setup seam is the operator-equivalent store helper used to
issue throwaway invitations; there is intentionally no HTTP admin route.
"""

from __future__ import annotations

import base64
import copy
import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hramatka.api.app import create_app
from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.baking.port import BakeError
from hramatka.api.config import Settings

ORIGIN = "https://pilot.example.test"
CSRF_KEY = b"test-only-hmac-key-that-is-not-a-deployment-secret"


def _fixture_template() -> dict[str, Any]:
    fixture = Path(__file__).parents[1] / "hramatka/api/fixtures/canonical-lesson.v1.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


class FixtureBaker:
    """Fast deterministic baker used only to drive durable API behaviour."""

    calls = 0

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        del anchor, duration, focus
        self.calls += 1
        return copy.deepcopy(_fixture_template())


class BlockingBaker(FixtureBaker):
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=3), "test baker was never released"
        del anchor, duration, focus
        return copy.deepcopy(_fixture_template())


class SecretLeakingFailureBaker:
    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        del anchor, duration, focus
        raise BakeError("provider response: bearer super-secret-token trace /private/path")


def _settings(tmp_path: Path, *, mock_mode: bool = False) -> Settings:
    """Pilot settings with an HTTPS origin and a deliberately throwaway CSRF key."""
    return Settings(
        database_path=tmp_path / "pilot.sqlite3",
        pilot_origin=ORIGIN,
        csrf_hmac_key=CSRF_KEY,
        mock_mode=mock_mode,
        bake_hard_timeout_seconds=30,
    )


@pytest.fixture
def app(tmp_path: Path):
    return create_app(settings=_settings(tmp_path), baker=FixtureBaker())


@pytest.fixture
def client(app) -> TestClient:
    # Secure __Host- cookies are not sent to the TestClient default HTTP URL.
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


def _issue_invite(app, *, display_name: str = "Тестова вчителька", expires_in_hours: int = 72):
    """Use the operator-store seam so tests never reproduce the private hash prefix."""
    teacher = app.state.store.create_teacher(display_name=display_name)
    invite, token = app.state.store.create_invite(
        teacher_id=teacher.id,
        expires_in_hours=expires_in_hours,
    )
    return teacher, invite, token


def _mutation_headers(csrf_token: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf_token}


def _lesson_request(
    lesson_id: str, *, duration: int = 45, focus: str | None = None
) -> dict[str, Any]:
    return {
        "id": lesson_id,
        "anchor": {
            "text": "Учні читають український текст. Потім вони обговорюють вправи.",
            "source": "teacher-paste",
        },
        "level": "B1",
        "duration": duration,
        "focus": focus,
    }


def _redeem(client: TestClient, token: str) -> dict[str, Any]:
    response = client.post(
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": token},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "__Host-hramatka_session=" in response.headers["set-cookie"]
    assert "Path=/" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=Lax" in response.headers["set-cookie"]
    assert "Domain=" not in response.headers["set-cookie"]
    return response.json()


def _error(
    response,
    status_code: int,
    code: str,
    *,
    lesson_id: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) in (
        {"code", "message", "retryable"},
        {"code", "message", "retryable", "lesson_id"},
    )
    assert body["code"] == code
    assert body["retryable"] is retryable
    if lesson_id is not None:
        assert body["lesson_id"] == lesson_id
    return body


def _wait_for_status(client: TestClient, lesson_id: str, expected: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/lessons/{lesson_id}/status")
        assert response.status_code == 200, response.text
        status = response.json()
        if status["status"] == expected:
            return status
        time.sleep(0.01)
    pytest.fail(f"lesson {lesson_id} did not become {expected}")


def _set_invite_expired(database_path: Path, invite_id: str) -> None:
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE pilot_invites SET expires_at = ? WHERE id = ?", (expired, invite_id)
        )


def _set_invite_revoked(database_path: Path, invite_id: str) -> None:
    revoked = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE pilot_invites SET revoked_at = ? WHERE id = ?", (revoked, invite_id)
        )


def _set_only_session(database_path: Path, column: str, value: str) -> None:
    assert column in {"expires_at", "revoked_at"}
    with sqlite3.connect(database_path) as connection:
        updated = connection.execute(f"UPDATE pilot_sessions SET {column} = ?", (value,)).rowcount
    assert updated == 1


def _canonical_token() -> str:
    return base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).rstrip(b"=").decode()


def _noncanonical_padding_bits(token: str) -> str:
    """Keep the decoded 32 bytes while setting a forbidden final padding bit."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    index = alphabet.index(token[-1])
    # A 32-byte token's final sextet has two zero padding bits.  Toggle one of
    # them: a permissive decoder accepts this spelling of the same bytes.
    assert index % 4 == 0
    return token[:-1] + alphabet[index + 1]


def test_invite_lifecycle_is_410_indistinguishable_and_rejects_padding_bits(app, client) -> None:
    _, used, used_token = _issue_invite(app, display_name="Використана")
    _, expired, expired_token = _issue_invite(app, display_name="Прострочена")
    _, revoked, revoked_token = _issue_invite(app, display_name="Відкликана")
    _set_invite_expired(app.state.settings.database_path, expired.id)
    _set_invite_revoked(app.state.settings.database_path, revoked.id)

    assert _redeem(client, used_token)["teacher"]["display_name"] == "Використана"
    used_response = client.post(
        "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": used_token}
    )
    expired_response = client.post(
        "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": expired_token}
    )
    revoked_response = client.post(
        "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": revoked_token}
    )
    unknown_response = client.post(
        "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": _canonical_token()}
    )
    unavailable = _error(used_response, 410, "invite_unavailable")
    assert _error(expired_response, 410, "invite_unavailable") == unavailable
    assert _error(revoked_response, 410, "invite_unavailable") == unavailable
    assert _error(unknown_response, 410, "invite_unavailable") == unavailable

    padding_response = client.post(
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": _noncanonical_padding_bits(_canonical_token())},
    )
    _error(padding_response, 422, "invalid_input")


def test_session_expiry_revocation_logout_and_unknown_are_401_indistinguishable(
    app, client
) -> None:
    _, _, token = _issue_invite(app)
    session = _redeem(client, token)
    assert session["expires_at"]
    assert session["csrf_token"]

    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    _set_only_session(app.state.settings.database_path, "expires_at", expired)
    expired_response = client.get("/api/session")
    expired_error = _error(expired_response, 401, "session_required")

    _set_only_session(
        app.state.settings.database_path,
        "revoked_at",
        datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    revoked_response = client.get("/api/session")
    assert _error(revoked_response, 401, "session_required") == expired_error

    client.cookies.clear()
    unknown_response = client.get("/api/session")
    assert _error(unknown_response, 401, "session_required") == expired_error

    # A fresh session proves logout commits revocation before it sends the cleared cookie.
    _, _, logout_token = _issue_invite(app, display_name="Вихід")
    logout_session = _redeem(client, logout_token)
    logout = client.delete("/api/session", headers=_mutation_headers(logout_session["csrf_token"]))
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    _error(client.get("/api/session"), 401, "session_required")


def test_origin_and_csrf_matrix_rejects_before_any_mutation(app, client) -> None:
    _, _, token = _issue_invite(app)
    for headers in (
        {},
        {"Origin": "null"},
        {"Origin": "http://pilot.example.test"},
        {"Origin": "https://evil.example"},
    ):
        response = client.post("/api/session/redeem", headers=headers, json={"token": token})
        _error(response, 403, "csrf_rejected")

    session = _redeem(client, token)
    lesson_id = str(uuid.uuid4())
    request = _lesson_request(lesson_id)
    for headers in (
        {},
        {"Origin": ORIGIN},
        {"X-CSRF-Token": session["csrf_token"]},
        {"Origin": "https://evil.example", "X-CSRF-Token": session["csrf_token"]},
        {"Origin": ORIGIN, "X-CSRF-Token": "bad"},
    ):
        response = client.post("/api/lessons", headers=headers, json=request)
        _error(response, 403, "csrf_rejected")

    invalid_request = copy.deepcopy(request)
    invalid_request["anchor"]["text"] = " \t\n"
    invalid = client.post(
        "/api/lessons",
        headers=_mutation_headers(session["csrf_token"]),
        json=invalid_request,
    )
    _error(invalid, 422, "invalid_input")

    created = client.post(
        "/api/lessons",
        headers=_mutation_headers(session["csrf_token"]),
        json=request,
    )
    assert created.status_code == 202, created.text
    assert created.json()["reused"] is False
    assert created.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in created.headers


def test_owner_scope_uses_teacher_and_uuid_together_and_hides_cross_owner_rows(app) -> None:
    _, _, token_one = _issue_invite(app, display_name="Одна")
    _, _, token_two = _issue_invite(app, display_name="Друга")
    lesson_id = str(uuid.uuid4())
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        first_session = _redeem(first, token_one)
        second_session = _redeem(second, token_two)
        first_create = first.post(
            "/api/lessons",
            headers=_mutation_headers(first_session["csrf_token"]),
            json=_lesson_request(lesson_id, focus="перша"),
        )
        second_create = second.post(
            "/api/lessons",
            headers=_mutation_headers(second_session["csrf_token"]),
            json=_lesson_request(lesson_id, focus="друга"),
        )
        assert first_create.status_code == second_create.status_code == 202
        assert first_create.json()["reused"] is False
        assert second_create.json()["reused"] is False

        _wait_for_status(first, lesson_id, "ready")
        _wait_for_status(second, lesson_id, "ready")
        first_only_id = str(uuid.uuid4())
        private_create = first.post(
            "/api/lessons",
            headers=_mutation_headers(first_session["csrf_token"]),
            json=_lesson_request(first_only_id, focus="лише перша"),
        )
        assert private_create.status_code == 202, private_create.text
        _wait_for_status(first, first_only_id, "ready")

        first_catalog = first.get("/api/lessons").json()["lessons"]
        second_catalog = second.get("/api/lessons").json()["lessons"]
        assert {item["id"] for item in first_catalog} == {lesson_id, first_only_id}
        assert [item["id"] for item in second_catalog] == [lesson_id]
        assert {item["focus"] for item in first_catalog} == {"перша", "лише перша"}
        assert second_catalog[0]["focus"] == "друга"

        # This UUID exists for the other teacher, but its status and resource
        # are indistinguishable from an absent aggregate.
        _error(second.get(f"/api/lessons/{first_only_id}/status"), 404, "lesson_not_found")
        _error(second.get(f"/api/lessons/{first_only_id}"), 404, "lesson_not_found")


def test_same_owner_idempotency_reuses_without_second_bake_and_conflicts_on_new_input(
    tmp_path: Path,
) -> None:
    baker = BlockingBaker()
    app = create_app(settings=_settings(tmp_path), baker=baker)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _, _, token = _issue_invite(app)
            session = _redeem(client, token)
            lesson_id = str(uuid.uuid4())
            request = _lesson_request(lesson_id)
            first = client.post(
                "/api/lessons", headers=_mutation_headers(session["csrf_token"]), json=request
            )
            assert first.status_code == 202, first.text
            assert first.json()["reused"] is False
            assert baker.started.wait(timeout=1)

            repeated = client.post(
                "/api/lessons", headers=_mutation_headers(session["csrf_token"]), json=request
            )
            assert repeated.status_code == 202, repeated.text
            assert repeated.json()["reused"] is True
            assert baker.calls == 1

            changed = _lesson_request(lesson_id, duration=60)
            conflict = client.post(
                "/api/lessons", headers=_mutation_headers(session["csrf_token"]), json=changed
            )
            _error(conflict, 409, "idempotency_conflict", lesson_id=lesson_id)
    finally:
        baker.release.set()


def test_revision_matrix_warning_ack_accept_and_draft_are_atomic(app, client) -> None:
    _, _, token = _issue_invite(app)
    session = _redeem(client, token)
    headers = _mutation_headers(session["csrf_token"])
    lesson_id = str(uuid.uuid4())
    created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
    assert created.status_code == 202, created.text
    _wait_for_status(client, lesson_id, "ready")
    resource = client.get(f"/api/lessons/{lesson_id}")
    assert resource.status_code == 200, resource.text
    initial = resource.json()
    assert set(initial["lesson"]["anchor"]) == {"text", "source", "chars"}
    revision = initial["revision"]
    warning_ids = [block["id"] for block in initial["lesson"]["blocks"] if block["mark"] == "warn"]
    assert warning_ids, "fixture must expose a visible warning acknowledgement path"

    blocked_accept = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    _error(blocked_accept, 409, "warning_acknowledgements_required", lesson_id=lesson_id)
    assert client.get(f"/api/lessons/{lesson_id}").json()["revision"] == revision

    first_ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/{warning_ids[0]}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    assert first_ack.status_code == 200, first_ack.text
    acknowledged_revision = first_ack.json()["revision"]
    assert acknowledged_revision == revision + 1

    repeated_ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/{warning_ids[0]}/accept",
        headers=headers,
        json={"expected_revision": acknowledged_revision},
    )
    assert repeated_ack.status_code == 200, repeated_ack.text
    assert repeated_ack.json()["revision"] == acknowledged_revision
    assert repeated_ack.json()["warning_acknowledgements"] == [warning_ids[0]]

    stale_ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/{warning_ids[1]}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    _error(stale_ack, 409, "revision_conflict", lesson_id=lesson_id)
    stale_accept = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    _error(stale_accept, 409, "revision_conflict", lesson_id=lesson_id)

    for warning_id in warning_ids[1:]:
        response = client.post(
            f"/api/lessons/{lesson_id}/blocks/{warning_id}/accept",
            headers=headers,
            json={"expected_revision": acknowledged_revision},
        )
        assert response.status_code == 200, response.text
        acknowledged_revision = response.json()["revision"]

    accepted = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": acknowledged_revision},
    )
    assert accepted.status_code == 200, accepted.text
    accepted_resource = accepted.json()
    assert accepted_resource["lesson"]["accepted"] is True
    assert accepted_resource["accepted_revision"] == accepted_resource["revision"]
    assert accepted_resource["revision"] == acknowledged_revision + 1

    draft = client.post(
        f"/api/lessons/{lesson_id}/draft",
        headers=headers,
        json={"expected_revision": accepted_resource["revision"]},
    )
    assert draft.status_code == 200, draft.text
    drafted_resource = draft.json()
    assert drafted_resource["lesson"]["accepted"] is False
    assert drafted_resource["accepted_at"] is None
    assert drafted_resource["accepted_revision"] is None
    assert drafted_resource["revision"] == accepted_resource["revision"] + 1

    stale_draft = client.post(
        f"/api/lessons/{lesson_id}/draft",
        headers=headers,
        json={"expected_revision": accepted_resource["revision"]},
    )
    _error(stale_draft, 409, "revision_conflict", lesson_id=lesson_id)
    state_draft = client.post(
        f"/api/lessons/{lesson_id}/draft",
        headers=headers,
        json={"expected_revision": drafted_resource["revision"]},
    )
    _error(state_draft, 409, "lesson_state_conflict", lesson_id=lesson_id)


def test_baker_failure_is_durable_sanitized_and_never_exposes_a_partial_lesson(
    tmp_path: Path,
) -> None:
    app = create_app(settings=_settings(tmp_path), baker=SecretLeakingFailureBaker())
    with TestClient(app, base_url=ORIGIN) as client:
        _, _, token = _issue_invite(app)
        session = _redeem(client, token)
        lesson_id = str(uuid.uuid4())
        response = client.post(
            "/api/lessons",
            headers=_mutation_headers(session["csrf_token"]),
            json=_lesson_request(lesson_id),
        )
        assert response.status_code == 202, response.text
        failed = _wait_for_status(client, lesson_id, "failed")
        assert failed["failure_code"] in {
            "bake_timeout",
            "worker_restarted",
            "provider_unavailable",
            "engine_unavailable",
            "lesson_schema_invalid",
            "unknown_safe_failure",
        }
        assert "super-secret-token" not in (failed["failure_message"] or "")
        assert "/private/path" not in (failed["failure_message"] or "")
        _error(client.get(f"/api/lessons/{lesson_id}"), 409, "lesson_not_ready")


def test_readyz_requires_real_baker_and_mock_mode_off(tmp_path: Path) -> None:
    mock_app = create_app(settings=_settings(tmp_path, mock_mode=True), baker=FixtureBaker())
    with TestClient(mock_app, base_url=ORIGIN) as client:
        assert client.get("/api/healthz").json() == {"status": "ok"}
        response = client.get("/api/readyz")
        _error(response, 503, "service_not_ready", retryable=True)


def test_startup_recovers_an_orphaned_baking_aggregate_without_partial_lesson(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app_one = create_app(settings=settings, baker=FixtureBaker())
    teacher = app_one.state.store.create_teacher("Перезапуск")
    lesson_id = str(uuid.uuid4())
    job, created = app_one.state.store.create_or_get(
        teacher.id,
        lesson_id,
        anchor_text="Учні читають український текст.",
        level="B1",
        duration=45,
        focus=None,
    )
    assert created is True
    claimed = app_one.state.store.claim_next_draft()
    assert claimed is not None and claimed.id == job.id and claimed.status == "baking"

    # The second process owns no resumable in-memory engine work.  Its startup
    # transaction must make the aggregate terminal before accepting new work.
    app_two = create_app(settings=settings, baker=FixtureBaker())
    with TestClient(app_two, base_url=ORIGIN):
        recovered = app_two.state.store.get(teacher.id, lesson_id)
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.failure_code == "worker_restarted"
    assert recovered.lesson is None

    # The readiness probe verifies production wiring, not whether a generation
    # has happened.  No bake is invoked, so the injected generator is inert.
    real_app = create_app(
        settings=_settings(tmp_path / "real"),
        baker=EngineLessonBaker(generator=lambda *_args, **_kwargs: {}),
    )
    with TestClient(real_app, base_url=ORIGIN) as client:
        assert client.get("/api/readyz").json() == {"status": "ready"}
