import { defineConfig, devices } from '@playwright/test';
import path from 'path';

export default defineConfig({
  testDir: './e2e',
  // real-backend.spec.ts needs the real-API rig from
  // playwright.real-backend.config.ts (invite token, HTTPS proxy) — the stub
  // suite must not pick it up.
  testIgnore: 'real-backend.spec.ts',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  webServer: [
    {
      // Every stub E2E spec owns and tears down its own API server on 8787.
      // Starting another one here races those fixtures and intermittently drops
      // the Vite proxy mid-suite. Playwright only needs to own the static preview.
      command: 'npm run build && npm run preview',
      url: 'http://localhost:5173/teacher/',
      reuseExistingServer: false,
      timeout: 120 * 1000,
    },
  ],
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
