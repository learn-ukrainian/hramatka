"""Migration v006: add bounded, retry-safe teacher session metadata."""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Add only nullable columns so existing durable sessions remain valid."""
    session_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pilot_sessions'"
    ).fetchone()
    # Historical migration fixtures intentionally model only the tables relevant
    # to their lesson-data assertion. There is no pilot session data to upgrade.
    if session_table is None:
        return
    connection.execute("ALTER TABLE pilot_sessions ADD COLUMN redeem_nonce_hash BLOB NULL")
    connection.execute("ALTER TABLE pilot_sessions ADD COLUMN idle_expires_at TEXT NULL")
    connection.execute("ALTER TABLE pilot_sessions ADD COLUMN last_seen_at TEXT NULL")
    # Existing sessions retain their original absolute deadline as their initial
    # idle deadline; their first successful request establishes the new window.
    connection.execute(
        """
        UPDATE pilot_sessions
        SET idle_expires_at = expires_at, last_seen_at = created_at
        WHERE idle_expires_at IS NULL OR last_seen_at IS NULL
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS pilot_sessions_redeem_nonce_idx "
        "ON pilot_sessions(invite_id, redeem_nonce_hash)"
    )
