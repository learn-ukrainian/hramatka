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
import re
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
from hramatka.api.baking.engine_adapter_v3 import EngineLessonBaker as EngineLessonBakerV3
from hramatka.api.baking.port import BakeError, FloorUnmetError, ProviderUnavailable
from hramatka.api.config import Settings
from hramatka.engine import data
from hramatka.engine.content_density import (
    FLOOR_SHORTFALL_UA_MESSAGE,
)
from hramatka.qualification import harness as qualification_harness

ORIGIN = "https://pilot.example.test"
CSRF_KEY = b"test-only-hmac-key-that-is-not-a-deployment-secret"
_TEACHER_PROGRESS_FIELDS = {
    "phase",
    "phases_total",
    "step",
    "calls_done",
    "calls_planned",
    "updated_at",
}


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
    """Fixture baker with one durable phase-two reserve beyond the B1 budget."""

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        template = super().bake(anchor, duration, focus)
        visible_extra = copy.deepcopy(
            next(block for block in template["blocks"] if block["id"] == "block-4")
        )
        visible_extra["id"] = "block-phase-two-visible"
        visible_extra["activity"]["id"] = "activity-phase-two-visible"
        template["blocks"].append(visible_extra)
        reserve = copy.deepcopy(visible_extra)
        reserve["id"] = "block-reserve"
        reserve["activity"]["id"] = "activity-reserve"
        template["blocks"].append(reserve)
        return template


class ErrorCorrectionFixtureBaker(FixtureBaker):
    """Fixture baker with an outer-only, multiplicity-preserving correction key."""

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        template = super().bake(anchor, duration, focus)
        sentence = "Я живу в Києв."
        correction = {"sentence": sentence, "error": "Києв", "correction": "Києві"}
        activity = {
            "id": "activity-error-correction-1",
            "type": "error-correction",
            "title": "Виправте помилку",
            "level": "b1",
            "payload": {
                "type": "error-correction",
                "instruction": "Виправте помилку в кожному реченні.",
                "items": [sentence],
            },
            "answer_key": {"items": ["Я живу в Києві."]},
            "provenance": {
                "source": "generated",
                "generator": "fixture",
                "gates": ["vesum"],
            },
        }
        template["blocks"][0] = {
            "id": "block-error-correction",
            "phase": 1,
            "type": "error-correction",
            "mode": "письмово",
            "activity": activity,
            "answer_key": {
                "items": ["Я живу в Києві.", "Я живу в Києві."],
                "corrections": [correction, copy.deepcopy(correction)],
            },
            "mark": "ok",
            "note": None,
            "edited": False,
            "provenance": {
                "source": "generated",
                "generator": "fixture",
                "gates": ["vesum"],
                "external_options": False,
            },
            "quality": "engine_ok",
            "flag_reason_uk": None,
            "flagged_content_hash": None,
            "engine_reason_class": None,
        }
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


# Floor-specific bakers exercise typed cause classification from the engine
# boundary through the durable runner projection.
class ThinSourceFloorBaker:
    """Raises FloorUnmetError with blames_source=True (thin source)."""

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        del anchor, duration, focus
        raise FloorUnmetError("private source diagnostic must not surface", blames_source=True)


class SufficientAnchorFloorBaker:
    """Raises FloorUnmetError with blames_source=False (sufficient anchor)."""

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        del anchor, duration, focus
        raise FloorUnmetError(
            "Bake failed: the lesson could not reach the minimum activity density.",
            blames_source=False,
        )


class UnknownBakeErrorBaker:
    """Regression baker: plain BakeError with unknown text must classify as engine_unavailable."""

    def bake(
        self, anchor: str | dict[str, Any], duration: int, focus: str | None
    ) -> dict[str, Any]:
        del anchor, duration, focus
        raise BakeError("some future unknown text")


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
        json={"token": token, "nonce": _canonical_token()},
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


def _keys_recursively(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys_recursively(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys_recursively(item) for item in value))
    return set()


def _valid_teacher_progress() -> dict[str, object]:
    return {
        "phase": 2,
        "phases_total": 3,
        "step": "generation",
        "calls_done": 4,
        "calls_planned": 6,
        "updated_at": "2026-07-19T00:00:00Z",
    }


def _create_progress_job(app, client):
    # These status-projection cases own durable telemetry directly; stop the
    # asynchronous baker so their intentionally draft aggregate cannot advance.
    app.state.runner.stop()
    teacher, _, token = _issue_invite(app)
    lesson_id = str(uuid.uuid4())
    job, created = app.state.store.create_or_get(
        teacher.id,
        lesson_id,
        anchor_text="Учні читають український текст.",
        duration=45,
        focus=None,
    )
    assert created
    _redeem(client, token)
    return teacher, lesson_id, job


def _write_raw_progress(app, lesson_id: str, progress: object) -> None:
    with sqlite3.connect(app.state.store.database_path) as connection:
        connection.execute(
            "UPDATE lesson_jobs SET progress_json = ? WHERE id = ?",
            (json.dumps(progress), lesson_id),
        )


def _teacher_progress_openapi_contract() -> tuple[set[str], set[str], str]:
    source = (Path(__file__).parents[1] / "hramatka/api/openapi.yaml").read_text(encoding="utf-8")
    schema_block = source.split("    TeacherSafeProgress:\n", maxsplit=1)[1].split(
        "    LessonCatalogItem:\n", maxsplit=1
    )[0]
    required_block = schema_block.split("      required:\n", maxsplit=1)[1].split(
        "      description:\n", maxsplit=1
    )[0]
    required = {
        line.removeprefix("        - ")
        for line in required_block.splitlines()
        if line.startswith("        - ")
    }
    properties_block = schema_block.split("      properties:\n", maxsplit=1)[1]
    properties = {
        line.strip().removesuffix(":")
        for line in properties_block.splitlines()
        if line.startswith("        ")
        and not line.startswith("          ")
        and line.rstrip().endswith(":")
    }
    return required, properties, schema_block


def test_status_projects_only_teacher_safe_durable_progress(app, client) -> None:
    teacher, lesson_id, job = _create_progress_job(app, client)
    rich_progress = {
        **_valid_teacher_progress(),
        "latency_watchdogs": [
            {
                "host": "provider.internal.example",
                "route": "primary-to-fallback",
                "repair": {"raw_response": "private diagnostic"},
            }
        ],
        "model_route": {"provider": "internal-provider", "model": "internal-model"},
        "failure_detail": {"errors": ["raw failure detail"]},
        "future_internal_key": {"reflection": {"nested": "must not echo"}},
    }
    assert app.state.store.update_progress(
        teacher.id,
        job.id,
        rich_progress,
        attempt=job.attempt,
        attempt_token=job.attempt_token,
    )

    response = client.get(f"/api/lessons/{lesson_id}/status")
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["progress"] == _valid_teacher_progress()
    returned_keys = _keys_recursively(status["progress"])
    assert returned_keys == _TEACHER_PROGRESS_FIELDS
    assert not any(
        key in returned_keys
        for key in {
            "host",
            "route",
            "provider",
            "model",
            "latency_watchdogs",
            "repair",
            "raw_response",
            "failure_detail",
            "errors",
            "future_internal_key",
            "reflection",
            "nested",
        }
    )
    stored = app.state.store.get(teacher.id, lesson_id)
    assert stored is not None
    assert stored.progress == rich_progress


def test_status_omits_mixed_validity_progress_instead_of_partial_subset(app, client) -> None:
    teacher, lesson_id, job = _create_progress_job(app, client)
    mixed_progress = {
        **_valid_teacher_progress(),
        "step": {"raw": "generation"},
        "calls_done": True,
        "future_internal_key": {"provider_host": "private.example"},
    }
    assert app.state.store.update_progress(
        teacher.id,
        job.id,
        mixed_progress,
        attempt=job.attempt,
        attempt_token=job.attempt_token,
    )

    malformed = client.get(f"/api/lessons/{lesson_id}/status")
    assert malformed.status_code == 200, malformed.text
    assert "progress" not in malformed.json()


@pytest.mark.parametrize("missing_field", sorted(_TEACHER_PROGRESS_FIELDS))
def test_status_omits_progress_when_any_safe_field_is_missing(
    app, client, missing_field: str
) -> None:
    _, lesson_id, _ = _create_progress_job(app, client)
    progress = _valid_teacher_progress()
    del progress[missing_field]
    _write_raw_progress(app, lesson_id, progress)

    response = client.get(f"/api/lessons/{lesson_id}/status")
    assert response.status_code == 200, response.text
    assert "progress" not in response.json()


@pytest.mark.parametrize("integer_field", ["phase", "phases_total", "calls_done", "calls_planned"])
def test_status_rejects_boolean_progress_integers(app, client, integer_field: str) -> None:
    teacher, lesson_id, job = _create_progress_job(app, client)
    progress = {**_valid_teacher_progress(), integer_field: True}
    assert app.state.store.update_progress(
        teacher.id,
        job.id,
        progress,
        attempt=job.attempt,
        attempt_token=job.attempt_token,
    )

    response = client.get(f"/api/lessons/{lesson_id}/status")
    assert response.status_code == 200, response.text
    assert "progress" not in response.json()


@pytest.mark.parametrize(
    "invalid_updated_at",
    ["not-a-time", "2026-07-19T00:00:00", True, {"nested": "private timestamp"}],
)
def test_status_omits_progress_with_invalid_updated_at(
    app, client, invalid_updated_at: object
) -> None:
    teacher, lesson_id, job = _create_progress_job(app, client)
    progress = {**_valid_teacher_progress(), "updated_at": invalid_updated_at}
    assert app.state.store.update_progress(
        teacher.id,
        job.id,
        progress,
        attempt=job.attempt,
        attempt_token=job.attempt_token,
    )

    response = client.get(f"/api/lessons/{lesson_id}/status")
    assert response.status_code == 200, response.text
    assert "progress" not in response.json()


def test_status_omits_none_and_non_object_durable_progress(app, client) -> None:
    _, lesson_id, _ = _create_progress_job(app, client)
    _write_raw_progress(app, lesson_id, ["legacy telemetry", {"host": "private.example"}])
    legacy_list = client.get(f"/api/lessons/{lesson_id}/status")
    assert legacy_list.status_code == 200, legacy_list.text
    assert "progress" not in legacy_list.json()

    with sqlite3.connect(app.state.store.database_path) as connection:
        connection.execute("UPDATE lesson_jobs SET progress_json = NULL WHERE id = ?", (lesson_id,))
    none_progress = client.get(f"/api/lessons/{lesson_id}/status")
    assert none_progress.status_code == 200, none_progress.text
    assert "progress" not in none_progress.json()


def test_teacher_progress_openapi_and_runtime_contracts_are_exactly_aligned(app, client) -> None:
    teacher, lesson_id, job = _create_progress_job(app, client)
    assert app.state.store.update_progress(
        teacher.id,
        job.id,
        _valid_teacher_progress(),
        attempt=job.attempt,
        attempt_token=job.attempt_token,
    )
    response = client.get(f"/api/lessons/{lesson_id}/status")
    assert response.status_code == 200, response.text

    runtime_fields = set(response.json()["progress"])
    schema_required, schema_properties, schema_block = _teacher_progress_openapi_contract()
    assert runtime_fields == schema_required == schema_properties == _TEACHER_PROGRESS_FIELDS
    assert "      additionalProperties: false" in schema_block


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


def _set_only_session(
    database_path: Path, column: str, value: str | None, *, session_id: str | None = None
) -> None:
    assert column in {"expires_at", "idle_expires_at", "revoked_at"}
    with sqlite3.connect(database_path) as connection:
        if session_id is None:
            updated = connection.execute(
                f"UPDATE pilot_sessions SET {column} = ?", (value,)
            ).rowcount
        else:
            updated = connection.execute(
                f"UPDATE pilot_sessions SET {column} = ? WHERE id = ?", (value, session_id)
            ).rowcount
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
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": used_token, "nonce": _canonical_token()},
    )
    expired_response = client.post(
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": expired_token, "nonce": _canonical_token()},
    )
    revoked_response = client.post(
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": revoked_token, "nonce": _canonical_token()},
    )
    unknown_response = client.post(
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": _canonical_token(), "nonce": _canonical_token()},
    )
    unavailable = _error(used_response, 410, "invite_unavailable")
    assert _error(expired_response, 410, "invite_unavailable") == unavailable
    assert _error(revoked_response, 410, "invite_unavailable") == unavailable
    assert _error(unknown_response, 410, "invite_unavailable") == unavailable

    padding_response = client.post(
        "/api/session/redeem",
        headers={"Origin": ORIGIN},
        json={"token": _noncanonical_padding_bits(_canonical_token()), "nonce": _canonical_token()},
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


def test_redeemed_session_survives_invite_expiry_and_has_idle_and_absolute_limits(
    app, client
) -> None:
    _, invite, token = _issue_invite(app)
    session = _redeem(client, token)
    _set_invite_expired(app.state.settings.database_path, invite.id)

    # Invite expiry applies only before the first committed exchange.
    assert client.get("/api/session").json()["teacher"] == session["teacher"]

    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    _set_only_session(app.state.settings.database_path, "idle_expires_at", expired)
    _error(client.get("/api/session"), 401, "session_required")


def test_lost_redeem_response_reissues_only_for_the_same_browser_nonce(app) -> None:
    _, _, token = _issue_invite(app)
    nonce = _canonical_token()
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as other:
        committed = first.post(
            "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": token, "nonce": nonce}
        )
        assert committed.status_code == 200, committed.text
        # Simulate the browser never receiving the committed Set-Cookie response.
        first.cookies.clear()

        retried = first.post(
            "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": token, "nonce": nonce}
        )
        assert retried.status_code == 200, retried.text
        assert first.get("/api/session").json()["teacher"] == retried.json()["teacher"]

        replay = other.post(
            "/api/session/redeem",
            headers={"Origin": ORIGIN},
            json={"token": token, "nonce": _canonical_token()},
        )
        _error(replay, 410, "invite_unavailable")


def test_same_nonce_reissue_renews_idle_even_after_the_original_idle_deadline(app) -> None:
    _, _, token = _issue_invite(app)
    nonce = _canonical_token()
    with TestClient(app, base_url=ORIGIN) as client:
        initial = client.post(
            "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": token, "nonce": nonce}
        )
        assert initial.status_code == 200, initial.text
        client.cookies.clear()

        near_boundary = datetime.now(UTC) + timedelta(minutes=1)
        _set_only_session(
            app.state.settings.database_path,
            "idle_expires_at",
            near_boundary.isoformat().replace("+00:00", "Z"),
        )
        renewed = client.post(
            "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": token, "nonce": nonce}
        )
        assert renewed.status_code == 200, renewed.text
        with sqlite3.connect(app.state.settings.database_path) as connection:
            idle_expires_at = connection.execute(
                "SELECT idle_expires_at FROM pilot_sessions"
            ).fetchone()[0]
        renewed_deadline = datetime.fromisoformat(idle_expires_at.replace("Z", "+00:00"))
        assert renewed_deadline > datetime.now(UTC) + timedelta(hours=23)
        assert client.get("/api/session").status_code == 200

        client.cookies.clear()
        _set_only_session(
            app.state.settings.database_path,
            "idle_expires_at",
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        )
        past_idle = client.post(
            "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": token, "nonce": nonce}
        )
        assert past_idle.status_code == 200, past_idle.text
        assert client.get("/api/session").status_code == 200


def test_same_nonce_reissue_rejects_null_idle_and_caps_cookie_to_absolute_lifetime(app) -> None:
    _, _, token = _issue_invite(app)
    nonce = _canonical_token()
    with TestClient(app, base_url=ORIGIN) as client:
        initial = client.post(
            "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": token, "nonce": nonce}
        )
        assert initial.status_code == 200, initial.text
        client.cookies.clear()
        _set_only_session(app.state.settings.database_path, "idle_expires_at", None)
        malformed = client.post(
            "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": token, "nonce": nonce}
        )
        _error(malformed, 410, "invite_unavailable")

    _, _, capped_token = _issue_invite(app)
    capped_nonce = _canonical_token()
    with TestClient(app, base_url=ORIGIN) as client:
        initial = client.post(
            "/api/session/redeem",
            headers={"Origin": ORIGIN},
            json={"token": capped_token, "nonce": capped_nonce},
        )
        assert initial.status_code == 200, initial.text
        client.cookies.clear()
        with sqlite3.connect(app.state.settings.database_path) as connection:
            capped_session_id = connection.execute(
                "SELECT id FROM pilot_sessions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        _set_only_session(
            app.state.settings.database_path,
            "expires_at",
            (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            session_id=capped_session_id,
        )
        reissued = client.post(
            "/api/session/redeem",
            headers={"Origin": ORIGIN},
            json={"token": capped_token, "nonce": capped_nonce},
        )
        assert reissued.status_code == 200, reissued.text
        max_age = int(re.search(r"Max-Age=(\d+)", reissued.headers["set-cookie"])[1])
        assert 0 < max_age <= 3600


def test_revoke_teacher_sessions_counts_only_active_sessions(app) -> None:
    teacher = app.state.store.create_teacher(display_name="Вчителька")
    sessions = []
    for _ in range(3):
        _, token = app.state.store.create_invite(teacher.id)
        sessions.append(app.state.store.redeem_invite(token, _canonical_token()).session)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(app.state.settings.database_path) as connection:
        connection.execute(
            "UPDATE pilot_sessions SET revoked_at = ? WHERE id = ?", (past, sessions[1].id)
        )
        connection.execute(
            "UPDATE pilot_sessions SET expires_at = ? WHERE id = ?", (past, sessions[2].id)
        )

    assert app.state.store.revoke_teacher_sessions(teacher.id) == 1
    assert app.state.store.revoke_teacher_sessions(teacher.id) == 0


def test_session_survives_app_restart_and_operator_revoke_all_blocks_next_request(
    app, client, monkeypatch, capsys
) -> None:
    teacher, _, token = _issue_invite(app)
    session = _redeem(client, token)
    cookie = client.cookies.get("__Host-hramatka_session")
    assert cookie is not None

    restarted = create_app(settings=app.state.settings, baker=FixtureBaker())
    with TestClient(restarted, base_url=ORIGIN) as after_restart:
        after_restart.cookies.set("__Host-hramatka_session", cookie)
        assert after_restart.get("/api/session").json()["teacher"] == session["teacher"]

        monkeypatch.setenv("HRAMATKA_DB_PATH", str(app.state.settings.database_path))
        from hramatka.api import sessions

        assert sessions.main(["revoke-all", "--teacher-id", teacher.id]) == 0
        assert capsys.readouterr().out == '{"revoked_sessions": 1}\n'
        _error(after_restart.get("/api/session"), 401, "session_required")


def test_redemption_persists_only_digests_and_never_logs_raw_credentials(
    app, client, caplog
) -> None:
    _, _, token = _issue_invite(app)
    nonce = _canonical_token()
    response = client.post(
        "/api/session/redeem", headers={"Origin": ORIGIN}, json={"token": token, "nonce": nonce}
    )
    assert response.status_code == 200, response.text
    cookie = client.cookies.get("__Host-hramatka_session")
    assert cookie is not None
    with sqlite3.connect(app.state.settings.database_path) as connection:
        durable = repr(connection.execute("SELECT * FROM pilot_invites").fetchall())
        durable += repr(connection.execute("SELECT * FROM pilot_sessions").fetchall())
    assert token not in durable
    assert nonce not in durable
    assert cookie not in durable
    assert token not in caplog.text
    assert nonce not in caplog.text
    assert cookie not in caplog.text


def test_origin_and_csrf_matrix_rejects_before_any_mutation(app, client) -> None:
    _, _, token = _issue_invite(app)
    for headers in (
        {},
        {"Origin": "null"},
        {"Origin": "http://pilot.example.test"},
        {"Origin": "https://evil.example"},
    ):
        response = client.post(
            "/api/session/redeem",
            headers=headers,
            json={"token": token, "nonce": _canonical_token()},
        )
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


def test_methodology_and_grammar_focus_are_strict_durable_and_owner_scoped(app) -> None:
    """The catalog must never disclose even the short anchor preview cross-teacher."""
    _, _, first_token = _issue_invite(app, display_name="Перша")
    _, _, second_token = _issue_invite(app, display_name="Друга")
    lesson_id = str(uuid.uuid4())
    with TestClient(app, base_url=ORIGIN) as first, TestClient(app, base_url=ORIGIN) as second:
        first_session = _redeem(first, first_token)
        _redeem(second, second_token)
        request = _lesson_request(lesson_id)
        request.update(
            {
                "methodology": "ttt",
                "grammar_focus": "  вищий ступінь прикметників  ",
            }
        )
        created = first.post(
            "/api/lessons",
            headers=_mutation_headers(first_session["csrf_token"]),
            json=request,
        )
        assert created.status_code == 202, created.text
        _wait_for_status(first, lesson_id, "ready")

        catalog = first.get("/api/lessons").json()["lessons"]
        assert len(catalog) == 1
        assert catalog[0]["methodology"] == "ttt"
        assert catalog[0]["grammar_focus"] == "вищий ступінь прикметників"
        assert catalog[0]["level"] == "B1"
        assert catalog[0]["anchor_snippet"] == request["anchor"]["text"]
        stored_job = app.state.store.get(first_session["teacher"]["id"], lesson_id)
        assert stored_job is not None
        assert stored_job.request["methodology"] == "ttt"
        assert stored_job.request["grammar_focus"] == "вищий ступінь прикметників"
        assert first.get(f"/api/lessons/{lesson_id}").json()["grammar_focus"] == (
            "вищий ступінь прикметників"
        )
        assert second.get("/api/lessons").json() == {"lessons": []}

        unknown_methodology = {**request, "id": str(uuid.uuid4()), "methodology": "ppp"}
        _error(
            first.post(
                "/api/lessons",
                headers=_mutation_headers(first_session["csrf_token"]),
                json=unknown_methodology,
            ),
            422,
            "invalid_input",
        )
        multiline_focus = {
            **request,
            "id": str(uuid.uuid4()),
            "grammar_focus": "прикметники\nприслівники",
        }
        _error(
            first.post(
                "/api/lessons",
                headers=_mutation_headers(first_session["csrf_token"]),
                json=multiline_focus,
            ),
            422,
            "invalid_input",
        )
        too_long_focus = {**request, "id": str(uuid.uuid4()), "grammar_focus": "а" * 121}
        _error(
            first.post(
                "/api/lessons",
                headers=_mutation_headers(first_session["csrf_token"]),
                json=too_long_focus,
            ),
            422,
            "invalid_input",
        )


def test_same_owner_idempotency_reuses_without_second_bake_and_rejects_unqualified_duration(
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
            rejected = client.post(
                "/api/lessons", headers=_mutation_headers(session["csrf_token"]), json=changed
            )
            _error(rejected, 422, "invalid_input")
            assert "45" in rejected.json()["message"]
    finally:
        baker.release.set()


def _inject_stale_ok_external_options_lesson(
    database_path: Path, lesson_id: str, *, stale_block_id: str, plain_ok_block_id: str
) -> None:
    """Bypass schema bake validation to plant latent mark:ok + external_options:true.

    New bakes couple external_options→mark:warn (schema allOf), so the latent
    asymmetry only appears via malformed/stale stored lesson JSON — same class
    as #128. Tests inject that shape after a successful fixture bake.
    """
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT lesson_json FROM lesson_jobs WHERE id = ?", (lesson_id,)
        ).fetchone()
        assert row is not None and row[0], "ready lesson_json must exist before injection"
        lesson = json.loads(row[0])
        for block in lesson["blocks"]:
            block_id = block["id"]
            if block_id == stale_block_id:
                block["mark"] = "ok"
                block.setdefault("provenance", {})
                block["provenance"]["external_options"] = True
            elif block_id == plain_ok_block_id:
                block["mark"] = "ok"
                block.setdefault("provenance", {})
                block["provenance"]["external_options"] = False
            else:
                # Keep the rest out of the needs-review set so the stale block is
                # the sole required ack (isolates the asymmetry under test).
                block["mark"] = "ok"
                block.setdefault("provenance", {})
                block["provenance"]["external_options"] = False
        connection.execute(
            """
            UPDATE lesson_jobs
            SET lesson_json = ?, warning_acknowledgements_json = '[]'
            WHERE id = ?
            """,
            (
                json.dumps(lesson, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                lesson_id,
            ),
        )
        connection.commit()


def test_stale_ok_external_options_is_ackable_and_blocks_accept(app, client) -> None:
    """#179: server warn-set must match UI blockNeedsReview for latent stale data.

    mark:ok + provenance.external_options:true is schema-invalid for new bakes
    but can exist in stored lessons. UI shows an ack chip; server must accept
    that ack and require it before lesson accept. warn never auto-accepts.
    """
    _, _, token = _issue_invite(app)
    session = _redeem(client, token)
    headers = _mutation_headers(session["csrf_token"])
    lesson_id = str(uuid.uuid4())
    created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
    assert created.status_code == 202, created.text
    _wait_for_status(client, lesson_id, "ready")

    stale_id = "block-1"
    plain_ok_id = "block-2"
    _inject_stale_ok_external_options_lesson(
        app.state.settings.database_path,
        lesson_id,
        stale_block_id=stale_id,
        plain_ok_block_id=plain_ok_id,
    )

    resource = client.get(f"/api/lessons/{lesson_id}")
    assert resource.status_code == 200, resource.text
    body = resource.json()
    revision = body["revision"]
    blocks_by_id = {block["id"]: block for block in body["lesson"]["blocks"]}
    assert blocks_by_id[stale_id]["mark"] == "ok"
    assert blocks_by_id[stale_id]["provenance"]["external_options"] is True
    assert blocks_by_id[plain_ok_id]["mark"] == "ok"
    assert blocks_by_id[plain_ok_id]["provenance"]["external_options"] is False

    # (b) acceptance requires the stale external_options block's ack
    # (sole needs-review block after injection — accept must refuse until acked)
    blocked = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    _error(blocked, 409, "warning_acknowledgements_required", lesson_id=lesson_id)
    assert client.get(f"/api/lessons/{lesson_id}").json()["revision"] == revision

    # (a) acknowledge_warning ACCEPTS an ack for the stale block
    ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/{stale_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    assert ack.status_code == 200, ack.text
    ack_body = ack.json()
    assert stale_id in ack_body["warning_acknowledgements"]
    acknowledged_revision = ack_body["revision"]
    assert acknowledged_revision == revision + 1

    # Regression: plain mark:ok (no external_options) cannot be acked
    plain_ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/{plain_ok_id}/accept",
        headers=headers,
        json={"expected_revision": acknowledged_revision},
    )
    _error(plain_ack, 404, "warning_block_not_found")

    # (c) with the ack present, accept succeeds (plain ok needs no ack)
    accepted = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": acknowledged_revision},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["accepted_at"] is not None
    assert accepted.json()["lesson"]["accepted"] is True


def test_plain_ok_blocks_need_no_ack_and_cannot_be_acked(app, client) -> None:
    """Regression: mark:ok without external_options is outside the needs-review set."""
    _, _, token = _issue_invite(app)
    session = _redeem(client, token)
    headers = _mutation_headers(session["csrf_token"])
    lesson_id = str(uuid.uuid4())
    created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
    assert created.status_code == 202, created.text
    _wait_for_status(client, lesson_id, "ready")

    # Force every visible block to plain ok (no external_options honesty flag).
    with sqlite3.connect(app.state.settings.database_path) as connection:
        row = connection.execute(
            "SELECT lesson_json FROM lesson_jobs WHERE id = ?", (lesson_id,)
        ).fetchone()
        lesson = json.loads(row[0])
        for block in lesson["blocks"]:
            block["mark"] = "ok"
            block.setdefault("provenance", {})
            block["provenance"]["external_options"] = False
        connection.execute(
            """
            UPDATE lesson_jobs
            SET lesson_json = ?, warning_acknowledgements_json = '[]'
            WHERE id = ?
            """,
            (
                json.dumps(lesson, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                lesson_id,
            ),
        )
        connection.commit()

    resource = client.get(f"/api/lessons/{lesson_id}")
    assert resource.status_code == 200, resource.text
    revision = resource.json()["revision"]
    plain_id = resource.json()["lesson"]["blocks"][0]["id"]

    cannot_ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/{plain_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    _error(cannot_ack, 404, "warning_block_not_found")

    accepted = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["lesson"]["accepted"] is True


def _store_focus_status(database_path: Path, lesson_id: str, focus_status: dict | None) -> None:
    """Give a ready lesson a focus outcome, and nothing else that needs review.

    The FixtureBaker bakes without a focus, so the carrier is written straight
    onto the stored document (unlike the stale-external_options injection above,
    this is an ordinary schema-legal shape). Every block is flattened to plain
    ok so the focus caveat is the sole required ack.
    """
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT lesson_json FROM lesson_jobs WHERE id = ?", (lesson_id,)
        ).fetchone()
        assert row is not None and row[0], "ready lesson_json must exist before injection"
        lesson = json.loads(row[0])
        for block in lesson["blocks"]:
            block["mark"] = "ok"
            block.setdefault("provenance", {})
            block["provenance"]["external_options"] = False
        if focus_status is None:
            lesson.pop("focus_status", None)
        else:
            lesson["focus_status"] = focus_status
        connection.execute(
            """
            UPDATE lesson_jobs
            SET lesson_json = ?, warning_acknowledgements_json = '[]'
            WHERE id = ?
            """,
            (
                json.dumps(lesson, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                lesson_id,
            ),
        )
        connection.commit()


_UNSUPPORTED_FOCUS_STATUS = {
    "requested": "умовний спосіб",
    "supported": False,
    "notice_uk": "Опора не містить достатньо перевіреного матеріалу для фокусу «умовний спосіб».",
}


def _ready_lesson_with_focus_status(app, client, focus_status: dict | None) -> tuple[str, dict]:
    _, _, token = _issue_invite(app)
    session = _redeem(client, token)
    headers = _mutation_headers(session["csrf_token"])
    lesson_id = str(uuid.uuid4())
    created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
    assert created.status_code == 202, created.text
    _wait_for_status(client, lesson_id, "ready")
    _store_focus_status(app.state.settings.database_path, lesson_id, focus_status)
    return lesson_id, headers


def test_unsupported_focus_status_is_ackable_and_blocks_accept(app, client) -> None:
    """#191: an unsupported focus is a caveat the teacher must accept explicitly.

    It rides the same needs-review machinery as #179/#192 under the reserved
    lesson-level id, so warn-never-auto-accepts holds for the focus notice too.
    """
    lesson_id, headers = _ready_lesson_with_focus_status(app, client, _UNSUPPORTED_FOCUS_STATUS)

    resource = client.get(f"/api/lessons/{lesson_id}")
    assert resource.status_code == 200, resource.text
    body = resource.json()
    revision = body["revision"]
    # The notice reaches the client on the document itself, not via a tray entry.
    assert body["lesson"]["focus_status"] == _UNSUPPORTED_FOCUS_STATUS
    assert all(block["mark"] == "ok" for block in body["lesson"]["blocks"])

    # (b) acceptance refuses while the sole caveat is unacknowledged
    blocked = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    _error(blocked, 409, "warning_acknowledgements_required", lesson_id=lesson_id)
    assert client.get(f"/api/lessons/{lesson_id}").json()["revision"] == revision

    # (a) the reserved id is ackable through the ordinary block-ack endpoint
    ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/focus-status/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    assert ack.status_code == 200, ack.text
    ack_body = ack.json()
    assert "focus-status" in ack_body["warning_acknowledgements"]
    acknowledged_revision = ack_body["revision"]
    assert acknowledged_revision == revision + 1

    # (c) with the ack present, accept succeeds
    accepted = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": acknowledged_revision},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["lesson"]["accepted"] is True


def test_supported_focus_status_needs_no_ack_and_cannot_be_acked(app, client) -> None:
    """A focus the anchor backs is not a caveat: nothing to acknowledge."""
    supported = {"requested": "читання", "supported": True, "notice_uk": None}
    lesson_id, headers = _ready_lesson_with_focus_status(app, client, supported)

    revision = client.get(f"/api/lessons/{lesson_id}").json()["revision"]
    cannot_ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/focus-status/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    _error(cannot_ack, 404, "warning_block_not_found")

    accepted = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["lesson"]["accepted"] is True


def test_lesson_without_focus_status_is_unaffected(app, client) -> None:
    """Regression: plain lessons neither gain a required ack nor a fake block."""
    lesson_id, headers = _ready_lesson_with_focus_status(app, client, None)

    body = client.get(f"/api/lessons/{lesson_id}").json()
    revision = body["revision"]
    assert "focus_status" not in body["lesson"]

    cannot_ack = client.post(
        f"/api/lessons/{lesson_id}/blocks/focus-status/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    _error(cannot_ack, 404, "warning_block_not_found")

    accepted = client.post(
        f"/api/lessons/{lesson_id}/accept",
        headers=headers,
        json={"expected_revision": revision},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["lesson"]["accepted"] is True


def test_focus_status_needs_review_predicate() -> None:
    """Unit lock: only a strictly-false `supported` is a caveat."""
    from hramatka.api.store import focus_status_needs_review

    assert focus_status_needs_review({"focus_status": {"supported": False}}) is True
    assert focus_status_needs_review({"focus_status": {"supported": True}}) is False
    assert focus_status_needs_review({}) is False
    assert focus_status_needs_review({"focus_status": None}) is False
    # Truthiness must not decide a gate: only the real boolean counts.
    assert focus_status_needs_review({"focus_status": {"supported": "false"}}) is False
    assert focus_status_needs_review({"focus_status": {"supported": 0}}) is False


def test_reserved_focus_status_id_cannot_collide_with_a_real_block_id(app, client) -> None:
    """The reserved id lives outside every generated block-id namespace."""
    from hramatka.api.lesson import restore_rejected_entry
    from hramatka.api.store import FOCUS_STATUS_ACK_ID

    _, _, token = _issue_invite(app)
    session = _redeem(client, token)
    headers = _mutation_headers(session["csrf_token"])
    lesson_id = str(uuid.uuid4())
    created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
    assert created.status_code == 202, created.text
    _wait_for_status(client, lesson_id, "ready")

    lesson = client.get(f"/api/lessons/{lesson_id}").json()["lesson"]
    real_ids = {block["id"] for block in lesson["blocks"]}
    assert real_ids, "the fixture bake must produce blocks to compare against"
    assert FOCUS_STATUS_ACK_ID not in real_ids
    # Composer ids are `block-<n>`; a teacher restore mints `restored-<uuid4hex>`.
    assert all(block_id.startswith(("block-", "restored-")) for block_id in real_ids)

    restorable = copy.deepcopy(lesson)
    restorable["rejected"] = [
        entry
        for entry in restorable["rejected"]
        if not str(entry.get("reason", "")).startswith(("shortfall-notice:", "focus-notice:"))
    ]
    if restorable["rejected"]:
        restored_id = restore_rejected_entry(restorable, 0, 1)
        assert restored_id != FOCUS_STATUS_ACK_ID
        assert restored_id.startswith("restored-")


def test_block_needs_review_predicate_matches_ui() -> None:
    """Unit lock: server predicate mirrors review-helpers.ts blockNeedsReview."""
    from hramatka.api.store import block_needs_review

    assert block_needs_review({"mark": "warn", "provenance": {"external_options": False}}) is True
    assert block_needs_review({"mark": "warn", "provenance": {"external_options": True}}) is True
    assert block_needs_review({"mark": "ok", "provenance": {"external_options": True}}) is True
    assert block_needs_review({"mark": "ok", "provenance": {"external_options": False}}) is False
    assert block_needs_review({"mark": "ok", "provenance": {}}) is False
    assert block_needs_review({"mark": "ok"}) is False
    assert block_needs_review({"mark": "ok", "provenance": {"external_options": "true"}}) is False
    assert block_needs_review({"mark": "ok", "provenance": None}) is False


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

        retired_duration_control = second.post(
            f"/api/lessons/{lesson_id}/duration",
            headers=_mutation_headers(second_session["csrf_token"]),
            json={"expected_revision": revision, "duration": 60},
        )
        assert retired_duration_control.status_code == 404

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


def test_error_correction_key_survives_api_edit_remove_restore_round_trip(
    tmp_path: Path,
) -> None:
    app = create_app(settings=_settings(tmp_path), baker=ErrorCorrectionFixtureBaker())
    with TestClient(app, base_url=ORIGIN) as client:
        _, _, token = _issue_invite(app)
        session = _redeem(client, token)
        headers = _mutation_headers(session["csrf_token"])
        lesson_id = str(uuid.uuid4())
        created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
        assert created.status_code == 202, created.text
        _wait_for_status(client, lesson_id, "ready")
        resource = client.get(f"/api/lessons/{lesson_id}").json()
        block = next(
            item for item in resource["lesson"]["blocks"] if item["id"] == "block-error-correction"
        )
        corrections = copy.deepcopy(block["answer_key"]["corrections"])
        assert len(corrections) == 2
        assert corrections[0] == corrections[1]
        assert "corrections" not in block["activity"]["answer_key"]

        replacement = copy.deepcopy(block["activity"])
        replacement["title"] = "Оновлена вправа"
        edited = client.put(
            f"/api/lessons/{lesson_id}/blocks/block-error-correction/activity",
            headers=headers,
            json={"expected_revision": resource["revision"], "activity": replacement},
        )
        assert edited.status_code == 200, edited.text
        edited_resource = edited.json()
        edited_block = next(
            item
            for item in edited_resource["lesson"]["blocks"]
            if item["id"] == "block-error-correction"
        )
        assert edited_block["answer_key"] == {
            "items": replacement["answer_key"]["items"],
            "corrections": corrections,
        }
        assert "corrections" not in edited_block["activity"]["answer_key"]

        removed = client.post(
            f"/api/lessons/{lesson_id}/blocks/block-error-correction/remove",
            headers=headers,
            json={"expected_revision": edited_resource["revision"]},
        )
        assert removed.status_code == 200, removed.text
        removed_resource = removed.json()
        rejected = removed_resource["lesson"]["rejected"][-1]
        assert rejected["answer_key"] == edited_block["answer_key"]
        assert "corrections" not in rejected["activity"]["answer_key"]

        restored = client.post(
            f"/api/lessons/{lesson_id}/rejected/0/restore",
            headers=headers,
            json={"expected_revision": removed_resource["revision"], "phase": 2},
        )
        assert restored.status_code == 200, restored.text
        restored_block = next(
            item
            for item in restored.json()["lesson"]["blocks"]
            if item["id"].startswith("restored-")
        )
        assert restored_block["answer_key"] == edited_block["answer_key"]
        assert "corrections" not in restored_block["activity"]["answer_key"]


def test_reserve_include_and_activity_replacement_preserve_review_safety(
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

        retired_duration_control = client.post(
            f"/api/lessons/{lesson_id}/duration",
            headers=headers,
            json={"expected_revision": initial["revision"], "duration": 90},
        )
        assert retired_duration_control.status_code == 404

        included = client.post(
            f"/api/lessons/{lesson_id}/blocks/block-reserve/include",
            headers=headers,
            json={"expected_revision": initial["revision"]},
        )
        assert included.status_code == 200, included.text
        included_resource = included.json()
        assert included_resource["revision"] == initial["revision"] + 1
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
            "lesson_floor_unmet",
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


def test_floor_bakeerror_on_insufficient_anchor_uses_source_capacity_code_and_exact_message(
    tmp_path: Path,
) -> None:
    """Source capacity receives its own safe durable code, never the generic floor."""
    baker = ThinSourceFloorBaker()
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

    assert failed["failure_code"] == "insufficient_anchor_capacity"
    assert failed["failure_message"] == (
        "Опорного матеріалу недостатньо для повного уроку. "
        "Спробуйте довший і різноманітніший текст із конкретними деталями."
    )
    assert "private source diagnostic" not in (failed["failure_message"] or "")
    # Must not fall back to the generic safe message.
    assert failed["failure_message"] != "Не вдалося скласти урок. Спробуйте, будь ласка, ще раз."


def test_v3_preflight_rejection_never_projects_generation_progress_or_calls(
    tmp_path: Path,
) -> None:
    """A valid but too-thin source fails before either provider or generation chrome.

    The status assertion deliberately catches both ways the regression can
    return: restoring the eager initial progress write, or recording a
    preflight trace through a persistence path that also writes its provisional
    ``generation`` snapshot.
    """
    provider_calls = 0

    def should_not_run(_prompt: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("v3 preflight must refuse before a provider call")

    baker = EngineLessonBakerV3(
        generator=should_not_run,
        bundle=qualification_harness._qualification_fixture_bundle(tmp_path / "data"),
    )
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

    assert provider_calls == 0
    assert failed["failure_code"] == "insufficient_anchor_capacity"
    # Absent is the only honest public projection before allocation: every
    # progress shape currently includes a generation phase and a call plan.
    assert "progress" not in failed


def test_floor_bakeerror_after_generation_uses_retry_compatible_floor_code_and_message(
    tmp_path: Path,
) -> None:
    """Post-generation floor remains non-blaming and retry-compatible."""
    baker = SufficientAnchorFloorBaker()
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

    assert failed["failure_code"] == "lesson_floor_unmet"
    assert failed["failure_message"] == FLOOR_SHORTFALL_UA_MESSAGE
    # Explicitly not the thin blame text.
    assert "З цього тексту не вдалося" not in (failed["failure_message"] or "")


def test_plain_bakeerror_unknown_text_classifies_as_engine_unavailable(
    tmp_path: Path,
) -> None:
    """Regression: unknown/future BakeError string still falls to engine_unavailable.

    Proves the classification no longer relies on (or leaks) specific floor strings.
    """
    baker = UnknownBakeErrorBaker()
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

    assert failed["failure_code"] == "engine_unavailable"
    assert failed["failure_message"] == "Не вдалося скласти урок. Спробуйте, будь ласка, ще раз."


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


def test_teacher_preferences_are_fixed_to_the_qualified_45_minute_default(app, client) -> None:
    teacher, _invite, token = _issue_invite(app)
    sess = _redeem(client, token)
    csrf = sess["csrf_token"]
    # initial GET yields default
    r = client.get("/api/teacher/preferences", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.json() == {"default_duration": 45}
    # 60/90 cannot be persisted back into the new-lesson form.
    r = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(csrf),
        json={"default_duration": 90},
    )
    _error(r, 422, "invalid_input")
    r = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(csrf),
        json={"default_duration": 45},
    )
    assert r.status_code == 200
    r = client.get("/api/teacher/preferences", headers={"Origin": ORIGIN})
    assert r.json()["default_duration"] == 45


@pytest.mark.parametrize("duration", [60, 90])
def test_new_lesson_rejects_an_unqualified_duration_with_scope_guidance(
    app, client, duration: int
) -> None:
    _teacher, _invite, token = _issue_invite(app)
    session = _redeem(client, token)
    response = client.post(
        "/api/lessons",
        headers=_mutation_headers(session["csrf_token"]),
        json=_lesson_request(str(uuid.uuid4()), duration=duration),
    )
    _error(response, 422, "invalid_input")
    assert "45" in response.json()["message"]
    assert "вчителями" in response.json()["message"]


def test_teacher_preferences_ownership_scoped(app, client) -> None:
    """Cross-teacher cannot read/write other's pref (owner-scoped like lessons)."""
    t1, _i1, tok1 = _issue_invite(app, display_name="T1")
    t2, _i2, tok2 = _issue_invite(app, display_name="T2")
    s1 = _redeem(client, tok1)
    # An old direct database value is normalized before it can reach the UI.
    app.state.store.set_teacher_default_duration(t1.id, 45)
    with app.state.store._write_transaction() as connection:
        connection.execute(
            "UPDATE teacher_preferences SET default_duration = 90 WHERE teacher_id = ?", (t1.id,)
        )
    assert app.state.store.get_teacher_default_duration(t1.id) == 45
    # t2 remains on the same qualified default.
    assert app.state.store.get_teacher_default_duration(t2.id) == 45
    # PUT for 45 stays owner-scoped.
    r_put = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(s1["csrf_token"]),
        json={"default_duration": 45},
    )
    assert r_put.status_code == 200, r_put.text
    assert app.state.store.get_teacher_default_duration(t1.id) == 45
    # t2 PUT does not affect t1.
    s2 = _redeem(client, tok2)
    client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(s2["csrf_token"]),
        json={"default_duration": 45},
    )
    assert app.state.store.get_teacher_default_duration(t1.id) == 45
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
    # A valid origin + CSRF reaches the input validator; an unqualified duration is rejected.
    r = client.put(
        "/api/teacher/preferences",
        headers=_mutation_headers(csrf),
        json={"default_duration": 60},
    )
    _error(r, 422, "invalid_input")


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
        # After v004 the final version is higher; assert against the live constant
        # (the test exercises additive migration on a v2-populated DB and data survival).
        assert current_schema_version(conn) == EXPECTED_SCHEMA_VERSION
        # data preserved
        trow = conn.execute("SELECT * FROM pilot_teachers WHERE id='t-pop-1'").fetchone()
        assert trow["display_name"] == "Популяційна"
        lrow = conn.execute("SELECT * FROM lesson_jobs WHERE id='l-1'").fetchone()
        assert lrow is not None
        # prefs table exists (from v3), no row yet -> get yields 60
        prow = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='teacher_preferences'"
        ).fetchone()
        assert prow is not None
        # simulate store usage: direct select default
        d = conn.execute(
            "SELECT default_duration FROM teacher_preferences WHERE teacher_id='t-pop-1'"
        ).fetchone()
        assert d is None  # absent row
    finally:
        conn.close()
    # Now via store: init will have migrated already, but re-open confirms
    from hramatka.api.store import JobStore

    store = JobStore(db_path)
    store.initialize()
    assert store.get_teacher_default_duration("t-pop-1") == 45
    store.set_teacher_default_duration("t-pop-1", 45)
    assert store.get_teacher_default_duration("t-pop-1") == 45


def test_migration_v005_extends_failure_code_check_and_preserves_data(tmp_path: Path) -> None:
    """v5 migration preserves v3 data and accepts the new generation codes."""
    from hramatka.api.migrations import (
        EXPECTED_SCHEMA_VERSION,
        apply_migrations,
        current_schema_version,
    )

    db_path = tmp_path / "populated-v3.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Simulate a v3-populated DB (apply up to v3, insert teacher + lesson)
        # Manual minimal schema up to v3 (no v4 CHECK yet).
        conn.execute("BEGIN IMMEDIATE")
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
        conn.execute(
            """
            CREATE TABLE teacher_preferences (
                teacher_id TEXT PRIMARY KEY REFERENCES pilot_teachers(id),
                default_duration INTEGER NOT NULL DEFAULT 60
                    CHECK (default_duration IN (45, 60, 90)),
                updated_at TEXT NOT NULL
            )
            """
        )
        ts = "2026-07-14T00:00:00Z"
        conn.execute(
            "INSERT INTO pilot_teachers "
            "(id, display_name, created_at, deactivated_at) VALUES (?,?,?,NULL)",
            ("t-pop-v4", "Міграція-v4", ts),
        )
        conn.execute(
            "INSERT INTO lesson_jobs (teacher_id, id, request_json, request_hash, "
            "status, step, warning_acknowledgements_json, accepted, revision, "
            "created_at, updated_at) VALUES (?,?,?,?, 'draft','текст отримано','[]',0,1,?,?)",
            (
                "t-pop-v4",
                "l-v4",
                '{"anchor":{"text":"y","source":"teacher-paste"},"level":"B1","duration":45,"focus":null}',
                b"hashv4",
                ts,
                ts,
            ),
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1,'pilot_schema',?), (2,'add_progress_column',?), "
            "(3,'add_teacher_preferences',?)",
            (ts, ts, ts),
        )
        conn.commit()
        # Apply every later migration, including v4 and the new v5 CHECK.
        apply_migrations(conn)
        assert current_schema_version(conn) == EXPECTED_SCHEMA_VERSION
        # data preserved
        lrow = conn.execute("SELECT * FROM lesson_jobs WHERE id='l-v4'").fetchone()
        assert lrow is not None
        assert lrow["teacher_id"] == "t-pop-v4"
        # Every additive code is accepted by the final CHECK.
        conn.execute(
            """
            UPDATE lesson_jobs
            SET status='failed', step='готово', failure_code='lesson_floor_unmet',
                failure_message=?, updated_at=?
            WHERE id='l-v4'
            """,
            ("З цього тексту не вдалося...", ts),
        )
        conn.commit()
        updated = conn.execute("SELECT failure_code FROM lesson_jobs WHERE id='l-v4'").fetchone()
        assert updated["failure_code"] == "lesson_floor_unmet"
        for failure_code in (
            "generation_failed",
            "no_eligible_activities",
            "insufficient_anchor_capacity",
        ):
            conn.execute("UPDATE lesson_jobs SET failure_code=? WHERE id='l-v4'", (failure_code,))
            conn.commit()
            updated = conn.execute(
                "SELECT failure_code FROM lesson_jobs WHERE id='l-v4'"
            ).fetchone()
            assert updated["failure_code"] == failure_code
    finally:
        conn.close()


def test_recreate_from_legacy_model_less_failed_job_is_rejected(app) -> None:
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
        _error(response, 409, "model_unavailable")
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


def test_recreate_store_invalid_request_returns_422(tmp_path: Path) -> None:
    """A stored request_json that fails store-level validation (e.g. non-B1 level)
    must produce a 422 envelope, NOT an unhandled 500."""
    app = create_app(settings=_settings(tmp_path), baker=FixtureBaker())
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
        # Seed a request_json with a level the store rejects.
        bad_request = json.dumps(
            {
                "anchor": {
                    "text": "Учні читають текст.",
                    "source": "teacher-paste",
                },
                "level": "A2",
                "duration": 45,
                "focus": None,
            }
        )
        with sqlite3.connect(app.state.store.database_path) as connection:
            connection.execute(
                "UPDATE lesson_jobs SET request_json = ? WHERE id = ?",
                (bad_request, lesson_id),
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


def test_submit_refusal_quarantines_unclaimed_attempts_for_retry_or_delete(
    tmp_path: Path,
) -> None:
    app = create_app(settings=_settings(tmp_path), baker=FixtureBaker())
    # Exercise the production API fallback rather than calling the store: a
    # runner that cannot admit work must terminally fail untouched drafts.
    app.state.runner.submit = lambda _lesson_id: False
    with TestClient(app, base_url=ORIGIN) as client:
        _, _, token = _issue_invite(app)
        session = _redeem(client, token)
        headers = _mutation_headers(session["csrf_token"])
        retry_id, delete_id = str(uuid.uuid4()), str(uuid.uuid4())

        refused_retry = client.post("/api/lessons", headers=headers, json=_lesson_request(retry_id))
        refused_delete = client.post(
            "/api/lessons", headers=headers, json=_lesson_request(delete_id)
        )
        assert refused_retry.status_code == refused_delete.status_code == 202
        assert refused_retry.json()["status"] == refused_delete.json()["status"] == "failed"
        retry_status = client.get(f"/api/lessons/{retry_id}/status").json()
        delete_status = client.get(f"/api/lessons/{delete_id}/status").json()
        assert retry_status["attempt"] == delete_status["attempt"] == 1

        # If fail_queued_drafts leaves quiescence NULL, both mutations return
        # 409 forever because no worker token exists to acknowledge it.
        retried = client.post(
            f"/api/lessons/{retry_id}/retry",
            headers=headers,
            json={"expected_revision": retry_status["revision"]},
        )
        assert retried.status_code == 202, retried.text
        assert retried.json()["status"] == "failed"
        assert retried.json()["attempt"] == 2
        deleted = client.delete(f"/api/lessons/{delete_id}", headers=headers)
        assert deleted.status_code == 204, deleted.text


def test_cancel_is_owner_scoped_idempotent_and_retry_preserves_job_history(tmp_path: Path) -> None:
    baker = BlockingBaker()
    app = create_app(settings=_settings(tmp_path), baker=baker)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            _, _, token = _issue_invite(app)
            session = _redeem(client, token)
            headers = _mutation_headers(session["csrf_token"])
            lesson_id = str(uuid.uuid4())
            created = client.post("/api/lessons", headers=headers, json=_lesson_request(lesson_id))
            assert created.status_code == 202, created.text
            assert baker.started.wait(timeout=1)
            active = client.get(f"/api/lessons/{lesson_id}/status").json()
            assert active["status"] == "baking"

            # Active DELETE must not make the provider operation invisible.
            _error(
                client.delete(f"/api/lessons/{lesson_id}", headers=headers),
                409,
                "lesson_state_conflict",
            )

            cancelled = client.post(
                f"/api/lessons/{lesson_id}/cancel",
                headers=headers,
                json={"expected_revision": active["revision"]},
            )
            assert cancelled.status_code == 200, cancelled.text
            cancelled_body = cancelled.json()
            assert cancelled_body["status"] == "cancelled"
            assert cancelled_body["attempt"] == 1
            assert cancelled_body["attempt_history"][-1]["status"] == "cancelled"
            assert cancelled_body["attempt_history"][-1]["failure_code"] == "cancelled"
            _error(
                client.delete(f"/api/lessons/{lesson_id}", headers=headers),
                409,
                "lesson_state_conflict",
                lesson_id=lesson_id,
            )
            pending_retry = client.post(
                f"/api/lessons/{lesson_id}/retry",
                headers=headers,
                json={"expected_revision": cancelled_body["revision"]},
            )
            pending_error = _error(
                pending_retry,
                409,
                "lesson_state_conflict",
                lesson_id=lesson_id,
            )
            assert "ще завершує" in pending_error["message"]

            repeated_cancel = client.post(
                f"/api/lessons/{lesson_id}/cancel",
                headers=headers,
                json={"expected_revision": cancelled_body["revision"]},
            )
            assert repeated_cancel.status_code == 200, repeated_cancel.text
            assert repeated_cancel.json() == cancelled_body

            # Let the already-started baker return: cancellation wins its later publish race.
            baker.release.set()
            time.sleep(0.05)
            after_provider = client.get(f"/api/lessons/{lesson_id}/status").json()
            assert after_provider["status"] == "cancelled"
            _error(client.get(f"/api/lessons/{lesson_id}"), 409, "lesson_not_ready")

            retried = client.post(
                f"/api/lessons/{lesson_id}/retry",
                headers=headers,
                json={"expected_revision": cancelled_body["revision"]},
            )
            assert retried.status_code == 202, retried.text
            assert retried.json()["id"] == lesson_id
            assert retried.json()["attempt"] == 2

            # A lost retry response may be replayed after the new attempt is claimed or ready.
            replay = client.post(
                f"/api/lessons/{lesson_id}/retry",
                headers=headers,
                json={"expected_revision": cancelled_body["revision"]},
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["attempt"] == 2
            ready = _wait_for_status(client, lesson_id, "ready")
            assert ready["attempt"] == 2
            assert [entry["attempt"] for entry in ready["attempt_history"]] == [1, 2]
            assert ready["attempt_history"][0]["status"] == "cancelled"
            assert ready["attempt_history"][0]["retry_requested_revision"] == cancelled_body[
                "revision"
            ]

            _error(
                client.post(
                    f"/api/lessons/{lesson_id}/cancel",
                    headers=headers,
                    json={"expected_revision": ready["revision"]},
                ),
                409,
                "lesson_state_conflict",
                lesson_id=lesson_id,
            )
    finally:
        baker.release.set()


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
