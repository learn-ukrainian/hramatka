# Hramatka ops

> **Pilot host connection + full ops runbook: [`pilot-vps.md`](pilot-vps.md)** — address, login,
> services, DB, deploy recipe, teacher CLIs, and the safety rules. The same box carries the
> private repo's self-hosted CI runner.

Pilot deploy and smoke helpers. From a reviewed checkout:

`./hramatka/ops/deploy.sh <user@pilot-host>`

`deploy.sh` replaces the hand-rolled partial rsync (app dist + engine + api). It builds with CSP guard, rsyncs the whole `hramatka` package (explicit excludes), blocks restart during in-flight bakes unless `--force`, then restarts, polls readyz, prints migration version, runs `smoke.sh`, and prints bundle hash + git SHA. Preview locally: `./hramatka/ops/deploy.sh --dry-run /tmp/fake-deploy`.

`./hramatka/ops/backup-db.sh --db <db-path> --dest-dir <dest-dir> --keep-days <days>`

`backup-db.sh` creates a consistent online SQLite backup snapshot via `.backup`, performs a `PRAGMA quick_check;` integrity check, and prunes old backups older than keep-days based on mtime.


## JSON-mode hang probe (#171)

`hramatka/ops/probe-json-mode.py` — **key-gated, not wired into CI**.

Sends one minimal real generation per cell (google-ais `gemma-4-31b-it` and
`gemma-4-26b-a4b-it`, each with and without `response_format: json_object`;
OpenRouter ≤2 paid calls). Hard client timeout ~120s so a hang is a measurement.

```bash
# Keys are ENV-ONLY in the probe (no hardcoded home paths). Export before run:
export HRAMATKA_AIS_API_KEY="$(cat /path/to/your/ais.key)"
export HRAMATKA_GEMMA_FALLBACK_API_KEY="$(cat /path/to/your/openrouter.key)"  # optional
# Or point at a directory that contains google-ais.key and openrouter.key
# (HRAMATKA_SECRET_DIR has no home default — unset means env vars only):
# export HRAMATKA_SECRET_DIR=/path/to/secret/dir

.venv/bin/python hramatka/ops/probe-json-mode.py
.venv/bin/python hramatka/ops/probe-json-mode.py --skip-openrouter
```

Keys: `HRAMATKA_AIS_API_KEY` / `HRAMATKA_GEMMA_FALLBACK_API_KEY` env vars, or
files under optional `HRAMATKA_SECRET_DIR` (no default). Never prints key values.

### Default configuration (Gemini re-probe, post-#171)

| Knob | Default | Notes |
| --- | --- | --- |
| `HRAMATKA_GEN_JSON_MODE` | unset or `"0"` | Gemini JSON mode is off by default. Set to `"1"` to enable it for one run. |
| Gemini 3.6 Flash / 3.1 Pro Preview | **opt-in** | With `HRAMATKA_GEN_JSON_MODE=1`, AIS OpenAI-compat requests send `response_format: {"type":"json_object"}` and native Vertex requests send `generationConfig.responseMimeType: "application/json"`. Either transport retries once without JSON mode after a 400 rejection. |
| Gemma seats | **hard-denied** | #171's Gemini-host/Gemma-model measurements remain authoritative: Gemma never receives JSON mode, even when the environment value is `"1"`. |
| Latency watchdog | always on when telemetry ctx present | Emits `latency_watchdog` when a call exceeds 3× rolling median of prior samples in the bake. |

For a v3 parse failure or serialized phase floor shortfall, the raw model response is retained
only in a per-bake UUID directory below the configured private engine output root as
`generation-raw-attempt<N>.txt` (capped at 512 KiB of raw response bytes). A capped artifact ends
with `...TRUNCATED`. The established 14-day artifact retention applies. Qualification diagnostics
place this beneath their private `raw-parse-failures/` scratch subtree; raw output never enters
receipts, durable job telemetry, or logs.

## Local Test Loop

`./hramatka/ops/local-loop.sh`

### Rule (Deploy Discipline)
The entire loop (code -> build -> test -> bake) must run **locally** to protect the pilot stability. Live pilot hosts get only reviewed+merged code plus one final parity smoke.

### Prerequisites
- Active virtual environment (e.g. `.venv`) with packages installed.
- Provider API key at `~/.secret/google-ais.key` (or passed as `HRAMATKA_AIS_API_KEY` env var).

### Options
- `-h`, `--help`: Usage summary
- `--anchor <file>`: Path to anchor text file (default: `hramatka/engine/tests/fixtures/anchor01.txt`)
- `--focus <string>`: Focus parameter for lesson generation (default: no focus)
- `--duration 45|60|90`: Duration of the lesson (default: `45`)
- `--model <id>`: Model ID to use for generation (default: `google-ais/gemma-4-31b-it`). The supported values are:
  - `google-ais/gemma-4-31b-it` (default Gemma model, $0)
  - `google-ais/gemma-4-26b-a4b-it` (free Gemma MoE model, $0)
  - `google-ais/gemini-3.6-flash` (free Gemini Flash seat, $0)
  - `google-ais/gemini-3.1-pro-preview` (paid Gemini model; requires setting `HRAMATKA_PAID_MODEL_OK=1` acknowledgement env var)
  - `deepseek/deepseek-v4-pro` (cheap DeepSeek API model; requires setting `HRAMATKA_DEEPSEEK_API_KEY` env var)
- `--keep`: Keep the local uvicorn stack running and do not clean up the tmp database/logs.
- `--stop`: Terminate any kept stacks and clean up their state.
- `--dry-run`: Dry run to validate env assembly and resolved paths.
- `./hramatka/ops/check-local-loop-venv.sh`: Read-only guard that reports stale/missing console-script interpreters and exits non-zero on failure.

### Examples

1. **Standard local E2E bake**:
   ```bash
   ./hramatka/ops/local-loop.sh
   ```

2. **Bake with a specific anchor file and focus**:
   ```bash
   ./hramatka/ops/local-loop.sh --anchor my-anchor.txt --focus "noun gender" --duration 60
   ```

3. **Keep the stack running for interactive API/DB checks**:
   ```bash
   ./hramatka/ops/local-loop.sh --keep
   ```

4. **Stop the kept stack afterwards**:
   ```bash
   ./hramatka/ops/local-loop.sh --stop
   ```

### Venv shebang guard (read-only)

Before running `local-loop.sh`, run the guard:

```bash
./hramatka/ops/check-local-loop-venv.sh
```

If the check reports missing or out-of-venv interpreters, recreate the local venv manually:

```bash
rm -rf .venv
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
```

## Local soak baseline (18 anchors)

`./hramatka/ops/local-soak.sh`

Sequential focus-free soak of the 18 real anchors in `.agent/tmp/soak-anchors.json` against **one** local stack (same env/boot/auth pattern as `local-loop.sh`). Pilot host is off-limits.

### What it writes (gitignored)

Under `.agent/tmp/local-soak-<UTC-stamp>/`:

| Path | Contents |
| --- | --- |
| `results.jsonl` | Per-anchor soak-v3 record shape + `focus: null` |
| `summary.md` | ready/floor_unmet/error counts, per-bucket, block types, rejected reasons, delta vs 07-15 host soak |
| `lessons/<anchor_id>.json` | Full `lesson_json` for ready bakes (kept for quality re-review) |
| `failures/<anchor_id>.json` | **Mandatory retention**: `request` + full `progress` / `failure_detail` for every non-ready |

Never commit anchors, lessons, failures, or results — only this script and README.

### Options

- `-h`, `--help`: Usage summary
- `--anchors <file>`: Anchors JSON (default: `.agent/tmp/soak-anchors.json`)
- `--out-root <dir>`: Output parent (default: `.agent/tmp`)
- `--duration 45|60|90`: Lesson duration (default: `45`)
- `--stop`: Stop any kept local-loop/local-soak stack (`local-loop.sh --stop`)
- `--dry-run`: Validate env/anchors assembly and exit

### Example

```bash
./hramatka/ops/local-soak.sh --dry-run   # env + anchors check
./hramatka/ops/local-soak.sh             # full 18-anchor soak (~40–90 min)
./hramatka/ops/local-soak.sh --stop      # if a stack was left running
```

## Local teacher application

Run the complete teacher UI and real baker locally with one foreground command:

```bash
./hramatka/ops/local-teacher.sh
```

The launcher builds the frontend, starts Uvicorn on `127.0.0.1:8788`, and serves the
same-origin teacher application over HTTPS on `127.0.0.1:8443`. Both loopback ports are
reserved before the build begins and handed directly to the child servers, so another process
cannot claim either port during startup.

### Trusted local TLS (one-time setup)

The launcher first uses the valid local certificate pair at
`$HOME/.local/state/hramatka/tls-cert.pem` and
`$HOME/.local/state/hramatka/tls-key.pem`. Install the mkcert root once, then create or refresh
that pair when needed:

```bash
mkcert -install
state_dir="$HOME/.local/state/hramatka"
mkdir -p "$state_dir"
umask 077
mkcert -cert-file "$state_dir/tls-cert.pem" -key-file "$state_dir/tls-key.pem" \
  127.0.0.1 localhost
```

With that pair and the one-time trust installation, browsers trust the local teacher URL without
an interstitial. The launcher never changes the system trust store itself. If either file is
missing or invalid, it falls back to a private temporary self-signed certificate and tells the
operator that a browser warning is expected.

### Default local teacher session

The default command creates or reuses a bookmarkable, durable local teacher account and prints
its loopback-only URL. It stores that account's SQLite file under
`$HOME/.local/state/hramatka/`, sets the local-launcher marker and loopback binding proof, and
shows the persistent unauthenticated-mode warning in the app. The local session route is not
registered without all of those conditions and is not part of the deployed pilot path.

The default launcher also permits the Pro receipt's explicit
`cli_self_reported` provenance tier only in that marked local environment. The API configuration
default remains `api_observed`, and deployed/pilot starts keep their invite door.

To intentionally use the temporary database and one-use 72-hour invite flow instead:

```bash
HRAMATKA_LOCAL_STATIC_TEACHER=0 ./hramatka/ops/local-teacher.sh
```

Prerequisites:

- the repository `.venv` and its exact `.venv/bin/python` interpreter;
- Node.js, npm, and OpenSSL;
- a verified data release selected by `HRAMATKA_DATA_DIR` plus
  `HRAMATKA_DATA_MANIFEST`, or available beneath `HRAMATKA_DATA_RELEASES_ROOT`;
- an Antigravity subscription CLI. The wrapper first uses
  `$HOME/.local/bin/agy`, then `agy` on `PATH`, and passes the selected executable only to the
  backend process.

The launcher enables only the subscription route by default (#563): no OpenRouter, Google AI
Studio, Vertex, or DeepInfra credential is required, constructed, or called. Google AI Studio is
also excluded from this local default because its unqualified Flash route would correctly fail the
exact-route gate. Set `HRAMATKA_BAKE_PROVIDERS` explicitly (see `PROVIDER_REQUIREMENTS` in
`hramatka/ops/local_teacher.py`) to opt into an additional provider and its credential. If a
requested provider credential or the required subscription CLI is absent, startup fails instead of
silently showing an empty model picker.

The model picker exposes only models that pass the separate qualification gate. `--api-port` and
`--https-port` override the loopback ports when necessary.

Press Ctrl-C to interrupt any startup phase and stop every owned process group. The launcher waits
up to three seconds for each group to exit, then escalates from `SIGTERM` to `SIGKILL` and waits
up to one further second. A terminated descendant's PID can remain visible briefly while the
operating system reaps it, so downstream checks must poll rather than assume an immediate PID
lookup failure. The temporary CSRF material, fallback certificate/key, and logs are mode-private
and are removed on normal exit, child failure, or a termination signal. The trusted user-local
certificate pair and durable local-teacher database are intentionally retained. With
`HRAMATKA_LOCAL_STATIC_TEACHER=0`, the temporary database is also removed and a new invocation
creates one one-use invite.

### Development runner (`./services.sh`)

For daemon-style lifecycle management, use the repository runner from the repository root:

```bash
./services.sh start     # start the teacher app in the background, print the one-use invite URL
./services.sh status    # report whether the teacher is running
./services.sh stop      # stop it cleanly (no orphans, no stale runtime state)
./services.sh restart   # stop then start
./services.sh build     # frontend build using the existing dependency tree
./services.sh rebuild   # stage/build the frontend, swap it, then start under one lock
./services.sh clean     # stop, then remove dist/log/pid state
./services.sh help      # full usage
```

The runner daemonizes `local-teacher.sh --skip-build`, waits for the app to become ready, and
prints the same bookmarkable local-teacher URL by default. Set
`HRAMATKA_LOCAL_STATIC_TEACHER=0` for a one-use invite instead. Same prerequisites as the
foreground launcher above.

Hard rules (same as the foreground launcher, plus runner-specific ones):

- Loopback only (`127.0.0.1`); refuses to run from a production-looking checkout
  (`/opt/hramatka*`) or with a non-loopback `HRAMATKA_PILOT_ORIGIN`.
- Never runs `npm install` or `npm ci` (2026-08-04 npm worm mitigation). The frontend build
  uses the existing `hramatka/app/node_modules` tree; when that tree and `dist` are both missing
  the runner prints exactly what to restore instead of installing.
- Restart leaves no orphaned processes and no stale runtime state: a stale
  `hramatka/app/.real-e2e` directory is removed before every start.
- Rebuild preflights tools, credentials, data, and the existing dependency tree;
  it builds and CSP-checks a staged artifact before stopping a healthy service
  or replacing `dist`, then swaps and starts under one lifecycle lock. A failed
  start restores the previous artifact, and foreign listeners are never killed.
- Missing prerequisites (repository `.venv`, credentials, data release, unbuilt frontend) fail
  with a specific message naming the missing item and the fix, never a stack trace or a hang.
- Loopback ports default to 8443 (https) / 8788 (api); override with
  `HRAMATKA_DEV_HTTPS_PORT` / `HRAMATKA_DEV_API_PORT`.
