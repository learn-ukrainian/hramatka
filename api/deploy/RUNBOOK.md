# Hramatka release deployment and rollback

This is the target release flow for the teacher pilot. It replaces ad-hoc
`rsync` of changed Python files, deletion of `__pycache__`, and an in-place
restart. It is repository documentation only: it does not authorize a host
change, provisioning, or disclosure of any secret.

## Invariants

- Every application release is an immutable checkout at
  `/opt/hramatka/releases/<stamp>`; `/opt/hramatka/current` is its sole active
  symlink.
- Each release has its own root-managed `.venv` at
  `/opt/hramatka/releases/<stamp>/.venv`. The service invokes it through
  `/opt/hramatka/current/.venv/bin/uvicorn`, so one `current` symlink always
  selects matching application code and dependencies, as defined in
  [`hramatka-api.service`](hramatka-api.service).
- SQLite state, engine output, backups, and configuration stay outside a
  release directory. Never copy a live SQLite database as a deployment step.
- `/etc/hramatka/api.env` is root-owned secret/config material. Do not `cat`,
  print, commit, copy into a release, or include it in deployment evidence.
- The corpus data release is read-only at `/srv/hramatka-data/<data-stamp>`.
  Its own `data-manifest.json` is the deployment authority; the application
  receives both its directory and manifest path through `/etc/hramatka/api.env`.

## Preflight

1. Select the reviewed commit and a sortable release stamp, for example
   `<stamp>`. Record the commit and intended rollback release in a private
   change record; do not record paste text, cookies, invite fragments, or
   secret values.
2. Confirm the source checkout is clean and reviewed, then run its applicable
   tests before transferring it. Verify the service unit still uses one Uvicorn
   process and `WorkingDirectory=/opt/hramatka/current`.
3. Confirm that the already mounted data release includes its matching
   `data-manifest.json`. Its digests must be verified by `/api/readyz`; never
   set `HRAMATKA_ALLOW_DATA_DRIFT=1` as part of a deploy.
4. Inspect the required variable *names* without printing their values. The
   root-owned environment file must contain `HRAMATKA_DB_PATH`,
   `HRAMATKA_PILOT_ORIGIN`, `HRAMATKA_CSRF_HMAC_KEY`, `HRAMATKA_DATA_DIR`, and
   `HRAMATKA_DATA_MANIFEST`. The latter two must refer to the same mounted data
   release, with `HRAMATKA_DATA_MANIFEST=$HRAMATKA_DATA_DIR/data-manifest.json`.
   Set `HRAMATKA_BAKE_WORKERS=4` (clamped 1–8),
   `HRAMATKA_MAX_PROVIDER_CONCURRENCY=8`, and
   `HRAMATKA_BAKE_PROVIDERS=google-ais,openrouter`. Also configure
   `HRAMATKA_GEMMA_FALLBACK_BASE_URL`, `HRAMATKA_GEMMA_FALLBACK_MODEL`, and one
   host-secret-store key source: `HRAMATKA_GEMMA_FALLBACK_API_KEY` or
   `HRAMATKA_GEMMA_FALLBACK_API_KEY_FILE`. Both routes are active round-robin
   primaries; the other route is outage-only failover. Never print a key or its file.

## Deploy

1. Create the new immutable checkout. Transfer a reviewed release artifact or
   use the repository’s approved read-only source, then check out the exact
   recorded commit:

   ```sh
   install -d -m 0755 /opt/hramatka/releases/<stamp>
   git clone --no-checkout <reviewed-private-repository> /opt/hramatka/releases/<stamp>
   git -C /opt/hramatka/releases/<stamp> checkout --detach <reviewed-commit>
   ```

   Do not use `rsync` to overlay files into `current`. A release checkout must
   not contain runtime `__pycache__`, a database, generated engine output, or a
   populated environment file.

2. Create the release-specific virtual environment from that exact checkout.
   This is done before activation so a dependency failure leaves `current`
   unchanged and a later rollback restores the prior dependency set too:

   ```sh
   python3 -m venv /opt/hramatka/releases/<stamp>/.venv
   /opt/hramatka/releases/<stamp>/.venv/bin/python -m pip install --upgrade pip
   /opt/hramatka/releases/<stamp>/.venv/bin/python -m pip install /opt/hramatka/releases/<stamp>
   ```

3. Build the reviewed teacher frontend in the release. Caddy serves the
   same-origin `/teacher/` path beneath `current`, so it will activate with the
   API on the one release-symlink flip:

   ```sh
   (cd /opt/hramatka/releases/<stamp>/hramatka/app && npm ci && npm run build)
   ```

4. Atomically activate the application checkout. The service startup performs
   the versioned SQLite migration transaction before it starts accepting work;
   do not run handwritten SQL or copy a database file:

   ```sh
   ln -sfn /opt/hramatka/releases/<stamp> /opt/hramatka/current.new
   mv -f /opt/hramatka/current.new /opt/hramatka/current
   systemctl daemon-reload
   systemctl restart hramatka-api.service
   ```

5. Require a healthy service and then run the repository smoke probe from the
   activated release. The smoke makes only GET requests and must pass before
   announcing the deploy:

   ```sh
   systemctl is-active --quiet hramatka-api.service
   HRAMATKA_ORIGIN=<pilot-origin> /opt/hramatka/current/hramatka/ops/smoke.sh
   ```

   Record only the probe’s concise status lines and release identifiers. A
   failed readiness probe is a failed deploy, even if `/api/healthz` is green.

## Rollback

1. Stop and investigate if the new service cannot pass the smoke probe. Keep
   its release directory for diagnosis; do not modify it in place.
2. Atomically repoint the one release symlink to the recorded prior release:

   ```sh
   ln -sfn /opt/hramatka/releases/<previous-stamp> /opt/hramatka/current.new
   mv -f /opt/hramatka/current.new /opt/hramatka/current
   systemctl restart hramatka-api.service
   HRAMATKA_ORIGIN=<pilot-origin> /opt/hramatka/current/hramatka/ops/smoke.sh
   ```

3. Do not roll back SQLite by copying files. A migration that is not compatible
   with the prior application release requires its separately reviewed restore
   procedure and a documented operator decision.
