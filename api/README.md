# Hramatka teacher-pilot API

This package implements the frozen, pasted-text-only teacher pilot. The browser
contract is [`openapi.yaml`](openapi.yaml); access, privacy, cookie, CSRF, and
operator lifecycle requirements are frozen in
[`teacher-access-contract.md`](teacher-access-contract.md); SQLite durability and
aggregate rules are frozen in
[`persistence-contract.md`](persistence-contract.md).

The browser uses only the `__Host-hramatka_session` cookie, normally issued through a
one-use invite exchange. There is no browser bearer-token path, no CORS, and no
public/admin HTTP route for lifecycle operations. The sole exception is the local
launcher-only `/api/session/local-teacher` route: it is not registered unless the
explicit opt-in, launcher marker, and unambiguous loopback binding all hold. It is
intentionally absent from the shipped OpenAPI contract and production deployment.
Every browser mutation requires the configured exact `Origin` and the current
session-derived CSRF token.

## Configuration

The API process requires these environment variables; keep their values in a
root-owned host secret file or another deployment secret store, never in this
repository:

| Variable | Meaning |
| --- | --- |
| `HRAMATKA_DB_PATH` | SQLite file on the persistent volume, outside the release directory. |
| `HRAMATKA_PILOT_ORIGIN` | Exact HTTPS pilot origin, without a path or trailing slash. |
| `HRAMATKA_CSRF_HMAC_KEY` | Independent canonical unpadded-base64url encoding of 32 random bytes. It is the server-side HMAC key for session-derived CSRF tokens and is never stored in SQLite. |
| `HRAMATKA_BAKE_WORKERS` | In-process bake worker-pool size. Defaults to `4`; values are clamped to `1`–`8`. |
| `HRAMATKA_MAX_PROVIDER_CONCURRENCY` | Process-wide cap on live Google-AIS/OpenRouter HTTP calls. Defaults to `8`. |
| `HRAMATKA_ACCEPT_METERED_PROVIDER_SPEND` | Literal `0` or `1`; defaults to `0`. Set to `1` only to acknowledge that an API-provider key may be billed. |
| `HRAMATKA_BAKE_PROVIDERS` | Comma-separated active primary providers, default `antigravity,openrouter`. Current logical-model bindings use Antigravity subscriptions for Flash and Pro, and OpenRouter for Gemma. |
| `HRAMATKA_AIS_API_KEY_FILE` | Preferred Google-AIS host credential: a root-readable secret-file path. `HRAMATKA_AIS_API_KEY` remains supported for compatibility and takes precedence if both are set. |

The deployed service must use the real `EngineLessonBaker` and leave mock mode off.
Its selected engine/provider configuration is deployment secret material. One Uvicorn
process owns the bounded worker pool so SQLite remains one local durable authority. The
service is ready only when the database is writable with current migrations and the real
baker is wired; see `/api/readyz` and the persistence contract.

## Operator lifecycle

These local/SSH commands use `HRAMATKA_DB_PATH` and have no web equivalent:

```sh
python -m hramatka.api.teachers create --display-name <label>
python -m hramatka.api.invites create --teacher-id <uuid> [--expires-in-hours 72]
python -m hramatka.api.invites revoke --invite-id <uuid>
python -m hramatka.api.sessions revoke --session-id <uuid>
python -m hramatka.api.teachers deactivate --teacher-id <uuid>
```

Invite creation needs `HRAMATKA_PILOT_ORIGIN` as well. It prints the fragment link
once; SQLite stores only a domain-separated digest, so later commands expose only
identifiers, timestamps, and state. Deactivating a teacher atomically revokes every
unredeemed invite and active session for that teacher.

## Deployment

[`deploy/`](deploy/) contains the Caddy configuration, single-Uvicorn-worker systemd
unit, secret-name-only environment template, and Hetzner CX23 deployment runbook. The
runbook makes a SQLite-consistent encrypted off-host daily backup and clean restore
drill a launch gate. It is configuration and procedure only: it does not provision a
server, DNS record, secret, volume, certificate, or backup destination.
