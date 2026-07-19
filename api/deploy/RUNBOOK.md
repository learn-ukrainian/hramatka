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
- `/etc/hramatka/review-attestation-signing-key.pem` is an Ed25519 private PEM,
  owned by `root:root` with mode `0600`. The unprivileged API reads only the
  private systemd credential copy configured by `hramatka-api.service`; the
  source PEM never enters the service environment, repository, or Actions.
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
5. For review attestation, first verify that `/etc/hramatka/api.env` contains
   the assigned `HRAMATKA_REVIEW_ATTESTATION_*` names from
   [`api.env.example`](api.env.example). The endpoint remains disabled unless
   `HRAMATKA_REVIEW_ATTESTATION_ENABLED` and
   `HRAMATKA_REVIEW_ATTESTATION_PAID_REVIEW_ENABLED` are both set to the literal
   `1`. These flags accept only `0` (disabled) or `1` (enabled); `true` and
   `false` are startup errors, not aliases. Its
   expected OIDC audience must equal the exact HTTPS URL held in the repository
   variable `HRAMATKA_REVIEW_ATTESTOR_URL`; do not put that URL in a workflow,
   source file, Caddy variable, or shell history. The trusted repository ID,
   workflow ref, and workflow-content digest are allow-list inputs, not hints.
   Review calls use the host's existing `HRAMATKA_AIS_API_KEY`; GitHub Actions
   receives no Google credential. The default reviewer is
   `google-ais/gemini-3.5-flash`. It never falls back automatically to Pro or
   another paid model. The signing-key path is the exception: do not assign it
   in `api.env`. The service unit sets
   `HRAMATKA_REVIEW_ATTESTATION_SIGNING_KEY_FILE` to its `%d` systemd credential
   copy, sourced from the root-only PEM.
6. The attestation body cap uses Caddy's native `request_body` handler, which
   is supported by the live-probed Caddy 2.11.4. Before reloading the proxy,
   run `caddy validate --config <root-owned-caddyfile> --adapter caddyfile` on
   the host. A missing handler or failed validation blocks the release; do not
   remove the cap or substitute a third-party rate-limit plugin. Stock Caddy
   has no native rate-limit handler. OIDC JWKS caching and the attestor's
   process-local concurrency limit of one are application-level defenses;
   Caddy independently enforces exact method/path, body size, timeouts, no
   cache, no CORS, and skipped access logging.
7. Generate and install the signing key only in a root shell on the host. Never
   redirect its contents to a terminal, paste it into a ticket, or copy it to a
   checkout:

   ```sh
   umask 077
   openssl genpkey -algorithm Ed25519 \
     -out /etc/hramatka/review-attestation-signing-key.pem
   chown root:root /etc/hramatka/review-attestation-signing-key.pem
   chmod 0600 /etc/hramatka/review-attestation-signing-key.pem
   ```

   Derive only the public PEM. Base64-encode that public output and set it as
   the non-secret GitHub repository variable
   `HRAMATKA_REVIEW_ATTESTATION_PUBLIC_KEY_B64`; never set the private PEM as an
   Actions secret or variable. Record the SHA-256 public-key fingerprint in the
   coordinated deployment record:

   ```sh
   openssl pkey -in /etc/hramatka/review-attestation-signing-key.pem \
     -pubout -outform PEM | base64 -w 0
   openssl pkey -in /etc/hramatka/review-attestation-signing-key.pem \
     -pubout -outform DER | openssl dgst -sha256
   ```

   Install the reviewed service unit, run `systemctl daemon-reload`, and require
   `systemd-analyze verify /etc/systemd/system/hramatka-api.service` before the
   application deploy. The unit's `LoadCredential` is what makes a private
   read-only copy available to `User=hramatka` without weakening source mode.

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
   unchanged and a later rollback restores the prior dependency set too. The
   package installation reads the exact reviewed checkout's `pyproject.toml`:

   ```sh
   python3 -m venv /opt/hramatka/releases/<stamp>/.venv
   /opt/hramatka/releases/<stamp>/.venv/bin/python -m pip install --upgrade pip
   /opt/hramatka/releases/<stamp>/.venv/bin/python -m pip install /opt/hramatka/releases/<stamp>
   ```

   The current live pilot has not yet migrated to this target per-release venv
   layout. Its reviewed `hramatka/ops/deploy.sh HOST` path first confirms that
   the deployed `/opt/hramatka/venv/bin/python` can import `jwt` and
   `cryptography`, then transfers code only if that bounded preflight passes.
   This prevents an attestation deploy from failing after a restart because of
   missing runtime dependencies; it is transitional proof, not a claim that
   the shared pilot venv is immutable or release-bound.

   If the live shared venv does not yet contain `jwt` and `cryptography`, first
   create the exact detached, reviewed checkout from step 1 and verify its HEAD
   and cleanliness. Then install from that immutable checkout—not the
   operator's working tree—and rerun the import probe before `deploy.sh`:

   ```sh
   test "$(git -C /opt/hramatka/releases/<stamp> rev-parse HEAD)" = "<reviewed-commit>"
   test -z "$(git -C /opt/hramatka/releases/<stamp> status --porcelain)"
   /opt/hramatka/venv/bin/python -m pip install /opt/hramatka/releases/<stamp>
   /opt/hramatka/venv/bin/python -c 'import jwt, cryptography'
   ```

   This is an explicit prerequisite change to the transitional shared venv. It
   is never performed automatically by the deploy script and must use the same
   reviewed commit being deployed.

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

6. Before enabling review attestation, make one bounded OIDC-authenticated
   request through the configured repository variable URL and verify only its
   HTTP status and attestation receipt identifier. Do not capture request
   bodies, assertions, diff content, or provider output. The endpoint is public
   HTTPS by transport design, but rejects requests without the exact trusted
   OIDC claims and content digest. Its provider billing is charged to the
   project/Cloud billing pool behind `HRAMATKA_AIS_API_KEY`; it is separate from
   an operator's Antigravity weekly quota. Keep the paid-review opt-in false
   until this dry verification and release review are recorded.

## Signing-key rotation

Rotation is a coordinated deployment, never a timer or silent host mutation.
Disable new attestation calls, let the one active application slot drain,
generate a new root-only key, record its public fingerprint, update
`HRAMATKA_REVIEW_ATTESTATION_PUBLIC_KEY_B64`, restart the service with the new
credential, and complete one signed receipt verification before re-enabling
paid review. The current single-public-key design has no overlap window: after
the GitHub variable changes, previously issued receipts no longer verify. Clear
or regenerate affected open-PR receipts during the same maintenance record.
Supporting old and new receipts simultaneously requires a separately reviewed
multi-key design; do not improvise overlap or retain untracked private keys.

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
