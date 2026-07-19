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

### Default configuration (post-#171)

| Knob | Default | Notes |
| --- | --- | --- |
| `HRAMATKA_GEN_JSON_MODE` | unset / not `"1"` | Opt-in only. local-loop and local-soak force `0`. |
| google-ais host | **hard-denied** | Even if env=`1`, transport never sends `response_format` to `google-ais`/`ais` hosts (production hang + probe: accept-but-thought-wrapped). |
| openrouter host | allowed when env=`1` | Probe: pure JSON, ~1s, both modes. |
| Latency watchdog | always on when telemetry ctx present | Emits `latency_watchdog` when a call exceeds 3× rolling median of prior samples in the bake. |

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
same-origin teacher application over temporary HTTPS on `127.0.0.1:8443`. It prints one fresh,
unredeemed invite URL once and does not open a browser. The browser will warn about the
short-lived self-signed certificate; the launcher does not modify the system trust store.
Both loopback ports are reserved before the build begins and handed directly to the child
servers, so another process cannot claim either port during startup.

Prerequisites:

- the repository `.venv` and its exact `.venv/bin/python` interpreter;
- Node.js, npm, and OpenSSL;
- a verified data release selected by `HRAMATKA_DATA_DIR` plus
  `HRAMATKA_DATA_MANIFEST`, or available beneath `HRAMATKA_DATA_RELEASES_ROOT`;
- `HRAMATKA_AIS_API_KEY`, or a local `~/.secret/google-ais.key` file.

OpenRouter is optional. If its environment credential or configured key file is absent, the
launcher explicitly uses Google AI Studio only. If `HRAMATKA_BAKE_PROVIDERS` requests a route
whose credential is absent, startup fails instead of silently changing routes.

Use `--model <provider/model>` to select a model that has passed the separate qualification
gate. The launcher passes that value to the baker and deliberately does not maintain a second
model allowlist. `--api-port` and `--https-port` override the loopback ports when necessary.

Press Ctrl-C to interrupt any startup phase and stop every owned process group. The temporary
database, CSRF material, certificate, key, and logs are mode-private and are removed on normal
exit, child failure, or a termination signal. A new invocation always creates a new database
and one-use invite.
