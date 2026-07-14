"""Deployment configuration for the frozen same-origin teacher pilot.

No secret has a default.  Local development and tests must deliberately provide a
throwaway CSRF HMAC key instead of quietly falling back to an in-repository value.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_DEFAULT_BAKE_WORKERS = 4
_MAX_BAKE_WORKERS = 8
_DEFAULT_MAX_PROVIDER_CONCURRENCY = 8
_DEFAULT_BAKE_PROVIDERS = ("google-ais", "openrouter")


def _clamp_bake_workers(value: int) -> int:
    """Keep the in-process I/O worker pool within the tested host envelope."""
    return min(_MAX_BAKE_WORKERS, max(1, value))


def _parse_bake_providers(value: str | None) -> tuple[str, ...]:
    if value is None:
        return _DEFAULT_BAKE_PROVIDERS
    providers = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    if not providers:
        raise RuntimeError("HRAMATKA_BAKE_PROVIDERS must name at least one provider.")
    unknown = sorted(set(providers) - set(_DEFAULT_BAKE_PROVIDERS))
    if unknown:
        raise RuntimeError(
            "HRAMATKA_BAKE_PROVIDERS supports only google-ais and openrouter "
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
    mock_mode: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "pilot_origin", _validate_origin(self.pilot_origin))
        if len(self.csrf_hmac_key) < 32:
            raise ValueError("csrf_hmac_key must contain at least 32 random bytes.")
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
            # Mock mode exists only as an explicit test/development seam.  It is
            # never ready for the deployed pilot and the production service does
            # not select it as its default baker.
            mock_mode=os.environ.get("HRAMATKA_MOCK_MODE", "0") == "1",
        )
