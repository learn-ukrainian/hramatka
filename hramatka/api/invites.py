"""Local-only pilot invite lifecycle commands.

The raw invite is intentionally available only in this process at creation time.
It is printed once as a fragment URL and is never written to SQLite or returned by
another command.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .store import InviteRecord, JobStore


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a UUID") from error


def _pilot_origin() -> str:
    origin = os.environ.get("HRAMATKA_PILOT_ORIGIN")
    if not origin:
        raise RuntimeError("HRAMATKA_PILOT_ORIGIN must be set to print an invite link.")
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


def _store() -> JobStore:
    database_path = os.environ.get("HRAMATKA_DB_PATH")
    if not database_path:
        raise RuntimeError("HRAMATKA_DB_PATH must be set for an operator command.")
    store = JobStore(Path(database_path))
    store.initialize()
    return store


def _invite_payload(record: InviteRecord) -> dict[str, str | None]:
    redeemed_at = record.redeemed_at
    revoked_at = record.revoked_at
    if revoked_at is not None:
        state = "revoked"
    elif redeemed_at is not None:
        state = "redeemed"
    else:
        state = "available"
    return {
        "id": record.id,
        "teacher_id": record.teacher_id,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "redeemed_at": redeemed_at,
        "revoked_at": revoked_at,
        "state": state,
    }


def _emit(record: InviteRecord) -> None:
    print(json.dumps(_invite_payload(record), sort_keys=True))


def _hours(value: str) -> int:
    try:
        hours = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a whole number") from error
    if not 1 <= hours <= 168:
        raise argparse.ArgumentTypeError("must be between 1 and 168 hours")
    return hours


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local Hramatka pilot invites.")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create one one-use invite")
    create.add_argument("--teacher-id", required=True, type=_uuid)
    create.add_argument("--expires-in-hours", type=_hours, default=72)

    revoke = commands.add_parser("revoke", help="revoke one invite")
    revoke.add_argument("--invite-id", required=True, type=_uuid)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    store = _store()

    if args.command == "create":
        # Validate the destination before consuming a one-time random token.
        # A bad operator environment must not create an invite whose sole link
        # was never safely presented to the operator.
        origin = _pilot_origin()
        record, raw_token = store.create_invite(
            args.teacher_id, expires_in_hours=args.expires_in_hours
        )
        # This metadata contains no usable credential.  The next line is the
        # sole output containing the opaque token; do not add it to metadata,
        # errors, logging, or any inspection command.
        _emit(record)
        print(f"{origin}/teacher/#invite={raw_token}")
        return 0

    record = store.revoke_invite(args.invite_id)
    if record is None:
        parser.error("invite not found")
    _emit(record)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by python -m
    raise SystemExit(main())
