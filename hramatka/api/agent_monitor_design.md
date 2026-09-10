# Agent Process & Resource Monitor API — Design Spec

- **Status**: APPROVED ADVISOR DESIGN (Sol `gpt-5.6-sol` @ `xhigh`, 2026-07-22)
- **Target Repo**: `learn-ukrainian-infra-private` (`hramatka/api/agent_monitor.py`)
- **Host Target**: (private infra) — size/topology live only in `learn-ukrainian-infra-private`; not recorded here.

---

## 1. Goal & Architecture

Prevents multi-agent workload overload, OOM crashes, and CPU contention on `hramatka` by providing an atomic capacity reservation and real-time process monitor API.

### Core Capacity Invariant:
`host_reserved (1250 MB) + active_reservations + requested_hard_limit <= safe_allocatable_memory (~2800 MB)`

---

## 2. API Endpoints (`/api/agent-monitor/`)

1. **`GET /api/agent-monitor/status`**:
   - Exposes system load (`1m`, `5m`, `15m`), physical RAM (`MemAvailable`), total active agent leases, and process table status (`psutil`).

2. **`POST /api/agent-monitor/preflight`**:
   - Accepts requested hard RAM (`required_ram_mb`) and CPU cores.
   - Evaluates host invariant against live `MemAvailable`.
   - Returns `APPROVED`, `QUEUED` (with retry recommendation), or `REJECTED`.

3. **`POST /api/agent-monitor/register`**:
   - Atomically registers an agent lease:
     - `agent_id`: e.g. `gemini/2156-eval`
     - `task_name`: e.g. `lemko_dictionary_scraping`
     - `pid`: Process ID
     - `process_create_time`: Process start timestamp (prevents PID reuse collisions)
     - `reserved_ram_mb`: Expected memory allocation
   - Binds lease and returns `lease_token`.

4. **`POST /api/agent-monitor/heartbeat`**:
   - Keeps lease active. Automatic expiry after 300s of missed heartbeats.

5. **`POST /api/agent-monitor/release`**:
   - Explicitly releases lease upon task completion.

---

## 3. Database Schema (`agent_leases`)

```sql
CREATE TABLE IF NOT EXISTS agent_leases (
    lease_token TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    task_name TEXT NOT NULL,
    pid INTEGER NOT NULL,
    process_create_time REAL NOT NULL,
    reserved_ram_mb INTEGER NOT NULL,
    status TEXT NOT NULL, -- APPROVED, EXPIRED, RELEASED
    created_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL
);
```
