/**
 * E2E for the dual-language (uk/en) chrome — ports the demo's EN/УКР header toggle.
 * Default UA; toggle → EN chrome across catalog, paste, lesson review, conductor and the
 * failure card; toggle back → UA restored; reload keeps the choice (localStorage).
 *
 * Shares the dev stub on 8787 with teacher-flow.spec.ts. Under the required run
 * (CI=1 → workers:1) spec files execute serially, so the two stubs never overlap.
 */
import { test, expect } from '@playwright/test';
import { spawn, ChildProcess } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const STUB_PORT = 8787;
const APP = 'http://localhost:5173';
const TEST_TOKEN = 'A'.repeat(42) + 'Q';

let stubProc: ChildProcess | null = null;

async function startStub() {
  if (stubProc) return;
  const serverPath = path.resolve(__dirname, '../dev/server.js');
  stubProc = spawn('node', [serverPath], {
    env: { ...process.env, PORT: String(STUB_PORT) },
    stdio: 'pipe',
  });
  await new Promise((r) => setTimeout(r, 1400));
}

async function stopStub() {
  if (stubProc) {
    stubProc.kill('SIGTERM');
    stubProc = null;
  }
}

async function redeem(page: import('@playwright/test').Page) {
  await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
  await page.reload();
  await page.waitForURL(/\/teacher\/?$/);
  await expect(page.getByRole('heading', { name: 'Створити новий урок' })).toBeVisible({ timeout: 10000 });
}

test.describe('Hramatka dual-language chrome E2E (stub)', () => {
  test.beforeAll(async () => {
    await startStub();
  });
  test.afterAll(async () => {
    await stopStub();
  });

  test.beforeEach(async ({ page, context }) => {
    await context.clearCookies();
    await page.goto(APP);
    await page.evaluate(async () => {
      localStorage.clear();
      await fetch('/api/dev/clear-session', { method: 'POST' }).catch(() => {});
      await fetch('/api/dev/reset', { method: 'POST' }).catch(() => {});
    });
  });

  test('toggle EN localizes paste and separate My Lessons chrome, reverses to UA, and persists across reload', async ({ page }) => {
    await redeem(page);

    const toggle = page.getByTestId('lang-toggle');
    await expect(toggle).toHaveText('EN'); // default UA → button offers EN

    // → EN
    await toggle.click();
    await expect(toggle).toHaveText('УКР');
    await expect(page.getByRole('heading', { name: 'Create a new lesson' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'My lessons' })).toBeVisible();
    await expect(page.getByRole('button', { name: /Generate lesson/ })).toBeVisible();
    await expect(page.getByPlaceholder(/Paste Ukrainian text/)).toBeVisible();

    await page.getByRole('button', { name: 'My lessons' }).click();
    await expect(page.getByRole('heading', { name: 'My lessons' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Refresh list' })).toBeVisible();
    await expect(page.getByText('Create your first lesson:')).toBeVisible();

    // → back to UA (reversible)
    await toggle.click();
    await expect(toggle).toHaveText('EN');
    await expect(page.getByRole('heading', { name: 'Мої заняття' })).toBeVisible();

    // → EN again, then reload keeps the choice (no re-redeem; session cookie survives)
    await toggle.click();
    await expect(toggle).toHaveText('УКР');
    await page.reload();
    await expect(page.getByTestId('lang-toggle')).toHaveText('УКР');
    await expect(page.getByRole('heading', { name: 'My lessons' })).toBeVisible({ timeout: 10000 });
  });

  test('EN chrome across lesson review and conductor (lesson content stays Ukrainian)', async ({ page }) => {
    await redeem(page);
    await page.getByTestId('lang-toggle').click(); // → EN

    const anchor = 'Текст для перевірки двомовного інтерфейсу у режимі перегляду й проведення.';
    await page.getByPlaceholder(/Paste Ukrainian text/).fill(anchor);
    await page.getByRole('button', { name: /Generate lesson/ }).click();

    // Review chrome in English
    await page.waitForSelector('.block', { timeout: 15000 });
    await expect(page.getByRole('heading', { name: /Test 1/ })).toBeVisible({ timeout: 5000 });
    await expect(page.getByRole('button', { name: 'Review mode' })).toBeVisible();
    await expect(page.locator('.teacher-key strong').first()).toHaveText('Answer key:');
    // Lesson CONTENT stays Ukrainian even with EN chrome (the anchor text is unchanged UA)
    // (the ActivityPlayer renders Ukrainian content; a Cyrillic activity string is present)
    await expect(page.locator('.bcontent').first()).toContainText(/[Ѐ-ӿ]/);

    // Ack any warnings, then accept (EN labels)
    const ackButtons = page.locator('.ack-btn');
    const nAcks = await ackButtons.count();
    for (let i = 0; i < nAcks; i++) {
      if (await ackButtons.first().isVisible()) {
        await ackButtons.first().click();
        await page.waitForTimeout(120);
      }
    }
    const acceptBtn = page.getByRole('button', { name: /Accept lesson/ });
    if (await acceptBtn.isVisible()) await acceptBtn.click();
    await expect(page.getByText('Accepted', { exact: true }).first()).toBeVisible({ timeout: 8000 });

    // Enter the conductor and assert EN chrome
    await page.getByRole('button', { name: /Run the lesson/ }).click();
    await expect(page.getByText(/Running the lesson/)).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.cond-rail').first()).toContainText('Teaching');
    await expect(page.locator('.cond-clock').first()).toContainText('budget');
    await expect(page.getByRole('button', { name: /Done/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Skip/ })).toBeVisible();

    // Student view banner in English
    await page.getByRole('button', { name: /Student view/ }).click();
    await expect(page.locator('.rolebanner.student')).toContainText('STUDENT SCREEN — no answers or hints');
  });

  test('failure card is localized and reverses back to UA while shown', async ({ page }) => {
    await redeem(page);
    await page.getByTestId('lang-toggle').click(); // → EN

    await page.getByPlaceholder(/Paste Ukrainian text/).fill('bad bake text');
    await page.evaluate(() => {
      const BAD = '00000000-0000-0000-0000-000000000bad';
      // @ts-ignore
      crypto.randomUUID = () => BAD;
    });
    await page.getByRole('button', { name: /Generate lesson/ }).click();

    const recovery = page.getByTestId('failure-recovery');
    await expect(recovery).toBeVisible({ timeout: 10000 });
    await expect(recovery).toContainText('The lesson could not be created');
    await expect(page.getByRole('button', { name: 'Create the lesson again from this text' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Back to the list' })).toBeVisible();

    // Toggling back re-renders the SAME overlay in Ukrainian (reversible chrome)
    await page.getByTestId('lang-toggle').click(); // → UK
    await expect(recovery).toContainText('Не вдалося створити урок');
    await expect(page.getByRole('button', { name: 'Створити урок ще раз із цим текстом' })).toBeVisible();
  });
});
