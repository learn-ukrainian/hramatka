import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const appDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const expectedStateDirectory = path.join(appDirectory, '.real-e2e');
const configuredStateDirectory = process.env.HRAMATKA_E2E_STATE_DIR || '';
if (path.resolve(configuredStateDirectory) !== expectedStateDirectory) {
  throw new Error('refusing to use an unexpected real-backend state path');
}
const stateDirectory = expectedStateDirectory;
const ownerMarkerPath = path.join(stateDirectory, 'owner');
const apiLogPath = path.join(stateDirectory, 'api.log');
const apiStatePath = path.join(stateDirectory, 'api-state.json');
const apiPidPath = path.join(stateDirectory, 'api.pid');
const READY_PAYLOAD_LIMIT = 256;
const ownerToken = process.env.HRAMATKA_E2E_OWNER_TOKEN || '';
if (!/^[a-f0-9]{64}$/.test(ownerToken)) {
  throw new Error('missing or invalid HRAMATKA_E2E_OWNER_TOKEN');
}
let apiProcess = null;
let apiSpawnError = null;
let stopping = false;

async function ownerMatches() {
  const stateStats = await fs.lstat(stateDirectory).catch(() => null);
  const markerStats = await fs.lstat(ownerMarkerPath).catch(() => null);
  if (
    !stateStats?.isDirectory()
    || stateStats.isSymbolicLink()
    || !markerStats?.isFile()
    || markerStats.isSymbolicLink()
  ) {
    return false;
  }
  const marker = await fs.readFile(ownerMarkerPath, 'utf8').catch(() => null);
  return marker === `${ownerToken}\n`;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function childIsTerminal(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function parseReadyPayload(raw, child) {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error('real-backend API published malformed readiness');
  }
  if (
    !parsed
    || typeof parsed !== 'object'
    || !Number.isInteger(parsed.pid)
    || parsed.pid !== child.pid
    || !Number.isInteger(parsed.port)
    || parsed.port < 1
    || parsed.port > 65535
    || typeof parsed.python_prefix !== 'string'
    || !parsed.python_prefix
  ) {
    throw new Error('real-backend API published invalid readiness');
  }
  return parsed;
}

function waitForApiIdentity(child, readiness) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let buffer = Buffer.alloc(0);
    const finish = (error, identity) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timeout);
      readiness.off('data', onData);
      readiness.off('end', onEnd);
      readiness.off('error', onError);
      child.off('exit', onExit);
      if (error) {
        reject(error);
      } else {
        resolve(identity);
      }
    };
    const onData = (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > READY_PAYLOAD_LIMIT) {
        finish(new Error('real-backend API readiness payload exceeded its limit'));
      }
    };
    const onEnd = () => {
      const payload = buffer.toString('utf8');
      if (!payload.endsWith('\n') || payload.slice(0, -1).includes('\n')) {
        finish(new Error('real-backend API closed with invalid readiness framing'));
        return;
      }
      try {
        finish(null, parseReadyPayload(payload.slice(0, -1), child));
      } catch (error) {
        finish(error);
      }
    };
    const onError = () => finish(new Error('real-backend API readiness channel failed'));
    const onExit = () => finish(new Error('real-backend API stopped before binding'));
    const timeout = setTimeout(
      () => finish(new Error('real-backend API did not publish readiness in time')),
      15_000,
    );
    readiness.on('data', onData);
    readiness.once('end', onEnd);
    readiness.once('error', onError);
    child.once('exit', onExit);
    if (apiSpawnError || childIsTerminal(child)) {
      finish(new Error('real-backend API could not be started'));
    }
  });
}

async function writeOwnedFile(destination, content) {
  if (!(await ownerMatches())) {
    throw new Error('real-backend state ownership changed before write');
  }
  const temporary = `${destination}.${process.pid}.tmp`;
  await fs.writeFile(temporary, content, {
    encoding: 'utf8',
    flag: 'wx',
    mode: 0o600,
  });
  if (!(await ownerMatches())) {
    await fs.rm(temporary, { force: true });
    throw new Error('real-backend state ownership changed during write');
  }
  await fs.rename(temporary, destination);
}

async function writeApiState(identity) {
  await writeOwnedFile(apiPidPath, `${identity.pid}\n`);
  await writeOwnedFile(
    apiStatePath,
    `${JSON.stringify({ ...identity, owner: ownerToken })}\n`,
  );
}

function healthReady(port) {
  return new Promise((resolve) => {
    const request = http.get(
      { host: '127.0.0.1', port, path: '/api/healthz', timeout: 500 },
      (response) => {
        response.resume();
        resolve(response.statusCode === 200);
      },
    );
    request.on('error', () => resolve(false));
    request.on('timeout', () => {
      request.destroy();
      resolve(false);
    });
  });
}

async function waitForApiReady(child, port) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (apiSpawnError) {
      throw new Error('real-backend API could not be started');
    }
    if (childIsTerminal(child)) {
      throw new Error('real-backend API stopped before readiness');
    }
    if (await healthReady(port)) {
      return;
    }
    await delay(50);
  }
  throw new Error('real-backend API did not become ready');
}

async function waitForExit(child, milliseconds) {
  if (apiSpawnError) {
    return true;
  }
  if (childIsTerminal(child)) {
    return true;
  }
  return Promise.race([
    new Promise((resolve) => child.once('exit', () => resolve(true))),
    delay(milliseconds).then(() => false),
  ]);
}

function signalApi(signalName) {
  if (!apiProcess || !apiProcess.pid || childIsTerminal(apiProcess)) {
    return;
  }
  try {
    apiProcess.kill(signalName);
  } catch (error) {
    if (error.code !== 'ESRCH') {
      throw error;
    }
  }
}

async function shutdown(exitCode) {
  if (stopping) {
    return;
  }
  stopping = true;
  if (apiProcess) {
    apiProcess.stdin?.end();
    signalApi('SIGTERM');
    if (!(await waitForExit(apiProcess, 1_200))) {
      signalApi('SIGKILL');
      await waitForExit(apiProcess, 300);
    }
  }
  process.exit(exitCode);
}

async function runRealBackendProxy() {
  if (!(await ownerMatches())) {
    throw new Error('real-backend state is not owned by this run');
  }
  const python = process.env.HRAMATKA_E2E_PYTHON;
  if (!python) {
    throw new Error('missing HRAMATKA_E2E_PYTHON');
  }
  const apiLog = await fs.open(apiLogPath, 'w', 0o600);
  const childEnvironment = {
    ...process.env,
    HRAMATKA_E2E_READY_FD: '3',
    HRAMATKA_E2E_STATE_DIR: stateDirectory,
  };
  delete childEnvironment.FORCE_COLOR;
  delete childEnvironment.NO_COLOR;
  apiProcess = spawn(
    python,
    ['-m', 'hramatka.app.e2e.real_backend_server'],
    {
      cwd: process.cwd(),
      detached: false,
      env: childEnvironment,
      stdio: ['pipe', apiLog.fd, apiLog.fd, 'pipe'],
    },
  );
  apiProcess.once('error', (error) => {
    apiSpawnError = error;
  });
  apiProcess.once('exit', () => {
    if (!stopping) {
      void shutdown(1);
    }
  });
  await apiLog.close();

  for (const [signalName, exitCode] of [
    ['SIGINT', 130],
    ['SIGTERM', 143],
    ['SIGHUP', 129],
  ]) {
    process.once(signalName, () => {
      void shutdown(exitCode);
    });
  }
  process.once('exit', () => {
    if (!stopping) {
      try {
        apiProcess?.stdin?.destroy();
        signalApi('SIGKILL');
      } catch {
        // Best effort only in the synchronous exit hook.
      }
    }
  });

  const readiness = apiProcess.stdio[3];
  if (!readiness) {
    throw new Error('real-backend API readiness channel was not created');
  }
  const identity = await waitForApiIdentity(apiProcess, readiness);
  await writeApiState(identity);
  await waitForApiReady(apiProcess, identity.port);
  if (apiSpawnError || childIsTerminal(apiProcess)) {
    throw new Error('real-backend API stopped immediately after readiness');
  }
  process.env.HRAMATKA_PROXY_API_PORT = String(identity.port);
  await import('../scripts/https-static-proxy.mjs');
}

const invokedDirectly = process.argv[1]
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invokedDirectly) {
  try {
    await runRealBackendProxy();
  } catch {
    await shutdown(1);
  }
}
