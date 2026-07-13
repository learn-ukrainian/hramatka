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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    store = _store()

    if args.command == "create":
        _emit(store.create_teacher(args.display_name))
        return 0

    record = store.deactivate_teacher(args.teacher_id)
    if record is None:
        parser.error("teacher not found")
    _emit(record)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by python -m
    raise SystemExit(main())
