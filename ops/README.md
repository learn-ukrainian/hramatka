# Hramatka ops

Pilot deploy and smoke helpers. From a reviewed checkout:

`./hramatka/ops/deploy.sh <user@pilot-host>`

`deploy.sh` replaces the hand-rolled partial rsync (app dist + engine + api). It builds with CSP guard, rsyncs the whole `hramatka` package (explicit excludes), blocks restart during in-flight bakes unless `--force`, then restarts, polls readyz, prints migration version, runs `smoke.sh`, and prints bundle hash + git SHA. Preview locally: `./hramatka/ops/deploy.sh --dry-run /tmp/fake-deploy`.

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
- `--keep`: Keep the local uvicorn stack running and do not clean up the tmp database/logs.
- `--stop`: Terminate any kept stacks and clean up their state.
- `--dry-run`: Dry run to validate env assembly and resolved paths.

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

