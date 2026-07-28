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
### Attestation ceremony walkthrough

This is the agent-runnable cross-family review attestation flow proven on
PRs #301, #302, and #303 (2026-07-27/28). It is documentation only: it does not
authorize host access, grant OIDC credentials, or expose the signing key.

An existing cross-family review is eligible for attestation only after a sealed
review record is added to the host-side attestation database. The record is
immutable for one repository/PR/head and binds the exact GitHub comment ID, URL,
author, timestamps, body digest, reviewer family, concrete reviewer model, and
provider origin. The attestor later fetches that comment using its ephemeral
Actions token and rejects an edited, deleted, swapped, or head-mismatched
record. A lifecycle marker is only a consistency check; it cannot authorize a
signature by itself.

#### 1. Write the review-of-record comment

The review comment that will be sealed must explicitly contain all three
elements the attestor prompt looks for:

- an approve/clean verdict phrase (`APPROVE — clean`, `approved`, or `clean`);
- the reviewer **family** word (e.g. `anthropic`);
- the concrete **model** words (e.g. `claude fable 5`).

Proven examples from #301/#302/#303:

> Review of record — cross-family: reviewer family **anthropic**, model
> **claude fable 5**; author lane grok/xAI.
> **Verdict: APPROVE — clean.**

> Review of record — cross-family: reviewer family **anthropic**, model
> **claude fable 5**; author lane codex/OpenAI.
> **Verdict: APPROVE — clean.**

> Review of record — cross-family: reviewer family **anthropic**, model
> **claude fable 5**; author lane kimi/moonshot.
> **Verdict: APPROVE — clean.**

The comment must be left on the PR at the exact head that will be sealed.

#### 2. Seal the review record on the host

Fetch the private review comment through an authenticated local GitHub client
and stream its JSON to the host registrar. Do not put a token in the command,
terminal output, host environment, or repository. The command prints only the
sealed record identifiers and digest:

```sh
gh api repos/<owner>/<repo>/issues/comments/<comment-id> | \
  ssh ops@<host> 'cd /opt/hramatka/current && \
    sudo -n .venv/bin/python -m hramatka.ops.register_review_record \
    --database /var/lib/hramatka/review-attestations.sqlite3 \
    --repository <owner>/<repo> \
    --pr-number <n> \
    --head-sha <40-hex-sha> \
    --reviewer-family <family> \
    --reviewer-model <family>/<concrete-model> \
    --provider-host <https-url>'
```

`--reviewer-model` **must** be in `family/model` form, for example
`anthropic/claude-fable-5`. The concrete model must match the model words in the
review comment.

To replace review evidence, create a new review at a new PR head; do not modify
the sealed SQLite row.

#### 3. Add the lifecycle marker to the PR body

Add a single HTML comment to the PR body. It must contain exactly eight fields
in one JSON blob:

```html
<!-- hramatka-pr-lifecycle:v1
  {"owner":"claude-hramatka",
   "state":"ready",
   "blocked_by":"none",
   "next_action":"trusted review attestation published",
   "author_family":"<author-family>",
   "review_receipt":"<sealed-review-comment-html-url>",
   "reviewer_family":"<reviewer-family>",
   "review_head":"<current-40-hex-head>"}
-->
```

Proven markers from the attested PRs:

- #301 (head `273139ae3bc84c6303561aa142a4fa689019fc7d`):
  ```html
  <!-- hramatka-pr-lifecycle:v1 {"author_family":"xai","blocked_by":"none","next_action":"trusted review attestation published","owner":"claude-hramatka","review_head":"273139ae3bc84c6303561aa142a4fa689019fc7d","review_receipt":"https://github.com/learn-ukrainian/learn-ukrainian-infra-private/pull/301#issuecomment-5097486342","reviewer_family":"anthropic","state":"ready"} -->
  ```
- #302 (head `9352fa8b0c65de23f4e3404a15e72e1562c4adae`):
  ```html
  <!-- hramatka-pr-lifecycle:v1 {"author_family":"openai","blocked_by":"none","next_action":"trusted review attestation published","owner":"claude-hramatka","review_head":"9352fa8b0c65de23f4e3404a15e72e1562c4adae","review_receipt":"https://github.com/learn-ukrainian/learn-ukrainian-infra-private/pull/302#issuecomment-5097322925","reviewer_family":"anthropic","state":"ready"} -->
  ```
- #303 (head `a707493d9a20d7f76bcee7c21b8175f41724d4a8`):
  ```html
  <!-- hramatka-pr-lifecycle:v1 {"author_family":"moonshot","blocked_by":"none","next_action":"trusted review attestation published","owner":"claude-hramatka","review_head":"a707493d9a20d7f76bcee7c21b8175f41724d4a8","review_receipt":"https://github.com/learn-ukrainian/learn-ukrainian-infra-private/pull/303#issuecomment-5097562414","reviewer_family":"anthropic","state":"ready"} -->
  ```

`review_receipt` is the **human review comment URL** at the time the marker is
first written. The bot will rewrite it to its own signed receipt comment URL
after publishing.

#### 4. Apply the label and complete the lifecycle

1. Apply the `review-attestation` label to the PR.
2. Wait for the `Review attestation` workflow to publish the bot receipt
   (proven green runs: #301 run `30335798445`, #302 run `30309956993`, #303 run
   `30311436701`). The bot removes the label and posts a receipt comment that
   includes a signed `hramatka-review-attestation:v2` payload.
3. After the bot receipt appears, **rerun the latest `PR lifecycle` workflow
   run** (do not create a new run manually; use the rerun button on the latest
   run). Proven green validate jobs: #301 run `30335790672`, #302 run
   `30309955879`, #303 run `30311435994`.
4. Confirm the lifecycle validate job is green and the marker in the PR body now
   points to the bot receipt URL with `next_action` set to `published`.
5. Merge from a private-repository working directory. Do not merge across a
   public checkout or from a bridge state directory.

#### 5. ⚠️ Gotchas

- **Bot rewrites the marker.** After successful attestation the bot publisher
  replaces the marker: `review_receipt` becomes the bot receipt comment URL and
  `next_action` becomes `published`. On any subsequent push, **rewrite the whole
  marker cleanly** with the new head and a fresh human review URL; never patch
  individual fields in place.
- **"Attestor request failed" is a generic surface.** It most often means the
  lifecycle marker and the sealed SQLite record disagree (wrong head, wrong
  comment URL, wrong reviewer family/model, or a stale marker). Check the marker
  **before** suspecting the attestor service or OIDC layer.
- **A PR touching `.github/workflows/review-attestation.yml` can never
  self-attest.** That workflow change must be reviewed and attested from a
  different PR/head, following the same cross-family rule.
- **Receipts are head-bound.** If the PR head moves after sealing, the old
  record is invalid. Create a new review comment at the new head and re-run the
  full ceremony.

### Post-merge: trusted workflow digest rotation

Trigger: a merged PR changed `.github/workflows/review-attestation.yml`.

Symptom signature: every subsequent `review-attestation` workflow run fails
with the generic «attestor request failed». The failure is not in the lifecycle
marker, the sealed SQLite record, or the OIDC layer: the merge replaced the
pinned workflow file while the API environment still holds the pre-merge
SHA-256 digest, so every new workflow run is rejected by the digest allow-list.

Proven instance recorded in issue #293 (2026-07-28):

> **Operational companion step (post-merge, completed 2026-07-28 ~13:0xZ) — for the record:** this PR changed `.github/workflows/review-attestation.yml`, and the attestor service pins that file's sha256 (`HRAMATKA_REVIEW_ATTESTATION_TRUSTED_WORKFLOW_DIGEST`). After the merge, every attest ran the NEW workflow against the OLD pin and failed as the generic «attestor request failed» (proven: old pin a8dc6be8… = pre-merge file, new file 17bc7967…; all content-layer checks — marker, sealed record, claims, digests — verified green locally). Rotated the pin to 17bc7967e2a65b98fc8f3117eee2ef6b2806b7b434a56a026837e2920c545bc0 on the host (env backup taken), service restarted, readyz 200.
>
> **Standing rule: any merge touching `review-attestation.yml` requires this digest rotation on the host as its finalize leg.**

Rotation recipe (host-side, root only; never print or commit secret values):

1. At the merged commit, compute the digest of the new workflow file:

   ```sh
   git show <merged-commit>:.github/workflows/review-attestation.yml | sha256sum
   ```

2. Back up `/etc/hramatka/api.env` before editing.

3. Set `HRAMATKA_REVIEW_ATTESTATION_TRUSTED_WORKFLOW_DIGEST` in
   `/etc/hramatka/api.env` to the new SHA-256 value. Do not change any other
   variable and do not print the file.

4. Restart the API service:

   ```sh
   systemctl restart hramatka-api.service
   ```

5. Verify `/api/readyz` returns HTTP 200 from the activated release.

6. Verify with a fresh attest/label cycle on an eligible PR: apply the
   `review-attestation` label, wait for the bot receipt, and rerun the latest
   `PR lifecycle` workflow. Only after that cycle is green treat the merge as
   fully finalized.

Standing rail: a PR that touches `.github/workflows/review-attestation.yml`
can **never** self-attest. An operator must merge such a PR by hand and
immediately run the rotation above as the finalize leg. The workflow change
must still be reviewed and attested from a different PR/head, following the
same cross-family rule.

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

   Stage-1 `hramatka/ops/deploy.sh ops@HOST` (issues #210/#212) builds a
   release-local `.venv` under `releases/<stamp>` and proves
   `jwt`/`cryptography`/`psutil` on that interpreter before the atomic
   `current` flip (`ln -sfn` + `mv -T`, never `mv -f` onto the symlink). Host
   migration from a pre-releases layout is
   `hramatka/ops/migrate_release_layout.sh` (operator-run; refuses if already
   migrated).

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
   mv -T /opt/hramatka/current.new /opt/hramatka/current
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

## Dedicated review-attestation runner

This is a documented installation plan only. It does not authorize runner
registration, a GitHub configuration change, or a host change. The existing
general-purpose runner remains separate and must not be repurposed.

Install the reviewed files
[`hramatka-attestor-runner.service`](hramatka-attestor-runner.service) and
[`verify-attestor-runner-job.sh`](verify-attestor-runner-job.sh) only in a
reviewed maintenance change. Create the dedicated no-login service account
`hramatka-attestor-runner` and `/opt/actions-runner-attestor`; do not add that
account to `root`, `hramatka`, Docker, or any privileged group. The API env,
root-only signing PEM, database/state directory, deployment tree, and existing
runner tree must remain inaccessible to the service.

The guard is installed outside the runner tree at
`/usr/local/lib/hramatka-attestor/verify-attestor-runner-job.sh` as
`root:root` mode `0755`, with its containing directory root-owned and not
writable by the runner account. The service verifies those ownership and mode
requirements before starting and uses GitHub's synchronous pre-job hook. A
rejected job fails before any action, shell, or pull-request code runs; the
guard deliberately logs only a generic rejection.

Register a second runner against this repository only after an operator obtains
a fresh single-use registration token interactively. Do not save, print, or
paste the token into shell history, a service unit, an environment file, or a
ticket. Configure it with `--unattended`, `--disableupdate`,
`--no-default-labels`, the name `pilot-vps-attestor`, and the sole custom label
`hramatka-attestor`. The absence of the default `self-hosted`, OS, and
architecture labels prevents this runner from accepting ordinary selectors used
by the existing CI jobs. The review-attestation workflow must explicitly target
the custom label.

Use a hidden, short-lived shell value only while configuring the runner; do not
place a real token in the command text below or retain it after configuration:

```sh
read -r -s registration_token
printf '\n'
sudo -u hramatka-attestor-runner \
  /opt/actions-runner-attestor/config.sh --unattended \
  --url https://github.com/learn-ukrainian/learn-ukrainian-infra-private \
  --token "$registration_token" --name pilot-vps-attestor \
  --labels hramatka-attestor --no-default-labels --disableupdate
unset registration_token
```

The configuration command deliberately runs as the service account because it
must write the initial runner registration files. Immediately after it succeeds
and before enabling the unit, root locks down the installation. Do not apply
the following ownership changes before configuration completes.

Run the following as root. The install root and immutable application tree use
`root:hramatka-attestor-runner` with directories mode `0750`, executable files
mode `0550`, and other regular files mode `0440`. The service account can read
and execute the runner, while the existing `ghrunner` account cannot traverse
or read the tree. The three runner credential files remain readable to the
service account with mode `0640` but are never writable by it.

```sh
runner_root=/opt/actions-runner-attestor
runner_user=hramatka-attestor-runner
runner_group=hramatka-attestor-runner

chown -R "root:$runner_group" "$runner_root"
find "$runner_root" -type d -exec chmod 0750 {} +
find "$runner_root" -type f -perm /111 -exec chmod 0550 {} +
find "$runner_root" -type f ! -perm /111 -exec chmod 0440 {} +

for credential in .runner .credentials .credentials_rsaparams; do
  credential_path="$runner_root/$credential"
  test -f "$credential_path"
  chown "root:$runner_group" "$credential_path"
  chmod 0640 "$credential_path"
done
```

Only these runtime directories are writable by
`hramatka-attestor-runner:hramatka-attestor-runner` with mode `0700`:

- `/opt/actions-runner-attestor/_actions`
- `/opt/actions-runner-attestor/_diag`
- `/opt/actions-runner-attestor/_tool`
- `/opt/actions-runner-attestor/_work`
- `/var/lib/hramatka-attestor-runner`

The systemd `StateDirectory` directive creates the final `/var/lib` directory
with that service account ownership and mode; do not create it manually.

```sh
for runtime_dir in \
  "$runner_root/_actions" \
  "$runner_root/_diag" \
  "$runner_root/_tool" \
  "$runner_root/_work"; do
  install -d -o "$runner_user" -g "$runner_group" -m 0700 "$runtime_dir"
  chown -R "$runner_user:$runner_group" "$runtime_dir"
  find "$runtime_dir" -type d -exec chmod 0700 {} +
  find "$runtime_dir" -type f -exec chmod 0600 {} +
done
```

The account needs read access to its runner credentials but never write access
to the application, guard, systemd unit, API configuration, key, database,
release tree, or existing runner tree. The unit refuses to start unless the
install root, `runsvc.sh`, and all three credential files have these exact
owner, group, and modes.

`--disableupdate` is required because automatic runner replacement would
violate that immutable-install boundary. Upgrade the runner only as a reviewed
root maintenance operation, then repeat the lockdown commands and re-check the
unit, guard ownership, and the four runtime directories before starting it.
The guard uses only the embedded
`/opt/actions-runner-attestor/externals/node24/bin/node` interpreter from that
immutable runner installation; it does not trust a host interpreter or `PATH`.

Configure the API's root-owned environment file with the exact non-secret
`HRAMATKA_REVIEW_ATTESTATION_TRUSTED_RUNNER_ID`,
`HRAMATKA_REVIEW_ATTESTATION_TRUSTED_RUNNER_GROUP_ID`, and
`HRAMATKA_REVIEW_ATTESTATION_TRUSTED_RUNNER_LABEL=hramatka-attestor` values
reported after registration. Configure the same three values as GitHub
repository variables for the PR lifecycle workflow. They let the lifecycle
verify the runner identity recorded by GitHub; they are not secrets and must
not be represented as Actions secrets.

The root-owned guard admits only GitHub-provided default context for the exact
repository, the workflow ref
`learn-ukrainian/learn-ukrainian-infra-private/.github/workflows/review-attestation.yml@refs/heads/main`,
the `pull_request_target` event, and the `attest` or `publish` job. It parses
the runner-supplied event file without logging its content and additionally
requires `action=labeled` and `label.name=review-attestation`.

Repository-scoped labels prevent accidental routing but are not an authorization
boundary: a permitted repository workflow can name the unique label. In this
zero-GitHub-spend design, the root-owned guard's exact workflow-ref, job, and
event checks are the mandatory authorization boundary and execute before any
workflow action, shell, or pull-request code. If available, an organization
runner group restricted to this repository and attestation workflow is optional
defense in depth; its absence does not block this design.

## Rollback

1. Stop and investigate if the new service cannot pass the smoke probe. Keep
   its release directory for diagnosis; do not modify it in place.
2. Atomically repoint the one release symlink to the recorded prior release:

   ```sh
   ln -sfn /opt/hramatka/releases/<previous-stamp> /opt/hramatka/current.new
   mv -T /opt/hramatka/current.new /opt/hramatka/current
   systemctl restart hramatka-api.service
   HRAMATKA_ORIGIN=<pilot-origin> /opt/hramatka/current/hramatka/ops/smoke.sh
   ```

3. Do not roll back SQLite by copying files. A migration that is not compatible
   with the prior application release requires its separately reviewed restore
   procedure and a documented operator decision.
