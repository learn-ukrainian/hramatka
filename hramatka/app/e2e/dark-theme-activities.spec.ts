/**
 * #129 — mark-the-words selected state must be visible in dark theme.
 * Regression guard: asserts a selected word gets a real (non-transparent)
 * background in dark theme, and that the background is visually distinct from
 * an unselected word. Covers the broader class: other activity selection
 * states are smoke-checked for non-transparent backgrounds too.
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

async function createReadyLesson(page: Page) {
  await page.getByPlaceholder(/Вставте/).fill('Текст для перевірки темної теми.');
  await page.getByRole('button', { name: /Згенерувати урок/ }).click();
  await page.waitForSelector('.block', { timeout: 15000 });
}

/** Wait for an element's background to settle to a fully-opaque colour.
 *  The kit uses `transition: all .2s ease` on interactive elements, so the
 *  background animates from transparent → target.  We poll until the computed
 *  value is `rgb(…)` (opaque) rather than mid-transition `rgba(…)`. */
async function waitForOpaqueBg(
  page: Page,
  handle: import('playwright').ElementHandle<HTMLElement>,
  timeout = 5000,
) {
  await page.waitForFunction(
    (el) => /^rgb\(/.test(getComputedStyle(el as HTMLElement).backgroundColor),
    handle,
    { timeout },
  );
}

test.describe('Dark-theme activity selection visibility (#129)', () => {
  test.beforeAll(async () => {
    await startStub();
  });
  test.afterAll(async () => {
    await stopStub();
  });

  test.beforeEach(async ({ page, context }) => {
    await context.clearCookies();
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.goto(APP);
    await page.evaluate(async () => {
      await fetch('/api/dev/clear-session', { method: 'POST' }).catch(() => {});
      await fetch('/api/dev/reset', { method: 'POST' }).catch(() => {});
    });
  });

  test('mark-the-words: selected word has visible background in dark theme', async ({ page }) => {
    await redeemSession(page);
    await createReadyLesson(page);
    await page.getByTestId('enter-student-mode-btn').click();
    await expect(page.getByTestId('student-surface')).toBeVisible();

    const mtw = page.locator('[data-activity-type="mark-the-words"]').first();
    await expect(mtw).toBeVisible({ timeout: 8000 });

    const firstWord = mtw.locator('[class*="markableWord"]').first();
    await expect(firstWord).toBeVisible();
    const wordHandle = await firstWord.elementHandle();
    expect(wordHandle).not.toBeNull();

    // BEFORE: unselected word must have a transparent background.
    const bgBefore = await wordHandle!.evaluate(
      (el) => getComputedStyle(el).backgroundColor,
    );
    expect(bgBefore).toMatch(/rgba?\(0,\s*0,\s*0,\s*0\)|transparent/i);

    // Click to select, then wait for the transition to settle.
    await wordHandle!.click();
    await waitForOpaqueBg(page, wordHandle!);

    // AFTER: selected word must have a REAL, opaque background.
    const bgAfter = await wordHandle!.evaluate(
      (el) => getComputedStyle(el).backgroundColor,
    );
    expect(bgAfter).toMatch(/^rgb\(/); // opaque — not mid-transition rgba

    // Contrast check: selected background vs page background >= 3:1 (WCAG UI).
    const pageBg = await page.evaluate(
      () => getComputedStyle(document.body).backgroundColor,
    );
    const ratio = contrastRatio(parseRgb(bgAfter), parseRgb(pageBg));
    expect(ratio).toBeGreaterThanOrEqual(3);

    // Text on the selected word must also be legible (dark-theme text override).
    const fg = await wordHandle!.evaluate((el) => getComputedStyle(el).color);
    const textRatio = contrastRatio(parseRgb(fg), parseRgb(bgAfter));
    expect(textRatio).toBeGreaterThanOrEqual(3);
  });

  test('other activity types: selection/active backgrounds are non-transparent in dark theme', async ({ page }) => {
    await redeemSession(page);
    await createReadyLesson(page);
    await page.getByTestId('enter-student-mode-btn').click();
    await expect(page.getByTestId('student-surface')).toBeVisible();

    // Quiz: selected option gets a tinted background.
    // Option buttons are <button> (the container is a <div> with "options").
    const quizBlock = page.locator('[data-activity-type="quiz"]').first();
    if (await quizBlock.isVisible().catch(() => false)) {
      const opt = quizBlock.locator('button[class*="option"]').first();
      if (await opt.isVisible().catch(() => false)) {
        const optHandle = await opt.elementHandle();
        await optHandle!.click();
        await waitForOpaqueBg(page, optHandle!);
        const bg = await optHandle!.evaluate(
          (el) => getComputedStyle(el).backgroundColor,
        );
        expect(bg).toMatch(/^rgb\(/);
      }
    }

    // Match-up: left-column items always have a tinted base background
    // (--lu-state-active via the token bridge).  Verify it is non-transparent.
    const matchBlock = page.locator('[data-activity-type="match-up"]').first();
    if (await matchBlock.isVisible().catch(() => false)) {
      const item = matchBlock.locator('[class*="matchItem"]').first();
      const bg = await item.evaluate(
        (el) => getComputedStyle(el).backgroundColor,
      );
      expect(bg).toMatch(/^rgb\(/);
    }
  });
});

/* --- colour helpers --- */

function parseRgb(str: string): [number, number, number] {
  const m = str.match(/rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/);
  if (!m) return [0, 0, 0];
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function contrastRatio(
  a: [number, number, number],
  b: [number, number, number],
): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

function relativeLuminance([r, g, b]: [number, number, number]): number {
  const f = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
