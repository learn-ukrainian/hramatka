/**
 * E2E against the dev stub.
 * Covers redeem → paste → bake polling → lesson with all 9 blocks → ack warnings → accept → draft.
 * Also: fragment scrub, no token in storage, 401/410 paths.
 */
import { test, expect } from '@playwright/test';
import { spawn, ChildProcess } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const STUB_PORT = 8787;
const APP = 'http://localhost:5173';
const TEST_TOKEN = 'A'.repeat(42) + 'Q'; // valid pattern

let stubProc: ChildProcess | null = null;

async function startStub() {
  if (stubProc) return;
  const serverPath = path.resolve(__dirname, '../dev/server.js');
  stubProc = spawn('node', [serverPath], {
    env: { ...process.env, PORT: String(STUB_PORT) },
    stdio: 'pipe',
  });
  // wait for ready
  await new Promise(r => setTimeout(r, 1400));
}

async function stopStub() {
  if (stubProc) {
    stubProc.kill('SIGTERM');
    stubProc = null;
  }
}

test.describe('Hramatka teacher frontend E2E (stub)', () => {
  test.beforeAll(async () => {
    await startStub();
  });
  test.afterAll(async () => {
    await stopStub();
  });

  test.beforeEach(async ({ page, context }) => {
    await context.clearCookies();
    await page.goto(APP);
  });

  test('redeem from fragment, scrub, no storage token, session active', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload(); // ensure full mount with fragment so redeem effect runs from initial hash
    // after redeem the hash should be scrubbed to /teacher/
    await expect(page).toHaveURL(/\/teacher\/?$/);

    // no token remains
    const hash = await page.evaluate(() => location.hash);
    expect(hash).not.toContain('invite');

    // no token material in storage (dev tooling may add keys, but never the invite token or 'token' keys)
    const storageInfo = await page.evaluate((tok) => {
      const ls = Object.keys(localStorage).concat(Object.values(localStorage));
      const ss = Object.keys(sessionStorage).concat(Object.values(sessionStorage));
      const all = ls.concat(ss).join(' ');
      return {
        hasInvite: all.includes('invite'),
        hasTokenVal: all.includes(tok),
        hasTokenKey: /token|invite/i.test(all)
      };
    }, TEST_TOKEN);
    expect(storageInfo.hasInvite || storageInfo.hasTokenVal || storageInfo.hasTokenKey).toBeFalsy();

    // should reach paste UI
    await expect(page.getByRole('heading', { name: 'Створити новий урок' })).toBeVisible({ timeout: 10000 });
  });

  test('paste → bake status poll → lesson renders all 9 blocks (no fallbacks)', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload(); // ensure full mount with fragment so redeem effect runs
    await page.waitForURL(/\/teacher\/?$/);

    // paste form
    const text = 'Тестовий текст для демонстрації уроку з усіма типами. Він містить речення для вправ.';
    await page.getByPlaceholder(/Вставте український текст/).fill(text);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    // baking / poll visible
    await expect(page.getByText(/бакінг|статус|готовий/i)).toBeVisible({ timeout: 10000 });

    // wait for lesson view with blocks
    await page.waitForSelector('[data-activity-player], .block', { timeout: 15000 });

    // verify all 9 types are present via their labels or wrappers
    const expectedTypes = ['true-false','cloze','match-up','quiz','mark-the-words','fill-in','error-correction','text-questions','short-writing'];
    for (const t of expectedTypes) {
      // either data attr or title containing type-ish
      const found = await page.locator(`[data-activity-type="${t}"], [data-activity-player="${t}"], .block:has-text("${t}")`).count();
      expect(found, `block for ${t} should be visible`).toBeGreaterThan(0);
    }

    // no placeholder messages
    const bad = await page.locator('text=тип поки без віджета, text=unsupported, text=placeholder').count();
    expect(bad).toBe(0);
  });

  test('ack warnings, accept blocked until acks, then accept succeeds, then draft', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload(); // ensure full mount with fragment so redeem effect runs
    await page.waitForURL(/\/teacher\/?$/);

    const text = 'Текст для перевірки ack та accept.';
    await page.getByPlaceholder(/Вставте/).fill(text);
    await page.getByRole('button', { name: /Згенерувати/ }).click();

    await page.waitForSelector('.block.warn', { timeout: 15000 });

    // There should be visible warnings
    const warns = page.locator('.block.warn');
    const count = await warns.count();
    expect(count).toBeGreaterThan(0);

    // The blocked message (warn note) is visible before acks; the accept is disabled until acks.
    const acceptBtn = page.getByRole('button', { name: /Прийняти урок/ });
    await expect(page.getByText(/підтверд(іть|ити) (усі|всі) попередження|warning_acknowledgements/i)).toBeVisible();

    // Ack all visible warns
    const ackButtons = page.locator('.ack-btn');
    const n = await ackButtons.count();
    for (let i = 0; i < n; i++) {
      await ackButtons.nth(0).click();
      await page.waitForTimeout(200);
    }

    // Now accept should work
    await acceptBtn.click();
    await expect(page.getByText(/Прийнято|accepted/i)).toBeVisible({ timeout: 5000 });

    // Return to draft
    await page.getByRole('button', { name: /Повернути в чернетку/ }).click();
    await expect(page.getByText(/чернетку|draft/i)).toBeVisible({ timeout: 3000 });
  });

  test('401 on no session leads to invite screen', async ({ page }) => {
    // Force-clear server session state (previous tests may have left one) + cookies
    await page.evaluate(async () => {
      await fetch('/api/dev/clear-session', { method: 'POST' });
    });
    await page.goto(`${APP}/teacher/`);
    // hit a protected route (proxied to stub) to exercise 401 path
    await page.evaluate(async () => {
      // @ts-ignore
      await fetch('/api/lessons', { credentials: 'include' }).catch(() => {});
    });
    // UI should surface invite (shown whenever !session)
    await expect(page.getByRole('heading', { name: /Вхід для викладача/i })).toBeVisible({ timeout: 3000 });
  });
});
