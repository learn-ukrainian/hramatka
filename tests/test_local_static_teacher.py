"""Contract tests for the explicitly guarded local static teacher link."""

from __future__ import annotations

import base64
import sqlite3
import sys
from dataclasses import replace

from fastapi.testclient import TestClient

from hramatka.api.app import create_app
from hramatka.api.config import Settings

_CSRF_KEY = b"local-static-teacher-test-key-32bytes"
_LOOPBACK_ORIGIN = "https://127.0.0.1:8443"
_STATIC_PATH = "/api/session/local-teacher"


def _settings(
    tmp_path, *, static: bool, bind_host: str = "127.0.0.1", launcher_marker: bool = True
) -> Settings:
    return Settings(
        database_path=tmp_path / "pilot.sqlite3",
        pilot_origin=_LOOPBACK_ORIGIN,
        csrf_hmac_key=_CSRF_KEY,
        local_static_teacher=static,
        local_launcher_marker=launcher_marker,
        server_bind_host=bind_host,
    )


def _registered_paths(app) -> set[str]:
    return {route.path for route in app.routes if hasattr(route, "path")}


def test_loopback_static_link_establishes_session_and_marks_banner(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path, static=True))
    with TestClient(app, base_url=_LOOPBACK_ORIGIN) as client:
        response = client.get(_STATIC_PATH, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/teacher/"
        assert "__Host-hramatka_session=" in response.headers["set-cookie"]

        session = client.get("/api/session")
        assert session.status_code == 200
        payload = session.json()
        assert payload["local_auth_disabled"] is True
        assert payload["teacher"]["display_name"] == "Локальний викладач"
        with sqlite3.connect(app.state.settings.database_path) as connection:
            assert connection.execute(
                "SELECT auth_method FROM pilot_sessions"
            ).fetchone() == ("local",)


def test_non_loopback_binding_never_registers_static_link(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path, static=True, bind_host="0.0.0.0"))
    assert _STATIC_PATH not in _registered_paths(app)
    with TestClient(app, base_url=_LOOPBACK_ORIGIN) as client:
        assert client.get(_STATIC_PATH).status_code == 404


def test_missing_local_launcher_marker_never_registers_static_link(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path, static=True, launcher_marker=False))
    assert _STATIC_PATH not in _registered_paths(app)
    with TestClient(app, base_url=_LOOPBACK_ORIGIN) as client:
        assert client.get(_STATIC_PATH).status_code == 404


def test_absent_flag_keeps_invite_flow_and_static_link_absent(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path, static=False))
    teacher = app.state.store.create_teacher("Звичайна вчителька")
    _invite, invite = app.state.store.create_invite(teacher.id)
    assert _STATIC_PATH not in _registered_paths(app)
    with TestClient(app, base_url=_LOOPBACK_ORIGIN) as client:
        assert client.get(_STATIC_PATH).status_code == 404
        redeemed = client.post(
            "/api/session/redeem",
            headers={"Origin": _LOOPBACK_ORIGIN, "Content-Type": "application/json"},
            json={"token": invite, "nonce": invite},
        )
        assert redeemed.status_code == 200
        assert "local_auth_disabled" not in redeemed.json()


def test_deployed_configuration_defaults_to_api_observed_and_has_no_local_door(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HRAMATKA_PILOT_ORIGIN", "https://pilot.example.test")
    monkeypatch.setenv(
        "HRAMATKA_CSRF_HMAC_KEY",
        base64.urlsafe_b64encode(b"01234567890123456789012345678901").decode().rstrip("="),
    )
    monkeypatch.setenv("HRAMATKA_BAKE_PROVIDERS", "antigravity,openrouter")
    monkeypatch.setenv("HRAMATKA_REVIEW_ATTESTATION_ENABLED", "0")
    monkeypatch.setenv("HRAMATKA_REVIEW_ATTESTATION_PAID_REVIEW_ENABLED", "0")
    for name in (
        "HRAMATKA_LOCAL_STATIC_TEACHER",
        "HRAMATKA_LOCAL_LAUNCHER",
        "HRAMATKA_SERVER_BIND_HOST",
        "HRAMATKA_SUBSCRIPTION_QUALIFICATION_PROVENANCE_TIER",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.subscription_qualification_provenance_tier == "api_observed"
    assert not settings.local_static_teacher_enabled


def test_marked_local_launcher_exposes_operational_flash_route(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HRAMATKA_SUBSCRIPTION_EXECUTABLE", sys.executable)
    monkeypatch.delenv("HRAMATKA_GEN_MODEL", raising=False)
    settings = replace(
        _settings(tmp_path, static=True),
        subscription_qualification_provenance_tier="cli_self_reported",
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url=_LOOPBACK_ORIGIN) as client:
        assert client.get(_STATIC_PATH, follow_redirects=False).status_code == 303
        models = client.get("/api/lesson-models").json()["models"]

    assert models == [
        {
            "id": "gemini-3.6-flash",
            "label": "Gemini 3.6 Flash",
            "description": "Швидке складання уроку.",
        }
    ]


def test_static_link_reuses_one_teacher_across_app_restarts(tmp_path) -> None:
    settings = _settings(tmp_path, static=True)
    with TestClient(create_app(settings=settings), base_url=_LOOPBACK_ORIGIN) as first:
        assert first.get(_STATIC_PATH, follow_redirects=False).status_code == 303
        first_teacher_id = first.get("/api/session").json()["teacher"]["id"]

    with TestClient(create_app(settings=settings), base_url=_LOOPBACK_ORIGIN) as second:
        assert second.get(_STATIC_PATH, follow_redirects=False).status_code == 303
        second_teacher_id = second.get("/api/session").json()["teacher"]["id"]

    assert first_teacher_id == second_teacher_id
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pilot_teachers").fetchone() == (1,)

    app = create_app(settings=settings)
    app.state.store.deactivate_teacher(first_teacher_id)
    with TestClient(app, base_url=_LOOPBACK_ORIGIN) as client:
        assert client.get(_STATIC_PATH, follow_redirects=False).status_code == 401
