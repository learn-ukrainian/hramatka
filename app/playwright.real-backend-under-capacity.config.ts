import { defineConfig, devices } from '@playwright/test';
import { e2eOrigin, resolvedE2ePortFromEnv } from './e2e/resolve-e2e-port.mjs';

const port = resolvedE2ePortFromEnv();
const origin = e2eOrigin(port);

export default defineConfig({
  testDir: './e2e',
  testMatch: 'real-backend-under-capacity.spec.ts',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: 'list',
  outputDir: 'test-results/real-backend-under-capacity',
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
      HRAMATKA_E2E_UNDER_CAPACITY: '1',
    },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
