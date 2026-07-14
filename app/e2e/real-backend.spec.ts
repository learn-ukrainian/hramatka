import { expect, test } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

const tokenPath = path.resolve('.real-e2e/invite-token');

test('real teacher loop preserves session, status, revisions, and direct links', async ({ page }) => {
  const token = (await fs.readFile(tokenPath, 'utf8')).trim();
  await page.goto(`/teacher/#invite=${token}`);
  await expect(page).toHaveURL(/\/teacher\/$/);
  await expect(page.getByRole('heading', { name: 'Створити новий урок' })).toBeVisible();

  await page.getByPlaceholder(/Вставте український текст/).fill(
    'Учні читають український текст і обговорюють вправи на уроці.'
  );
  await page.locator('select').selectOption('45');
  await page.getByRole('button', { name: /Згенерувати урок/ }).click();
  // 45-min B1 lesson = 8 activities under the canonical sizing policy (hramatka/sizing_policy.py).
  await expect(page.locator('.lesson-view .dblock:not(.empty-phase)')).toHaveCount(8, { timeout: 15_000 });
  await expect(page.locator('[data-activity-player]')).toHaveCount(8);

  const revisionBefore = await page.locator('.meta').textContent();
  const initialRevision = Number(revisionBefore?.match(/Ревізія: (\d+)/)?.[1]);
  expect(initialRevision).toBeGreaterThan(0);
  const warnings = await page.locator('.ack-btn').count();
  for (let remaining = warnings - 1; remaining >= 0; remaining -= 1) {
    await page.locator('.ack-btn').first().click();
    await expect(page.locator('.ack-btn')).toHaveCount(remaining);
  }
  await page.getByRole('button', { name: /Прийняти заняття/ }).click();
  await expect(page.getByText('Прийнято', { exact: true })).toBeVisible();
  await expect(page.locator('.meta')).toContainText(`Ревізія: ${initialRevision + warnings + 1}`);

  const directUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(directUrl);
  await expect(page.locator('.lesson-view .dblock:not(.empty-phase)')).toHaveCount(8, { timeout: 10_000 });
  await expect(page.getByText('Прийнято', { exact: true })).toBeVisible();
});
