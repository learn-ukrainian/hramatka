/**
 * Resolve the HTTPS proxy port for real-backend E2E.
 *
 * Contract:
 * - Explicit HRAMATKA_PROXY_PORT / HRAMATKA_E2E_PORT / HRAMATKA_E2E_HTTPS_PORT wins.
 * - Under CI (CI=true|1) without an explicit port, allocate an ephemeral free
 *   loopback port (OS assign via listen(0), then close). This is the Node/Playwright
 *   idiomatic pattern so a zombie on :5174 cannot wedge the self-hosted runner.
 * - Outside CI, default remains 5174 for local human muscle memory.
 *
 * Resolution runs BEFORE Playwright loads its config (pinned @playwright/test 1.61.x
 * rejects `defineConfig(async () => …)`). npm scripts, CI workflow steps, and
 * `run-real-backend.sh` call this module as a CLI and export the port; the
 * Playwright configs only read the already-resolved env synchronously.
 */
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const DEFAULT_LOCAL_E2E_PORT = 5174;

/**
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {number | null}
 */
export function parseExplicitE2ePort(env = process.env) {
  const raw =
    env.HRAMATKA_PROXY_PORT
    || env.HRAMATKA_E2E_PORT
    || env.HRAMATKA_E2E_HTTPS_PORT
    || '';
  if (raw === '') {
    return null;
  }
  if (!/^[1-9]\d{0,4}$/.test(raw)) {
    throw new Error(`invalid e2e proxy port: ${raw}`);
  }
  const port = Number.parseInt(raw, 10);
  if (port > 65535) {
    throw new Error(`invalid e2e proxy port: ${raw}`);
  }
  return port;
}

/**
 * Bind 127.0.0.1:0, read the kernel-assigned port, close.
 * Same primitive used by get-port and Playwright's own examples.
 * @returns {Promise<number>}
 */
export function freeLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      if (!address || typeof address === 'string') {
        server.close();
        reject(new Error('failed to allocate loopback port'));
        return;
      }
      const { port } = address;
      server.close((error) => {
        if (error) {
          reject(error);
          return;
        }
        resolve(port);
      });
    });
  });
}

/**
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {Promise<number>}
 */
export async function resolveE2ePort(env = process.env) {
  const explicit = parseExplicitE2ePort(env);
  if (explicit !== null) {
    return explicit;
  }
  if (env.CI === 'true' || env.CI === '1') {
    return freeLoopbackPort();
  }
  return DEFAULT_LOCAL_E2E_PORT;
}

/**
 * Synchronous port for Playwright config load (must not allocate).
 *
 * Prefers explicit env; local default 5174; under CI without env, throws so
 * launchers are forced to pre-export via `node e2e/resolve-e2e-port.mjs`.
 *
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {number}
 */
export function resolvedE2ePortFromEnv(env = process.env) {
  const explicit = parseExplicitE2ePort(env);
  if (explicit !== null) {
    return explicit;
  }
  if (env.CI === 'true' || env.CI === '1') {
    throw new Error(
      'HRAMATKA_PROXY_PORT (or HRAMATKA_E2E_PORT / HRAMATKA_E2E_HTTPS_PORT) must be set '
      + 'before loading Playwright real-backend config under CI. '
      + 'Example: export HRAMATKA_PROXY_PORT=$(node e2e/resolve-e2e-port.mjs)',
    );
  }
  return DEFAULT_LOCAL_E2E_PORT;
}

/**
 * @param {number} port
 * @returns {string}
 */
export function e2eOrigin(port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`invalid e2e proxy port: ${port}`);
  }
  return `https://127.0.0.1:${port}`;
}

const invokedDirectly = process.argv[1]
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;

if (invokedDirectly) {
  const port = await resolveE2ePort();
  process.stdout.write(`${port}\n`);
}
