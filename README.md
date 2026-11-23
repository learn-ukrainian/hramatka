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

### Frontend

Use the exact Node and npm versions in the app's [package.json](hramatka/app/package.json).
The Node release pinned in [CI](.github/workflows/ci.yml) includes the required npm version.
`npm ci` uses the app's [install-script allowlist](hramatka/app/.npmrc).

From the repository root, change to `hramatka/app`.

```bash
cd hramatka/app
```

Run the frontend checks from that directory.

```bash
npm ci && npm run typecheck && npm run lint && npm test
```

If `npm ci` reports `EBADDEVENGINES` or `EBADENGINE`, switch to the pinned Node
release. With `nvm` and an existing Node installation, run this from `hramatka/app`:

```bash
nvm install "$(node -p 'require("./package.json").engines.node')"
```

If Node matches and npm still reports a version mismatch, restore the pinned npm
from the same directory:

```bash
npm install --global "npm@$(node -p 'require("./package.json").engines.npm')"
```

After fixing the version mismatch, rerun the frontend checks from `hramatka/app`.

### Python

```bash
# Python (from repo root)
export PYTHONPATH="$PWD"
pytest -q hramatka/engine/tests
```
