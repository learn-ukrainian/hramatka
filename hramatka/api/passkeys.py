"""Small WebAuthn adapter; all ceremony state is single-use and short-lived."""

from __future__ import annotations

import json
from urllib.parse import urlsplit
from uuid import UUID

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json
from webauthn.helpers.structs import AuthenticatorSelectionCriteria, UserVerificationRequirement


def rp_id(origin: str) -> str:
    host = urlsplit(origin).hostname
    if host is None:
        raise ValueError("pilot origin must be an HTTPS origin")
    return host


def enrollment_options(
    *, origin: str, teacher_id: str, display_name: str, challenge: bytes
) -> dict:
    options = generate_registration_options(
        rp_id=rp_id(origin),
        rp_name="Hramatka",
        user_id=UUID(teacher_id).bytes,
        user_name=teacher_id,
        user_display_name=display_name,
        challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    return json.loads(options_to_json(options))


def assertion_options(*, origin: str, challenge: bytes) -> dict:
    return json.loads(
        options_to_json(
            generate_authentication_options(
                rp_id=rp_id(origin),
                challenge=challenge,
                user_verification=UserVerificationRequirement.REQUIRED,
            )
        )
    )


def verify_registration(*, credential: dict, challenge: bytes, origin: str):
    return verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id(origin),
        expected_origin=origin,
        require_user_verification=True,
    )


def verify_assertion(
    *, credential: dict, challenge: bytes, origin: str, public_key: bytes, sign_count: int
):
    return verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id(origin),
        expected_origin=origin,
        credential_public_key=public_key,
        credential_current_sign_count=sign_count,
        require_user_verification=True,
    )
