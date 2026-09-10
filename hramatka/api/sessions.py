"""Local-only pilot session lifecycle commands."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from .store import JobStore, SessionRecord


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a UUID") from error


def _store() -> JobStore:
    database_path = os.environ.get("HRAMATKA_DB_PATH")
    if not database_path:
        raise RuntimeError("HRAMATKA_DB_PATH must be set for an operator command.")
    store = JobStore(Path(database_path))
    store.initialize()
    return store


def _session_payload(record: SessionRecord) -> dict[str, str | None]:
    revoked_at = record.revoked_at
    return {
        "id": record.id,
        "teacher_id": record.teacher_id,
        "invite_id": record.invite_id,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "revoked_at": revoked_at,
        "state": "revoked" if revoked_at is not None else "active",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local Hramatka pilot sessions.")
    commands = parser.add_subparsers(dest="command", required=True)
    revoke = commands.add_parser("revoke", help="revoke one session")
    revoke.add_argument("--session-id", required=True, type=_uuid)
    revoke_all = commands.add_parser("revoke-all", help="revoke every session for one teacher")
    revoke_all.add_argument("--teacher-id", required=True, type=_uuid)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    store = _store()
    if args.command == "revoke-all":
        payload = {"revoked_sessions": store.revoke_teacher_sessions(args.teacher_id)}
        print(json.dumps(payload, sort_keys=True))
        return 0
    record = store.revoke_session(args.session_id)
    if record is None:
        parser.error("session not found")
    print(json.dumps(_session_payload(record), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by python -m
    raise SystemExit(main())
