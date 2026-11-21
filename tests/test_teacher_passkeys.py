"""Mutation-oriented passkey and recovery re-entry contract tests."""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import sqlite3
import struct
from pathlib import Path
from types import SimpleNamespace

import cbor2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
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


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _attestation_object(
    credential_id: bytes,
    cose_public_key: bytes,
    *,
    fmt: str = "none",
    att_stmt: dict[str, object] | None = None,
) -> str:
    rp_id_hash = hashlib.sha256(b"pilot.example.test").digest()
    auth_data = (
        rp_id_hash
        + bytes([0x45])  # UP | UV | AT
        + struct.pack(">I", 0)
        + (b"\x11" * 16)
        + struct.pack(">H", len(credential_id))
        + credential_id
        + cose_public_key
    )
    return _b64url(cbor2.dumps({"fmt": fmt, "attStmt": att_stmt or {}, "authData": auth_data}))


def _invalid_version_x509_der() -> bytes:
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, algorithm=None)
    )
    der = bytearray(cert.public_bytes(Encoding.DER))
    version_offset = der.index(bytes([0xA0, 0x03, 0x02, 0x01, 0x02]))
    der[version_offset + 4] = 0x03  # Invalid X.509 v4 triggers x509.InvalidVersion
    return bytes(der)


def test_malformed_webauthn_payload_returns_client_error_never_5xx(app, client) -> None:
    """Defect class: malformed WebAuthn payload returns 5xx (unmocked webauthn paths)."""
    expected_422 = {
        "code": "invalid_input",
        "message": "Запит містить помилку.",
        "retryable": False,
    }
    expected_401 = {
        "code": "session_required",
        "message": "Потрібна чинна сесія вчителя.",
        "retryable": False,
    }

    teacher, token = _issue_invite(app)
    session = _redeem(client, token)
    headers = _mutation_headers(session["csrf_token"])
    options = client.post("/api/passkeys/enrollment/options", headers=headers).json()
    challenge_id = options["challenge_id"]
    challenge = options["publicKey"]["challenge"]
    credential_id = b"unmocked-credential"
    encoded_id = _b64url(credential_id)
    create_client_data = _b64url(
        json.dumps({"type": "webauthn.create", "challenge": challenge, "origin": ORIGIN}).encode()
    )

    # Enrollment: sibling WebAuthnException subclasses (InvalidJSONStructure,
    # UnsupportedPublicKeyType), COSE decode KeyError/IndexError, deeply nested JSON
    # RecursionError, invalid base64url, and SafetyNet AttributeError must return 422.
    valid_ed25519_cose = cbor2.dumps({1: 1, 3: -8, -1: 6, -2: b"\x01" * 32})
    safetynet_array_jws = f"{_b64url(b'[]')}.{_b64url(b'{}')}.{_b64url(b'sig')}".encode("ascii")
    enrollment_cases = [
        # Deeply nested JSON in clientDataJSON -> RecursionError in json.loads
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {"clientDataJSON": _b64url(("[" * 5000 + "]" * 5000).encode("ascii"))},
        },
        # Invalid base64url in clientDataJSON and challenge -> 422
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {"clientDataJSON": "!!!!"},
        },
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64url(
                    json.dumps(
                        {"type": "webauthn.create", "challenge": "!!!!", "origin": ORIGIN}
                    ).encode()
                )
            },
        },
        # Missing attestationObject -> InvalidJSONStructure
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {"clientDataJSON": create_client_data},
        },
        # Unsupported COSE kty=99 -> UnsupportedPublicKeyType
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": create_client_data,
                "attestationObject": _attestation_object(
                    credential_id, cbor2.dumps({1: 99, 3: -7})
                ),
            },
        },
        # EC2 COSE key missing required CRV/X/Y -> KeyError in decode_credential_public_key
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": create_client_data,
                "attestationObject": _attestation_object(
                    credential_id, cbor2.dumps({1: 2, 3: -7})
                ),
            },
        },
        # Non-map CBOR public key ([]) -> IndexError in decode_credential_public_key
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": create_client_data,
                "attestationObject": _attestation_object(credential_id, cbor2.dumps([])),
            },
        },
        # SafetyNet JWS header decoding to JSON array -> AttributeError in verify_android_safetynet
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": create_client_data,
                "attestationObject": _attestation_object(
                    credential_id,
                    valid_ed25519_cose,
                    fmt="android-safetynet",
                    att_stmt={"ver": "1", "response": safetynet_array_jws},
                ),
            },
        },
        # SafetyNet non-bytes response -> AttributeError in verify_android_safetynet
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": create_client_data,
                "attestationObject": _attestation_object(
                    credential_id,
                    valid_ed25519_cose,
                    fmt="android-safetynet",
                    att_stmt={"ver": "1", "response": 123},
                ),
            },
        },
        # Packed attestation leaf with invalid X.509 version -> x509.InvalidVersion
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": create_client_data,
                "attestationObject": _attestation_object(
                    credential_id,
                    valid_ed25519_cose,
                    fmt="packed",
                    att_stmt={"alg": -8, "sig": b"sig", "x5c": [_invalid_version_x509_der()]},
                ),
            },
        },
    ]
    for credential_payload in enrollment_cases:
        response = client.post(
            f"/api/passkeys/enrollment/{challenge_id}",
            headers=headers,
            json={"credential": credential_payload},
        )
        assert response.status_code == 422, response.text
        assert response.json() == expected_422

    # Sign-in: enroll a credential with a missing-key COSE map so unmocked verify_assertion
    # exercises both InvalidJSONStructure (missing credential keys) and KeyError (COSE decode).
    app.state.store.add_webauthn_credential(
        teacher_id=teacher.id,
        credential_id=credential_id,
        public_key=cbor2.dumps({1: 2, 3: -7}),
        sign_count=0,
    )
    client.cookies.clear()
    auth_options = client.post(
        "/api/passkeys/authentication/options", headers={"Origin": ORIGIN}
    ).json()
    get_client_data = _b64url(
        json.dumps(
            {
                "type": "webauthn.get",
                "challenge": auth_options["publicKey"]["challenge"],
                "origin": ORIGIN,
            }
        ).encode()
    )

    # Pre-lookup malformed rawId (missing, non-string, empty, invalid or non-canonical base64url)
    # -> 422 invalid_input
    for malformed_raw_id_credential in (
        {"response": {"clientDataJSON": get_client_data}},
        {"rawId": 12345, "response": {"clientDataJSON": get_client_data}},
        {"rawId": "", "response": {"clientDataJSON": get_client_data}},
        {"rawId": "!!!!", "response": {"clientDataJSON": get_client_data}},
        {"rawId": "ab+c", "response": {"clientDataJSON": get_client_data}},
        {"rawId": "YWI=", "response": {"clientDataJSON": get_client_data}},
        {"rawId": "ab", "response": {"clientDataJSON": get_client_data}},
    ):
        response = client.post(
            "/api/passkeys/authentication",
            headers={"Origin": ORIGIN},
            json={
                "challenge_id": auth_options["challenge_id"],
                "credential": malformed_raw_id_credential,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json() == expected_422

    # Post-lookup failures against an enrolled credential -> 401 session_required (never 5xx
    # and indistinguishable from an unknown credential).
    auth_data = hashlib.sha256(b"pilot.example.test").digest() + bytes([0x05]) + struct.pack(">I", 1)
    assertion_cases = [
        # Missing id, authenticatorData, signature -> InvalidJSONStructure
        {
            "rawId": encoded_id,
            "response": {"clientDataJSON": get_client_data},
        },
        # Structurally complete assertion hitting COSE KeyError in decode_credential_public_key
        {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": get_client_data,
                "authenticatorData": _b64url(auth_data),
                "signature": _b64url(b"not-a-valid-signature"),
            },
        },
    ]
    for assertion_credential in assertion_cases:
        response = client.post(
            "/api/passkeys/authentication",
            headers={"Origin": ORIGIN},
            json={
                "challenge_id": auth_options["challenge_id"],
                "credential": assertion_credential,
            },
        )
        assert response.status_code == 401, response.text
        assert response.json() == expected_401
