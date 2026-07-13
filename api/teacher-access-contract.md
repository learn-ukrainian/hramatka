# Frozen teacher access, session, and ownership contract

Status: **FROZEN for the pasted-text pilot**, 2026-07-13. This contract turns the
teacher-access design in bridge message `2629` (`sol-teacher-access-design`) into the
implementation boundary for the backend and teacher frontend. The wire surface is
`openapi.yaml`; this document freezes security and privacy behavior that is not fully
expressible in OpenAPI.

## Runtime and scope boundary

The deployment target is **Hetzner CX23**. The design is otherwise host-agnostic:
Caddy serves one static teacher bundle and proxies `/api/*` on the same HTTPS origin
to one FastAPI/Uvicorn process; SQLite lives on a persistent volume outside the
release directory. The teacher flow is not connected to GitHub Pages.

The pilot supports 3–5 invited teachers who paste non-sensitive Ukrainian text, run a
real bake, review/run the resulting lesson, acknowledge warnings, accept or return it
to draft, print in the browser, and download accepted JSON.

Out of scope: URL ingestion (`teacher-url`), students, assignments, public or shared
lesson URLs, collaborative editing, LMS integration, analytics, general accounts,
passwords/recovery, PostgreSQL, object storage, HA, platform abstractions, and a web
admin product.

## Invite exchange

1. An operator creates a `pilot_teachers` row and a one-use invite. Invite tokens are
   32 cryptographically random bytes encoded as 43 unpadded base64url characters.
2. The only invite-link form is
   `https://<pilot-origin>/teacher/#invite=<opaque-token>`. A fragment is never sent
   in an HTTP request or `Referer` header.
3. The static bundle keeps the token in memory, calls `POST /api/session/redeem` with
   `{ "token": "..." }`, and in a `finally` path replaces the browser URL with
   `/teacher/` using `history.replaceState`, whether redemption succeeds or fails.
   It must not put the token in logs, telemetry, browser storage, query parameters,
   or application state snapshots.
4. Redemption requires an `Origin` header exactly equal to the configured HTTPS
   pilot origin. In one SQLite transaction the server verifies the invite digest,
   teacher state, expiry, revocation, and unused state; marks the invite redeemed;
   and inserts the session. Success is returned only after commit.
5. Unknown, malformed-but-schema-valid, used, expired, and revoked invites are
   indistinguishable: `410 invite_unavailable`. Schema/format failures are
   `422 invalid_input`. A persistence/commit failure is `503` and consumes nothing.

Invite records contain only a domain-separated SHA-256 digest of the random token,
never the raw token. Invites default to 72 hours, may be created for 1–168 hours, are
one-use, and can be revoked before redemption.

All 43-character token fields use the canonical unpadded base64url encoding of exactly
32 bytes. The server rejects encodings whose final character carries non-zero padding
bits, even when a permissive decoder could map them to the same bytes.

## Cookie session

A redeemed invite creates a fresh 32-byte random session secret. SQLite stores only
`SHA-256("hramatka-session\0" || secret)`. The raw secret exists only in the browser
cookie and transient request memory. Sessions expire at a fixed absolute time seven
days after redemption; they do not slide. Expired, revoked, unknown, or inactive-
teacher sessions all return `401 session_required` and never reveal which condition
matched.

The exact success cookie is:

```text
__Host-hramatka_session=<opaque>; Path=/; Max-Age=604800; HttpOnly; Secure; SameSite=Lax
```

There is no `Domain` attribute. Logout commits `revoked_at` before returning `204` and
clears the cookie with the same attributes and `Max-Age=0`. All session and lesson API
responses carry `Cache-Control: no-store`.

`GET /api/session` returns teacher display metadata, absolute expiry, and a CSRF token.
The CSRF token is the unpadded base64url encoding of
`HMAC-SHA-256(server_csrf_key, raw_session_secret)`; it is not stored in SQLite and
must remain in browser memory. The server-side CSRF key is deployment secret material,
not repository or database data.

## Same-origin and CSRF rules

CORS is disabled. No `/api/*` response emits `Access-Control-Allow-Origin`. Every
`POST` and `DELETE` requires an `Origin` header exactly equal to the configured origin;
missing, `null`, HTTP, sibling-subdomain, or otherwise different origins return
`403 csrf_rejected`. Authenticated mutations additionally require `X-CSRF-Token`
matching the current session-derived token in constant time. The redeem mutation has
no session yet, so exact-Origin validation is its CSRF control.

Mutation content type is `application/json`. Cookies plus `SameSite=Lax` are defense in
depth, not substitutes for Origin and CSRF validation.

## Authentication boundary

The browser surface in `openapi.yaml` accepts only the pilot session cookie. The
existing shared bearer credential is server/ops-only, never embedded in the static
bundle, browser JavaScript, HTML, local storage, logs, or API examples. It is not an
alternative authentication scheme for any frozen `/api/*` route. If retained during
migration, it may authenticate loopback/private operational endpoints outside this
spec only; it cannot bypass teacher ownership.

## Ownership and non-disclosure

Every lesson read and write is scoped by the authenticated teacher in the SQL query,
not by a post-query authorization check. The required predicate is equivalent to
`WHERE teacher_id = :session_teacher_id AND id = :lesson_id`. The catalog uses only
`WHERE teacher_id = :session_teacher_id`. Jobs are never transferred between teachers.

A missing lesson and another teacher's lesson are indistinguishable:
`404 lesson_not_found`. The same rule applies to status, get, warning acknowledgement,
accept, and draft. A teacher may create the same UUID value independently because the
durable idempotency key is `(teacher_id, lesson_id)`.

The browser may receive only its teacher identity, its own original pasted anchor,
materialized lesson, answer keys, warnings/rejected blocks, required provenance, and
acceptance metadata. It must never receive another teacher's data, internal IR, DB
paths, shared bearer credentials, model credentials, token digests, raw provider
responses, or debug traces.

Do not log request bodies, anchor excerpts, lesson JSON, answer keys, cookies, invite
tokens, CSRF tokens, or authorization headers. No third-party analytics or assets are
allowed. Before submission, the UI must disclose that pasted text is sent to the
configured external Gemma provider and prohibit sensitive or personal text.

## Operator-only lifecycle

There is no admin website. The implementation must expose these local/SSH operator CLI
capabilities (command spelling may be wrapped by deployment tooling, semantics may not):

```text
python -m hramatka.api.teachers create --display-name <label>
python -m hramatka.api.invites create --teacher-id <uuid> [--expires-in-hours 72]
python -m hramatka.api.invites revoke --invite-id <uuid>
python -m hramatka.api.sessions revoke --session-id <uuid>
python -m hramatka.api.teachers deactivate --teacher-id <uuid>
```

Invite creation prints the fragment link exactly once. Subsequent inspection shows
only IDs, timestamps, and state, never a usable token. Deactivating a teacher revokes
all unredeemed invites and active sessions in the same transaction.

## Frozen error semantics

Every non-empty error response is exactly
`{code, message, retryable, lesson_id?}` with no additional properties. Messages are
safe teacher-facing text. `retryable` is true only for transient service/readiness or
persistence failures; retrying a failed bake creates a new browser UUID.

| HTTP | Required code(s) | Meaning |
|---|---|---|
| 401 | `session_required` | Missing/invalid/expired/revoked session |
| 403 | `csrf_rejected` | Origin or CSRF enforcement failed |
| 404 | `lesson_not_found`, `warning_block_not_found` | Owner-hidden lesson/block absence |
| 409 | `idempotency_conflict`, `lesson_not_ready`, `revision_conflict`, `lesson_state_conflict`, `warning_acknowledgements_required` | Durable state conflict; never auto-resolved by overwriting |
| 410 | `invite_unavailable` | Unknown/used/expired/revoked invite |
| 422 | `invalid_input` | Invalid JSON/fields/UUID, whitespace paste, paste over 100,000 code points, or `teacher-url` |
| 503 | `persistence_unavailable`, `service_not_ready` | No success before durable commit/readiness |

Bake timeout, API restart while baking, provider failure, engine failure, and public-
schema failure become a durable `failed` job with a sanitized `failure_code` and
message. They are returned by the status resource, not as a partial lesson and never
by substituting the mock baker.
