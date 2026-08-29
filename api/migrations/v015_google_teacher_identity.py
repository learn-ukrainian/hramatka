"""Migration v015: Google teacher identities and one-use login nonces."""

from __future__ import annotations

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    """Add Google identity state without changing a teacher or lesson owner id."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pilot_sessions'"
        ).fetchone()
        is None
    ):
        # Historical lesson-only migration fixtures have no access tables.
        return

    # ``webauthn_challenges.session_id`` references this table. Rename that
    # child first, then rebuild it after the new parent exists; otherwise
    # SQLite rewrites its FK to the temporary table name and breaks passkeys.
    has_webauthn_challenges = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'webauthn_challenges'"
        ).fetchone()
        is not None
    )
    if has_webauthn_challenges:
        connection.execute("ALTER TABLE webauthn_challenges RENAME TO webauthn_challenges_v014")

    # SQLite cannot alter a CHECK constraint. Preserve every existing session
    # verbatim while adding ``google`` as another audited re-entry method.
    connection.execute("ALTER TABLE pilot_sessions RENAME TO pilot_sessions_v014")
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
            auth_method TEXT NOT NULL CHECK (
                auth_method IN ('invite', 'local', 'passkey', 'recovery', 'google')
            )
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
               redeem_nonce_hash, idle_expires_at, last_seen_at, auth_method
        FROM pilot_sessions_v014
        """
    )
    connection.execute("DROP TABLE pilot_sessions_v014")
    connection.execute("CREATE INDEX pilot_sessions_teacher_idx ON pilot_sessions(teacher_id)")
    connection.execute(
        "CREATE INDEX pilot_sessions_redeem_nonce_idx "
        "ON pilot_sessions(invite_id, redeem_nonce_hash)"
    )
    if has_webauthn_challenges:
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
            """
            INSERT INTO webauthn_challenges
            (id, teacher_id, session_id, kind, challenge_hash, expires_at, used_at)
            SELECT id, teacher_id, session_id, kind, challenge_hash, expires_at, used_at
            FROM webauthn_challenges_v014
            """
        )
        connection.execute("DROP TABLE webauthn_challenges_v014")
        connection.execute(
            "CREATE INDEX webauthn_challenges_expiry_idx ON webauthn_challenges(expires_at)"
        )

    connection.execute(
        """
        CREATE TABLE google_teacher_identities (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL REFERENCES pilot_teachers(id),
            subject TEXT NOT NULL UNIQUE,
            email_at_link TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT NULL,
            revoked_at TEXT NULL,
            UNIQUE (teacher_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX google_teacher_identities_teacher_idx "
        "ON google_teacher_identities(teacher_id)"
    )
    connection.execute(
        """
        CREATE TABLE google_login_nonces (
            id TEXT PRIMARY KEY,
            nonce_hash BLOB NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK (kind IN ('signin', 'link')),
            teacher_id TEXT NULL REFERENCES pilot_teachers(id),
            session_id TEXT NULL REFERENCES pilot_sessions(id),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX google_login_nonces_expiry_idx ON google_login_nonces(expires_at)"
    )
