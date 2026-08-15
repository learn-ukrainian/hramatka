import { expect, test } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

const tokenPath = path.resolve('.real-e2e/invite-token');

test('under-capacity preflight never renders a generation phase or provider-call plan', async ({ page }) => {
  const token = (await fs.readFile(tokenPath, 'utf8')).trim();
  await page.goto(`/teacher/#invite=${token}`);
  await expect(page).toHaveURL(/\/teacher\/$/);

  // This is deliberately valid Ukrainian but cannot allocate the B1 plan. The
  // real backend fixture delays deterministic preflight long enough for the
  // first status poll, and its provider seam raises if it is ever reached.
  await page.getByPlaceholder(/Вставте український текст/).fill('Учні читають український текст.');
  await page.getByRole('button', { name: /Згенерувати урок/ }).click();

  const statusView = page.getByTestId('baking-status-view');
  const subline = page.getByTestId('baking-subline');
  await expect(statusView).toBeVisible();
  await expect(subline).not.toContainText('Фаза');
  await expect(subline).not.toContainText('створення завдань');
  await expect(subline).not.toContainText('виклики постачальника');

  const recovery = page.getByTestId('failure-recovery-body');
  await expect(recovery).toHaveAttribute('data-failure-code', 'insufficient_anchor_capacity');
  await expect(recovery).toContainText('Опорного матеріалу недостатньо для повного уроку.');
});
