import { defineConfig, devices } from '@playwright/test';
import { e2eOrigin, resolvedE2ePortFromEnv } from './e2e/resolve-e2e-port.mjs';

// Sync config only: pinned Playwright 1.61.x rejects defineConfig(async).
// Port is resolved before load (npm scripts / CI / run-real-backend.sh CLI)
// and threaded via HRAMATKA_PROXY_PORT so baseURL / webServer stay aligned.
const port = resolvedE2ePortFromEnv();
const origin = e2eOrigin(port);

export default defineConfig({
  testDir: './e2e',
  testMatch: 'real-backend-empty.spec.ts',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: 'list',
  outputDir: 'test-results/real-backend-empty',
  use: {
    baseURL: origin,
    ignoreHTTPSErrors: true,
    trace: 'on-first-retry',
  },
  webServer: {
    command: './e2e/run-real-backend.sh',
    url: `${origin}/teacher/`,
    ignoreHTTPSErrors: true,
    reuseExistingServer: false,
    timeout: 120_000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
    env: {
      ...process.env,
      HRAMATKA_PROXY_PORT: String(port),
      HRAMATKA_E2E_ORIGIN: origin,
      HRAMATKA_E2E_EMPTY_REGISTRY: '1',
    },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
