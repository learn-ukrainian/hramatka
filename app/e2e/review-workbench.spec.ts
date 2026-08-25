/**
 * E2E: review workbench mutations (move, remove→tray→restore→ack, reserve, edit validate).
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

async function loginAndBake(page: import('@playwright/test').Page) {
  await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
  await page.reload();
  await page.waitForURL(/\/teacher\/?$/);
  await page.getByPlaceholder(/Вставте український текст/).fill('Текст для перевірки робочого столу перегляду з усіма типами завдань.');
  await page.getByRole('button', { name: /Згенерувати урок/ }).click();
  await page.waitForSelector('[data-testid="review-workbench"]', { timeout: 15000 });
}

test.describe('Review workbench E2E (stub)', () => {
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
      await fetch('/api/dev/clear-session', { method: 'POST' }).catch(() => {});
      await fetch('/api/dev/reset', { method: 'POST' }).catch(() => {});
    });
  });

  // CONFIRMED E2E GUARD (per task):
  // The flows below (loginAndBake + review selectors) exercise import of review-helpers.ts
  // which statically imports the generated activityValidator.js.
  // - A require( or any top-level module init error → synchronous throw on import → blank page
  //   → subsequent waitForSelector for review-workbench or actions will timeout/fail.
  // - This catches BOTH the old eval-crash and the require-crash (the latter is unconditional,
  //   does not require CSP header).
  // The dev stub server (dev/server.js) does NOT set a strict CSP response header on assets
  // (it is API-only; Vite :5173 serves the app JS). Adding prod-like CSP to E2E (to also catch
  // eval regressions under header) is tracked in issue #123. The bundle guard + this E2E load
  // path together close the regression window for CI.

  test('move block within review document', async ({ page }) => {
    await loginAndBake(page);
    const before = await page.locator('.review-paper .dblock:not(.empty-phase)').count();
    await page.locator('[data-action="b-down"]').first().click();
    await page.waitForTimeout(400);
    await expect(page.locator('.banner.fail.error')).toHaveCount(0);
    const after = await page.locator('.review-paper .dblock:not(.empty-phase), .reserve-tray .dblock').count();
    expect(after).toBeGreaterThanOrEqual(before);
  });

  test('remove → rejected tray → restore → requires ack', async ({ page }) => {
    await loginAndBake(page);
    const plannedBefore = await page.locator('.review-paper .dblock:not(.empty-phase)').count();
    await page.locator('[data-action="b-del"]').first().click();
    await page.waitForTimeout(400);

    await expect(page.locator('[data-testid="rejected-tray"]')).toBeVisible();
    await page.locator('[data-action="toggle-rejected"]').click();
    await expect(page.locator('.rejected-block')).toBeVisible();

    await page.locator('[data-action="b-restore"]').first().click();
    await page.waitForTimeout(400);

    await expect(page.locator('.chip.warn').first()).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.mnote').filter({ hasText: /повернено з відхилених/ })).toBeVisible();

    const acceptBtn = page.getByRole('button', { name: /Прийняти заняття/ });
    await expect(acceptBtn).toBeDisabled();
    await page.locator('.ack-btn').first().click();
    await page.waitForTimeout(300);

    const plannedAfter = await page.locator('.review-paper .dblock:not(.empty-phase)').count();
    expect(plannedAfter).toBeGreaterThanOrEqual(plannedBefore);
  });

  test('review shows the saved duration but provides no duration mutation control', async ({ page }) => {
    await loginAndBake(page);
    await expect(page.getByTestId('review-duration-readonly')).toContainText('45');
    await expect(page.locator('[data-action="review-duration"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="reserve-tray"]')).toBeVisible();
  });

  test('edit validates and rejects empty instruction', async ({ page }) => {
    await loginAndBake(page);
    await page.locator('[data-action="b-edit"]').first().click();
    await expect(page.getByTestId('activity-editor')).toBeVisible();

    const instruction = page.locator('.activity-editor textarea').first();
    await instruction.fill('   ');
    await page.getByRole('button', { name: 'Зберегти зміни' }).click();
    await expect(page.getByTestId('editor-validation-errors')).toBeVisible();
    await expect(page.getByTestId('activity-editor')).toBeVisible();
  });

  test('edit with valid data saves without contentEditable', async ({ page }) => {
    await loginAndBake(page);
    await page.locator('[data-action="b-edit"]').first().click();
    const instruction = page.locator('.activity-editor textarea').first();
    await instruction.fill('Оновлена інструкція для перевірки.');
    await page.getByRole('button', { name: 'Зберегти зміни' }).click();
    await page.waitForTimeout(400);
    await expect(page.getByTestId('activity-editor')).toHaveCount(0);
    await expect(page.locator('.chip.ok').filter({ hasText: /змінено вами/ }).first()).toBeVisible();
  });

  test('editing an acknowledged warning requires and allows a fresh acknowledgement', async ({ page }) => {
    await loginAndBake(page);
    const warning = page.locator('.review-paper .dblock.warn').first();

    await warning.locator('[data-action="b-accept"]').click();
    await expect(warning.locator('.ack-btn')).toHaveCount(0);

    await warning.locator('[data-action="b-edit"]').click();
    await warning.locator('.activity-editor textarea').first().fill('Оновлена інструкція для попередження.');
    await warning.getByRole('button', { name: 'Зберегти зміни' }).click();

    await expect(warning.locator('.ack-btn')).toBeVisible();
    await expect(page.getByRole('button', { name: /Прийняти заняття/ })).toBeDisabled();
  });

  test('reserve warnings do not gate acceptance', async ({ page }) => {
    await loginAndBake(page);

    // Move a visible warning into the fixed 45-minute plan's reserve tray.
    // Review may rearrange blocks, but must not offer a duration mutation.
    await page.locator('.review-paper .dblock.warn').last().locator('[data-action="b-down"]').click();
    await expect(page.locator('[data-testid="reserve-tray"] [data-activity-type="text-questions"]')).toBeVisible();

    // The remaining visible warnings still need acknowledging — but the reserve
    // warn must NOT block acceptance. Ack each visible warn with a brief settle
    // pause so React's removal of the just-acked button doesn't race the next click.
    const ackButtons = page.locator('.review-paper .ack-btn');
    while (await ackButtons.count()) {
      await ackButtons.first().click();
      await page.waitForTimeout(200);
    }

    const accept = page.getByRole('button', { name: /Прийняти заняття/ });
    await expect(accept).toBeEnabled();
    await accept.click();
    await expect(accept).toBeDisabled();
  });

  test('review path loads without load-time crash (require or eval in validator)', async ({ page }) => {
    // Explicit load-time crash detector for the merge blocker.
    // Any ReferenceError on `require` (or EvalError) during import of activityValidator
    // will cause the page to fail to render the workbench; the wait will fail or pageerror fires.
    const jsErrors: string[] = [];
    page.on('pageerror', (err) => {
      jsErrors.push(String(err));
    });

    await loginAndBake(page);

    // If we reached here the module initialized without throwing require/eval.
    await expect(page.locator('[data-testid="review-workbench"]')).toBeVisible();

    const badErrors = jsErrors.filter((m) => /require is not defined|EvalError|new Function/i.test(m));
    expect(badErrors, `Unexpected crash errors during review load: ${badErrors.join('; ')}`).toHaveLength(0);
  });
});
