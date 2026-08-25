/**
 * P0-3: clean student sharing surface — Zoom-share-safe views from ready lesson + Conductor.
 */
import { test, expect, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const STUB_PORT = 8787;
const APP = 'http://localhost:5173';
const TEST_TOKEN = 'A'.repeat(42) + 'Q';
const TEACHER_NAME = 'Тетяна';

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

async function redeemSession(page: Page) {
  await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
  await page.reload();
  await page.waitForURL(/\/teacher\/?$/);
}

async function createReadyLesson(page: Page, text = 'Текст для перевірки екрана учня.') {
  await page.getByPlaceholder(/Вставте/).fill(text);
  await page.getByRole('button', { name: /Згенерувати урок/ }).click();
  await page.waitForSelector('.block', { timeout: 15000 });
}

async function ackAllWarnings(page: Page) {
  const ackButtons = page.locator('.ack-btn');
  const n = await ackButtons.count();
  for (let i = 0; i < n; i++) {
    if (await ackButtons.first().isVisible()) {
      await ackButtons.first().click();
      await page.waitForTimeout(120);
    }
  }
}

async function acceptLesson(page: Page) {
  const acceptBtn = page.getByRole('button', { name: /Прийняти заняття/ });
  if (await acceptBtn.isVisible()) {
    await acceptBtn.click();
    await expect(page.locator('.lesson-actions .chip.ok').filter({ hasText: 'Прийнято' })).toBeVisible({ timeout: 8000 });
  }
}

async function useTrueFalseActivity(scope: ReturnType<Page['locator']>) {
  const answerBtn = scope.getByRole('button', { name: 'Правда', exact: true });
  await expect(answerBtn).toBeVisible();
  await answerBtn.click();
  const checkBtn = scope.getByRole('button', { name: 'Перевірити' });
  await expect(checkBtn).toBeEnabled({ timeout: 3000 });
  await checkBtn.click();
  await expect(scope.locator('[data-activity="tf-row-feedback"]')).toBeVisible({ timeout: 3000 });
}

async function useMatchUpActivity(scope: ReturnType<Page['locator']>) {
  const left = scope.locator('[data-activity="match-left-tile"]').first();
  const matchingRight = scope.locator('[data-activity="match-right-tile"][data-original-index="0"]');
  await expect(left).toBeVisible();
  await left.click();
  await expect(left).toHaveAttribute('data-selected', 'true');
  await matchingRight.click();
  await expect(left).toHaveAttribute('data-matched', 'true');
}

async function assertStudentSurfaceClean(page: Page) {
  await expect(page.getByTestId('student-surface')).toBeVisible();
  await expect(page.getByTestId('student-mode-badge')).toContainText(/учень/i);
  await expect(page.getByTestId('student-banner')).toBeVisible();

  await expect(page.getByTestId('teacher-display-name')).toHaveCount(0);
  await expect(page.getByTestId('logout-btn')).toHaveCount(0);
  await expect(page.locator('.teacher-key')).toHaveCount(0);
  await expect(page.locator('.block-meta')).toHaveCount(0);
  await expect(page.getByTestId('warning-ack-btn')).toHaveCount(0);
  await expect(page.locator('.prov, .prov-detail')).toHaveCount(0);
  await expect(page.getByText(TEACHER_NAME)).toHaveCount(0);
}

test.describe('Student sharing surface (P0-3)', () => {
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

  test('ready lesson: student mode hides teacher chrome; return works; activity usable', async ({ page }) => {
    await redeemSession(page);
    await createReadyLesson(page);

    await page.getByTestId('enter-student-mode-btn').click();
    await assertStudentSurfaceClean(page);

    // Lesson content stays Ukrainian (activity instruction from stub)
    await expect(page.locator('[data-activity-type="true-false"]').first()).toBeVisible();
    const ukrainianSnippet = page.locator('.activity-wrapper').first();
    await expect(ukrainianSnippet).not.toHaveText(/unsupported|placeholder/i);

    // Interactive: answer + check in true/false activity
    const tfBlock = page.locator('[data-activity-type="true-false"]').first();
    await useTrueFalseActivity(tfBlock);

    // Teacher return restores teacher chrome
    await page.getByTestId('teacher-return-btn').click();
    await expect(page.getByTestId('teacher-surface')).toBeVisible();
    await expect(page.getByTestId('teacher-display-name')).toContainText(TEACHER_NAME);
    await expect(page.getByTestId('logout-btn')).toBeVisible();
    await expect(page.getByTestId('enter-student-mode-btn')).toBeVisible();
  });

  test('Conductor: student preview hides teacher chrome; return works; activity usable', async ({ page }) => {
    await redeemSession(page);
    await createReadyLesson(page);
    await ackAllWarnings(page);
    await acceptLesson(page);

    await page.getByRole('button', { name: /Провести заняття/ }).click();
    await expect(page.locator('.cond-rail')).toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId('teacher-display-name')).toContainText(TEACHER_NAME);

    await page.getByTestId('enter-student-preview-btn').click();
    await assertStudentSurfaceClean(page);
    await expect(page.getByTestId('conductor-student-preview')).toBeVisible();
    await expect(page.locator('.cond-panel')).toHaveCount(0);
    await expect(page.locator('.cond-rail')).toHaveCount(0);
    await expect(page.getByTestId('cond-answer-key')).toHaveCount(0);

    const shared = page.locator('.cond-shared');
    await expect(shared).toBeVisible();
    // The qualified 45-minute plan starts with match-up; true/false remains
    // available in the reserve but is not a conductor-plan assumption.
    await useMatchUpActivity(shared);

    await page.getByTestId('teacher-return-btn').click();
    await expect(page.getByTestId('teacher-surface')).toBeVisible();
    await expect(page.getByTestId('teacher-display-name')).toContainText(TEACHER_NAME);
    await expect(page.locator('.cond-panel')).toBeVisible();
    await expect(page.getByTestId('enter-student-preview-btn')).toBeVisible();
  });
});
