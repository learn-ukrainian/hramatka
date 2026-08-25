# Frozen SQLite persistence contract

Status: **FROZEN for the pasted-text pilot**, 2026-07-13. SQLite is the sole
datastore. There is no PostgreSQL, object store, public lesson URL, or second source of
truth. The job row is the authoritative job/lesson aggregate.

## Process and database configuration

One FastAPI/Uvicorn process owns one in-process bake runner. Multiple Uvicorn workers
are forbidden because they would duplicate the runner. The SQLite file and WAL live on
a persistent volume outside the release directory. Every connection enables:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
```

Migrations are ordered, versioned, and transactional. `/api/readyz` is ready only when
the DB opens read/write, foreign keys are enabled, the expected schema version is
current, and the deployed baker is the real `EngineLessonBaker` with mock mode off.

## Required tables

Names are frozen; implementations may add indexes and non-secret operational columns
without changing wire behavior.

### `pilot_teachers`

| Column | Contract |
|---|---|
| `id TEXT PRIMARY KEY` | UUID |
| `display_name TEXT NOT NULL` | 1–100 characters; pilot label, not an account login |
| `created_at TEXT NOT NULL` | UTC RFC 3339 |
| `deactivated_at TEXT NULL` | Non-null disables invites and sessions |

### `pilot_invites`

| Column | Contract |
|---|---|
| `id TEXT PRIMARY KEY` | UUID shown to operators |
| `teacher_id TEXT NOT NULL` | FK to `pilot_teachers(id)` |
| `token_hash BLOB NOT NULL UNIQUE` | Domain-separated SHA-256 digest only |
| `created_at`, `expires_at TEXT NOT NULL` | UTC RFC 3339; expiry 1–168 hours |
| `redeemed_at TEXT NULL` | Set exactly once in the session-creation transaction |
| `revoked_at TEXT NULL` | Operator revocation timestamp |

Raw invite tokens are never stored. An invite is usable only when the teacher is active
and `redeemed_at IS NULL AND revoked_at IS NULL AND expires_at > now`.

### `pilot_sessions`

| Column | Contract |
|---|---|
| `id TEXT PRIMARY KEY` | UUID shown to operators; never used as browser credential |
| `teacher_id TEXT NOT NULL` | FK to `pilot_teachers(id)` |
| `invite_id TEXT NULL UNIQUE` | Invite-created sessions retain the FK; guarded local sessions and passkey/recovery re-entry have none |
| `secret_hash BLOB NOT NULL UNIQUE` | Domain-separated SHA-256 digest only |
| `redeem_nonce_hash BLOB NULL` | Domain-separated browser retry-proof digest; no raw nonce |
| `created_at`, `expires_at TEXT NOT NULL` | Seven-day absolute session window |
| `idle_expires_at`, `last_seen_at TEXT NULL` | Additive v006 columns: migration immediately backfills both from the original session lifetime; new/reissued sessions set a 24-hour renewable idle window and last authenticated use |
| `revoked_at TEXT NULL` | Browser logout/operator/teacher-deactivation revocation |
| `auth_method TEXT NOT NULL` | `invite`, `local`, `passkey`, or `recovery`; audit only, never browser authority |

Raw session and CSRF secrets are never stored. Session lookup hashes the presented
cookie and joins the teacher row so absolute expiry, idle expiry, revocation, and
deactivation are one authorization decision. A committed first exchange can reissue
a fresh secret only when the presented invite and nonce digest match the same row.
The same-nonce recovery is valid until absolute expiry even if the original idle
deadline passed; it renews the idle window, capped at absolute expiry. A NULL idle
deadline is malformed legacy state and must not reissue a credential.

### `webauthn_credentials`, `webauthn_challenges`, and `recovery_codes`

Credentials retain only a teacher FK, public credential ID, public COSE key, sign
counter, and lifecycle timestamps. Enrollment and assertion challenges retain only a
purpose-separated digest, ceremony kind, expiry, used marker, and (for enrollment)
the requesting session binding. Recovery rows retain only a teacher FK, purpose-
separated code digest, and used/superseded timestamps. No raw passkey response,
challenge, recovery code, email address, or private key enters SQLite.

### `teacher_preferences`

Per-teacher operational preferences (P2-6). Not part of identity; display_name
remains the sole pilot label in `pilot_teachers`.

| Column | Contract |
|---|---|
| `teacher_id TEXT PRIMARY KEY` | FK to `pilot_teachers(id)`; owner scope |
| `default_duration INTEGER NOT NULL` | Historical rows may contain 45 \| 60 \| 90. The API always returns 45 and only accepts 45 for new-lesson preferences. |
| `updated_at TEXT NOT NULL` | UTC RFC 3339 |

### `lesson_jobs`

| Column | Contract |
|---|---|
| `teacher_id TEXT NOT NULL` | FK; first half of owner/idempotency key |
| `id TEXT NOT NULL` | Browser UUID; second half of `PRIMARY KEY (teacher_id, id)` |
| `request_json TEXT NOT NULL` | Canonical original paste request, including text/source/level/duration/focus |
| `request_hash BLOB NOT NULL` | SHA-256 of canonical UTF-8 `request_json` |
| `status TEXT NOT NULL` | `draft`, `baking`, `ready`, `failed`, or terminal `cancelled` |
| `step TEXT NOT NULL` | Frozen status step from OpenAPI |
| `failure_code`, `failure_message TEXT NULL` | Sanitized durable failure only; no raw exception/provider text |
| `lesson_json TEXT NULL` | Validated pilot-constrained `lu.lesson.v1`; never a partial lesson |
| `warning_acknowledgements_json TEXT NOT NULL` | Canonical sorted unique block-ID array, default `[]` |
| `accepted INTEGER NOT NULL` | Boolean current acceptance state; when a lesson is materialized, must equal `lesson_json.accepted` |
| `accepted_at TEXT NULL` | UTC RFC 3339 for current acceptance |
| `accepted_revision INTEGER NULL` | Exact revision produced by current acceptance |
| `revision INTEGER NOT NULL` | Starts at 1 and increases by exactly 1 per durable aggregate transition |
| `created_at`, `updated_at TEXT NOT NULL` | UTC RFC 3339 |
| `started_at`, `completed_at TEXT NULL` | Bake lifecycle timestamps |
| `attempt INTEGER NOT NULL` | Starts at 1; increments only when a failed/cancelled aggregate is retried in place |
| `attempt_history_json TEXT NOT NULL` | Canonical safe terminal history, including the current attempt once terminal; no provider text or lesson content |
| `retry_requested_revision INTEGER NULL` | Durable source revision of the current retry intent, used to make lost-response retries idempotent |
| `attempt_token TEXT NULL` | Immutable opaque claim token; every worker publication and telemetry write predicates on it |
| `provider_started_at TEXT NULL` | Durable provider-call lease linearization point for the current claim |
| `attempt_quiesced_at TEXT NULL` | Present only once the current terminal worker/provider attempt has exited; retry and DELETE require it |

The canonical request is exactly the OpenAPI request after JSON validation: object keys
sorted; compact UTF-8 JSON; `focus` present as `null` when omitted; anchor source fixed
to `teacher-paste`. Same owner/id and equal hash returns the aggregate (`202`,
`reused: true`) without scheduling a second bake or changing revision. Same owner/id
and different hash is `409 idempotency_conflict`. Another teacher's identical UUID is
a distinct key and reveals nothing.

## Transaction and revision rules

Every successful state change commits all affected columns before the HTTP success is
sent. A SQLite read/write/commit error is `503 persistence_unavailable`; it is never
translated into success. No mutation silently retries a stale semantic write.

Session lookup renews the idle deadline in a `BEGIN IMMEDIATE` transaction. SQLite has
a single writer, so authenticated teacher traffic serializes at that point; this is an
accepted one-teacher-pilot constraint, not a general multi-teacher scaling design.

All aggregate transitions increment `revision` exactly once, including runner claim,
step change, ready/failure completion, a newly persisted warning acknowledgement,
lesson acceptance, and return to draft. Repeating an already-recorded warning
acknowledgement with the current revision is a successful idempotent no-op: it returns
the current revision and does not update the row or timestamp. A stale expected revision
returns `409 revision_conflict` except for the documented durable replay keys of cancel
and retry.

Cancellation is an owner-scoped `BEGIN IMMEDIATE` compare-and-swap at
`expected_revision`. It applies only to `draft` or `baking`, changes the aggregate to
terminal `cancelled`, records a safe `cancelled` disposition in attempt history, and
increments revision once. Repeating the acknowledgement with the returned revision is
an idempotent no-op. Completion and failure writes predicate on `status = 'baking'`,
so a cancellation committed first can never publish a result. DELETE refuses active
jobs; it never removes a row while provider work may still be in flight. Every worker
claim receives an immutable attempt token. A provider call first acquires an atomic,
attempt-bound lease that races with cancellation: if cancellation commits first, no
call starts; if the lease commits first, cancellation truthfully treats that call as
already started and waits for the same token to acknowledge quiescence before DELETE.
Every terminal provider attempt (including a timeout) retains that quiescence marker;
retry returns `409 lesson_state_conflict` until it is present, so a newer attempt never
overlaps or forgets an older worker/provider call. Queued cancellation has no worker
token and may delete immediately.

Retry is one in-place transition from `failed` or `cancelled` to `draft` at the exact
revision. It retains the job ID, archives the terminal current attempt, increments
`attempt`, and records `retry_requested_revision`. Replaying that same source revision
returns the same current attempt without adding another queue entry, including after a
worker has claimed it. A later terminal revision may start one subsequent attempt.

Every browser lesson mutation body supplies `expected_revision`. The update predicate
must include owner, lesson ID, state, and revision. Zero updated rows are resolved with
an owner-scoped lookup only: absent gives `404`; present-but-stale or wrong-state gives
`409`. The server never applies an accept or draft request to a later revision.

Warning acknowledgement is one `BEGIN IMMEDIATE` transaction: load the owner-scoped
ready lesson at `expected_revision`, verify the block is visible and marked `warn`, add
its ID to the canonical set, and update revision/timestamp. A present non-ready lesson
returns `409 lesson_state_conflict`; an absent or other-owner lesson returns
`404 lesson_not_found`; only an absent/non-warning block in a present ready lesson
returns `404 warning_block_not_found`.

Lesson acceptance is one `BEGIN IMMEDIATE` transaction: load the owner-scoped ready
lesson at `expected_revision`; derive every visible warning block; require the exact
set to be acknowledged; set `accepted = 1` in both aggregate metadata and lesson JSON;
increment revision; set `accepted_revision` to that new revision and `accepted_at` to
the commit timestamp. Unresolved warnings return
`409 warning_acknowledgements_required` without changing any row.

Return-to-draft is one `BEGIN IMMEDIATE` transaction at `expected_revision`: require a
currently accepted ready lesson; set `accepted = 0` in metadata and lesson JSON; clear
`accepted_at` and `accepted_revision`; increment revision and timestamp. This endpoint
does not discard the validated lesson or warning acknowledgements.

The real baker validates the complete lesson against the digest-pinned public contract
and the private nine-type constraint before the completion transaction. Workers check
the durable baking state before provider/phase work, after provider return, and before
complete/fail publication. On validation
failure, it persists only `failed`, a safe failure code/message, and timestamps—never
the partial document or internal IR. Mock output is never substituted.

## Catalog and owner-scoped reads

Every single-lesson query includes both keys:

```sql
SELECT ... FROM lesson_jobs WHERE teacher_id = ? AND id = ?;
```

The complete pilot catalog query is:

```sql
SELECT <catalog columns>
FROM lesson_jobs
WHERE teacher_id = ?
ORDER BY updated_at DESC, id DESC;
```

It returns metadata only; it does not return another teacher's rows, original paste,
lesson JSON, answer keys, internal IR, or provider/debug payloads. The single-lesson
resource may return the authenticated owner's original anchor through the validated
lesson document.

## Operational durability boundary

SQLite-consistent daily off-host backup and a demonstrated clean restore are launch
gates from message 2629, not alternate persistence paths. Restoring must reproduce the
teacher owner, original request, accepted lesson JSON, warning acknowledgements,
acceptance metadata, and exact revision. No database file, backup, raw paste, or secret
is committed to this repository.
