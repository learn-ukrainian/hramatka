"""Migration v016: staff grants and one-use verified-email preauthorization."""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Add roles without changing any existing teacher, identity, or session row."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pilot_teachers'"
        ).fetchone()
        is None
    ):
        # Historical lesson-only migration fixtures have no identity tables.
        return
    connection.execute(
        """
        CREATE TABLE staff_authorization_grants (
            teacher_id TEXT PRIMARY KEY REFERENCES pilot_teachers(id),
            role TEXT NOT NULL CHECK (role IN ('admin', 'teacher')),
            created_at TEXT NOT NULL,
            revoked_at TEXT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX staff_authorization_grants_active_idx "
        "ON staff_authorization_grants(role, teacher_id) WHERE revoked_at IS NULL"
    )
    connection.execute(
        """
        CREATE TABLE google_email_preauthorizations (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL UNIQUE REFERENCES pilot_teachers(id),
            email_digest BLOB NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            consumed_at TEXT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX google_email_preauthorizations_pending_idx "
        "ON google_email_preauthorizations(email_digest) WHERE consumed_at IS NULL"
    )
