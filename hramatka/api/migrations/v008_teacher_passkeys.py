"""Migration v008: durable passkey and recovery-code records.

Runs after v007's local-static table rewrite, so every database reaching this
migration has durable session columns and a nullable invite id.  Existing
local-only sessions retain their distinct audit method when the table is
rebuilt.
"""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Keep existing invite sessions intact while allowing non-invite re-entry."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pilot_sessions'"
        ).fetchone()
        is None
    ):
        # Historical lesson-only migration fixtures contain no pilot access data.
        return
    connection.execute("ALTER TABLE pilot_sessions RENAME TO pilot_sessions_v007")
    connection.execute(
        """
        CREATE TABLE pilot_sessions (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            invite_id TEXT NULL UNIQUE REFERENCES pilot_invites(id),
            secret_hash BLOB NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT NULL,
            redeem_nonce_hash BLOB NULL,
            idle_expires_at TEXT NULL,
            last_seen_at TEXT NULL,
            auth_method TEXT NOT NULL
                CHECK (auth_method IN ('invite', 'local', 'passkey', 'recovery'))
        )
        """
    )
    connection.execute(
        """
        INSERT INTO pilot_sessions (
          id, teacher_id, invite_id, secret_hash, created_at, expires_at, revoked_at,
          redeem_nonce_hash, idle_expires_at, last_seen_at, auth_method
        )
        SELECT id, teacher_id, invite_id, secret_hash, created_at, expires_at, revoked_at,
               redeem_nonce_hash, idle_expires_at, last_seen_at,
               CASE WHEN invite_id IS NULL THEN 'local' ELSE 'invite' END
        FROM pilot_sessions_v007
        """
    )
    connection.execute("DROP TABLE pilot_sessions_v007")
    connection.execute("CREATE INDEX pilot_sessions_teacher_idx ON pilot_sessions(teacher_id)")
    connection.execute(
        "CREATE INDEX pilot_sessions_redeem_nonce_idx "
        "ON pilot_sessions(invite_id, redeem_nonce_hash)"
    )
    connection.execute(
        """
        CREATE TABLE webauthn_credentials (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            credential_id BLOB NOT NULL UNIQUE,
            public_key BLOB NOT NULL,
            sign_count INTEGER NOT NULL CHECK (sign_count >= 0),
            created_at TEXT NOT NULL,
            last_used_at TEXT NULL,
            revoked_at TEXT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX webauthn_credentials_teacher_idx ON webauthn_credentials(teacher_id)"
    )
    connection.execute(
        """
        CREATE TABLE webauthn_challenges (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NULL REFERENCES pilot_teachers(id),
            session_id TEXT NULL REFERENCES pilot_sessions(id),
            kind TEXT NOT NULL CHECK (kind IN ('enrollment', 'assertion')),
            challenge_hash BLOB NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX webauthn_challenges_expiry_idx ON webauthn_challenges(expires_at)"
    )
    connection.execute(
        """
        CREATE TABLE recovery_codes (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            code_hash BLOB NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            used_at TEXT NULL,
            superseded_at TEXT NULL
        )
        """
    )
    connection.execute("CREATE INDEX recovery_codes_teacher_idx ON recovery_codes(teacher_id)")
