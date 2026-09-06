"""Lifecycle and wire binding to the verified public V4 parent runtime."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from hramatka.engine.v4_runtime_vendor import VerifiedV4Release, verify_installed

from .config import Settings
from .v4_execution_auth import (
    V4ExecutionAuthError,
    V4ExecutionAuthVerifier,
    parse_v4_authorization_request,
    parse_v4_execution_request,
)

log = logging.getLogger(__name__)

# Only literal codes from the pinned public runtime may enter private logs.
# Never log arbitrary exception arguments, captures, credentials or tracebacks.
_REFUSAL_CODES = frozenset(
    {
        "adapter_executable_unpinned",
        "adapter_model_unqualified",
        "adapter_separability_unqualified",
        "adapter_unqualified",
        "admitted_constraints_required",
        "assignment_not_freezable",
        "author_required_fields",
        "author_row_absent",
        "author_row_json",
        "author_row_marker",
        "author_row_shape",
        "author_semantic_input_missing",
        "author_snapshot",
        "authored_capture_digest",
        "authored_capture_unresolved",
        "authored_row_digest",
        "authored_row_keys",
        "authored_row_required",
        "authored_row_values",
        "authorization_id",
        "authorization_inactive",
        "authorization_ownership",
        "authorship_receipt_digest",
        "authorship_unresolved",
        "binding_digest",
        "binding_ownership",
        "canonical_preparation_reference_required",
        "capture_limit",
        "child_capture_invalid",
        "child_credential_disclosure",
        "child_event_invalid",
        "child_identity_or_terminal_unproved",
        "child_launch_failed",
        "child_unsuccessful",
        "constraint_fields",
        "constraint_language",
        "constraint_level",
        "constraint_stratum",
        "constraint_task",
        "constraint_tools",
        "consumed_jti",
        "control_role_required",
        "correction_authority_required",
        "correction_source_role_required",
        "correction_target_separate",
        "credential_memfd_unavailable",
        "duplicate_key",
        "execution_expired",
        "execution_release_changed",
        "execution_timeout",
        "finalization_ownership",
        "foreign_child_capture",
        "invalid_authenticated_policy",
        "invalid_authenticated_principal",
        "invalid_request",
        "linguistic_authority_unresolved",
        "linguistic_claim_type",
        "linguistic_dependencies",
        "linguistic_dependency",
        "linguistic_dependency_index",
        "linguistic_dependency_relation",
        "linguistic_feature_value",
        "linguistic_features",
        "linguistic_matcher",
        "linguistic_matcher_kind",
        "linguistic_mechanism",
        "linguistic_pos",
        "linguistic_protections",
        "linguistic_scope",
        "linguistic_target_required",
        "linguistic_token",
        "linguistic_tokens",
        "noncanonical_request",
        "normative_source_role_required",
        "operation_role",
        "preparation_artifact_digest",
        "preparation_artifact_noncanonical",
        "preparation_artifact_schema",
        "preparation_artifact_shape",
        "provider_credential_auth_json",
        "provider_credential_expired",
        "provider_credential_file",
        "provider_credential_file_changed",
        "provider_credential_mismatch",
        "provider_credential_schema",
        "provider_credential_scope",
        "provider_credential_token",
        "request_deadline_ownership",
        "request_inactive",
        "request_keys",
        "request_schema",
        "request_size",
        "review_verdict_absent",
        "reviewer_semantic_origin_mismatch",
        "reviewer_snapshot",
        "rubric_digest",
        "runtime_closure_digest",
        "runtime_closure_keys",
        "runtime_closure_mount",
        "runtime_closure_writable",
        "runtime_profile",
        "runtime_profile_digest",
        "semantic_input_digest",
        "semantic_input_size",
        "semantic_preparation_digest",
        "source_qualified_target_required",
        "target_assignment_binding",
        "target_evidence_binding",
        "target_evidence_locators",
        "target_evidence_required",
        "target_source_role",
        "target_upstream_binding",
    }
)


def _refusal_code(error: BaseException) -> str:
    args = error.args
    if len(args) == 1 and type(args[0]) is str and args[0] in _REFUSAL_CODES:
        return args[0]
    return "unknown_refusal"


class ActionsVerifierAdapter:
    """Preserve every reviewed private identity in public ownership JSON."""

    def __init__(self, authenticator: V4ExecutionAuthVerifier):
        verify_installed()
        from learn_ukrainian_v4_runtime.operation_auth import ActionsPrincipal

        @dataclass(frozen=True)
        class CompleteActionsPrincipal(ActionsPrincipal):
            repository: str
            audience: str
            workflow_sha: str
            job_name: str

        self._principal = CompleteActionsPrincipal
        self._authenticator = authenticator

    def authenticate(self, *, oidc_token: str, github_bearer: str):
        p = self._authenticator.authenticate(
            oidc_token=oidc_token,
            github_bearer=github_bearer,
        )
        return self._principal(
            repository=p.repository,
            repository_id=p.repository_id,
            audience=p.audience,
            ref=p.ref,
            subject=p.subject,
            workflow_ref=p.workflow_ref,
            workflow_sha=p.workflow_sha,
            workflow_sha256=p.workflow_digest,
            run_id=p.run_id,
            run_attempt=p.run_attempt,
            check_run_id=p.check_run_id,
            job_name=p.job_name,
            runner_id=p.runner_id,
            runner_group_id=p.runner_group_id,
            runner_label=p.runner_label,
            authz_policy_sha256=p.authz_policy_sha256,
            # The public store hashes this comparison-safe value once more.
            # Raw JWT/JTI values never become persistence or diagnostics.
            jti=p.oidc_jti_sha256,
        )


class V4Integration:
    """Each call owns a scoped connection; shutdown drains parent-owned work."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self._condition = threading.Condition()
        self._accepting = False
        self._active = 0
        self._authenticator = None
        self._verifier = None
        self._http_client = http_client
        self._clock = clock

    def start(self) -> None:
        if not self.settings.v4_execution_enabled:
            return
        try:
            verify_installed()  # Must precede every first public import.
            from learn_ukrainian_v4_runtime.pg_schema import verify_pg_schema
            from learn_ukrainian_v4_runtime.readiness import (
                require_admission_enabled,
                require_execution_enabled,
                require_readiness,
            )
            from learn_ukrainian_v4_runtime.scoped_store import ScopedAuthorityStore

            require_execution_enabled()
            require_readiness()
            if self.settings.v4_admission_enabled:
                require_admission_enabled()
            store = ScopedAuthorityStore()
            try:
                if verify_pg_schema(store.connection) != 6:
                    raise RuntimeError("schema")
            finally:
                store.close()
            self._authenticator = V4ExecutionAuthVerifier(
                self.settings,
                client=self._http_client,
                clock=self._clock,
            )
            self._verifier = ActionsVerifierAdapter(self._authenticator)
            with self._condition:
                self._accepting = True
        except Exception:
            self.stop()
            raise RuntimeError("v4_startup_refused") from None

    def stop(self) -> None:
        with self._condition:
            self._accepting = False
            while self._active:
                self._condition.wait()
        if self._authenticator is not None:
            self._authenticator.client.close()
            self._authenticator = None

    def invoke(self, raw: bytes, *, execution: bool, oidc_token: str, github_bearer: str):
        with self._condition:
            if not self._accepting:
                raise V4ExecutionAuthError("v4_unavailable", status_code=503)
            self._active += 1
        try:
            verify_installed()
            from learn_ukrainian_v4_runtime.operation_auth import OperationRefused
            from learn_ukrainian_v4_runtime.operation_store import OperationStore
            from learn_ukrainian_v4_runtime.scoped_store import ScopedAuthorityStore
            from learn_ukrainian_v4_runtime.service_runtime import V4ServiceRuntime

            scoped = ScopedAuthorityStore(write=True)
            try:
                runtime = V4ServiceRuntime(
                    store=OperationStore(scoped.connection),
                    verifier=self._verifier,
                    release_provider=VerifiedV4Release(),
                )
                method = runtime.execute if execution else runtime.authorize
                try:
                    return method(raw, oidc_token=oidc_token, github_bearer=github_bearer)
                except OperationRefused as error:
                    log.warning(
                        "v4 operation refused phase=%s code=%s",
                        "execute" if execution else "authorize",
                        _refusal_code(error),
                    )
                    raise V4ExecutionAuthError("v4_operation_refused", status_code=409) from None
            finally:
                scoped.close()
        except V4ExecutionAuthError:
            raise
        except Exception:
            log.warning(
                "v4 operation failed phase=%s code=unhandled_failure",
                "execute" if execution else "authorize",
            )
            raise V4ExecutionAuthError("v4_unavailable", status_code=503) from None
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


def register_v4_routes(app: FastAPI, integration: V4Integration) -> None:
    """Only bounded canonical bodies and two machine credentials cross ASGI."""

    async def dispatch(request: Request, *, execution: bool):
        try:
            if request.url.query:
                raise V4ExecutionAuthError("invalid_request", status_code=422)
            content_type = request.headers.get("content-type", "").lower()
            if content_type not in ("application/json", "application/json; charset=utf-8"):
                raise V4ExecutionAuthError("invalid_request", status_code=422)
            for name in ("content-length", "content-type", "authorization", "x-github-oidc-token"):
                if len(request.headers.getlist(name)) > 1:
                    raise V4ExecutionAuthError("invalid_request", status_code=422)
            length = request.headers.get("content-length")
            if length is not None and (
                not length.isascii()
                or not length.isdigit()
                or len(length) > 4
                or int(length) > 1024
            ):
                raise V4ExecutionAuthError("invalid_request", status_code=422)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 1024:
                    raise V4ExecutionAuthError("invalid_request", status_code=422)
                body.extend(chunk)
            raw = bytes(body)
            parser = parse_v4_execution_request if execution else parse_v4_authorization_request
            parser(raw)
            authorization = request.headers.get("authorization", "")
            token = request.headers.get("x-github-oidc-token", "")
            if not authorization.startswith("Bearer "):
                raise V4ExecutionAuthError("github_token_required", status_code=401)
            result = await run_in_threadpool(
                integration.invoke,
                raw,
                execution=execution,
                oidc_token=token,
                github_bearer=authorization[7:],
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except V4ExecutionAuthError as error:
            return JSONResponse(
                {"code": error.code},
                status_code=error.status_code,
                headers={"Cache-Control": "no-store"},
            )

    @app.post("/api/internal/v4/execution-authorizations")
    async def authorize(request: Request):
        return await dispatch(request, execution=False)

    @app.post("/api/internal/v4/executions")
    async def execute(request: Request):
        return await dispatch(request, execution=True)
