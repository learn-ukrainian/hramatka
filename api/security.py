"""Small, auditable helpers for opaque pilot credentials and CSRF proof."""

from __future__ import annotations

import base64
import hashlib
import hmac


def token_digest(domain: bytes, raw: bytes) -> bytes:
    """Hash an opaque random secret under a purpose-specific domain separator."""
    return hashlib.sha256(domain + b"\0" + raw).digest()


def csrf_token(server_key: bytes, raw_session_secret: bytes) -> str:
    return (
        base64.urlsafe_b64encode(hmac.new(server_key, raw_session_secret, hashlib.sha256).digest())
        .decode("ascii")
        .rstrip("=")
    )


def csrf_matches(server_key: bytes, raw_session_secret: bytes, supplied: str | None) -> bool:
    return supplied is not None and hmac.compare_digest(
        csrf_token(server_key, raw_session_secret), supplied
    )
