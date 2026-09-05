"""Fail-closed authentication for the fixed V4 Actions operation workflow.

This module authenticates a GitHub Actions caller and returns only immutable,
comparison-safe principal and request-digest data.  It deliberately does not
select work, persist authorizations, claim executions, or authorize review
attestations; those operation-ownership concerns belong to the later V4
runtime integration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from .config import (
    _V4_EXECUTION_AUDIENCE,
    _V4_EXECUTION_REF,
    _V4_EXECUTION_WORKFLOW_PATH,
    Settings,
)

_API_ROOT = "https://api.github.com"
_GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
_GITHUB_JWKS = f"{_GITHUB_ISSUER}/.well-known/jwks"
_AUTHORIZE_SCHEMA = "hramatka-v4-operation-authorize.v1"
_EXECUTE_SCHEMA = "hramatka-v4-operation-execute.v1"
_JOB_NAME = "execute-v4-real-slot"
_MAX_BODY_BYTES = 1024
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_BASE64_WRAPPING_WHITESPACE = b" \t\r\n"


class V4ExecutionAuthError(RuntimeError):
    """A safe, stable failure from the V4 machine authentication boundary."""

    def __init__(self, code: str, *, status_code: int = 403) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class V4CanonicalRequest:
    """One exact caller body; its digest is calculated only from trusted bytes."""

    schema: str
    body_sha256: str
    authorization_id: str | None = None


@dataclass(frozen=True)
class V4ExecutionPrincipal:
    """The exact authenticated Actions identity shared by both V4 operations."""

    repository: str
    repository_id: int
    audience: str
    ref: str
    subject: str
    workflow_ref: str
    workflow_sha: str
    workflow_digest: str
    run_id: int
    run_attempt: int
    check_run_id: int
    job_name: str
    runner_id: int
    runner_group_id: int
    runner_label: str
    authz_policy_sha256: str
    oidc_jti_sha256: str


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_canonical_body(body: bytes, *, expected_schema: str) -> dict[str, object]:
    if not isinstance(body, bytes) or len(body) > _MAX_BODY_BYTES:
        raise V4ExecutionAuthError("invalid_request", status_code=422)
    try:
        decoded = body.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise V4ExecutionAuthError("invalid_request", status_code=422) from error
    if not isinstance(payload, dict) or payload.get("schema") != expected_schema:
        raise V4ExecutionAuthError("invalid_request", status_code=422)
    if body != _canonical_json(payload):
        raise V4ExecutionAuthError("invalid_request", status_code=422)
    return payload


def parse_v4_authorization_request(body: bytes) -> V4CanonicalRequest:
    """Parse the one permitted authorization body, including exact UTF-8 bytes."""
    payload = _parse_canonical_body(body, expected_schema=_AUTHORIZE_SCHEMA)
    if set(payload) != {"schema"}:
        raise V4ExecutionAuthError("invalid_request", status_code=422)
    return V4CanonicalRequest(schema=_AUTHORIZE_SCHEMA, body_sha256=_sha256(body))


def parse_v4_execution_request(body: bytes) -> V4CanonicalRequest:
    """Parse the one permitted execution body without accepting a supplied digest."""
    payload = _parse_canonical_body(body, expected_schema=_EXECUTE_SCHEMA)
    if set(payload) != {"schema", "authorization_id"}:
        raise V4ExecutionAuthError("invalid_request", status_code=422)
    authorization_id = payload.get("authorization_id")
    if not isinstance(authorization_id, str) or _OPAQUE_ID_RE.fullmatch(authorization_id) is None:
        raise V4ExecutionAuthError("invalid_request", status_code=422)
    return V4CanonicalRequest(
        schema=_EXECUTE_SCHEMA,
        authorization_id=authorization_id,
        body_sha256=_sha256(body),
    )


def _positive_int(value: object, *, allow_digit_string: bool) -> int:
    if isinstance(value, bool):
        raise V4ExecutionAuthError("oidc_claim_mismatch")
    if isinstance(value, int):
        result = value
    elif allow_digit_string and isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise V4ExecutionAuthError("oidc_claim_mismatch")
    if not 1 <= result <= 9_223_372_036_854_775_807:
        raise V4ExecutionAuthError("oidc_claim_mismatch")
    return result


def _required_text(value: object, *, maximum: int = 300) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\n" in value
        or "\r" in value
    ):
        raise V4ExecutionAuthError("oidc_claim_mismatch")
    return value


class V4ExecutionAuthVerifier:
    """Verify a new fixed-workflow Actions token against GitHub authority data."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))
        self.clock = clock or (lambda: datetime.now(UTC))
        self._jwks_lock = threading.Lock()
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_expires_at: datetime | None = None

    def authenticate(
        self, *, oidc_token: str | None, github_bearer: str | None
    ) -> V4ExecutionPrincipal:
        """Fresh identity only; the public runtime parses bodies and owns replay checks."""
        return self._verify(
            oidc_token=oidc_token, github_token=github_bearer,
        )

    def verify_authorization(
        self,
        request: V4CanonicalRequest,
        *,
        oidc_token: str | None,
        github_token: str | None,
    ) -> V4ExecutionPrincipal:
        if request.schema != _AUTHORIZE_SCHEMA:
            raise V4ExecutionAuthError("invalid_request", status_code=422)
        return self._verify(request, oidc_token=oidc_token, github_token=github_token)

    def verify_execution(
        self,
        request: V4CanonicalRequest,
        *,
        authorization_principal: V4ExecutionPrincipal,
        oidc_token: str | None,
        github_token: str | None,
    ) -> V4ExecutionPrincipal:
        if request.schema != _EXECUTE_SCHEMA or request.authorization_id is None:
            raise V4ExecutionAuthError("invalid_request", status_code=422)
        principal = self._verify(request, oidc_token=oidc_token, github_token=github_token)
        if principal.oidc_jti_sha256 == authorization_principal.oidc_jti_sha256:
            raise V4ExecutionAuthError("oidc_jti_not_fresh")
        if not same_v4_operation_principal(authorization_principal, principal):
            raise V4ExecutionAuthError("operation_principal_mismatch")
        return principal

    def _verify(
        self,
        request: V4CanonicalRequest | None = None,
        *,
        oidc_token: str | None,
        github_token: str | None,
    ) -> V4ExecutionPrincipal:
        if not self.settings.v4_execution_enabled:
            raise V4ExecutionAuthError("v4_execution_disabled", status_code=404)
        self._validate_fixed_policy_configuration()
        if not isinstance(oidc_token, str) or not 1 <= len(oidc_token) <= 16_384:
            raise V4ExecutionAuthError("oidc_required", status_code=401)
        if not isinstance(github_token, str) or not 20 <= len(github_token) <= 512:
            raise V4ExecutionAuthError("github_token_required", status_code=401)
        claims = self._verify_oidc(oidc_token)
        run_id = _positive_int(claims.get("run_id"), allow_digit_string=True)
        run_attempt = _positive_int(claims.get("run_attempt"), allow_digit_string=True)
        check_run_id = _positive_int(claims.get("check_run_id"), allow_digit_string=True)
        workflow_sha = _required_text(claims.get("workflow_sha"), maximum=40)
        if _SHA1_RE.fullmatch(workflow_sha) is None:
            raise V4ExecutionAuthError("oidc_claim_mismatch")
        jti = _required_text(claims.get("jti"), maximum=512)
        self._validate_authoritative_actions_context(
            check_run_id=check_run_id,
            run_id=run_id,
            run_attempt=run_attempt,
            workflow_sha=workflow_sha,
            token=github_token,
        )
        workflow_digest = self._fetch_and_verify_workflow(workflow_sha, github_token)
        repository = self._setting("v4_execution_repository")
        ref = _V4_EXECUTION_REF
        return V4ExecutionPrincipal(
            repository=repository,
            repository_id=_positive_int(
                self._setting("v4_execution_repository_id"), allow_digit_string=True
            ),
            audience=self._setting("v4_execution_audience"),
            ref=ref,
            subject=f"repo:{repository}:ref:{ref}",
            workflow_ref=self._setting("v4_execution_workflow_ref"),
            workflow_sha=workflow_sha,
            workflow_digest=workflow_digest,
            run_id=run_id,
            run_attempt=run_attempt,
            check_run_id=check_run_id,
            job_name=_JOB_NAME,
            runner_id=_positive_int(
                self._setting("v4_execution_trusted_runner_id"), allow_digit_string=True
            ),
            runner_group_id=_positive_int(
                self._setting("v4_execution_trusted_runner_group_id"), allow_digit_string=True
            ),
            runner_label=self._setting("v4_execution_trusted_runner_label"),
            authz_policy_sha256=self._policy_digest(),
            oidc_jti_sha256=_sha256(jti),
        )

    def _setting(self, name: str) -> str:
        value = getattr(self.settings, name)
        if not isinstance(value, str) or not value:
            raise V4ExecutionAuthError("v4_execution_misconfigured", status_code=503)
        return value

    def _validate_fixed_policy_configuration(self) -> None:
        repository = self._setting("v4_execution_repository")
        if self._setting("v4_execution_audience") != _V4_EXECUTION_AUDIENCE:
            raise V4ExecutionAuthError("v4_execution_misconfigured", status_code=503)
        expected_workflow_ref = (
            f"{repository}/{_V4_EXECUTION_WORKFLOW_PATH}@{_V4_EXECUTION_REF}"
        )
        if self._setting("v4_execution_workflow_ref") != expected_workflow_ref:
            raise V4ExecutionAuthError("v4_execution_misconfigured", status_code=503)

    def _verify_oidc(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            if (
                not isinstance(header, dict)
                or header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
            ):
                raise V4ExecutionAuthError("oidc_invalid", status_code=401)
            keys = self._get_jwks()
            key = next(item for item in keys.keys if item.key_id == header["kid"])
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self._setting("v4_execution_audience"),
                issuer=_GITHUB_ISSUER,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "jti",
                        "ref",
                        "sub",
                        "check_run_id",
                        "workflow_sha",
                    ],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except V4ExecutionAuthError:
            raise
        except (jwt.PyJWTError, httpx.HTTPError, StopIteration, ValueError) as error:
            raise V4ExecutionAuthError("oidc_invalid", status_code=401) from error
        now = self.clock()
        iat, exp = claims.get("iat"), claims.get("exp")
        if (
            isinstance(iat, bool)
            or not isinstance(iat, (int, float))
            or isinstance(exp, bool)
            or not isinstance(exp, (int, float))
        ):
            raise V4ExecutionAuthError("oidc_invalid", status_code=401)
        try:
            issued, expires = datetime.fromtimestamp(iat, UTC), datetime.fromtimestamp(exp, UTC)
        except (OverflowError, OSError, ValueError) as error:
            raise V4ExecutionAuthError("oidc_invalid", status_code=401) from error
        if (
            issued > now + timedelta(seconds=30)
            or now - issued > timedelta(minutes=5)
            or expires <= now
            or expires > issued + timedelta(minutes=10)
        ):
            raise V4ExecutionAuthError("oidc_stale", status_code=401)
        repository = self._setting("v4_execution_repository")
        expected = {
            "aud": _V4_EXECUTION_AUDIENCE,
            "repository": repository,
            "repository_id": self._setting("v4_execution_repository_id"),
            "repository_visibility": "private",
            "event_name": "workflow_dispatch",
            "ref": _V4_EXECUTION_REF,
            "runner_environment": "self-hosted",
            "sub": f"repo:{repository}:ref:{_V4_EXECUTION_REF}",
            "workflow_ref": self._setting("v4_execution_workflow_ref"),
        }
        if any(claims.get(name) != value for name, value in expected.items()):
            raise V4ExecutionAuthError("oidc_claim_mismatch")
        return claims

    def _get_jwks(self) -> jwt.PyJWKSet:
        now = self.clock()
        with self._jwks_lock:
            if (
                self._jwks is not None
                and self._jwks_expires_at is not None
                and now < self._jwks_expires_at
            ):
                return self._jwks
            try:
                response = self.client.get(_GITHUB_JWKS)
                response.raise_for_status()
                keys = jwt.PyJWKSet.from_dict(response.json())
            except (jwt.PyJWTError, httpx.HTTPError, ValueError) as error:
                raise V4ExecutionAuthError("oidc_invalid", status_code=401) from error
            self._jwks = keys
            self._jwks_expires_at = now + timedelta(
                seconds=self.settings.v4_execution_jwks_ttl_seconds
            )
            return keys

    def _github_get(self, path: str, token: str) -> httpx.Response:
        response = self.client.get(
            f"{_API_ROOT}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return response

    def _validate_authoritative_actions_context(
        self, *, check_run_id: int, run_id: int, run_attempt: int, workflow_sha: str, token: str
    ) -> None:
        repository = self._setting("v4_execution_repository")
        encoded_repo = quote(repository, safe="/")
        try:
            job = self._github_get(
                f"/repos/{encoded_repo}/actions/jobs/{check_run_id}", token
            ).json()
            run = self._github_get(f"/repos/{encoded_repo}/actions/runs/{run_id}", token).json()
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise V4ExecutionAuthError("github_authority_unavailable", status_code=503) from error
        expected_job = {
            "id": check_run_id,
            "run_id": run_id,
            "name": _JOB_NAME,
            "status": "in_progress",
            "conclusion": None,
            "runner_id": _positive_int(
                self._setting("v4_execution_trusted_runner_id"), allow_digit_string=True
            ),
            "runner_group_id": _positive_int(
                self._setting("v4_execution_trusted_runner_group_id"), allow_digit_string=True
            ),
            "labels": [self._setting("v4_execution_trusted_runner_label")],
        }
        if not isinstance(job, dict) or any(
            job.get(name) != value for name, value in expected_job.items()
        ):
            raise V4ExecutionAuthError("actions_job_mismatch")
        if (
            not isinstance(run, dict)
            or run.get("id") != run_id
            or run.get("event") != "workflow_dispatch"
            or run.get("path") != self._workflow_path()
            or run.get("run_attempt") != run_attempt
            or run.get("head_sha") != workflow_sha
        ):
            raise V4ExecutionAuthError("actions_run_mismatch")

    def _fetch_and_verify_workflow(self, workflow_sha: str, token: str) -> str:
        repository = self._setting("v4_execution_repository")
        encoded_repo = quote(repository, safe="/")
        try:
            workflow_path = quote(self._workflow_path(), safe="/")
            contents = self._github_get(
                f"/repos/{encoded_repo}/contents/{workflow_path}?ref={workflow_sha}",
                token,
            ).json()
            raw_workflow = self._decode_github_base64_content(contents)
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise V4ExecutionAuthError("github_authority_unavailable", status_code=503) from error
        digest = _sha256(raw_workflow)
        if digest != self._setting("v4_execution_workflow_digest"):
            raise V4ExecutionAuthError("workflow_digest_mismatch")
        return digest

    @staticmethod
    def _decode_github_base64_content(contents: object) -> bytes:
        if (
            not isinstance(contents, dict)
            or contents.get("encoding") != "base64"
            or not isinstance(contents.get("content"), str)
        ):
            raise ValueError("malformed workflow")
        encoded = contents["content"].encode("ascii")
        compact = bytes(byte for byte in encoded if byte not in _BASE64_WRAPPING_WHITESPACE)
        return base64.b64decode(compact, validate=True)

    def _workflow_path(self) -> str:
        repository = self._setting("v4_execution_repository")
        reference = self._setting("v4_execution_workflow_ref")
        expected = f"{repository}/{_V4_EXECUTION_WORKFLOW_PATH}@{_V4_EXECUTION_REF}"
        if reference != expected:
            raise V4ExecutionAuthError("workflow_ref_invalid")
        return _V4_EXECUTION_WORKFLOW_PATH

    def _policy_digest(self) -> str:
        return _sha256(
            _canonical_json(
                {
                    "audience": self._setting("v4_execution_audience"),
                    "event": "workflow_dispatch",
                    "job": _JOB_NAME,
                    "label": self._setting("v4_execution_trusted_runner_label"),
                    "policy": "hramatka-v4-actions-authz.v1",
                    "ref": _V4_EXECUTION_REF,
                    "repository": self._setting("v4_execution_repository"),
                    "repository_id": _positive_int(
                        self._setting("v4_execution_repository_id"), allow_digit_string=True
                    ),
                    "runner_group_id": _positive_int(
                        self._setting("v4_execution_trusted_runner_group_id"),
                        allow_digit_string=True,
                    ),
                    "runner_id": _positive_int(
                        self._setting("v4_execution_trusted_runner_id"), allow_digit_string=True
                    ),
                    "workflow_digest": self._setting("v4_execution_workflow_digest"),
                    "workflow_ref": self._setting("v4_execution_workflow_ref"),
                }
            )
        )


def same_v4_operation_principal(
    authorization: V4ExecutionPrincipal, execution: V4ExecutionPrincipal
) -> bool:
    """Compare every operation owner field while intentionally excluding fresh JTI/body digests."""
    return (
        authorization.repository,
        authorization.repository_id,
        authorization.audience,
        authorization.ref,
        authorization.subject,
        authorization.workflow_ref,
        authorization.workflow_sha,
        authorization.workflow_digest,
        authorization.run_id,
        authorization.run_attempt,
        authorization.check_run_id,
        authorization.job_name,
        authorization.runner_id,
        authorization.runner_group_id,
        authorization.runner_label,
        authorization.authz_policy_sha256,
    ) == (
        execution.repository,
        execution.repository_id,
        execution.audience,
        execution.ref,
        execution.subject,
        execution.workflow_ref,
        execution.workflow_sha,
        execution.workflow_digest,
        execution.run_id,
        execution.run_attempt,
        execution.check_run_id,
        execution.job_name,
        execution.runner_id,
        execution.runner_group_id,
        execution.runner_label,
        execution.authz_policy_sha256,
    )
