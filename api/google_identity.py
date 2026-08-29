"""Small, fail-closed verifier for Google Identity Services ID tokens.

This module accepts no browser session, does not create accounts, and never
returns or persists a raw Google credential.  The API layer binds a verified
identity to the pilot's existing opaque cookie-session seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from .config import Settings

_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})


class GoogleCredentialInvalid(ValueError):
    """The Google credential cannot establish a pilot identity."""


@dataclass(frozen=True)
class VerifiedGoogleIdentity:
    subject: str
    email: str
    nonce: str


@cache
def _jwks_client(url: str) -> PyJWKClient:
    # PyJWT honors cache headers internally; this cache only ensures one client
    # object per fixed issuer-owned URL for the process lifetime.
    return PyJWKClient(url, cache_keys=True, lifespan=300)


def verify_google_credential(credential: str, settings: Settings) -> VerifiedGoogleIdentity:
    """Verify Google's signed ID token against the exact configured audience."""
    if not settings.google_auth_enabled or settings.google_client_id is None:
        raise GoogleCredentialInvalid("Google sign-in is unavailable.")
    if not isinstance(credential, str) or not credential or len(credential) > 16_384:
        raise GoogleCredentialInvalid("Invalid Google credential.")
    try:
        key = _jwks_client(settings.google_jwks_url).get_signing_key_from_jwt(credential)
        claims: dict[str, Any] = jwt.decode(
            credential,
            key.key,
            algorithms=["RS256"],
            audience=settings.google_client_id,
            issuer=list(_GOOGLE_ISSUERS),
            options={
                "require": ["aud", "email", "email_verified", "exp", "iat", "iss", "nonce", "sub"],
                "verify_iat": True,
            },
            leeway=60,
        )
    except (InvalidTokenError, PyJWKClientError, ValueError, TypeError) as error:
        raise GoogleCredentialInvalid("Invalid Google credential.") from error

    subject = claims.get("sub")
    email = claims.get("email")
    nonce = claims.get("nonce")
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > 255
        or not isinstance(email, str)
        or not email
        or len(email) > 320
        or claims.get("email_verified") is not True
        or not isinstance(nonce, str)
        or len(nonce) > 128
    ):
        raise GoogleCredentialInvalid("Invalid Google credential.")
    return VerifiedGoogleIdentity(subject=subject, email=email, nonce=nonce)
