# Hramatka

Secret-free teacher product repository.

## Layout

- `hramatka/app` — teacher frontend (Vite/React)
- `hramatka/api` — teacher API
- `hramatka/engine` — lesson engine
- `hramatka/` — Python package (`import hramatka…`)
- `tests/` — product qualification / teacher API tests
- `docs/` — product docs

Host inventory, credentials, and deploy topology stay in `learn-ukrainian-infra-private` only.

## Local checks

```bash
# Frontend
cd hramatka/app && npm ci && npm run typecheck && npm run lint && npm test

# Python (from repo root)
export PYTHONPATH="$PWD"
pytest -q hramatka/engine/tests
```
