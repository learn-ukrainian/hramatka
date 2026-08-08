import { expect, test } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

const tokenPath = path.resolve('.real-e2e/invite-token');

test('real teacher sees empty model picker and disabled bake when no model is qualified', async ({ page }) => {
  const token = (await fs.readFile(tokenPath, 'utf8')).trim();
  await page.goto(`/teacher/#invite=${token}`);
  await expect(page).toHaveURL(/\/teacher\/$/);
  await expect(page.getByRole('heading', { name: 'Створити новий урок' })).toBeVisible();

  await page.getByPlaceholder(/Вставте український текст/).fill(
    'Учні читають український текст і обговорюють вправи на уроці.'
  );

  // Fail-closed contract: the picker is replaced by an explanatory banner.
  await expect(page.getByRole('combobox', { name: 'Модель для уроку' })).not.toBeVisible();
  const banner = page.getByTestId('no-qualified-models');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(
    'Моделі тимчасово недоступні: кваліфікація для поточних правил уроку ще не завершена.'
  );

  // The bake button must stay disabled while no qualified model is selected.
  const bakeButton = page.getByRole('button', { name: /Згенерувати урок/ });
  await expect(bakeButton).toBeDisabled();
});
