/**
 * #471 — header navigation must remain legible in both shipped color-scheme
 * paths, and its hover pill must win the later global button:hover rule.
 */
import { test, expect, type Locator, type Page } from '@playwright/test';
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
  await new Promise((resolve) => setTimeout(resolve, 1400));
}

async function stopStub() {
  if (stubProc) {
    stubProc.kill('SIGTERM');
    stubProc = null;
  }
}

async function login(page: Page) {
  await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
  await page.reload();
  await page.waitForURL(/\/teacher\/?$/);
  await expect(page.locator('.appnav')).toBeVisible({ timeout: 10000 });
}

async function navButtonStyle(navButton: Locator) {
  return navButton.first().evaluate((element) => {
    const button = element as HTMLElement;
    const appbar = button.closest('.appbar') as HTMLElement;
    const buttonStyle = getComputedStyle(button);
    const appbarStyle = getComputedStyle(appbar);
    return {
      color: buttonStyle.color,
      background: buttonStyle.backgroundColor,
      appbarBackground: appbarStyle.backgroundColor,
    };
  });
}

test.describe('#471 header navigation contrast', () => {
  test.beforeAll(startStub);
  test.afterAll(stopStub);

  test.beforeEach(async ({ page, context }) => {
    await context.clearCookies();
    await page.goto(APP);
    await page.evaluate(async () => {
      await fetch('/api/dev/clear-session', { method: 'POST' }).catch(() => {});
      await fetch('/api/dev/reset', { method: 'POST' }).catch(() => {});
    });
  });

  test('keeps idle and hover styles readable in light and OS-dark themes', async ({ page }) => {
    for (const scheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await login(page);

      const navButton = page.locator('.appnav button:not(.on)').first();
      await expect(navButton).toBeVisible();
      await expect(navButton).not.toHaveClass('on');
      const activeNavButton = page.locator('.appnav button.on');
      await expect(activeNavButton).toHaveCount(1);
      await page.locator('.brand').hover();

      const idle = await navButtonStyle(navButton);
      expect(idle).toEqual({
        color: scheme === 'light' ? 'rgb(255, 255, 255)' : 'rgb(25, 26, 23)',
        background: 'rgba(0, 0, 0, 0)',
        appbarBackground: scheme === 'light' ? 'rgb(47, 74, 126)' : 'rgb(143, 169, 217)',
      });

      await navButton.hover();
      const hover = await navButtonStyle(navButton);
      expect(hover).toEqual({
        color: idle.color,
        background: 'rgba(255, 255, 255, 0.18)',
        appbarBackground: idle.appbarBackground,
      });

      expect((await navButtonStyle(activeNavButton)).background).toBe('rgba(255, 255, 255, 0.18)');
    }
  });
});
