/**
 * Operator report 2026-07-28:
 * 1. Activity widget controls must be readable in both shipped themes
 *    (computed text/background contrast >= 4.5:1).
 * 2. Review-mode density must fit at least three activity cards into two
 *    viewport heights at 1440x900.
 * 3. No body horizontal scroll at 1440x900 and 1568x774.
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

async function loginAndBake(page: Page) {
  await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
  await page.reload();
  await page.waitForURL(/\/teacher\/?$/);
  await page.getByPlaceholder(/Вставте український текст/).fill('Текст для перевірки контрасту та щільності всіх типів завдань.');
  await page.getByRole('button', { name: /Згенерувати урок/ }).click();
  await page.waitForSelector('[data-testid="review-workbench"]', { timeout: 15000 });
}

async function effectiveBackgroundColor(el: HTMLElement): string {
  let current: Element | null = el;
  while (current && current !== document.body) {
    const style = getComputedStyle(current as HTMLElement);
    const bg = style.backgroundColor;
    if (bg && !/rgba?\(0,\s*0,\s*0,\s*0\)|transparent/i.test(bg)) {
      return bg;
    }
    current = current.parentElement;
  }
  return getComputedStyle(document.body).backgroundColor;
}

test.describe('Activity widget contrast + review density (operator report 2026-07-28)', () => {
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

  for (const scheme of ['light', 'dark'] as const) {
    test(`activity controls meet WCAG AA contrast in ${scheme} theme`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await loginAndBake(page);

      const controls = await page.locator('[data-activity] button, [data-activity] select').all();
      expect(controls.length).toBeGreaterThan(0);

      const failures: string[] = [];
      for (const control of controls) {
        const info = await control.evaluate((el) => {
          const style = getComputedStyle(el as HTMLElement);
          const rect = el.getBoundingClientRect();
          return {
            tag: el.tagName.toLowerCase(),
            text: (el as HTMLElement).innerText?.slice(0, 40) ?? '',
            disabled: (el as HTMLButtonElement | HTMLSelectElement).disabled,
            visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden',
            color: style.color,
          };
        });

        if (!info.visible || info.disabled) continue;

        const bg = await control.evaluate(effectiveBackgroundColor);
        const ratio = contrastRatio(parseRgb(info.color), parseRgb(bg));
        if (ratio < 4.5) {
          failures.push(`${info.tag} "${info.text}" color=${info.color} bg=${bg} ratio=${ratio.toFixed(2)}`);
        }
      }

      expect(failures, `Low-contrast controls in ${scheme} theme:\n${failures.join('\n')}`).toHaveLength(0);
    });
  }

  test('review-mode density: first three activity cards fit within 2x viewport height at 1440x900', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await loginAndBake(page);

    const cards = await page.locator('[data-activity] [class*="activityContainer"]').all();
    expect(cards.length).toBeGreaterThanOrEqual(3);

    let total = 0;
    for (let i = 0; i < 3; i++) {
      const box = await cards[i].boundingBox();
      expect(box).not.toBeNull();
      total += box!.height;
    }

    const viewportHeight = await page.evaluate(() => window.innerHeight);
    expect(total, `sum of first 3 card heights (${total}px) should be <= 2x viewport (${2 * viewportHeight}px)`).toBeLessThanOrEqual(2 * viewportHeight);
  });

  test('no body horizontal scroll at 1440x900 and 1568x774', async ({ page }) => {
    for (const size of [{ width: 1440, height: 900 }, { width: 1568, height: 774 }]) {
      await page.setViewportSize(size);
      await loginAndBake(page);
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      const clientWidth = await page.evaluate(() => document.documentElement.clientWidth);
      expect(scrollWidth, `horizontal overflow at ${size.width}x${size.height}`).toBeLessThanOrEqual(clientWidth);
      // Reset for next iteration so loginAndBake starts from a clean viewport.
      await page.evaluate(async () => {
        await fetch('/api/dev/clear-session', { method: 'POST' }).catch(() => {});
        await fetch('/api/dev/reset', { method: 'POST' }).catch(() => {});
      });
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
