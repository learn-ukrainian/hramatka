# Hramatka teacher-pilot deployment runbook

This is a configuration and rehearsal runbook for the frozen pasted-text pilot.
It does not authorize or perform provisioning. Use placeholder values only in
tickets, commands saved as evidence, and documentation. Do not commit a database,
backup, raw paste, provider credential, hostname, or populated environment file.

## Launch gate

Do not arm the pilot until all of the following are recorded as passing:

1. The reviewed release has the frozen API contracts and its full test suite is green.
2. A real `EngineLessonBaker` is configured; mock mode is off; `/api/readyz` returns
   `200` from the deployed unit.
3. HTTPS serves the teacher bundle and `/api/*` from the exact one configured origin;
   CORS remains disabled.
4. An operator has performed the clean restore drill below from a daily off-host copy,
   verified its checks, and recorded only non-sensitive evidence.
5. The explicit pilot-arming decision required by bridge message 2629 is present.

A red readiness probe, missing backup destination, failed restore drill, or a change
that adds Uvicorn processes is a launch blocker. Disarm by stopping
`hramatka-api.service`; do not work around a failed gate with the mock baker.

## Host layout

The target is one Hetzner CX23 (2 vCPU / 4 GB). The layout deliberately separates
immutable releases from mutable private state. Provider generation is I/O-bound; the
default four in-process bake workers are appropriate for this host, while a shared
eight-request provider cap bounds phase fan-out and provider-rate-limit pressure.

| Path | Owner and purpose |
| --- | --- |
| `/opt/hramatka/current` | Read-only reviewed release checkout, including its release-specific virtual environment. |
| `/var/lib/hramatka/hramatka.sqlite3` | Persistent-volume SQLite database, plus SQLite-managed `-wal` and `-shm` files. |
| `/etc/hramatka/api.env` | Root-owned service environment file; contains deployment secrets and is never in git. |
| `/var/lib/hramatka/backups/staging` | Mode-0700 short-lived local backup staging only; no release directory, repository, or web root. |

Create a dedicated unprivileged `hramatka` service account. Mount or attach the
persistent volume before the service is started, make `/var/lib/hramatka` owned by
that account, and keep it outside the release deployment path. SQLite's WAL and SHM
files share this directory; do not copy any of the three files as a hand-made backup.

## Service and proxy installation

1. Build the teacher bundle in the reviewed release checkout. Caddy resolves it
   beneath `/opt/hramatka/current/hramatka/app/dist`, so the same release
   symlink activates the API and teacher UI together.
2. Put [`hramatka-api.service`](hramatka-api.service) at
   `/etc/systemd/system/hramatka-api.service`, then run
   `systemctl daemon-reload` and `systemctl enable --now hramatka-api.service`.
   Its explicit `--workers 1` is required: one API process owns the bounded
   in-process bake pool and its SQLite durable authority.
3. Create `/etc/hramatka/api.env` from [`api.env.example`](api.env.example), set its
   owner to `root:hramatka` and mode to `0640`, then populate values only from the
   host secret store. `HRAMATKA_CSRF_HMAC_KEY` is the mandatory server-side HMAC key;
   it must be an independent canonical 32-byte unpadded-base64url value and must
   never be database data or a repository value. Set `HRAMATKA_PILOT_ORIGIN` to the
   exact HTTPS origin, without a trailing slash.
4. Install [`Caddyfile`](Caddyfile) as the Caddy configuration and set
   `HRAMATKA_PILOT_HOST` in Caddy's root-owned service environment to the same host
   represented by `HRAMATKA_PILOT_ORIGIN`. Caddy obtains TLS certificates, serves
   `/teacher/*` with SPA fallback, and proxies `/api/*` only to loopback.
5. Confirm the public firewall admits HTTPS only. Do not expose Uvicorn's loopback
   port, the SQLite volume, the environment file, the release checkout, or an
   operator CLI endpoint to the browser.

Before enabling teacher traffic, verify the unit is one process, Caddy is serving the
single origin, `/api/healthz` returns `200`, and `/api/readyz` returns `200`. Inspect
service configuration rather than relying on an operator's memory:

```sh
systemctl show hramatka-api.service -p ExecStart -p User -p StateDirectory
systemctl status hramatka-api.service --no-pager
curl --fail --silent --show-error https://<pilot-hostname>/api/healthz
curl --fail --silent --show-error https://<pilot-hostname>/api/readyz
```

Do not include cookies, invite fragments, request bodies, CSRF values, or response
payloads containing teacher data in terminal captures or support logs.

## Bake capacity and provider routing

Set `HRAMATKA_BAKE_WORKERS=4`, `HRAMATKA_MAX_PROVIDER_CONCURRENCY=8`, and
`HRAMATKA_BAKE_PROVIDERS=antigravity,openrouter` in the root-owned environment file
unless measured provider limits justify a reviewed change. The worker setting is
clamped to 1–8. Current logical-model bindings use Antigravity subscriptions for
Gemini Flash and Pro, and OpenRouter for Gemma. Google-AIS remains a separate
qualification route; do not configure a paid Vertex fallback. Set
`HRAMATKA_ACCEPT_METERED_PROVIDER_SPEND=1` only after acknowledging that an
API-provider bake key may be billed. Without it, standard API-provider bake calls
are refused before transport; subscription-client routes are unaffected.

The transcribed current-v3 Flash receipt has `cli_self_reported` subscription
provenance. Set
`HRAMATKA_SUBSCRIPTION_QUALIFICATION_PROVENANCE_TIER=cli_self_reported` only to
permit that explicitly lower-observability receipt; it does not make the
receipt API-observed. With that setting, `/api/lesson-models` exposes Flash
while unqualified Pro and Gemma remain hidden.

For Google-AIS on a host, prefer `HRAMATKA_AIS_API_KEY_FILE` pointing to the
root-readable secret file rather than copying the key into the environment file.
`HRAMATKA_AIS_API_KEY` remains supported for compatibility and takes precedence
when both sources are set.

Before pilot deployment, perform the separately driver-coordinated live check with
four simultaneous teacher bakes. Record only concurrency counts, sanitized timing,
provider host names, terminal job states, and `/api/readyz`—never prompts, provider
responses, or secret values.

## Daily SQLite-consistent off-host backup

Select the encrypted off-host destination and credential in the host secret store
before launch; its actual name and credentials are deliberately not in this repository.
Run the following procedure daily from a root-owned timer or equivalent scheduler,
with output limited to timestamps, snapshot IDs, integrity results, and hashes:

1. Create a unique, mode-0700 staging directory under
   `/var/lib/hramatka/backups/staging`.
2. Use SQLite's online backup API (for example, the `sqlite3` shell `.backup` command)
   against the live database to create one standalone snapshot in that staging
   directory. Do **not** use `cp`, `rsync`, or an archive of the live database,
   `-wal`, and `-shm` files.
3. Open the snapshot read-only and require both `PRAGMA integrity_check;` returning
   `ok` and `PRAGMA foreign_key_check;` returning no rows. On either failure, do not
   upload the snapshot; alert the operator and retain it only according to incident
   policy.
4. Hash the verified snapshot, encrypt it with the separately managed backup key, and
   upload the encrypted object plus its hash to the selected off-host destination.
   Verify the remote object hash after upload before reporting success.
5. Keep at least 14 daily and 8 weekly verified encrypted snapshots, according to the
   selected retention policy. Securely remove the plaintext staging snapshot only
   after its off-host verification is complete.

The backup is required to preserve all pilot tables and therefore the teacher owner,
canonical original request, accepted lesson JSON, warning acknowledgements, acceptance
metadata, and exact revision. A filesystem backup that merely captures part of a WAL
transaction does not meet this requirement.

## Restore drill (launch gate and recurring rehearsal)

Run this drill before first launch and at least monthly afterwards. Restore only into a
fresh mode-0700 temporary directory or disposable volume; never overwrite the live
database during a drill.

1. Retrieve a chosen encrypted off-host snapshot and its recorded checksum. Verify the
   downloaded encrypted object, decrypt it outside the checkout, and verify the
   plaintext snapshot hash before opening it.
2. With SQLite opened read-only, require `PRAGMA integrity_check;` = `ok` and an empty
   `PRAGMA foreign_key_check;`. Confirm the expected current migration version in the
   migration table.
3. Query only the restored database locally to confirm that at least one selected
   teacher record, its associated lesson request, materialized accepted lesson JSON,
   warning acknowledgements, acceptance metadata, and aggregate revision are present
   and mutually consistent. Record row IDs/counts and revision numbers, not pasted
   text, tokens, cookies, or lesson material.
4. Start a separate one-worker test service against a copy of the restored database
   with outbound model calls disabled. Confirm it opens the DB, recognizes the current
   schema, and handles an orphaned `baking` job through the documented durable failure
   recovery path. Do not point a restore-drill service at production Caddy or reuse a
   production cookie/CSRF secret.
5. Record snapshot timestamp, checksum verification, SQLite checks, schema version,
   recovery result, and operator/date in private launch evidence. Delete the temporary
   plaintext copy when the drill ends.

Failure at any restore step blocks launch or requires disarming an already armed pilot
until a newly verified backup and clean restore drill pass.
