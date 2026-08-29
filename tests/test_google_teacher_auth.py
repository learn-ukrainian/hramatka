"""Google-first teacher sign-in: cookie seam, linking, and fail-closed checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from hramatka.api import app as app_module
from hramatka.api import google_identity
from hramatka.api.app import create_app
from hramatka.api.config import (
    Settings,
    _parse_google_allowed_emails,
    _validate_google_allowed_email_teacher_id,
)
from hramatka.api.google_identity import (
    GoogleCredentialInvalid,
    VerifiedGoogleIdentity,
    verify_google_credential,
)
from hramatka.api.migrations import MIGRATIONS, current_schema_version
from hramatka.api.store import JobStore, SessionUnavailable

ORIGIN = "https://pilot.example.test"
CSRF_KEY = b"test-only-hmac-key-that-is-not-a-deployment-secret"
CLIENT_ID = "123456789-test.apps.googleusercontent.com"
QA_EMAIL = "u2600322959@gmail.com"
REPO_ROOT = Path(__file__).parents[1]


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        database_path=tmp_path / "pilot.sqlite3",
        pilot_origin=ORIGIN,
        csrf_hmac_key=CSRF_KEY,
        google_client_id=CLIENT_ID,
        **overrides,
    )


@pytest.fixture
def app(tmp_path: Path):
    return create_app(settings=_settings(tmp_path), baker=SimpleNamespace(bake=lambda *_: {}))


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


def _invite_session(
    app, client: TestClient, name: str = "Тестова вчителька"
) -> tuple[object, dict]:
    teacher = app.state.store.create_teacher(name)
    _, token = app.state.store.create_invite(teacher.id)
    response = client.post(
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": token, "nonce": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
    )
    assert response.status_code == 200, response.text
    return teacher, response.json()


def _options(client: TestClient, csrf: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {"Origin": ORIGIN}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    response = client.post("/api/auth/google/options", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _google_callback(
    client: TestClient,
    *,
    credential: str = "signed-google-id-token",
    csrf: str = "google-double-submit",
):
    client.cookies.set("g_csrf_token", csrf, domain="pilot.example.test", path="/")
    return client.post(
        "/api/auth/google/complete",
        data={"credential": credential, "g_csrf_token": csrf},
        follow_redirects=False,
    )


def _identity(subject: str, email: str, nonce: str) -> VerifiedGoogleIdentity:
    return VerifiedGoogleIdentity(subject=subject, email=email, nonce=nonce)


def test_authenticated_link_then_returning_google_sign_in_uses_same_cookie_seam(
    monkeypatch, app, client
) -> None:
    teacher, invite_session = _invite_session(app, client)
    assert invite_session["google_linked"] is False
    linking = _options(client, invite_session["csrf_token"])
    assert linking["client_id"] == CLIENT_ID
    assert linking["login_uri"] == f"{ORIGIN}/api/auth/google/complete"
    monkeypatch.setattr(
        app_module,
        "verify_google_credential",
        lambda *_: _identity("google-stable-sub", "teacher@example.test", linking["nonce"]),
    )

    # A Lax cookie does not accompany Google’s cross-site form POST. The
    # nonce-bound server record, not the browser cookie, authorizes first link.
    client.cookies.clear()
    linked = _google_callback(client)
    assert linked.status_code == 303
    assert linked.headers["location"] == "/teacher/?google=linked"
    assert "__Host-hramatka_session=" in linked.headers["set-cookie"]
    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.json()["teacher"]["id"] == teacher.id
    assert session.json()["google_linked"] is True

    # The authoritative session flag prevents a second setup ceremony.
    connected_options = client.post(
        "/api/auth/google/options",
        headers={"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]},
    )
    assert connected_options.status_code == 409
    assert app.state.store.teacher_has_google_identity(teacher.id) is True

    # A fresh signed-out ceremony resolves only the durable provider subject.
    client.cookies.clear()
    sign_in = _options(client)
    monkeypatch.setattr(
        app_module,
        "verify_google_credential",
        lambda *_: _identity("google-stable-sub", "renamed@example.test", sign_in["nonce"]),
    )
    signed_in = _google_callback(client)
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/teacher/"
    returned = client.get("/api/session")
    assert returned.status_code == 200
    assert returned.json()["teacher"]["id"] == teacher.id

    # Logout revokes this ordinary Google-derived session just like every other entry method.
    assert (
        client.delete(
            "/api/session",
            headers={"Origin": ORIGIN, "X-CSRF-Token": returned.json()["csrf_token"]},
        ).status_code
        == 204
    )
    assert client.get("/api/session").status_code == 401


def test_google_callback_rejects_unknown_nonce_csrf_replay_and_unknown_identity(
    monkeypatch, app, client
) -> None:
    sign_in = _options(client)
    calls = 0

    def verified(*_):
        nonlocal calls
        calls += 1
        return _identity("unknown-google-sub", "unknown@example.test", sign_in["nonce"])

    monkeypatch.setattr(app_module, "verify_google_credential", verified)
    csrf_rejected = client.post(
        "/api/auth/google/complete",
        data={"credential": "token", "g_csrf_token": "body"},
        follow_redirects=False,
    )
    assert csrf_rejected.headers["location"] == "/teacher/?google=failed"
    assert calls == 0

    unknown = _google_callback(client)
    assert unknown.headers["location"] == "/teacher/?google=failed"
    assert calls == 1
    replay = _google_callback(client)
    assert replay.headers["location"] == "/teacher/?google=failed"
    assert calls == 2

    bad_nonce = _options(client)
    monkeypatch.setattr(
        app_module,
        "verify_google_credential",
        lambda *_: _identity("whatever", "teacher@example.test", "A" * 43),
    )
    rejected = _google_callback(client)
    assert rejected.headers["location"] == "/teacher/?google=failed"
    assert bad_nonce["nonce"] != "A" * 43


def test_google_link_cannot_relink_another_teachers_subject(monkeypatch, app, client) -> None:
    _, first_session = _invite_session(app, client, "Перша")
    first_options = _options(client, first_session["csrf_token"])
    monkeypatch.setattr(
        app_module,
        "verify_google_credential",
        lambda *_: _identity("shared-sub", "one@example.test", first_options["nonce"]),
    )
    client.cookies.clear()
    assert _google_callback(client).headers["location"] == "/teacher/?google=linked"

    client.cookies.clear()
    _, second_session = _invite_session(app, client, "Друга")
    second_options = _options(client, second_session["csrf_token"])
    monkeypatch.setattr(
        app_module,
        "verify_google_credential",
        lambda *_: _identity("shared-sub", "two@example.test", second_options["nonce"]),
    )
    client.cookies.clear()
    assert _google_callback(client).headers["location"] == "/teacher/?google=failed"


def test_allowlisted_qa_email_binds_dedicated_teacher_and_mints_session(
    monkeypatch, tmp_path
) -> None:
    bootstrap = create_app(
        settings=_settings(tmp_path), baker=SimpleNamespace(bake=lambda *_: {})
    )
    teacher = bootstrap.state.store.create_teacher("QA викладач")
    app = create_app(
        settings=_settings(
            tmp_path,
            google_allowed_emails=frozenset({QA_EMAIL}),
            google_allowed_email_teacher_id=teacher.id,
        ),
        baker=SimpleNamespace(bake=lambda *_: {}),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        sign_in = _options(client)
        monkeypatch.setattr(
            app_module,
            "verify_google_credential",
            lambda *_: _identity("qa-google-sub", "U2600322959@Gmail.com", sign_in["nonce"]),
        )
        completed = _google_callback(client)
        assert completed.status_code == 303
        assert completed.headers["location"] == "/teacher/"
        assert "__Host-hramatka_session=" in completed.headers["set-cookie"]
        session = client.get("/api/session")
        assert session.status_code == 200
        assert session.json()["teacher"]["id"] == teacher.id
        assert session.json()["teacher"]["display_name"] == "QA викладач"
        assert session.json()["google_linked"] is True
        assert app.state.store.teacher_has_google_identity(teacher.id) is True

        client.cookies.clear()
        again = _options(client)
        monkeypatch.setattr(
            app_module,
            "verify_google_credential",
            lambda *_: _identity("qa-google-sub", QA_EMAIL, again["nonce"]),
        )
        returning = _google_callback(client)
        assert returning.headers["location"] == "/teacher/"
        returned = client.get("/api/session")
        assert returned.status_code == 200
        assert returned.json()["teacher"]["id"] == teacher.id


def test_allowlisted_qa_email_mints_session_when_staff_authorization_is_required(
    monkeypatch, tmp_path
) -> None:
    bootstrap = create_app(
        settings=_settings(tmp_path), baker=SimpleNamespace(bake=lambda *_: {})
    )
    teacher = bootstrap.state.store.create_teacher("QA викладач")
    app = create_app(
        settings=_settings(
            tmp_path,
            google_allowed_emails=frozenset({QA_EMAIL}),
            google_allowed_email_teacher_id=teacher.id,
            staff_authorization_required=True,
        ),
        baker=SimpleNamespace(bake=lambda *_: {}),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        sign_in = _options(client)
        monkeypatch.setattr(
            app_module,
            "verify_google_credential",
            lambda *_: _identity("qa-staff-sub", QA_EMAIL, sign_in["nonce"]),
        )
        completed = _google_callback(client)
        assert completed.status_code == 303
        assert completed.headers["location"] == "/teacher/"
        session = client.get("/api/session")
        assert session.status_code == 200
        assert session.json()["teacher"]["id"] == teacher.id
        assert session.json()["google_linked"] is True
        assert session.json()["role"] == "teacher"


def test_allowlisted_email_creates_teacher_when_host_omits_teacher_id(
    monkeypatch, tmp_path
) -> None:
    app = create_app(
        settings=_settings(tmp_path, google_allowed_emails=frozenset({QA_EMAIL})),
        baker=SimpleNamespace(bake=lambda *_: {}),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        sign_in = _options(client)
        monkeypatch.setattr(
            app_module,
            "verify_google_credential",
            lambda *_: _identity("created-google-sub", QA_EMAIL, sign_in["nonce"]),
        )
        completed = _google_callback(client)
        assert completed.headers["location"] == "/teacher/"
        session = client.get("/api/session")
        assert session.status_code == 200
        teacher_id = session.json()["teacher"]["id"]
        assert session.json()["google_linked"] is True
        assert app.state.store.teacher_has_google_identity(teacher_id) is True


def test_unlisted_google_email_fails_with_visible_query_and_no_session(
    monkeypatch, app, client
) -> None:
    sign_in = _options(client)
    monkeypatch.setattr(
        app_module,
        "verify_google_credential",
        lambda *_: _identity("stranger-sub", "stranger@example.test", sign_in["nonce"]),
    )
    rejected = _google_callback(client)
    assert rejected.status_code == 303
    assert rejected.headers["location"] == "/teacher/?google=failed"
    assert "__Host-hramatka_session=" not in rejected.headers.get("set-cookie", "")
    assert client.get("/api/session").status_code == 401


def test_parse_google_allowed_emails_casefolds_and_deduplicates() -> None:
    parsed = _parse_google_allowed_emails(
        f" {QA_EMAIL.upper()}, {QA_EMAIL}, other.teacher@example.test "
    )
    assert parsed == frozenset({QA_EMAIL, "other.teacher@example.test"})
    assert _parse_google_allowed_emails(None) == frozenset()
    assert _parse_google_allowed_emails("  ") == frozenset()
    with pytest.raises(ValueError, match="comma-separated emails"):
        _parse_google_allowed_emails("not-an-email")


def test_google_allowed_email_teacher_id_must_be_a_uuid() -> None:
    teacher_id = "123e4567-e89b-12d3-a456-426614174000"
    assert _validate_google_allowed_email_teacher_id(teacher_id) == teacher_id
    assert _validate_google_allowed_email_teacher_id("") is None
    with pytest.raises(ValueError, match="teacher UUID"):
        _validate_google_allowed_email_teacher_id("not-a-uuid")


def test_google_link_never_reuses_a_revoked_subject(monkeypatch, app, client) -> None:
    first, _ = _invite_session(app, client, "Історична")
    with app.state.store._write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO google_teacher_identities
            (id, teacher_id, subject, email_at_link, created_at, last_used_at, revoked_at)
            VALUES (?, ?, 'permanently-bound-sub', 'historic@example.test',
                    '2026-01-01T00:00:00Z', NULL, '2026-01-02T00:00:00Z')
            """,
            (str(uuid4()), first.id),
        )
    client.cookies.clear()
    _, second_session = _invite_session(app, client, "Нова")
    options = _options(client, second_session["csrf_token"])
    monkeypatch.setattr(
        app_module,
        "verify_google_credential",
        lambda *_: _identity(
            "permanently-bound-sub", "new@example.test", options["nonce"]
        ),
    )
    client.cookies.clear()
    assert _google_callback(client).headers["location"] == "/teacher/?google=failed"


def test_google_options_fail_closed_without_the_explicit_public_client_id(tmp_path) -> None:
    disabled = create_app(
        settings=Settings(
            database_path=tmp_path / "disabled.sqlite3",
            pilot_origin=ORIGIN,
            csrf_hmac_key=CSRF_KEY,
        ),
        baker=SimpleNamespace(bake=lambda *_: {}),
    )
    with TestClient(disabled, base_url=ORIGIN) as test_client:
        response = test_client.post("/api/auth/google/options", headers={"Origin": ORIGIN})
    assert response.status_code == 404


def test_google_verifier_rejects_a_malformed_credential(tmp_path) -> None:
    with pytest.raises(GoogleCredentialInvalid):
        verify_google_credential("not-a-jwt", _settings(tmp_path))


@pytest.mark.parametrize(
    "jwks_failure",
    [
        PyJWKClientError("Unable to find a signing key that matches: missing-kid"),
        PyJWKClientConnectionError("Google key set is unavailable"),
    ],
)
def test_google_callback_fails_closed_when_google_key_selection_or_fetch_fails(
    monkeypatch, app, client, jwks_failure
) -> None:
    _options(client)

    def unavailable_key(_credential: str):
        raise jwks_failure

    monkeypatch.setattr(
        google_identity,
        "_jwks_client",
        lambda _: SimpleNamespace(get_signing_key_from_jwt=unavailable_key),
    )
    response = _google_callback(client, credential="header.payload.signature")
    assert response.status_code == 303
    assert response.headers["location"] == "/teacher/?google=failed"


def test_google_link_options_returns_clean_401_when_the_bound_session_races_away(
    monkeypatch, app, client
) -> None:
    _, invite_session = _invite_session(app, client)

    def lost_session(**_: object):
        raise SessionUnavailable("The teacher session is no longer active.")

    monkeypatch.setattr(app.state.store, "issue_google_login_nonce", lost_session)
    response = client.post(
        "/api/auth/google/options",
        headers={"Origin": ORIGIN, "X-CSRF-Token": invite_session["csrf_token"]},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "session_required"


def test_frontend_primary_google_cta_keeps_fallbacks_and_never_persists_a_credential() -> None:
    source = (REPO_ROOT / "hramatka/app/src/App.tsx").read_text(encoding="utf-8")
    assert 'data-testid="google-sign-in-button"' in source
    assert "ux_mode: 'redirect'" in source
    assert "login_uri: googleOptions.login_uri" in source
    assert "params.get('google') === 'failed'" in source
    assert "data-testid=\"passkey-sign-in-btn\"" in source
    assert "data-testid=\"recovery-code-sign-in\"" in source
    assert '<details className="sign-in-alternatives"' in source
    assert "{t('auth.otherWays')}" in source
    assert "!session.google_linked && googleOptions" in source
    assert 'data-testid="google-setup-card"' in source
    assert "google.setupTitle" in source and "google.setupLead" in source
    assert "google-link-button" not in source
    assert "prompt(t('invite.prompt'))" not in source
    assert "localStorage.setItem('credential'" not in source
    assert "sessionStorage.setItem('credential'" not in source

    caddyfile = (REPO_ROOT / "hramatka/api/deploy/Caddyfile").read_text(encoding="utf-8")
    assert "https://accounts.google.com/gsi/client" in caddyfile
    assert "frame-src https://accounts.google.com" in caddyfile
    assert "connect-src 'self' https://accounts.google.com" in caddyfile


@pytest.mark.parametrize(
    "claim, value",
    [
        ("aud", "other.apps.googleusercontent.com"),
        ("exp", "expired"),
        ("email_verified", False),
        ("iss", "https://wrong.example"),
    ],
)
def test_google_verifier_rejects_wrong_audience_expiry_unverified_email_and_issuer(
    monkeypatch, tmp_path, claim, value
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        "hramatka.api.google_identity._jwks_client",
        lambda _: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key())
        ),
    )
    now = datetime.now(UTC)
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "stable-subject",
        "email": "teacher@example.test",
        "email_verified": True,
        "nonce": "A" * 43,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    if claim == "exp":
        claims[claim] = int((now - timedelta(minutes=5)).timestamp())
    else:
        claims[claim] = value
    token = jwt.encode(claims, private_key, algorithm="RS256")
    with pytest.raises(GoogleCredentialInvalid):
        verify_google_credential(token, _settings(tmp_path))


def test_v015_preserves_v014_sessions_and_adds_google_identity_tables(tmp_path) -> None:
    database = tmp_path / "v014.sqlite3"
    store = JobStore(database)
    store.initialize()
    teacher = store.create_teacher("Збережена")
    _, token = store.create_invite(teacher.id)
    store.redeem_invite(token, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    # Rewind only the migration ledger and schema to a real v014 state, then
    # apply v015 as a deployed upgrade would. The invite-derived session stays.
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")
        connection.execute("DROP TABLE google_login_nonces")
        connection.execute("DROP TABLE google_teacher_identities")
        connection.execute("ALTER TABLE pilot_sessions RENAME TO pilot_sessions_v015")
        connection.execute(
            """
            CREATE TABLE pilot_sessions AS
            SELECT id, teacher_id, invite_id, secret_hash, created_at, expires_at, revoked_at,
                   redeem_nonce_hash, idle_expires_at, last_seen_at, auth_method
            FROM pilot_sessions_v015
            """
        )
        connection.execute("DROP TABLE pilot_sessions_v015")
        connection.commit()
    store.initialize()
    with store._read_connection() as connection:
        assert current_schema_version(connection) == MIGRATIONS[-1][0]
    # This database was generated by the normal application, so only a fresh
    # opaque secret remains necessary to prove its retained session row exists.
    with store._read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM pilot_sessions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'google_teacher_identities'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'google_email_preauthorizations'"
        ).fetchone()
