"""Migration v007: allow a local-only session without an invite row.

The deployed invite path remains unchanged: its session rows still retain their
invite id.  Only the separately guarded local launcher may create the nullable
form, and the existing foreign key remains in force whenever an id is present.
"""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Recreate the session table while preserving every existing session row."""
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pilot_sessions'"
    ).fetchone()
    if existing is None:
        _create_sessions_table(connection, "pilot_sessions")
        _create_sessions_index(connection)
        return
    _ensure_durable_session_columns(connection)
    _create_sessions_table(connection, "pilot_sessions_new")
    connection.execute(
        """
        INSERT INTO pilot_sessions_new (
            id, teacher_id, invite_id, secret_hash, redeem_nonce_hash,
            created_at, expires_at, idle_expires_at, last_seen_at, revoked_at
        )
        SELECT id, teacher_id, invite_id, secret_hash, redeem_nonce_hash,
               created_at, expires_at, idle_expires_at, last_seen_at, revoked_at
        FROM pilot_sessions
        """
    )
    connection.execute("DROP TABLE pilot_sessions")
    connection.execute("ALTER TABLE pilot_sessions_new RENAME TO pilot_sessions")
    _create_sessions_index(connection)


def _ensure_durable_session_columns(connection: sqlite3.Connection) -> None:
    """Bridge a database created by this branch before v006 was renumbered.

    Such a database recorded the old local-static migration as version 6.  The
    merged registry reserves v006 for durable sessions, so its normal apply
    loop correctly skips that already-recorded number.  Bring that historical
    table to v006 shape before the v007 table rewrite copies its columns.
    """
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(pilot_sessions)").fetchall()
    }
    additions = (
        ("redeem_nonce_hash", "BLOB NULL"),
        ("idle_expires_at", "TEXT NULL"),
        ("last_seen_at", "TEXT NULL"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.execute(f"ALTER TABLE pilot_sessions ADD COLUMN {name} {definition}")
    connection.execute(
        """
        UPDATE pilot_sessions
        SET idle_expires_at = expires_at, last_seen_at = created_at
        WHERE idle_expires_at IS NULL OR last_seen_at IS NULL
        """
    )


def _create_sessions_table(connection: sqlite3.Connection, name: str) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {name} (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            invite_id TEXT NULL UNIQUE REFERENCES pilot_invites(id),
            secret_hash BLOB NOT NULL UNIQUE,
            redeem_nonce_hash BLOB NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            idle_expires_at TEXT NULL,
            last_seen_at TEXT NULL,
            revoked_at TEXT NULL
        )
        """
    )


def _create_sessions_index(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS pilot_sessions_teacher_idx ON pilot_sessions(teacher_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS pilot_sessions_redeem_nonce_idx "
        "ON pilot_sessions(invite_id, redeem_nonce_hash)"
    )
