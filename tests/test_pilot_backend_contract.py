"""Black-box behaviour tests for the frozen Hramatka pilot API.

These deliberately exercise the browser contract rather than implementation
details.  The only setup seam is the operator-equivalent store helper used to
issue throwaway invitations; there is intentionally no HTTP admin route.
"""

from __future__ import annotations

import base64
import copy
import hashlib
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
from hramatka.api.baking.port import BakeError, ProviderUnavailable
from hramatka.api.config import Settings
from hramatka.engine import data

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


class ReserveFixtureBaker(FixtureBaker):
    """Fixture baker with one durable phase-two reserve block."""

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        template = super().bake(anchor, duration, focus)
        reserve = copy.deepcopy(
            next(block for block in template["blocks"] if block["id"] == "block-4")
        )
        reserve["id"] = "block-reserve"
        reserve["activity"]["id"] = "activity-reserve"
        template["blocks"].append(reserve)
        return template


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


class InvalidClozeMarkerBaker(FixtureBaker):
    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        template = super().bake(anchor, duration, focus)
        cloze = next(block for block in template["blocks"] if block["type"] == "cloze")
        cloze["activity"]["payload"]["text"] = "Помилковий маркер [___:0]."
        return template


class TransientProviderBaker:
    def __init__(self) -> None:
        self.calls = 0

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        del anchor, duration, focus
        self.calls += 1
        raise ProviderUnavailable("provider 500: private trace must not be served")


def _settings(tmp_path: Path, *, mock_mode: bool = False) -> Settings:
    """Pilot settings with an HTTPS origin and a deliberately throwaway CSRF key."""
    return Settings(
        database_path=tmp_path / "pilot.sqlite3",
        pilot_origin=ORIGIN,
        csrf_hmac_key=CSRF_KEY,
        mock_mode=mock_mode,
        bake_hard_timeout_seconds=30,
    )


def _configure_ready_bundle(monkeypatch, tmp_path: Path, *, drifted: bool = False) -> None:
    """Give readyz a small production-shaped digest-pinned bundle."""
    release = tmp_path / "data-release"
    release.mkdir()
    payload = b"readiness fixture data"
    (release / "vesum.db").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if drifted:
        digest = "0" * 64
    manifest = {
        "inputs": {
            "vesum.db": {"path": "vesum.db", "sha256": digest, "size": len(payload)},
        }
    }
    manifest_path = tmp_path / "data-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("HRAMATKA_DATA_DIR", str(release))
    monkeypatch.setenv("HRAMATKA_DATA_MANIFEST", str(manifest_path))
    monkeypatch.setattr(data, "_active", None)


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
    assert first_ack.json()["lesson"]["id"] == lesson_id

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


def test_review_assembly_mutations_are_revision_guarded_owner_scoped_and_csrf_protected(
    app,
) -> None:
    _, _, first_token = _issue_invite(app, display_name="Перша")
    _, _, second_token = _issue_invite(app, display_name="Друга")
    lesson_id = str(uuid.uuid4())
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        first_session = _redeem(first, first_token)
        second_session = _redeem(second, second_token)
        headers = _mutation_headers(first_session["csrf_token"])
        created = first.post(
            "/api/lessons", headers=headers, json=_lesson_request(lesson_id, duration=45)
        )
        assert created.status_code == 202, created.text
        _wait_for_status(first, lesson_id, "ready")
        initial = first.get(f"/api/lessons/{lesson_id}").json()
        revision = initial["revision"]

        missing_csrf = first.post(
            f"/api/lessons/{lesson_id}/blocks/block-1/move",
            json={"expected_revision": revision, "direction": "down"},
        )
        _error(missing_csrf, 403, "csrf_rejected")

        foreign = second.post(
            f"/api/lessons/{lesson_id}/duration",
            headers=_mutation_headers(second_session["csrf_token"]),
            json={"expected_revision": revision, "duration": 60},
        )
        _error(foreign, 404, "lesson_not_found")

        moved = first.post(
            f"/api/lessons/{lesson_id}/blocks/block-1/move",
            headers=headers,
            json={"expected_revision": revision, "direction": "down"},
        )
        assert moved.status_code == 200, moved.text
        moved_resource = moved.json()
        assert moved_resource["revision"] == revision + 1
        assert moved_resource["lesson"]["blocks"][0]["id"] == "block-2"
        assert moved_resource["lesson"]["blocks"][1]["id"] == "block-1"

        stale = first.post(
            f"/api/lessons/{lesson_id}/blocks/block-1/remove",
            headers=headers,
            json={"expected_revision": revision},
        )
        _error(stale, 409, "revision_conflict", lesson_id=lesson_id)


def test_removal_restore_requires_fresh_warning_acknowledgement(app, client) -> None:
    _, _, token = _issue_invite(app)
    session = _redeem(client, token)
    headers = _mutation_headers(session["csrf_token"])
    lesson_id = str(uuid.uuid4())
    created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
    assert created.status_code == 202, created.text
    _wait_for_status(client, lesson_id, "ready")
    initial = client.get(f"/api/lessons/{lesson_id}").json()

    acknowledged = client.post(
        f"/api/lessons/{lesson_id}/blocks/block-1/accept",
        headers=headers,
        json={"expected_revision": initial["revision"]},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert "block-1" in acknowledged.json()["warning_acknowledgements"]

    removed = client.post(
        f"/api/lessons/{lesson_id}/blocks/block-1/remove",
        headers=headers,
        json={"expected_revision": acknowledged.json()["revision"]},
    )
    assert removed.status_code == 200, removed.text
    removed_resource = removed.json()
    assert removed_resource["revision"] == acknowledged.json()["revision"] + 1
    assert "block-1" not in removed_resource["warning_acknowledgements"]
    assert removed_resource["lesson"]["rejected"] == [
        {
            "type": "true-false",
            "activity": initial["lesson"]["blocks"][0]["activity"],
            "reason": "вилучено вчителем",
        }
    ]

    restored = client.post(
        f"/api/lessons/{lesson_id}/rejected/0/restore",
        headers=headers,
        json={"expected_revision": removed_resource["revision"], "phase": 1},
    )
    assert restored.status_code == 200, restored.text
    restored_resource = restored.json()
    assert restored_resource["revision"] == removed_resource["revision"] + 1
    restored_block = next(
        block
        for block in restored_resource["lesson"]["blocks"]
        if block["id"].startswith("restored-")
    )
    assert restored_block["mark"] == "warn"
    assert restored_block["note"] == "повернено з відхилених — погляньте ще раз"
    assert restored_block["id"] not in restored_resource["warning_acknowledgements"]

    blocked = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": restored_resource["revision"]},
    )
    _error(blocked, 409, "warning_acknowledgements_required", lesson_id=lesson_id)

    revision = restored_resource["revision"]
    for block in restored_resource["lesson"]["blocks"]:
        if block["mark"] == "warn":
            response = client.post(
                f"/api/lessons/{lesson_id}/blocks/{block['id']}/accept",
                headers=headers,
                json={"expected_revision": revision},
            )
            assert response.status_code == 200, response.text
            revision = response.json()["revision"]
    accepted = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    assert accepted.status_code == 200, accepted.text


def test_duration_reserve_include_and_activity_replacement_preserve_review_safety(
    tmp_path: Path,
) -> None:
    app = create_app(settings=_settings(tmp_path), baker=ReserveFixtureBaker())
    with TestClient(app, base_url=ORIGIN) as client:
        _, _, token = _issue_invite(app)
        session = _redeem(client, token)
        headers = _mutation_headers(session["csrf_token"])
        lesson_id = str(uuid.uuid4())
        created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
        assert created.status_code == 202, created.text
        _wait_for_status(client, lesson_id, "ready")
        initial = client.get(f"/api/lessons/{lesson_id}").json()
        original_ids = {block["id"] for block in initial["lesson"]["blocks"]}
        assert "block-reserve" in original_ids

        expanded = client.post(
            f"/api/lessons/{lesson_id}/duration",
            headers=headers,
            json={"expected_revision": initial["revision"], "duration": 90},
        )
        assert expanded.status_code == 200, expanded.text
        assert expanded.json()["lesson"]["duration"] == 90
        assert expanded.json()["revision"] == initial["revision"] + 1
        assert {block["id"] for block in expanded.json()["lesson"]["blocks"]} == original_ids

        trimmed = client.post(
            f"/api/lessons/{lesson_id}/duration",
            headers=headers,
            json={"expected_revision": expanded.json()["revision"], "duration": 45},
        )
        assert trimmed.status_code == 200, trimmed.text
        trimmed_resource = trimmed.json()
        assert trimmed_resource["lesson"]["duration"] == 45
        assert trimmed_resource["revision"] == expanded.json()["revision"] + 1
        assert {block["id"] for block in trimmed_resource["lesson"]["blocks"]} == original_ids

        included = client.post(
            f"/api/lessons/{lesson_id}/blocks/block-reserve/include",
            headers=headers,
            json={"expected_revision": trimmed_resource["revision"]},
        )
        assert included.status_code == 200, included.text
        included_resource = included.json()
        assert included_resource["revision"] == trimmed_resource["revision"] + 1
        phase_two_ids = [
            block["id"] for block in included_resource["lesson"]["blocks"] if block["phase"] == 2
        ]
        assert phase_two_ids[0] == "block-reserve"

        warning = next(
            block for block in included_resource["lesson"]["blocks"] if block["id"] == "block-1"
        )
        acknowledged = client.post(
            f"/api/lessons/{lesson_id}/blocks/block-1/accept",
            headers=headers,
            json={"expected_revision": included_resource["revision"]},
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert "block-1" in acknowledged.json()["warning_acknowledgements"]
        replacement = copy.deepcopy(warning["activity"])
        replacement["title"] = "Оновлена вправа"
        replaced = client.put(
            f"/api/lessons/{lesson_id}/blocks/block-1/activity",
            headers=headers,
            json={"expected_revision": acknowledged.json()["revision"], "activity": replacement},
        )
        assert replaced.status_code == 200, replaced.text
        replaced_resource = replaced.json()
        assert replaced_resource["revision"] == acknowledged.json()["revision"] + 1
        replaced_block = next(
            block for block in replaced_resource["lesson"]["blocks"] if block["id"] == "block-1"
        )
        assert replaced_block["mark"] == "warn"
        assert replaced_block["edited"] is True
        assert "block-1" not in replaced_resource["warning_acknowledgements"]

        invalid = client.put(
            f"/api/lessons/{lesson_id}/blocks/block-1/activity",
            headers=headers,
            json={
                "expected_revision": replaced_resource["revision"],
                "activity": {**replacement, "type": "roleplay-dialog"},
            },
        )
        _error(invalid, 422, "invalid_input")
        unchanged = client.get(f"/api/lessons/{lesson_id}").json()
        assert unchanged["revision"] == replaced_resource["revision"]
        unchanged_block = next(
            block for block in unchanged["lesson"]["blocks"] if block["id"] == "block-1"
        )
        assert unchanged_block == replaced_block


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


def test_invalid_cloze_markers_become_a_sanitized_schema_failure(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path), baker=InvalidClozeMarkerBaker())
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
        assert failed["failure_code"] == "lesson_schema_invalid"
        assert "[___:0]" not in (failed["failure_message"] or "")
        _error(client.get(f"/api/lessons/{lesson_id}"), 409, "lesson_not_ready")


def test_provider_unavailability_retries_once_then_persists_a_ukrainian_failure(
    tmp_path: Path,
) -> None:
    baker = TransientProviderBaker()
    app = create_app(settings=_settings(tmp_path), baker=baker)
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

    assert baker.calls == 2
    assert failed["failure_code"] == "provider_unavailable"
    assert failed["failure_message"] == "Не вдалося скласти урок. Спробуйте, будь ласка, ще раз."
    assert "private trace" not in (failed["failure_message"] or "")


def test_readyz_requires_real_baker_and_mock_mode_off(tmp_path: Path) -> None:
    mock_app = create_app(settings=_settings(tmp_path, mock_mode=True), baker=FixtureBaker())
    with TestClient(mock_app, base_url=ORIGIN) as client:
        assert client.get("/api/healthz").json() == {"status": "ok"}
        response = client.get("/api/readyz")
        _error(response, 503, "service_not_ready", retryable=True)


def test_readyz_resolves_the_engine_bundle_without_running_a_bake(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_ready_bundle(monkeypatch, tmp_path)
    calls = 0

    def inert_generator(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "{}"

    app = create_app(
        settings=_settings(tmp_path),
        baker=EngineLessonBaker(generator=inert_generator),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/readyz").json() == {"status": "ready"}
    assert calls == 0


@pytest.mark.parametrize("drifted, absent", [(True, False), (False, True)])
def test_readyz_returns_frozen_503_when_bundle_resolution_fails(
    tmp_path: Path, monkeypatch, drifted: bool, absent: bool
) -> None:
    _configure_ready_bundle(monkeypatch, tmp_path, drifted=drifted)
    if absent:
        monkeypatch.setenv("HRAMATKA_DATA_MANIFEST", str(tmp_path / "absent-manifest.json"))
    app = create_app(
        settings=_settings(tmp_path),
        baker=EngineLessonBaker(generator=lambda _prompt: "{}"),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        _error(client.get("/api/readyz"), 503, "service_not_ready", retryable=True)


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


# --- P2-6: teacher preferences (default duration) persistence, ownership, CSRF ---

def test_teacher_preferences_defaults_to_60_and_persists_per_teacher(app, client) -> None:
    """Persist/GET roundtrip; absent row yields 60. Quotes raw from contract."""
    teacher, _invite, token = _issue_invite(app)
    sess = _redeem(client, token)
    csrf = sess["csrf_token"]
    # initial GET yields default
    r = client.get("/api/teacher/preferences", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.json() == {"default_duration": 60}
    # PUT updates
    r = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(csrf),
        json={"default_duration": 90},
    )
    assert r.status_code == 200
    assert r.json() == {"default_duration": 90}
    # GET reflects
    r = client.get("/api/teacher/preferences", headers={"Origin": ORIGIN})
    assert r.json() == {"default_duration": 90}
    # also test 45
    r = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(csrf),
        json={"default_duration": 45},
    )
    assert r.status_code == 200
    r = client.get("/api/teacher/preferences", headers={"Origin": ORIGIN})
    assert r.json()["default_duration"] == 45


def test_teacher_preferences_ownership_scoped(app, client) -> None:
    """Cross-teacher cannot read/write other's pref (owner-scoped like lessons)."""
    t1, _i1, tok1 = _issue_invite(app, display_name="T1")
    t2, _i2, tok2 = _issue_invite(app, display_name="T2")
    s1 = _redeem(client, tok1)
    # set for t1 (client cookie is now t1's from redeem)
    r_put = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(s1["csrf_token"]),
        json={"default_duration": 90},
    )
    assert r_put.status_code == 200, r_put.text
    # t2 still default (verified via store; cookie sequencing handled by sequential redeem)
    assert app.state.store.get_teacher_default_duration(t1.id) == 90
    assert app.state.store.get_teacher_default_duration(t2.id) == 60
    # t2 PUT does not affect t1
    s2 = _redeem(client, tok2)
    client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(s2["csrf_token"]),
        json={"default_duration": 45},
    )
    assert app.state.store.get_teacher_default_duration(t1.id) == 90
    assert app.state.store.get_teacher_default_duration(t2.id) == 45
    # GET under t2 session returns its value (owner-scoped read)
    r = client.get("/api/teacher/preferences", headers={"Origin": ORIGIN})
    assert r.status_code == 200 and r.json()["default_duration"] == 45


def test_teacher_preferences_requires_csrf_and_origin_for_put(app, client) -> None:
    """PUT mutations obey same CSRF/Origin rules as #113 review assembly mutations."""
    _t, _inv, token = _issue_invite(app)
    sess = _redeem(client, token)
    csrf = sess["csrf_token"]
    # missing csrf -> 403
    r = client.put(
        "/api/teacher/preferences",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        json={"default_duration": 90},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "csrf_rejected"
    # bad origin
    r = client.put(
        "/api/teacher/preferences",
        headers={
            "Origin": "https://evil.test",
            "X-CSRF-Token": csrf,
            "Content-Type": "application/json",
        },
        json={"default_duration": 90},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "csrf_rejected"
    # good still works
    r = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(csrf),
        json={"default_duration": 60},
    )
    assert r.status_code == 200


def test_teacher_preferences_invalid_input_is_422(app, client) -> None:
    _t, _inv, token = _issue_invite(app)
    sess = _redeem(client, token)
    csrf = sess["csrf_token"]
    r = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(csrf),
        json={"default_duration": 30},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_input"


def test_migration_v003_on_populated_v2_db_preserves_data_and_adds_prefs(tmp_path: Path) -> None:
    """Migration-on-populated-DB: v2 data survives v003; prefs table appears; defaults work."""
    from hramatka.api.migrations import (
        EXPECTED_SCHEMA_VERSION,
        apply_migrations,
        current_schema_version,
    )
    db_path = tmp_path / "populated-v2.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Simulate a v2-populated DB (apply up to v2, insert teacher + lesson)
        # Manually apply v1 + v2 without v3
        conn.execute("BEGIN IMMEDIATE")
        # minimal v1 schema subset sufficient for test (teachers + lesson_jobs + schema_migrations)
        conn.execute(
            """
            CREATE TABLE pilot_teachers (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
                created_at TEXT NOT NULL,
                deactivated_at TEXT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE lesson_jobs (
                teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
                id TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_hash BLOB NOT NULL,
                status TEXT NOT NULL,
                step TEXT NOT NULL,
                failure_code TEXT NULL,
                failure_message TEXT NULL,
                lesson_json TEXT NULL,
                warning_acknowledgements_json TEXT NOT NULL DEFAULT '[]',
                accepted INTEGER NOT NULL DEFAULT 0,
                accepted_at TEXT NULL,
                accepted_revision INTEGER NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NULL,
                completed_at TEXT NULL,
                progress_json TEXT NULL,
                PRIMARY KEY (teacher_id, id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        ts = "2026-07-14T00:00:00Z"
        conn.execute(
            "INSERT INTO pilot_teachers "
            "(id, display_name, created_at, deactivated_at) VALUES (?,?,?,NULL)",
            ("t-pop-1", "Популяційна", ts),
        )
        conn.execute(
            "INSERT INTO lesson_jobs (teacher_id, id, request_json, request_hash, "
            "status, step, warning_acknowledgements_json, accepted, revision, "
            "created_at, updated_at) VALUES (?,?,?,?, 'draft','текст отримано','[]',0,1,?,?)",
            (
                "t-pop-1",
                "l-1",
                '{"anchor":{"text":"x","source":"teacher-paste"},"level":"B1","duration":60,"focus":null}',
                b"hash",
                ts,
                ts,
            ),
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1,'pilot_schema',?), (2,'add_progress_column',?)",
            (ts, ts),
        )
        conn.commit()
        # now apply full migrations (incl v3)
        apply_migrations(conn)
        assert current_schema_version(conn) == EXPECTED_SCHEMA_VERSION == 3
        # data preserved
        trow = conn.execute("SELECT * FROM pilot_teachers WHERE id='t-pop-1'").fetchone()
        assert trow["display_name"] == "Популяційна"
        lrow = conn.execute("SELECT * FROM lesson_jobs WHERE id='l-1'").fetchone()
        assert lrow is not None
        # prefs table exists (from v3), no row yet -> get yields 60
        prow = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='teacher_preferences'"
        ).fetchone()
        assert prow is not None
        # simulate store usage: direct select default
        d = conn.execute(
            "SELECT default_duration FROM teacher_preferences "
            "WHERE teacher_id='t-pop-1'"
        ).fetchone()
        assert d is None  # absent row
    finally:
        conn.close()
    # Now via store: init will have migrated already, but re-open confirms
    from hramatka.api.store import JobStore
    store = JobStore(db_path)
    store.initialize()
    assert store.get_teacher_default_duration("t-pop-1") == 60
    store.set_teacher_default_duration("t-pop-1", 90)
    assert store.get_teacher_default_duration("t-pop-1") == 90


def test_recreate_from_failed_starts_a_new_job(app) -> None:
    with TestClient(app, base_url=ORIGIN) as client:
        _, _, token = _issue_invite(app)
        session = _redeem(client, token)
        failed_id = str(uuid.uuid4())
        created = client.post(
            "/api/lessons",
            headers=_mutation_headers(session["csrf_token"]),
            json=_lesson_request(failed_id),
        )
        assert created.status_code == 202, created.text
        _wait_for_status(client, failed_id, "ready")
        with sqlite3.connect(app.state.store.database_path) as connection:
            connection.execute(
                """
                UPDATE lesson_jobs
                SET status = 'failed', step = 'готово', failure_code = 'engine_unavailable',
                    failure_message = 'Сервіс недоступний.', lesson_json = NULL
                WHERE id = ?
                """,
                (failed_id,),
            )
            connection.commit()
        response = client.post(
            f"/api/lessons/{failed_id}/recreate",
            headers=_mutation_headers(session["csrf_token"]),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["id"] != failed_id
        assert body["status"] in {"draft", "baking"}
        assert body["reused"] is False
        new_id = body["id"]
        ready = _wait_for_status(client, new_id, "ready")
        assert ready["status"] == "ready"
        failed_status = client.get(f"/api/lessons/{failed_id}/status").json()
        assert failed_status["status"] == "failed"


def test_recreate_without_stored_request_returns_422(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path), baker=FixtureBaker())
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
        _wait_for_status(client, lesson_id, "ready")
        with sqlite3.connect(app.state.store.database_path) as connection:
            connection.execute(
                "UPDATE lesson_jobs SET request_json = '{}' WHERE id = ?",
                (lesson_id,),
            )
            connection.commit()
        recreate = client.post(
            f"/api/lessons/{lesson_id}/recreate",
            headers=_mutation_headers(session["csrf_token"]),
        )
        _error(recreate, 422, "invalid_input")


def test_delete_lesson_returns_204_and_removes_row(app) -> None:
    with TestClient(app, base_url=ORIGIN) as client:
        _, _, token = _issue_invite(app)
        session = _redeem(client, token)
        lesson_id = str(uuid.uuid4())
        created = client.post(
            "/api/lessons",
            headers=_mutation_headers(session["csrf_token"]),
            json=_lesson_request(lesson_id),
        )
        assert created.status_code == 202, created.text
        _wait_for_status(client, lesson_id, "ready")
        deleted = client.delete(
            f"/api/lessons/{lesson_id}",
            headers=_mutation_headers(session["csrf_token"]),
        )
        assert deleted.status_code == 204, deleted.text
        assert deleted.content == b""
        _error(client.get(f"/api/lessons/{lesson_id}/status"), 404, "lesson_not_found")
        with sqlite3.connect(app.state.store.database_path) as connection:
            row = connection.execute(
                "SELECT 1 FROM lesson_jobs WHERE id = ?", (lesson_id,)
            ).fetchone()
        assert row is None


def test_delete_foreign_teacher_lesson_returns_404(app) -> None:
    _, _, first_token = _issue_invite(app, display_name="Перша")
    _, _, second_token = _issue_invite(app, display_name="Друга")
    lesson_id = str(uuid.uuid4())
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        first_session = _redeem(first, first_token)
        second_session = _redeem(second, second_token)
        created = first.post(
            "/api/lessons",
            headers=_mutation_headers(first_session["csrf_token"]),
            json=_lesson_request(lesson_id),
        )
        assert created.status_code == 202, created.text
        _wait_for_status(first, lesson_id, "ready")
        foreign = second.delete(
            f"/api/lessons/{lesson_id}",
            headers=_mutation_headers(second_session["csrf_token"]),
        )
        _error(foreign, 404, "lesson_not_found")
        # The foreign delete must NOT remove the owner's row (review #127).
        owner_status = first.get(f"/api/lessons/{lesson_id}/status")
        assert owner_status.status_code == 200, owner_status.text
        assert owner_status.json()["status"] == "ready"


def test_recreate_foreign_teacher_lesson_returns_404(app) -> None:
    _, _, first_token = _issue_invite(app, display_name="Перша")
    _, _, second_token = _issue_invite(app, display_name="Друга")
    lesson_id = str(uuid.uuid4())
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        first_session = _redeem(first, first_token)
        second_session = _redeem(second, second_token)
        created = first.post(
            "/api/lessons",
            headers=_mutation_headers(first_session["csrf_token"]),
            json=_lesson_request(lesson_id),
        )
        assert created.status_code == 202, created.text
        _wait_for_status(first, lesson_id, "ready")
        foreign = second.post(
            f"/api/lessons/{lesson_id}/recreate",
            headers=_mutation_headers(second_session["csrf_token"]),
        )
        _error(foreign, 404, "lesson_not_found")
        # No new job was created for the foreign teacher.
        assert second.get("/api/lessons").json()["lessons"] == []


def test_delete_and_recreate_require_csrf(app) -> None:
    with TestClient(app, base_url=ORIGIN) as client:
        _, _, token = _issue_invite(app)
        session = _redeem(client, token)
        lesson_id = str(uuid.uuid4())
        created = client.post(
            "/api/lessons",
            headers=_mutation_headers(session["csrf_token"]),
            json=_lesson_request(lesson_id),
        )
        assert created.status_code == 202, created.text
        _wait_for_status(client, lesson_id, "ready")
        missing_recreate = client.post(f"/api/lessons/{lesson_id}/recreate")
        _error(missing_recreate, 403, "csrf_rejected")
        missing_delete = client.delete(f"/api/lessons/{lesson_id}")
        _error(missing_delete, 403, "csrf_rejected")
