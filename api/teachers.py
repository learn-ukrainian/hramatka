"""Local-only pilot teacher lifecycle commands.

Run these commands only through a trusted shell on the API host.  They operate
on the durable SQLite database directly and deliberately have no HTTP route.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from .store import JobStore, TeacherRecord


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


def _teacher_payload(record: TeacherRecord) -> dict[str, str | None]:
    deactivated_at = record.deactivated_at
    return {
        "id": record.id,
        "display_name": record.display_name,
        "created_at": record.created_at,
        "deactivated_at": deactivated_at,
        "state": "deactivated" if deactivated_at is not None else "active",
    }


def _emit(record: TeacherRecord) -> None:
    print(json.dumps(_teacher_payload(record), ensure_ascii=False, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local Hramatka pilot teachers.")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create an active teacher")
    create.add_argument("--display-name", required=True, help="pilot label (1–100 characters)")

    deactivate = commands.add_parser("deactivate", help="deactivate a teacher and revoke access")
    deactivate.add_argument("--teacher-id", required=True, type=_uuid)

    bootstrap_admin = commands.add_parser(
        "bootstrap-admin",
        help="grant admin to an existing Google-linked teacher",
    )
    bootstrap_admin.add_argument("--teacher-id", required=True, type=_uuid)

    grant = commands.add_parser(
        "grant",
        help="preauthorize an existing teacher for Google-bound staff access",
    )
    grant.add_argument("--teacher-id", required=True, type=_uuid)
    grant.add_argument("--role", required=True, choices=("admin", "teacher"))

    preauthorize = commands.add_parser(
        "preauthorize-google",
        help="reserve one verified Google email for a named staff member",
    )
    subject = preauthorize.add_mutually_exclusive_group(required=True)
    subject.add_argument("--teacher-id", type=_uuid, help="existing active teacher")
    subject.add_argument("--display-name", help="create this approved teacher atomically")
    preauthorize.add_argument("--email", required=True, help="verified Google account email")
    preauthorize.add_argument("--role", required=True, choices=("admin", "teacher"))

    revoke_grant = commands.add_parser(
        "revoke-grant",
        help="revoke an explicit staff authorization grant",
    )
    revoke_grant.add_argument("--teacher-id", required=True, type=_uuid)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    store = _store()

    if args.command == "create":
        _emit(store.create_teacher(args.display_name))
        return 0

    if args.command == "bootstrap-admin":
        store.grant_staff_role(args.teacher_id, "admin", require_google_identity=True)
        record = store.get_teacher(args.teacher_id)
        assert record is not None
        _emit(record)
        return 0

    if args.command == "grant":
        # Admin grants are also bound to an already verified Google identity;
        # teachers may be preauthorized before their one-time Google link.
        store.grant_staff_role(
            args.teacher_id,
            args.role,
            require_google_identity=args.role == "admin",
        )
        record = store.get_teacher(args.teacher_id)
        assert record is not None
        _emit(record)
        return 0

    if args.command == "preauthorize-google":
        if args.teacher_id is not None:
            store.preauthorize_google_email(args.teacher_id, args.email, args.role)
            record = store.get_teacher(args.teacher_id)
            assert record is not None
        else:
            record = store.create_preauthorized_google_teacher(
                args.display_name, args.email, args.role
            )
        _emit(record)
        return 0

    if args.command == "revoke-grant":
        if not store.revoke_staff_role(args.teacher_id):
            parser.error("active staff grant not found")
        record = store.get_teacher(args.teacher_id)
        assert record is not None
        _emit(record)
        return 0

    record = store.deactivate_teacher(args.teacher_id)
    if record is None:
        parser.error("teacher not found")
    _emit(record)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by python -m
    raise SystemExit(main())
