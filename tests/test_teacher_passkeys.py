"""Mutation-oriented passkey and recovery re-entry contract tests."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hramatka.api import app as app_module
from hramatka.api.app import create_app
from hramatka.api.config import Settings

ORIGIN = "https://pilot.example.test"
CSRF_KEY = b"test-only-hmac-key-that-is-not-a-deployment-secret"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "pilot.sqlite3", pilot_origin=ORIGIN, csrf_hmac_key=CSRF_KEY
    )


def _issue_invite(app):
    teacher = app.state.store.create_teacher("Тестова вчителька")
    _, token = app.state.store.create_invite(teacher.id)
    return teacher, token


def _mutation_headers(csrf_token: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf_token}


def _redeem(client: TestClient, token: str) -> dict:
    response = client.post(
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": token, "nonce": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _app(tmp_path: Path):
    # The baker is never reached by these auth-only requests.
    return create_app(settings=_settings(tmp_path), baker=SimpleNamespace(bake=lambda *_: {}))


@pytest.fixture
def app(tmp_path: Path):
    return _app(tmp_path)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


def _credential(challenge: str, credential_id: bytes) -> dict[str, object]:
    client_data = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"type": "webauthn.create", "challenge": challenge, "origin": ORIGIN}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return {
        "id": base64.urlsafe_b64encode(credential_id).decode().rstrip("="),
        "rawId": base64.urlsafe_b64encode(credential_id).decode().rstrip("="),
        "type": "public-key",
        "response": {"clientDataJSON": client_data},
    }


def _enroll(monkeypatch, client: TestClient, session: dict, credential_id: bytes) -> list[str]:
    options = client.post(
        "/api/passkeys/enrollment/options", headers=_mutation_headers(session["csrf_token"])
    )
    assert options.status_code == 200, options.text
    payload = options.json()
    credential = _credential(payload["publicKey"]["challenge"], credential_id)
    monkeypatch.setattr(
        app_module,
        "verify_registration",
        lambda **_: SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"public-key-" + credential_id,
            sign_count=0,
        ),
    )
    registered = client.post(
        f"/api/passkeys/enrollment/{payload['challenge_id']}",
        headers=_mutation_headers(session["csrf_token"]),
        json={"credential": credential},
    )
    assert registered.status_code == 200, registered.text
    return registered.json()["recovery_codes"]


def _assertion(monkeypatch, client: TestClient, credential_id: bytes) -> dict:
    options = client.post("/api/passkeys/authentication/options", headers={"Origin": ORIGIN})
    assert options.status_code == 200, options.text
    payload = options.json()
    credential = _credential(payload["publicKey"]["challenge"], credential_id)
    monkeypatch.setattr(
        app_module,
        "verify_assertion",
        lambda **_: SimpleNamespace(credential_id=credential_id, new_sign_count=1),
    )
    response = client.post(
        "/api/passkeys/authentication",
        headers={"Origin": ORIGIN},
        json={"challenge_id": payload["challenge_id"], "credential": credential},
    )
    assert response.status_code == 200, response.text
    return {
        "payload": response.json(),
        "cookie": response.headers["set-cookie"],
        "request": {"challenge_id": payload["challenge_id"], "credential": credential},
    }


def test_enrollment_requires_an_active_invite_derived_session(app, client) -> None:
    missing = client.post(
        "/api/passkeys/enrollment/options", headers={"Origin": ORIGIN, "X-CSRF-Token": "x"}
    )
    assert missing.status_code == 401

    _, token = _issue_invite(app)
    session = _redeem(client, token)
    assert (
        client.post(
            "/api/passkeys/enrollment/options", headers=_mutation_headers(session["csrf_token"])
        ).status_code
        == 200
    )


def test_passkey_reentry_uses_the_same_cookie_csrf_expiry_and_revocation(
    monkeypatch, app, client
) -> None:
    teacher, token = _issue_invite(app)
    session = _redeem(client, token)
    _enroll(monkeypatch, client, session, b"laptop-credential")
    client.cookies.clear()
    authenticated = _assertion(monkeypatch, client, b"laptop-credential")
    assert "__Host-hramatka_session=" in authenticated["cookie"]
    assert "HttpOnly" in authenticated["cookie"] and "Secure" in authenticated["cookie"]
    assert authenticated["payload"]["csrf_token"]
    assert authenticated["payload"]["expires_at"]
    assert app.state.store.revoke_teacher_sessions(teacher.id) >= 1
    assert client.get("/api/session").status_code == 401


def test_passkey_challenge_is_single_use_and_expiry_refuses_assertion(
    monkeypatch, app, client
) -> None:
    _, token = _issue_invite(app)
    session = _redeem(client, token)
    _enroll(monkeypatch, client, session, b"replay-credential")
    client.cookies.clear()
    authenticated = _assertion(monkeypatch, client, b"replay-credential")
    monkeypatch.setattr(
        app_module,
        "verify_assertion",
        lambda **_: SimpleNamespace(credential_id=b"replay-credential", new_sign_count=2),
    )
    replay = client.post(
        "/api/passkeys/authentication", headers={"Origin": ORIGIN}, json=authenticated["request"]
    )
    assert replay.status_code == 401

    options = client.post("/api/passkeys/authentication/options", headers={"Origin": ORIGIN}).json()
    with sqlite3.connect(app.state.store.database_path) as connection:
        connection.execute(
            "UPDATE webauthn_challenges SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (options["challenge_id"],),
        )
    credential = _credential(options["publicKey"]["challenge"], b"replay-credential")
    monkeypatch.setattr(
        app_module,
        "verify_assertion",
        lambda **_: SimpleNamespace(credential_id=b"replay-credential", new_sign_count=2),
    )
    assert (
        client.post(
            "/api/passkeys/authentication",
            headers={"Origin": ORIGIN},
            json={"challenge_id": options["challenge_id"], "credential": credential},
        ).status_code
        == 401
    )


def test_recovery_codes_are_digests_and_are_single_use(monkeypatch, app, client) -> None:
    _, token = _issue_invite(app)
    codes = _enroll(monkeypatch, client, _redeem(client, token), b"recovery-credential")
    code = codes[0]
    with sqlite3.connect(app.state.store.database_path) as connection:
        durable = "\n".join(str(row) for row in connection.execute("SELECT * FROM recovery_codes"))
    assert code not in durable
    client.cookies.clear()
    assert (
        client.post(
            "/api/recovery-codes/redeem", headers={"Origin": ORIGIN}, json={"code": code}
        ).status_code
        == 200
    )
    client.cookies.clear()
    assert (
        client.post(
            "/api/recovery-codes/redeem", headers={"Origin": ORIGIN}, json={"code": code}
        ).status_code
        == 401
    )


def test_two_authenticators_can_be_enrolled_and_either_can_reenter(
    monkeypatch, app, client
) -> None:
    _, token = _issue_invite(app)
    session = _redeem(client, token)
    _enroll(monkeypatch, client, session, b"laptop")
    _enroll(monkeypatch, client, session, b"phone")
    for credential_id in (b"laptop", b"phone"):
        client.cookies.clear()
        assert (
            _assertion(monkeypatch, client, credential_id)["payload"]["teacher"]
            == session["teacher"]
        )
