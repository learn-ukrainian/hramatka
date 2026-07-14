import { defineConfig, devices } from '@playwright/test';

/**
 * Live acceptance harness for hramatka pilot (Sol 9-item + workbench).
 * Points at the REAL deployed origin with prod CSP.
 * NO webServer (the stub one is deliberately avoided).
 * baseURL is the live teacher root.
 *
 * Usage:
 *   cd hramatka/app
 *   LIVE_INVITE_URL="https://hramatka.46-225-212-209.sslip.io/teacher/#invite=..." \
 *     npx playwright test -c ../../acceptance/playwright.live.config.ts
 *
 * The invite URL must be passed via env (never committed).
 * Run headless (default). Capture console + pageerror on every page.
 */
export default defineConfig({
  testDir: '.',
  testMatch: 'live-acceptance.spec.ts',
  fullyParallel: false, // sequential, stateful teacher session + long bakes
  forbidOnly: !!process.env.CI,
  retries: 0, // live: no auto retry on flakes; manual analysis
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  timeout: 30 * 60 * 1000, // 30 min per test (bakes can approach 25m)
  expect: {
    timeout: 30 * 1000,
  },
  use: {
    baseURL: 'https://hramatka.46-225-212-209.sslip.io',
    // The target uses a valid *.sslip.io cert in prod; keep strict unless live cert hiccup.
    // ignoreHTTPSErrors: true,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 15_000,
    navigationTimeout: 60_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
