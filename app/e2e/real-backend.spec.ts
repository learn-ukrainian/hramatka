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
  // Production receipts are intentionally empty after #375; this flow relies
  // on the real-backend harness's qualified test-registry injection seam.
  const modelPicker = page.getByLabel('Модель для уроку');
  await expect(modelPicker).toHaveValue('gemini-3.6-flash');
  await modelPicker.selectOption('gemini-3.6-flash');
  await page.locator('.field').filter({ hasText: 'Тривалість' }).locator('select').selectOption('45');
  const submittedLesson = page.waitForRequest(
    request => request.url().endsWith('/api/lessons') && request.method() === 'POST',
  );
  await page.getByRole('button', { name: /Згенерувати урок/ }).click();
  expect((await submittedLesson).postDataJSON().logical_model_id).toBe('gemini-3.6-flash');
  // 45-min B1 lesson = 8 activities under the canonical sizing policy (hramatka/sizing_policy.py).
  await expect(page.locator('.lesson-view .dblock:not(.empty-phase)')).toHaveCount(8, { timeout: 15_000 });
  await expect(page.locator('[data-activity-player]')).toHaveCount(8);

  const revisionBefore = await page.locator('.meta').textContent();
  const initialRevision = Number(revisionBefore?.match(/Ревізія: (\d+)/)?.[1]);
  expect(initialRevision).toBeGreaterThan(0);

  // A real browser crosses the real API, durable queue, runner and revision CAS.
  // Exactly one selected activity changes; the old one remains visible while work runs.
  const lessonBlocks = page.locator('.lesson-view .dblock:not(.empty-phase)');
  const blockContent = (index: number) =>
    lessonBlocks.nth(index).locator(':scope > .bcontent').textContent();
  const beforeActivities = await Promise.all(
    Array.from({ length: await lessonBlocks.count() }, (_, index) => blockContent(index)),
  );
  const firstBlock = page.locator('.lesson-view [data-block-id="block-1"]');
  await firstBlock.getByRole('button', { name: 'Створити інший варіант' }).click();
  await firstBlock.getByPlaceholder(/менше очевидних підказок/).fill(
    'Зробіть формулювання природнішим.',
  );
  const regenerationRequest = page.waitForRequest(
    request => request.url().includes('/blocks/block-1/regenerations')
      && request.method() === 'POST',
  );
  await firstBlock.getByRole('button', { name: 'Створити новий варіант' }).click();
  expect((await regenerationRequest).postDataJSON().feedback).toBe(
    'Зробіть формулювання природнішим.',
  );
  await expect(firstBlock.getByTestId('regeneration-succeeded')).toBeVisible({ timeout: 10_000 });
  const afterActivities = await Promise.all(
    Array.from({ length: await lessonBlocks.count() }, (_, index) => blockContent(index)),
  );
  expect(afterActivities[0]).not.toBe(beforeActivities[0]);
  expect(afterActivities.slice(1)).toEqual(beforeActivities.slice(1));
  await expect(page.locator('.meta')).toContainText(`Ревізія: ${initialRevision + 1}`);

  const warnings = await page.locator('.ack-btn').count();
  for (let remaining = warnings - 1; remaining >= 0; remaining -= 1) {
    await page.locator('.ack-btn').first().click();
    await expect(page.locator('.ack-btn')).toHaveCount(remaining);
  }
  await page.getByRole('button', { name: /Прийняти заняття/ }).click();
  await expect(page.getByText('Прийнято', { exact: true })).toBeVisible();
  await expect(page.locator('.meta')).toContainText(
    `Ревізія: ${initialRevision + 1 + warnings + 1}`,
  );

  const directUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(directUrl);
  await expect(page.locator('.lesson-view .dblock:not(.empty-phase)')).toHaveCount(8, { timeout: 10_000 });
  await expect(page.getByText('Прийнято', { exact: true })).toBeVisible();
});
