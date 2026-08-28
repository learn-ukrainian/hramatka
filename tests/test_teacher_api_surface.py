"""Fail-closed FastAPI docs and honest unknown-route envelopes (#515)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hramatka.api.app import create_app
from hramatka.api.config import Settings

_ORIGIN = "https://pilot.example.test"
_REVIEW_ATTESTATIONS = "/api/internal/review-attestations"
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        database_path=tmp_path / "pilot.sqlite3",
        pilot_origin=_ORIGIN,
        csrf_hmac_key=b"test-only-hmac-key-that-is-not-a-deployment-secret",
        **overrides,
    )


def _registered_paths(app) -> set[str]:
    return {route.path for route in app.routes if hasattr(route, "path")}


def test_fastapi_docs_and_openapi_are_not_served(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url=_ORIGIN) as client:
        for path in _DOC_PATHS:
            response = client.get(path)
            assert response.status_code != 200, path
            body = response.json()
            assert body["code"] != "lesson_not_found"
            assert set(body) == {"code", "message", "retryable"}


def test_unknown_api_path_is_not_lesson_not_found(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url=_ORIGIN) as client:
        response = client.get("/api/foo")
    assert response.status_code == 404
    body = response.json()
    assert body == {
        "code": "not_found",
        "message": "Не знайдено.",
        "retryable": False,
    }


def test_wrong_method_on_known_api_path_is_not_lesson_not_found(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url=_ORIGIN) as client:
        response = client.post("/api/healthz")
    assert response.status_code == 405
    assert response.json()["code"] == "not_found"


def test_review_attestations_are_unregistered_when_disabled(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path, review_attestation_enabled=False))
    assert _REVIEW_ATTESTATIONS not in _registered_paths(app)
    with TestClient(app, base_url=_ORIGIN) as client:
        response = client.post(
            _REVIEW_ATTESTATIONS,
            headers={"Content-Type": "application/json"},
            json={},
        )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.json()["code"] != "github_token_required"
