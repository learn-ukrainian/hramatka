/**
 * Print-media emulation E2E (hardens #109 review follow-up L4).
 *
 * The existing student-print test checks a screen proxy (':visible'), which
 * passes silently if the '@media print' CSS block is deleted — the screen
 * rule '.print-variant-student .teacher-key { display: none }' (teacher.css
 * ~L256) hides keys regardless of media, so ':visible' stays 0 without any
 * '@media print' rule actually applying.
 *
 * This spec drives 'page.emulateMedia({ media: "print" })' so the '@media
 * print' rules in src/teacher.css and src/index.css are exercised by the
 * layout engine. We discriminate "print media really active" via '.anchor-print'
 * (display:none on screen, display:block under '@media print') — that Assert
 * fails if the '@media print' block is deleted, closing the silent-pass gap.
 *
 *   (1) student variant under print media → ZERO answer-key content visible
 *   (2) teacher variant under print media → keys PRESENT
 *   (3) screen media restored between cases (beforeEach + inline verification)
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

test.describe('Hramatka print-media emulation (stub)', () => {
  test.beforeAll(async () => {
    await startStub();
  });
  test.afterAll(async () => {
    await stopStub();
  });

  test.beforeEach(async ({ page, context }) => {
    await context.clearCookies();
    // (3) screen media restored between cases — no print-media bleed across tests
    await page.emulateMedia({ media: 'screen' });
    await page.goto(APP);
    await page.evaluate(async () => {
      await fetch('/api/dev/clear-session', { method: 'POST' }).catch(() => {});
      await fetch('/api/dev/reset', { method: 'POST' }).catch(() => {});
    });
  });

  test('(1) student print variant under emulated print media: ZERO answer-key content visible', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill('Текст для перевірки друку для учня у print media.');
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });

    // sanity: under screen media, teacher keys exist in DOM and are visible by default
    expect(await page.locator('[data-testid="teacher-answer-key"]').count()).toBeGreaterThan(0);

    // confirm the '.anchor-print' discriminator is attached (hidden on screen)
    await expect(page.getByTestId('anchor-print')).toBeAttached();
    await expect(page.getByTestId('anchor-print')).not.toBeVisible();

    // trigger student print variant (windows.print stubbed so the dialog does not block)
    await page.evaluate(() => { window.print = () => {}; });
    await page.getByTestId('print-student').click();
    await expect(page.locator('.teacher-app')).toHaveClass(/print-variant-student/);

    // emulate print media — '@media print' rules now apply
    await page.emulateMedia({ media: 'print' });

    // '@media print' must really be active: '.anchor-print' flips to display:block.
    // If the '@media print' CSS block were deleted, this fails — closing the silent-pass gap.
    await expect(page.getByTestId('anchor-print')).toBeVisible();

    // assert ZERO answer-key content visible via three independent discriminators.
    // Use ':has-text()' (substring) — the DOM label is «Ключ відповіді:» (trailing colon),
    // so exact-match 'text="..."' would silently miss it.
    const keyCount = await page.locator('[data-testid="teacher-answer-key"]:visible').count();
    const classCount = await page.locator('.teacher-key:visible').count();
    const phraseCount = await page.locator('strong:has-text("Ключ відповіді"):visible').count();
    expect(keyCount).toBe(0);
    expect(classCount).toBe(0);
    expect(phraseCount).toBe(0);

    // Assert honesty footer is visible under print media and is translated
    const honestyFooter = page.getByTestId('honesty-footer');
    await expect(honestyFooter).toBeVisible();
    await expect(honestyFooter).toHaveText(/Складено з тексту вчителя/);

    // Switch back to screen media to click the language toggle (hidden under print media)
    await page.emulateMedia({ media: 'screen' });
    await page.getByTestId('lang-toggle').click();

    // Emulate print media again to verify English footer
    await page.emulateMedia({ media: 'print' });
    await expect(honestyFooter).toHaveText(/Compiled from the teacher’s text/);

    // Restore screen media to switch language back to Ukrainian
    await page.emulateMedia({ media: 'screen' });
    await page.getByTestId('lang-toggle').click();

    // Emulate print media again for the remainder of the test
    await page.emulateMedia({ media: 'print' });

    // (3) inline: restore screen media and verify the print-only rule no longer applies
    await page.emulateMedia({ media: 'screen' });
    await expect(page.getByTestId('anchor-print')).not.toBeVisible();
  });

  test('(2) teacher print variant under emulated print media: keys PRESENT', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill('Текст для перевірки друку для вчителя у print media.');
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });

    await page.evaluate(() => { window.print = () => {}; });
    await page.getByTestId('print-teacher').click();
    await expect(page.locator('.teacher-app')).toHaveClass(/print-variant-teacher/);

    // '.anchor-print' is hidden on screen, so we are starting from screen media
    await expect(page.getByTestId('anchor-print')).not.toBeVisible();

    // emulate print media — '@media print' rules now apply
    await page.emulateMedia({ media: 'print' });

    // '@media print' active: '.anchor-print' visible
    await expect(page.getByTestId('anchor-print')).toBeVisible();

    // teacher variant: keys PRESENT under print media
    // (@media print L395: '.print-variant-teacher .teacher-key { display: block !important }')
    const keyCount = await page.locator('[data-testid="teacher-answer-key"]:visible').count();
    expect(keyCount).toBeGreaterThan(0);

    // the «Ключ відповіді» label is visible under print media for teacher variant.
    // ':has-text()' does substring match — the DOM label is «Ключ відповіді:» (trailing colon).
    const phraseCount = await page.locator('strong:has-text("Ключ відповіді"):visible').count();
    expect(phraseCount).toBeGreaterThan(0);

    // Assert honesty footer is visible under print media and is translated
    const honestyFooter = page.getByTestId('honesty-footer');
    await expect(honestyFooter).toBeVisible();
    await expect(honestyFooter).toHaveText(/Складено з тексту вчителя/);

    // Switch back to screen media to click the language toggle (hidden under print media)
    await page.emulateMedia({ media: 'screen' });
    await page.getByTestId('lang-toggle').click();

    // Emulate print media again to verify English footer
    await page.emulateMedia({ media: 'print' });
    await expect(honestyFooter).toHaveText(/Compiled from the teacher’s text/);

    // Restore screen media to switch language back to Ukrainian
    await page.emulateMedia({ media: 'screen' });
    await page.getByTestId('lang-toggle').click();

    // Emulate print media again for the remainder of the test
    await page.emulateMedia({ media: 'print' });

    // (3) inline: restore screen media and verify the print-only rule no longer applies
    await page.emulateMedia({ media: 'screen' });
    await expect(page.getByTestId('anchor-print')).not.toBeVisible();
    // teacher keys remain visible under screen media (no rule hides them for teacher variant)
    await expect(page.locator('[data-testid="teacher-answer-key"]').first()).toBeVisible();
  });
});
