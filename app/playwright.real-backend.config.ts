import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: 'real-backend.spec.ts',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: 'list',
  outputDir: 'test-results/real-backend',
  use: {
    baseURL: 'https://127.0.0.1:5174',
    ignoreHTTPSErrors: true,
    trace: 'on-first-retry',
  },
  webServer: {
    command: [
      'rm -rf .real-e2e',
      'mkdir -p .real-e2e',
      'npm run build',
      'openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=127.0.0.1 -addext subjectAltName=IP:127.0.0.1 -keyout .real-e2e/key.pem -out .real-e2e/cert.pem >/dev/null 2>&1',
      'PYTHONPATH=../.. HRAMATKA_E2E_ORIGIN=https://127.0.0.1:5174 HRAMATKA_E2E_DB_PATH=$PWD/.real-e2e/pilot.sqlite3 HRAMATKA_E2E_INVITE_PATH=$PWD/.real-e2e/invite-token python -m hramatka.app.e2e.real_backend_server >.real-e2e/api.log 2>&1 & api_pid=$!; trap "kill $api_pid" EXIT; i=0; until curl --fail --silent http://127.0.0.1:8788/api/healthz >/dev/null; do i=$((i + 1)); [ $i -lt 100 ] || exit 1; sleep 0.1; done; HRAMATKA_E2E_FRONTEND_DIR=$PWD/dist HRAMATKA_E2E_TLS_CERT=$PWD/.real-e2e/cert.pem HRAMATKA_E2E_TLS_KEY=$PWD/.real-e2e/key.pem node e2e/https-static-proxy.mjs',
    ].join(' && '),
    url: 'https://127.0.0.1:5174/teacher/',
    ignoreHTTPSErrors: true,
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
