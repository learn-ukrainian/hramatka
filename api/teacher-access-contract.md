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
3. The static bundle keeps the token in memory, creates a random browser-local entry
   nonce, and calls `POST /api/session/redeem` with `{ "token": "...", "nonce": "..." }`.
   It persists only that nonce (never the token) in `sessionStorage`, so the retry
   proof is deliberately bound to one browser tab rather than the whole browser. It
   persists the nonce long enough to retry a lost response,
   and in a `finally` path replaces the browser URL with
   `/teacher/` using `history.replaceState`, whether redemption succeeds or fails.
   It must not put the token in logs, telemetry, browser storage, query parameters,
   or application state snapshots.
4. Redemption requires an `Origin` header exactly equal to the configured HTTPS
   pilot origin. In one SQLite transaction the server verifies the invite digest,
   teacher state, expiry, revocation, and unused state; marks the invite redeemed;
   and inserts the session. It stores only a domain-separated nonce digest. If the
   commit succeeded but the response was lost, the same nonce can reissue a rotated
   session credential; a different nonce receives the generic unavailable response.
   Success is returned only after commit.
5. Unknown, malformed-but-schema-valid, used, expired, and revoked invites are
   indistinguishable: `410 invite_unavailable`. Schema/format failures are
   `422 invalid_input`. A persistence/commit failure is `503` and consumes nothing.

Invite records contain only a domain-separated SHA-256 digest of the random token,
never the raw token. Invites default to 72 hours, may be created for 1–168 hours, are
one-use, and can be revoked before redemption.

All 43-character token fields use the canonical unpadded base64url encoding of exactly
32 bytes. The server rejects encodings whose final character carries non-zero padding
bits, even when a permissive decoder could map them to the same bytes.

## Passkeys and recovery

After the first successful invite exchange, the active **invite-created** session may
register one or more WebAuthn passkeys. The server verifies the RP ID and exact
HTTPS origin, requires user verification, and stores only the credential ID, public
key, and signature counter. It never accepts a name, email address, or browser-supplied
teacher identity as authority to bind a first passkey.

`POST /api/passkeys/authentication` verifies an assertion and terminates in the same
ordinary cookie-session minting seam as invite redemption. It therefore has the same
seven-day absolute lifetime, idle renewal, HttpOnly `__Host-hramatka_session` cookie,
CSRF HMAC, rotation, and next-request revocation behaviour. A later Google OIDC door
must use this same seam.

Every enrollment ceremony challenge is bound to the requesting session, stored only
as a domain-separated digest, expires within one hour, and is atomically single-use.
Assertion challenges are likewise short-lived and single-use; they have no session yet
because their purpose is to establish one.

Registration shows ten recovery codes once. SQLite contains only purpose-separated
SHA-256 digests. A code establishes the same ordinary recovery session exactly once;
regeneration from a freshly authenticated session supersedes all unused prior codes.
If every passkey and recovery code is lost, the deliberate break-glass path is a new
72-hour operator-created invite. There is no automated reset, mail transport, or
email-address account field.

## Cookie session

A redeemed invite creates a fresh 32-byte random session secret. The explicitly
opted-in loopback local launcher may create the same ordinary session without an
invite exchange; that route is not registered unless its local-only guard is
complete. SQLite stores only
`SHA-256("hramatka-session\0" || secret)`. The raw secret exists only in the browser
cookie and transient request memory. Sessions have a fixed absolute time seven days
after redemption and a 24-hour idle deadline renewed by successful authenticated use.
Expired, idle-expired, revoked, unknown, or inactive-
teacher sessions all return `401 session_required` and never reveal which condition
matched.

The exact success cookie is:

```text
__Host-hramatka_session=<opaque>; Path=/; Max-Age=<remaining-absolute-seconds>; HttpOnly; Secure; SameSite=Lax
```

There is no `Domain` attribute. Logout commits `revoked_at` before returning `204` and
clears the cookie with the same attributes and `Max-Age=0`. Operators can use
`python -m hramatka.api.sessions revoke-all --teacher-id <uuid>` to revoke all of one
teacher's sessions without deactivating the teacher. All session and lesson API
responses carry `Cache-Control: no-store`.

`GET /api/session` returns teacher display metadata, absolute expiry, and a CSRF token.
The CSRF token is the unpadded base64url encoding of
`HMAC-SHA-256(server_csrf_key, raw_session_secret)`; it is not stored in SQLite and
must remain in browser memory. The server-side CSRF key is deployment secret material,
not repository or database data.

`GET /api/teacher/preferences` and `PUT /api/teacher/preferences` (P2-6) are owner-scoped
and follow identical CSRF/Origin/401/403/422/503 envelope rules as lesson mutations
from #113. `display_name` in the identity record remains read-only and server-owned;
no local editing surface is provided.

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
persistence failures; failed/cancelled bakes use the explicit in-place retry endpoint.

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
message. Teacher cancellation becomes the separate terminal `cancelled` disposition.
It is acknowledged at an exact revision; a repeated acknowledgement at the returned
revision is a no-op. A cancelled or timed-out attempt must first acknowledge worker
quiescence before an in-place retry; while that acknowledgement is pending, retry is a
safe `409 lesson_state_conflict` and no new provider call starts. A cancelled or failed
job may otherwise be retried in place at its exact terminal revision: it preserves the
lesson ID and exposes safe attempt history. DELETE refuses active or unquiesced work so
it cannot hide a provider call. Status returns these durable outcomes, never a partial
lesson and never mock-substituted output.
