/**
 * Source-blind E2E: deliberate-error badge through review → accept → Conductor (#208 / #164).
 *
 * Sol F2: a proof that stops at ReviewWorkbench student mode never exercises the
 * Conductor teacher badge or Conductor student-preview branch. This scenario:
 *   generate/review → badge + mark/list → safe edit preserves triple → accept →
 *   real «Провести заняття» → navigate to error-correction → teacher Conductor
 *   badge (+ revealed answer key) → enter-student-preview-btn → isolation →
 *   return to teacher Conductor and restore badge.
 *
 * Regular run-mode isolation is intentionally omitted: it is a distinct surface
 * covered elsewhere and does not prove the #208 Conductor change.
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

// Golden fixture wording (vendored activity envelope; outer triple is stub-wired).
const WRONG_SENTENCE = 'Це моя стіл.';
const CORRECTED_SENTENCE = 'Це мій стіл.';
const ERROR_FORM = 'моя';
const CORRECTION_FORM = 'мій';
const BADGE_UK = 'Навмисна помилка';
const BADGE_EN = 'Deliberate mistake';

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

async function loginAndBake(page: Page) {
  await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
  await page.reload();
  await page.waitForURL(/\/teacher\/?$/);
  await page
    .getByPlaceholder(/Вставте український текст/)
    .fill('Текст для перевірки навмисної помилки у блоці виправлення.');
  await page.getByRole('button', { name: /Згенерувати урок/ }).click();
  await page.waitForSelector('[data-testid="review-workbench"]', { timeout: 15000 });
}

function errorCorrectionBlock(page: Page) {
  // Semantic type marker inside the planned review paper (count asserted at use).
  return page
    .locator('.review-paper .dblock')
    .filter({ has: page.locator('[data-activity-type="error-correction"]') });
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
  await expect(acceptBtn).toBeEnabled({ timeout: 5000 });
  await acceptBtn.click();
  await expect(page.locator('.lesson-actions').getByText(/Прийнято|accepted/i)).toBeVisible({
    timeout: 8000,
  });
}

/**
 * Advance the Conductor plan with the real «Пропустити» control until the
 * teacher panel type chip is error-correction (or the shared task shows the
 * golden wrong sentence). No fabricated DOM, no deep-link to a block id.
 */
async function navigateToErrorCorrectionInConductor(page: Page) {
  for (let step = 0; step < 12; step++) {
    const panel = page.locator('.cond-panel');
    await expect(panel).toBeVisible({ timeout: 5000 });

    const typeChip = panel.locator('.chip.info').first();
    const typeText = ((await typeChip.textContent()) || '').trim();
    const sharedHasWrong = await page
      .locator('.cond-shared')
      .getByText(WRONG_SENTENCE, { exact: false })
      .count();

    if (typeText === 'error-correction' || sharedHasWrong > 0) {
      await expect(typeChip).toHaveText('error-correction');
      return;
    }

    const skip = page.getByRole('button', { name: /Пропустити/ });
    await expect(skip).toBeVisible();
    await skip.click();
  }
  throw new Error('Conductor plan never reached error-correction');
}

test.describe('Deliberate-error badge source-blind E2E (#208)', () => {
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

  test('review badge → safe edit → accept → conduct badge → student preview isolation → restore', async ({
    page,
  }) => {
    await loginAndBake(page);

    // --- Teacher review: intended deliberate-error affordance ---
    await expect(page.getByText(/Ревізія:\s*1/)).toBeVisible();

    const ecBlocks = errorCorrectionBlock(page);
    await expect(ecBlocks).toHaveCount(1);
    const ec = ecBlocks.first();

    const badge = ec.getByTestId('deliberate-error-badge');
    await expect(badge).toBeVisible();
    await expect(badge).toHaveText(BADGE_UK);

    // Answers are shown by default: list marks the form and shows the correction.
    const mark = ec.getByTestId('deliberate-error-mark');
    await expect(mark).toBeVisible();
    await expect(mark).toHaveText(ERROR_FORM);
    await expect(ec.getByTestId('deliberate-error-list')).toContainText(CORRECTION_FORM);
    await expect(ec.getByTestId('teacher-answer-key')).toContainText(CORRECTED_SENTENCE);
    // Task surface still shows the deliberately wrong sentence.
    await expect(ec.locator('[data-activity-type="error-correction"]')).toContainText(
      WRONG_SENTENCE,
    );

    // --- Safe edit: keep wrong/corrected pair; only instruction changes ---
    await ec.locator('[data-action="b-edit"]').click();
    await expect(page.getByTestId('activity-editor')).toBeVisible();
    // First textarea is the instruction field for error-correction.
    const instruction = page.locator('[data-testid="activity-editor"] textarea').first();
    await instruction.fill('Оновлена інструкція — виправте помилку (редакція).');
    await page.getByRole('button', { name: 'Зберегти зміни' }).click();
    await expect(page.getByTestId('activity-editor')).toHaveCount(0);

    // Revision advances; badge and intent survive because the triple still matches.
    await expect(page.getByText(/Ревізія:\s*2/)).toBeVisible({ timeout: 5000 });
    await expect(ec.getByTestId('deliberate-error-badge')).toBeVisible();
    await expect(ec.getByTestId('deliberate-error-badge')).toHaveText(BADGE_UK);
    await expect(ec.getByTestId('deliberate-error-mark')).toHaveText(ERROR_FORM);
    await expect(ec.locator('.chip.ok').filter({ hasText: /змінено вами/ })).toBeVisible();

    // --- Accept and open the real Conductor (the #208 surface under test) ---
    await ackAllWarnings(page);
    await acceptLesson(page);

    const conductBtn = page.getByRole('button', { name: /Провести заняття/ });
    await expect(conductBtn).toBeVisible({ timeout: 5000 });
    await conductBtn.click();

    await expect(page.locator('.conductor-view .cond-title b')).toContainText('Проведення заняття', {
      timeout: 5000,
    });
    await expect(page.locator('.cond-rail').first()).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.cond-panel')).toBeVisible();

    await navigateToErrorCorrectionInConductor(page);

    // Teacher Conductor panel: badge answers "is this a bug?" even with answers hidden.
    const condPanel = page.locator('.cond-panel');
    const condBadge = condPanel.getByTestId('deliberate-error-badge');
    await expect(condBadge).toBeVisible();
    await expect(condBadge).toHaveText(BADGE_UK);
    // Shared student surface keeps the deliberately wrong sentence as learner content.
    await expect(page.locator('.cond-shared')).toContainText(WRONG_SENTENCE);
    // Mark/list are answer-revealing and live in the teacher key path (review), not
    // in the shared surface. Conductor still must not leak them before answer reveal.
    await expect(page.locator('.cond-shared').getByTestId('deliberate-error-mark')).toHaveCount(0);
    await expect(page.locator('.cond-shared').getByTestId('deliberate-error-list')).toHaveCount(0);
    await expect(page.getByTestId('cond-answer-key')).toHaveCount(0);

    // Reveal the teacher answer key (real Conductor control) — corrected form/list path.
    await page.getByRole('button', { name: /🔑 Відповідь/ }).click();
    const answerKey = page.getByTestId('cond-answer-key');
    await expect(answerKey).toBeVisible();
    await expect(answerKey).toContainText(CORRECTED_SENTENCE);
    // Correction form is present in the corrected sentence; wrong form stays task-only.
    await expect(answerKey).toContainText(CORRECTION_FORM);
    await expect(condPanel.getByTestId('deliberate-error-badge')).toBeVisible();

    // --- Conductor student preview: no badge, no mark/list, no corrected answer ---
    await page.getByTestId('enter-student-preview-btn').click();
    await expect(page.getByTestId('conductor-student-preview')).toBeVisible();
    await expect(page.getByTestId('student-banner')).toBeVisible();
    await expect(page.locator('.cond-panel')).toHaveCount(0);

    await expect(page.getByTestId('deliberate-error-badge')).toHaveCount(0);
    await expect(page.getByTestId('deliberate-error-list')).toHaveCount(0);
    await expect(page.getByTestId('deliberate-error-mark')).toHaveCount(0);
    await expect(page.getByTestId('cond-answer-key')).toHaveCount(0);
    await expect(page.getByTestId('teacher-answer-key')).toHaveCount(0);

    const studentRoot = page.getByTestId('conductor-student-preview');
    const studentText = await studentRoot.innerText();
    expect(studentText).not.toContain(BADGE_UK);
    expect(studentText).not.toContain(BADGE_EN);
    expect(studentText).not.toContain(CORRECTED_SENTENCE);
    // Wrong task sentence may remain as learner content — that is expected.
    expect(studentText).toContain(WRONG_SENTENCE);

    await expect(studentRoot.locator('mark.deliberate-error-mark')).toHaveCount(0);

    // --- Return to teacher Conductor: badge and panel restored on the same block ---
    await page.getByTestId('teacher-return-btn').click();
    await expect(page.getByTestId('conductor-student-preview')).toHaveCount(0);
    await expect(page.locator('.cond-panel')).toBeVisible();
    await expect(page.getByTestId('enter-student-preview-btn')).toBeVisible();

    const panelAgain = page.locator('.cond-panel');
    await expect(panelAgain.locator('.chip.info').first()).toHaveText('error-correction');
    await expect(panelAgain.getByTestId('deliberate-error-badge')).toBeVisible();
    await expect(panelAgain.getByTestId('deliberate-error-badge')).toHaveText(BADGE_UK);
    await expect(page.locator('.cond-shared')).toContainText(WRONG_SENTENCE);
  });
});
