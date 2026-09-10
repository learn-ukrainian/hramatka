# Hramatka ops

Product code for Hramatka lives in this repository. **Host topology, deploy
inventory, credentials wiring, and backup destinations stay private** under
`learn-ukrainian/learn-ukrainian-infra-private` (`hramatka/ops/iac/` and related
runbooks). This tree is documentation and operator trails only — it is not a
deploy root.

## What belongs here

| Path | Role |
| --- | --- |
| `hramatka/ops/README.md` | This file — public-safe ops orientation |
| `hramatka/ops/trails/` | Operator decision trails / notes safe for the public product repo |

## What does **not** belong here

Deleted pilot helpers that used to live beside this README (for example
`deploy.sh`, `pilot-vps.md`, `local-loop.sh`, `backup-db.sh`,
`probe-json-mode.py`, soak scripts) were removed with the P2.4 private shrink.
Do **not** resurrect them in the public product repo.

- **Deploy / host cutover** — private infra runbooks + Ansible inventory only.
- **Secrets / DSN / SSH** — private repo / authorized private coordination only
  (AC-WARTIME).
- **Teacher release e2e gate** — tracked on the private Hramatka board (#417),
  not by scripts under this folder.

## Local development (product)

From a clean checkout of this repository:

```bash
# Frontend (app)
cd hramatka/app && npm ci && npm run typecheck && npm test

# Engine (DB-free CI surface + optional VESUM fixture suite)
python -m pip install -e ".[dev]"
pytest -q hramatka/engine/tests/test_boundary.py hramatka/engine/tests/test_vesum_gate.py
```

Browser / Playwright suites live under `hramatka/app/e2e/` (`npm run test:e2e`).
Real-backend Playwright configs need a local teacher stack; they are not the
default PR gate.

## Related boards

- Private extract / residual board: `learn-ukrainian-infra-private#444`
- Private teacher-ready epic board: `learn-ukrainian-infra-private#349`
- CI layout (P2.3): `.github/workflows/ci.yml` in this repo
