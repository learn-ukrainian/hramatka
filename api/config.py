"""Deployment configuration for the frozen same-origin teacher pilot.

No secret has a default.  Local development and tests must deliberately provide a
throwaway CSRF HMAC key instead of quietly falling back to an in-repository value.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

_DEFAULT_BAKE_WORKERS = 4
_MAX_BAKE_WORKERS = 8
_DEFAULT_MAX_PROVIDER_CONCURRENCY = 8
_DEFAULT_BAKE_PROVIDERS = ("antigravity", "openrouter")
_EXPLICIT_SUBSCRIPTION_PROVIDER = "antigravity"
_DEFAULT_SUBSCRIPTION_QUALIFICATION_PROVENANCE_TIER = "api_observed"
_DEFAULT_GOOGLE_AIS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
_GOOGLE_ID_TOKEN_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ALLOWED_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,63}$")


def _clamp_bake_workers(value: int) -> int:
    """Keep the in-process I/O worker pool within the tested host envelope."""
    return min(_MAX_BAKE_WORKERS, max(1, value))


def _get_default_bake_providers() -> tuple[str, ...]:
    if "DEEPINFRA_API_KEY" in os.environ:
        return (*_DEFAULT_BAKE_PROVIDERS, "deepinfra")
    return _DEFAULT_BAKE_PROVIDERS


def _parse_bake_providers(value: str | None) -> tuple[str, ...]:
    allowed = tuple(
        dict.fromkeys(
            (*_get_default_bake_providers(), _EXPLICIT_SUBSCRIPTION_PROVIDER, "google-ais")
        )
    )
    if value is None:
        return allowed
    providers = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    if not providers:
        raise RuntimeError("HRAMATKA_BAKE_PROVIDERS must name at least one provider.")
    unknown = sorted(set(providers) - set(allowed))
    if unknown:
        raise RuntimeError(
            f"HRAMATKA_BAKE_PROVIDERS supports only {', '.join(allowed)} "
            f"(got {', '.join(unknown)})."
        )
    return tuple(dict.fromkeys(providers))


def _decode_secret(value: str) -> bytes:
    """Decode one canonical, unpadded base64url 256-bit deployment secret."""
    if len(value) != 43 or not value.isascii() or "=" in value:
        raise RuntimeError("HRAMATKA_CSRF_HMAC_KEY must be an unpadded 32-byte base64url secret.")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except ValueError as error:
        raise RuntimeError("HRAMATKA_CSRF_HMAC_KEY must be base64url.") from error
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        raise RuntimeError("HRAMATKA_CSRF_HMAC_KEY must be a canonical 32-byte base64url secret.")
    return decoded


def _parse_zero_or_one_flag(name: str, *, default: str = "0") -> bool:
    """Read an operator-controlled feature flag without silently accepting typos."""
    value = os.environ.get(name, default)
    if value not in {"0", "1"}:
        raise RuntimeError(f"{name} must be the literal 0 or 1.")
    return value == "1"


def _parse_provenance_tier(
    name: str,
    *,
    default: str = _DEFAULT_SUBSCRIPTION_QUALIFICATION_PROVENANCE_TIER,
) -> str:
    value = os.environ.get(name, default)
    if value not in {"api_observed", "cli_self_reported"}:
        raise RuntimeError(f"{name} must be api_observed or cli_self_reported.")
    return value


def _validate_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("HRAMATKA_PILOT_ORIGIN must be one HTTPS origin without a path.")
    return origin.rstrip("/")


def _is_loopback_host(value: str | None) -> bool:
    """Accept only an unambiguous IP loopback binding."""
    if not value:
        return False
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _validate_google_ais_base_url(value: str) -> str:
    """Accept only the locked Google OpenAI-compatible HTTPS endpoint."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "generativelanguage.googleapis.com"
        or parsed.path.rstrip("/") != "/v1beta/openai"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "review attestation AIS base URL must be the locked Google OpenAI endpoint."
        )
    return _DEFAULT_GOOGLE_AIS_BASE_URL


def _validate_google_client_id(value: str | None) -> str | None:
    """Accept only the public OAuth audience, never a secret or broad URL."""
    if value is None or value == "":
        return None
    if (
        not value.isascii()
        or len(value) > 255
        or any(char.isspace() for char in value)
        or not value.endswith(".apps.googleusercontent.com")
    ):
        raise ValueError("HRAMATKA_GOOGLE_CLIENT_ID must be one Google OAuth web client id.")
    return value


def _parse_google_allowed_emails(value: str | None) -> frozenset[str]:
    """Parse a host-owned, casefolded, comma-separated Google email allowlist."""
    if value is None or value.strip() == "":
        return frozenset()
    emails: list[str] = []
    for part in value.split(","):
        email = part.strip().casefold()
        if not email:
            continue
        if len(email) > 320 or _GOOGLE_ALLOWED_EMAIL_RE.fullmatch(email) is None:
            raise ValueError(
                "HRAMATKA_GOOGLE_ALLOWED_EMAILS must be comma-separated emails."
            )
        emails.append(email)
    return frozenset(emails)


def _normalize_google_allowed_emails(emails: frozenset[str]) -> frozenset[str]:
    """Accept only already-constructed allowlist members in Settings tests."""
    normalized: list[str] = []
    for email in emails:
        if not isinstance(email, str):
            raise ValueError("google_allowed_emails must contain only emails.")
        folded = email.strip().casefold()
        if len(folded) > 320 or _GOOGLE_ALLOWED_EMAIL_RE.fullmatch(folded) is None:
            raise ValueError("google_allowed_emails must contain only emails.")
        normalized.append(folded)
    return frozenset(normalized)


def _validate_google_allowed_email_teacher_id(value: str | None) -> str | None:
    """Accept one existing teacher UUID, or none when the host will create."""
    if value is None or value.strip() == "":
        return None
    try:
        return str(UUID(value.strip()))
    except ValueError as error:
        raise ValueError(
            "HRAMATKA_GOOGLE_ALLOWED_EMAIL_TEACHER_ID must be one teacher UUID."
        ) from error


@dataclass(frozen=True)
class Settings:
    """Non-secret process settings plus the in-memory CSRF HMAC key."""

    database_path: Path
    pilot_origin: str
    csrf_hmac_key: bytes
    bake_hard_timeout_seconds: int = 1800
    bake_workers: int = _DEFAULT_BAKE_WORKERS
    max_provider_concurrency: int = _DEFAULT_MAX_PROVIDER_CONCURRENCY
    bake_providers: tuple[str, ...] = _DEFAULT_BAKE_PROVIDERS
    # A deliberate operator pause is distinct from a provider or worker
    # outage: saved lessons remain available while no new generation starts.
    generation_enabled: bool = True
    mock_mode: bool = False
    local_static_teacher: bool = False
    local_launcher_marker: bool = False
    server_bind_host: str | None = None
    subscription_qualification_provenance_tier: str = (
        _DEFAULT_SUBSCRIPTION_QUALIFICATION_PROVENANCE_TIER
    )
    # The review attestor is deliberately off by default.  Its configuration is
    # separate from the teacher-pilot surface because it accepts a GitHub
    # Actions credential, not a browser session.
    review_attestation_enabled: bool = False
    review_attestation_paid_enabled: bool = False
    review_attestation_repository: str | None = None
    review_attestation_repository_id: str | None = None
    review_attestation_audience: str | None = None
    review_attestation_workflow_ref: str | None = None
    review_attestation_workflow_digest: str | None = None
    review_attestation_trusted_runner_id: str | None = None
    review_attestation_trusted_runner_group_id: str | None = None
    review_attestation_trusted_runner_label: str | None = None
    review_attestation_db_path: Path | None = None
    review_attestation_signing_key_file: Path | None = None
    review_attestation_ais_base_url: str = _DEFAULT_GOOGLE_AIS_BASE_URL
    review_attestation_model: str = "google-ais/gemini-3.6-flash"
    review_attestation_max_calls_per_run: int = 1
    review_attestation_prompt_version: str = "hramatka-review-attestation-prompt.v2"
    review_attestation_max_diff_bytes: int = 500_000
    review_attestation_provider_timeout_seconds: int = 45
    review_attestation_jwks_ttl_seconds: int = 300
    # Google teacher login is disabled unless this public OAuth audience is
    # explicitly configured. It has no client secret and no Google API scope.
    google_client_id: str | None = None
    # Host EnvironmentFile allowlist. Empty means no first-bind from email;
    # already-linked Google subjects still authenticate.
    google_allowed_emails: frozenset[str] = frozenset()
    google_allowed_email_teacher_id: str | None = None
    # Direct Settings construction is the explicit local test/development
    # seam. The deployed environment enables this fail-closed policy by default.
    staff_authorization_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "pilot_origin", _validate_origin(self.pilot_origin))
        if len(self.csrf_hmac_key) < 32:
            raise ValueError("csrf_hmac_key must contain at least 32 random bytes.")
        object.__setattr__(
            self, "google_client_id", _validate_google_client_id(self.google_client_id)
        )
        object.__setattr__(
            self,
            "google_allowed_emails",
            _normalize_google_allowed_emails(self.google_allowed_emails),
        )
        object.__setattr__(
            self,
            "google_allowed_email_teacher_id",
            _validate_google_allowed_email_teacher_id(self.google_allowed_email_teacher_id),
        )
        if self.bake_hard_timeout_seconds <= 0:
            raise ValueError("bake_hard_timeout_seconds must be positive.")
        object.__setattr__(self, "bake_workers", _clamp_bake_workers(self.bake_workers))
        if self.max_provider_concurrency <= 0:
            raise ValueError("max_provider_concurrency must be positive.")
        object.__setattr__(
            self,
            "bake_providers",
            _parse_bake_providers(",".join(self.bake_providers)),
        )
        if self.subscription_qualification_provenance_tier not in {
            "api_observed",
            "cli_self_reported",
        }:
            raise ValueError("subscription qualification provenance tier is unsupported.")
        if self.review_attestation_enabled:
            required = {
                "repository": self.review_attestation_repository,
                "repository_id": self.review_attestation_repository_id,
                "audience": self.review_attestation_audience,
                "workflow_ref": self.review_attestation_workflow_ref,
                "workflow_digest": self.review_attestation_workflow_digest,
                "trusted_runner_id": self.review_attestation_trusted_runner_id,
                "trusted_runner_group_id": self.review_attestation_trusted_runner_group_id,
                "trusted_runner_label": self.review_attestation_trusted_runner_label,
                "signing_key_file": self.review_attestation_signing_key_file,
            }
            missing = sorted(name for name, value in required.items() if not value)
            if missing:
                raise ValueError(
                    "review attestation requires explicit " + ", ".join(missing) + " settings."
                )
            if not self.review_attestation_paid_enabled:
                raise ValueError("review attestation requires paid review enablement.")
            for name, value in {
                "trusted_runner_id": self.review_attestation_trusted_runner_id,
                "trusted_runner_group_id": self.review_attestation_trusted_runner_group_id,
            }.items():
                if (
                    not isinstance(value, str)
                    or not value.isascii()
                    or not value.isdigit()
                    or value.startswith("0")
                    or len(value) > 19
                    or int(value) <= 0
                    or int(value) > 9_223_372_036_854_775_807
                ):
                    raise ValueError(f"review attestation {name} must be a positive integer.")
            if self.review_attestation_trusted_runner_label != "hramatka-attestor":
                raise ValueError("review attestation runner label must be hramatka-attestor.")
            object.__setattr__(
                self,
                "review_attestation_ais_base_url",
                _validate_google_ais_base_url(self.review_attestation_ais_base_url),
            )
            if self.review_attestation_model != "google-ais/gemini-3.6-flash":
                raise ValueError("review attestation model must be google-ais/gemini-3.6-flash.")
            if self.review_attestation_max_calls_per_run != 1:
                raise ValueError("review attestation cost cap must be exactly one provider call.")
            if not self.review_attestation_prompt_version:
                raise ValueError("review attestation prompt version must be explicit.")
            if not 1 <= self.review_attestation_max_diff_bytes <= 500_000:
                raise ValueError("review attestation diff cap must be 1–500000 bytes.")
            if not 1 <= self.review_attestation_provider_timeout_seconds <= 60:
                raise ValueError("review attestation provider timeout must be 1–60 seconds.")
            if not 1 <= self.review_attestation_jwks_ttl_seconds <= 3600:
                raise ValueError("review attestation JWKS TTL must be 1–3600 seconds.")
            if self.review_attestation_db_path is None:
                object.__setattr__(
                    self,
                    "review_attestation_db_path",
                    self.database_path.with_name("review-attestations.sqlite3"),
                )

    @property
    def local_static_teacher_enabled(self) -> bool:
        """Return true only for the explicitly marked loopback launcher path."""
        origin_host = urlsplit(self.pilot_origin).hostname
        return (
            self.local_static_teacher
            and self.local_launcher_marker
            and _is_loopback_host(self.server_bind_host)
            and _is_loopback_host(origin_host)
        )

    @property
    def google_auth_enabled(self) -> bool:
        return self.google_client_id is not None

    @property
    def google_jwks_url(self) -> str:
        """The issuer-owned key set is fixed rather than deployment configurable."""
        return _GOOGLE_ID_TOKEN_JWKS_URL

    @classmethod
    def from_env(cls) -> Settings:
        origin = os.environ.get("HRAMATKA_PILOT_ORIGIN")
        if not origin:
            raise RuntimeError("HRAMATKA_PILOT_ORIGIN must be set before starting the API.")
        csrf_key = os.environ.get("HRAMATKA_CSRF_HMAC_KEY")
        if not csrf_key:
            raise RuntimeError("HRAMATKA_CSRF_HMAC_KEY must be set before starting the API.")
        timeout = int(os.environ.get("HRAMATKA_BAKE_HARD_TIMEOUT_SECONDS", "1800"))
        workers = int(os.environ.get("HRAMATKA_BAKE_WORKERS", str(_DEFAULT_BAKE_WORKERS)))
        max_provider_concurrency = int(
            os.environ.get(
                "HRAMATKA_MAX_PROVIDER_CONCURRENCY",
                str(_DEFAULT_MAX_PROVIDER_CONCURRENCY),
            )
        )
        return cls(
            database_path=Path(
                os.environ.get("HRAMATKA_DB_PATH", "/var/lib/hramatka/hramatka.sqlite3")
            ),
            pilot_origin=origin,
            csrf_hmac_key=_decode_secret(csrf_key),
            bake_hard_timeout_seconds=timeout,
            bake_workers=workers,
            max_provider_concurrency=max_provider_concurrency,
            bake_providers=_parse_bake_providers(os.environ.get("HRAMATKA_BAKE_PROVIDERS")),
            generation_enabled=_parse_zero_or_one_flag(
                "HRAMATKA_GENERATION_ENABLED", default="1"
            ),
            # Mock mode exists only as an explicit test/development seam.  It is
            # never ready for the deployed pilot and the production service does
            # not select it as its default baker.
            mock_mode=os.environ.get("HRAMATKA_MOCK_MODE", "0") == "1",
            local_static_teacher=_parse_zero_or_one_flag("HRAMATKA_LOCAL_STATIC_TEACHER"),
            local_launcher_marker=_parse_zero_or_one_flag("HRAMATKA_LOCAL_LAUNCHER"),
            server_bind_host=os.environ.get("HRAMATKA_SERVER_BIND_HOST"),
            subscription_qualification_provenance_tier=_parse_provenance_tier(
                "HRAMATKA_SUBSCRIPTION_QUALIFICATION_PROVENANCE_TIER"
            ),
            review_attestation_enabled=_parse_zero_or_one_flag(
                "HRAMATKA_REVIEW_ATTESTATION_ENABLED"
            ),
            review_attestation_paid_enabled=_parse_zero_or_one_flag(
                "HRAMATKA_REVIEW_ATTESTATION_PAID_REVIEW_ENABLED"
            ),
            review_attestation_repository=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_TRUSTED_REPOSITORY"
            ),
            review_attestation_repository_id=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_TRUSTED_REPOSITORY_ID"
            ),
            review_attestation_audience=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_EXPECTED_AUDIENCE"
            ),
            review_attestation_workflow_ref=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_TRUSTED_WORKFLOW_REF"
            ),
            review_attestation_workflow_digest=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_TRUSTED_WORKFLOW_DIGEST"
            ),
            review_attestation_trusted_runner_id=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_TRUSTED_RUNNER_ID"
            ),
            review_attestation_trusted_runner_group_id=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_TRUSTED_RUNNER_GROUP_ID"
            ),
            review_attestation_trusted_runner_label=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_TRUSTED_RUNNER_LABEL"
            ),
            review_attestation_db_path=(
                Path(value)
                if (value := os.environ.get("HRAMATKA_REVIEW_ATTESTATION_DB_PATH"))
                else None
            ),
            review_attestation_signing_key_file=(
                Path(value)
                if (value := os.environ.get("HRAMATKA_REVIEW_ATTESTATION_SIGNING_KEY_FILE"))
                else None
            ),
            review_attestation_ais_base_url=os.environ.get(
                "HRAMATKA_AIS_BASE_URL", _DEFAULT_GOOGLE_AIS_BASE_URL
            ),
            review_attestation_model=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_REVIEWER_MODEL",
                "google-ais/gemini-3.6-flash",
            ),
            review_attestation_prompt_version=os.environ.get(
                "HRAMATKA_REVIEW_ATTESTATION_PROMPT_VERSION",
                "hramatka-review-attestation-prompt.v2",
            ),
            review_attestation_max_diff_bytes=int(
                os.environ.get("HRAMATKA_REVIEW_ATTESTATION_MAX_DIFF_BYTES", "500000")
            ),
            review_attestation_provider_timeout_seconds=int(
                os.environ.get("HRAMATKA_REVIEW_ATTESTATION_PROVIDER_TIMEOUT_SECONDS", "45")
            ),
            review_attestation_jwks_ttl_seconds=int(
                os.environ.get("HRAMATKA_REVIEW_ATTESTATION_JWKS_TTL_SECONDS", "300")
            ),
            google_client_id=os.environ.get("HRAMATKA_GOOGLE_CLIENT_ID"),
            google_allowed_emails=_parse_google_allowed_emails(
                os.environ.get("HRAMATKA_GOOGLE_ALLOWED_EMAILS")
            ),
            google_allowed_email_teacher_id=os.environ.get(
                "HRAMATKA_GOOGLE_ALLOWED_EMAIL_TEACHER_ID"
            ),
            staff_authorization_required=_parse_zero_or_one_flag(
                "HRAMATKA_STAFF_AUTHORIZATION_REQUIRED", default="1"
            ),
        )
