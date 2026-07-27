/**
 * E2E against the dev stub.
 * Covers redeem → paste → bake polling → lesson with all 9 blocks → ack warnings → accept → draft.
 * Also: fragment scrub, no token in storage, 401/410 paths.
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
const TEST_LOGICAL_MODEL_ID = 'gemini-3.5-flash';

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

test.describe('Hramatka teacher frontend E2E (stub)', () => {
  test.beforeAll(async () => {
    await startStub();
  });
  test.afterAll(async () => {
    await stopStub();
  });

  test.beforeEach(async ({ page, context }) => {
    await context.clearCookies();
    await page.goto(APP);
    // #93 item6: reset stateful stub between tests + clear after valid origin (no about:blank)
    await page.evaluate(async () => {
      await fetch('/api/dev/clear-session', { method: 'POST' }).catch(() => {});
      await fetch('/api/dev/reset', { method: 'POST' }).catch(() => {});
    });
  });

  test('redeem from fragment, scrub, no storage token, session active', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload(); // ensure full mount with fragment so redeem effect runs from initial hash
    // after redeem the hash should be scrubbed to /teacher/
    await expect(page).toHaveURL(/\/teacher\/?$/);

    // no token remains
    const hash = await page.evaluate(() => location.hash);
    expect(hash).not.toContain('invite');

    // no token material in storage (dev tooling may add keys, but never the invite token or 'token' keys)
    const storageInfo = await page.evaluate((tok) => {
      const ls = Object.keys(localStorage).concat(Object.values(localStorage));
      const ss = Object.keys(sessionStorage).concat(Object.values(sessionStorage));
      const all = ls.concat(ss).join(' ');
      return {
        hasInvite: all.includes('invite'),
        hasTokenVal: all.includes(tok),
        hasTokenKey: /token|invite/i.test(all)
      };
    }, TEST_TOKEN);
    expect(storageInfo.hasInvite || storageInfo.hasTokenVal || storageInfo.hasTokenKey).toBeFalsy();

    // should reach paste UI
    await expect(page.getByRole('heading', { name: 'Створити новий урок' })).toBeVisible({ timeout: 10000 });
  });

  test('qualified model stub contract → paste → bake renders all 9 blocks', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload(); // ensure full mount with fragment so redeem effect runs
    await page.waitForURL(/\/teacher\/?$/);

    const modelContract = await page.evaluate(async () => {
      const response = await fetch('/api/lesson-models');
      return { status: response.status, payload: await response.json() };
    });
    expect(modelContract).toEqual({
      status: 200,
      payload: {
        registry_version: 'QualifiedLogicalModels.v1',
        models: [{
          id: TEST_LOGICAL_MODEL_ID,
          label: 'Gemini 3.5 Flash',
          description: 'Детермінована тестова модель.',
        }],
        unavailable_message: null,
      },
    });
    await expect(page.getByLabel('Модель для уроку')).toHaveValue(TEST_LOGICAL_MODEL_ID);

    const rejectedModelRequests = await page.evaluate(async () => {
      const session = await (await fetch('/api/session')).json();
      const attempt = async (logicalModelId?: string) => {
        const body: Record<string, unknown> = {
          id: crypto.randomUUID(),
          anchor: { text: 'Текст для перевірки відхилення моделі.', source: 'teacher-paste' },
          level: 'B1',
          duration: 45,
          focus: null,
        };
        if (logicalModelId !== undefined) body.logical_model_id = logicalModelId;
        const response = await fetch('/api/lessons', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': session.csrf_token,
          },
          body: JSON.stringify(body),
        });
        return { status: response.status, code: (await response.json()).code };
      };
      return {
        missing: await attempt(),
        unknown: await attempt('unknown-model'),
      };
    });
    expect(rejectedModelRequests).toEqual({
      missing: { status: 409, code: 'model_unavailable' },
      unknown: { status: 409, code: 'model_unavailable' },
    });

    // paste form
    const text = 'Тестовий текст для демонстрації уроку з усіма типами. Він містить речення для вправ.';
    await page.getByPlaceholder(/Вставте український текст/).fill(text);
    const submittedLesson = page.waitForRequest(
      request => request.url().endsWith('/api/lessons') && request.method() === 'POST',
    );
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    expect((await submittedLesson).postDataJSON().logical_model_id).toBe(TEST_LOGICAL_MODEL_ID);

    // baking / poll visible
    await expect(page.getByText(/бакінг|статус|готово/i)).toBeVisible({ timeout: 10000 });

    // wait for lesson view with blocks
    await page.waitForSelector('[data-activity-player], .dblock, .block', { timeout: 15000 });

    // verify all 9 types are present via their labels or wrappers
    const expectedTypes = ['true-false','cloze','match-up','quiz','mark-the-words','fill-in','error-correction','text-questions','short-writing'];
    for (const t of expectedTypes) {
      // either data attr or title containing type-ish
      const found = await page.locator(`[data-activity-type="${t}"], [data-activity-player="${t}"], .dblock:has-text("${t}"), .block:has-text("${t}")`).count();
      expect(found, `block for ${t} should be visible`).toBeGreaterThan(0);
    }

    const lessonId = new URL(page.url()).hash.match(/^#\/lessons\/([^?]+)/)?.[1];
    expect(lessonId).toBeTruthy();
    const persistedModelId = await page.evaluate(async (id) => {
      const response = await fetch(`/api/lessons/${id}`);
      return (await response.json()).logical_model_id;
    }, lessonId!);
    expect(persistedModelId).toBe(TEST_LOGICAL_MODEL_ID);

    const recreatedModel = await page.evaluate(async (id) => {
      const session = await (await fetch('/api/session')).json();
      const response = await fetch(`/api/lessons/${id}/recreate`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': session.csrf_token,
        },
      });
      const recreated = await response.json();
      await fetch(`/api/lessons/${recreated.id}/status`);
      const resource = await (await fetch(`/api/lessons/${recreated.id}`)).json();
      return { status: response.status, logicalModelId: resource.logical_model_id };
    }, lessonId!);
    expect(recreatedModel).toEqual({ status: 202, logicalModelId: TEST_LOGICAL_MODEL_ID });

    // no placeholder messages
    const bad = await page.locator('text=тип поки без віджета, text=unsupported, text=placeholder').count();
    expect(bad).toBe(0);
  });

  test('ack warnings, accept blocked until acks, then accept succeeds, then draft', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload(); // ensure full mount with fragment so redeem effect runs
    await page.waitForURL(/\/teacher\/?$/);

    const text = 'Текст для перевірки ack та accept.';
    await page.getByPlaceholder(/Вставте/).fill(text);
    await page.getByRole('button', { name: /Згенерувати/ }).click();

    await page.waitForSelector('.dblock.warn, .block.warn', { timeout: 15000 });

    // There should be visible warnings
    const warns = page.locator('.dblock.warn, .block.warn');
    const count = await warns.count();
    expect(count).toBeGreaterThan(0);

    // The blocked message (warn note) is visible before acks; the accept is disabled until acks.
    const acceptBtn = page.getByRole('button', { name: /Прийняти заняття/ });
    await expect(page.getByText(/підтверд(іть|ити) (усі|всі) попередження|warning_acknowledgements/i)).toBeVisible();

    // Ack all visible warns
    const ackButtons = page.locator('.ack-btn');
    const n = await ackButtons.count();
    for (let i = 0; i < n; i++) {
      await ackButtons.nth(0).click();
      await page.waitForTimeout(200);
    }

    // Now accept should work. Assert the accepted CHIP specifically (exact «Прийнято»);
    // the meta line also contains «Прийнято: так», so a loose regex matches 2 elements and
    // trips Playwright strict mode nondeterministically under CI timing.
    await acceptBtn.click();
    await expect(page.locator('span.chip.ok').getByText('Прийнято', { exact: true })).toBeVisible({ timeout: 5000 });

    // Return to draft
    await page.getByRole('button', { name: /Зберегти як чернетку/ }).click();
    await expect(page.getByText(/чернетку|draft/i)).toBeVisible({ timeout: 3000 });
  });

  test('401 on no session leads to invite screen', async ({ page }) => {
    // #93 item6: clear after goto (valid origin, never about:blank fetch)
    await page.goto(`${APP}/teacher/`);
    await page.evaluate(async () => {
      await fetch('/api/dev/clear-session', { method: 'POST' }).catch(() => {});
    });
    // hit a protected route (proxied to stub) to exercise 401 path
    await page.evaluate(async () => {
      // @ts-ignore
      await fetch('/api/lessons', { credentials: 'include' }).catch(() => {});
    });
    // UI should surface invite (shown whenever !session)
    await expect(page.getByRole('heading', { name: /Вхід для викладача/i })).toBeVisible({ timeout: 3000 });
  });

  // #93 item1: direct-link load (no empty page)
  test('direct-link / refresh to lesson renders content (no blank, session gate)', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const text = 'Текст для прямого посилання.';
    await page.getByPlaceholder(/Вставте український текст/).fill(text);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    await page.waitForSelector('.lesson-view, .dblock', { timeout: 15000 });
    const lessonUrl = await page.url();
    expect(lessonUrl).toMatch(/#\/lessons\//);

    // direct / refresh simulation
    await page.goto(`${APP}/teacher/`);
    await page.goto(lessonUrl);
    await expect(page.locator('.lesson-view')).toBeVisible({ timeout: 8000 });
    await page.waitForSelector('.dblock', { timeout: 10000 });
  });

  // #93 item2: catalog auto-loads
  test('catalog auto-loads on session ready without clicking Оновити список', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    // create one lesson via UI flow (ensures session + lesson exists)
    const text = 'Текст для перевірки авто-каталогу.';
    await page.getByPlaceholder(/Вставте український текст/).fill(text);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.dblock, .block', { timeout: 15000 });

    // navigate back to the canonical My Lessons route — this triggers catalog refresh
    await page.getByRole('button', { name: /До списку/ }).click();

    // without clicking «Оновити список», My Lessons should have populated
    await expect(page.locator('.catalog li')).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('button', { name: /Оновити список/ })).toBeVisible();
  });

  // #93 item3: terminal convergence + no stale
  test('pollStatus always converges to terminal state and refreshes step (no stale baking after ready)', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const text = 'Тест збіжності poll.';
    await page.getByPlaceholder(/Вставте/).fill(text);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    await expect(page.getByText(/готується|статус/i)).toBeVisible({ timeout: 5000 });
    await page.waitForSelector('.dblock, .block', { timeout: 15000 });

    await expect(page.locator('.lesson-view .dblock:not(.empty-phase)')).toHaveCount(9, { timeout: 5000 });
    const staleCount = await page.locator('text=завдання складено').count();
    expect(staleCount).toBe(0);
  });

  // #93 item4: UA failure banner (key off code, use server msg or UA fallback)
  test('failure banner is Ukrainian (server message or fallback, no English leak)', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const text = 'bad bake text';
    await page.getByPlaceholder(/Вставте/).fill(text);

    // force stub bad id for immediate failed (with UA message in stub)
    await page.evaluate(() => {
      const BAD = '00000000-0000-0000-0000-000000000bad';
      // @ts-ignore
      crypto.randomUUID = () => BAD;
    });

    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    const recovery = page.getByTestId('failure-recovery');
    await expect(recovery).toBeVisible({ timeout: 10000 });
    await expect(recovery).toContainText(/Не вдалося створити урок/);
    await expect(recovery).toContainText(/Постачальник тимчасово недоступний|текст уже збережено/i);
    await expect(page.locator('.step.fail .sd')).not.toContainText(/готово|завдання складено/i);

    const en = await page.locator('text=The lesson bake could not be completed').count();
    expect(en).toBe(0);
  });

  test('session boundary: logout then new invite clears form and saved request (cross-teacher leak)', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const teacherAText = 'Секретний текст викладача А — не повинен з’явитися у наступній сесії.';
    await page.getByPlaceholder(/Вставте український текст/).fill(teacherAText);
    const durationPicker = page.locator('.field').filter({ hasText: 'Тривалість' }).locator('select');
    await durationPicker.selectOption('90');
    await page.getByPlaceholder(/вищий ступінь/).fill('майбутній час');

    await page.getByRole('button', { name: 'Вийти' }).click();
    await expect(page.getByRole('heading', { name: /Вхід для викладача/i })).toBeVisible();

    const tokenB = 'B'.repeat(42) + 'Q';
    await page.evaluate((tok) => {
      window.location.hash = `#invite=${tok}`;
    }, tokenB);

    await expect(page.getByRole('heading', { name: 'Створити новий урок' })).toBeVisible({ timeout: 10000 });
    await expect(page.getByPlaceholder(/Вставте український текст/)).toHaveValue('');
    await expect(durationPicker).toHaveValue('60');
    await expect(page.getByPlaceholder(/вищий ступінь/)).toHaveValue('');

    const storageCleared = await page.evaluate(() => {
      return sessionStorage.getItem('hramatka:last-bake-request') === null;
    });
    expect(storageCleared).toBe(true);
  });

  test('failure recovery: retry button creates a new lesson from saved text', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const text = 'Текст для повторної спроби після збою.';
    await page.getByPlaceholder(/Вставте/).fill(text);

    await page.evaluate(() => {
      const BAD = '00000000-0000-0000-0000-000000000bad';
      // @ts-ignore
      crypto.randomUUID = () => BAD;
    });

    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await expect(page.getByTestId('failure-recovery')).toBeVisible({ timeout: 10000 });

    // restore real UUID for the retry
    await page.evaluate(() => {
      // @ts-ignore
      delete crypto.randomUUID;
    });

    await page.getByRole('button', { name: /Створити урок ще раз із цим текстом/ }).click();
    await page.waitForSelector('.dblock, .block', { timeout: 15000 });
    await expect(page.locator('.lesson-view .dblock:not(.empty-phase)')).toHaveCount(9, { timeout: 5000 });
  });

  test('help overlay opens from header and shows Ukrainian guidance', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByRole('button', { name: 'Довідка' }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('heading', { name: /Як користуватися «Граматкою»/ })).toBeVisible();
    await expect(page.getByText(/Створення уроку/)).toBeVisible();
    await expect(page.getByText(/Якщо сталася помилка/)).toBeVisible();
    await page.getByRole('button', { name: 'Зрозуміло' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  });

  test('review mode toggles answer keys client-side', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const text = 'Текст для перемикача відповідей.';
    await page.getByPlaceholder(/Вставте/).fill(text);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });

    await page.getByRole('button', { name: 'Сховати відповіді' }).click();
    await expect(page.locator('.teacher-key')).toHaveCount(0);

    await page.getByRole('button', { name: 'Показати відповіді' }).click();
    await expect(page.locator('.teacher-key').first()).toBeVisible();
  });

  test('conductor: accept stub lesson → start conduct → clock present → student screen (no answers) → skip → summary', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    // Create a lesson (stub produces ready blocks quickly)
    const text = 'Текст для перевірки Проведення заняття.';
    await page.getByPlaceholder(/Вставте/).fill(text);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    // Wait for lesson content (blocks)
    await page.waitForSelector('.dblock, .block', { timeout: 15000 });

    // Ack any warnings to enable accept
    const ackButtons = page.locator('.ack-btn');
    const nAcks = await ackButtons.count();
    for (let i = 0; i < nAcks; i++) {
      if (await ackButtons.first().isVisible()) {
        await ackButtons.first().click();
        await page.waitForTimeout(120);
      }
    }

    // Accept
    const acceptBtn = page.getByRole('button', { name: /Прийняти заняття/ });
    if (await acceptBtn.isVisible()) {
      await acceptBtn.click();
    }
    // Scope to the header chip: the meta line ("Ревізія … • Прийнято: так") also
    // contains «Прийнято», so an unscoped getByText is strict-mode ambiguous.
    await expect(page.locator('.lesson-actions').getByText(/Прийнято|accepted/i)).toBeVisible({ timeout: 8000 });

    // The conductor button should now be visible (only for accepted)
    const conductBtn = page.getByRole('button', { name: /Провести заняття/ });
    await expect(conductBtn).toBeVisible({ timeout: 5000 });

    // Start conduct
    await conductBtn.click();

    // Conductor UI appears (title + rail/clock)
    await expect(page.locator('.conductor-view .cond-title b')).toContainText('Проведення заняття', { timeout: 5000 });
    await expect(page.locator('.cond-rail').first()).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.cond-clock').first()).toBeVisible({ timeout: 5000 });

    // Shared student screen visible (green border area), ActivityPlayer inside, no teacher keys visible in shared
    await expect(page.locator('.cond-shared')).toBeVisible({ timeout: 5000 });
    // Keys/answer panel only in teacher private (wheat) — shared must not leak answers
    await expect(page.locator('.cond-shared .teacher-key')).toHaveCount(0);

    // Clock element shows initial time form
    await expect(page.locator('.cond-clock .big')).toBeVisible();

    // Skip current task
    const skipBtn = page.getByRole('button', { name: /Пропустити/ });
    if (await skipBtn.count() > 0) {
      await skipBtn.first().click();
      await page.waitForTimeout(200);
    }

    // Force-finish by skipping remaining to reliably reach summary in stub E2E (9 blocks)
    for (let k = 0; k < 12; k++) {
      const sk = page.getByRole('button', { name: /Пропустити/ });
      if (await sk.count() > 0) {
        await sk.first().click();
        await page.waitForTimeout(120);
      } else {
        break;
      }
    }

    // Also try any remaining "Готово"
    for (let k = 0; k < 4; k++) {
      const dn = page.getByRole('button', { name: /Готово|✓ Готово/ });
      if (await dn.count() > 0 && await dn.first().isVisible()) {
        await dn.first().click();
        await page.waitForTimeout(120);
      }
    }

    // Summary should appear
    await expect(page.locator('.cond-sum')).toBeVisible({ timeout: 10000 });
  });

  test('review mode shows the source text inline in the document', async ({ page }) => {
    const anchorSnippet = 'Унікальний якірний текст для перевірки рендеру в каталозі.';
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill(anchorSnippet);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.dblock, .block', { timeout: 15000 });

    // Document-first review workbench keeps the anchor always visible inside the paper,
    // plus the print-only copy stays attached for the teacher print stylesheet.
    await expect(page.locator('.review-paper .anchorbody')).toContainText(anchorSnippet);
    await expect(page.getByTestId('anchor-print')).toBeAttached();
  });

  test('run mode: teacher can open source text without leaving run', async ({ page }) => {
    const anchorSnippet = 'Текст для режиму запуску без виходу з перегляду.';
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill(anchorSnippet);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.block', { timeout: 15000 });

    await page.getByTestId('enter-student-mode-btn').click();
    await expect(page.getByTestId('anchor-panel-run')).toHaveCount(0);
    await page.getByTestId('anchor-toggle-run').click();
    await expect(page.getByTestId('anchor-panel-run')).toBeVisible();
    await expect(page.getByTestId('anchor-panel-run').getByTestId('anchor-text-body')).toContainText(anchorSnippet);
    await expect(page.locator('.block').first()).toBeVisible();
  });

  test('resumed baking lesson from catalog shows live status view and polling', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const slowText = '__SLOW_BAKE__ Текст для перевірки відновленого статусу з каталогу.';
    await page.getByPlaceholder(/Вставте/).fill(slowText);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    await expect(page.getByTestId('baking-status-view')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('baking-polling')).toBeVisible({ timeout: 8000 });

    await page.getByRole('button', { name: /До списку/ }).click();
    await expect(page.getByRole('heading', { name: 'Мої заняття' })).toBeVisible();

    await page.locator('.catalog li button').first().click();
    await expect(page.getByTestId('baking-status-view')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('baking-polling')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('baking-subline')).toContainText(/Фаза|Текст отримано|Генерація|Ще працюємо/i);
    await expect(page.locator('.block')).toHaveCount(0);
  });

  test('student print variant has zero answer-key content in print DOM', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill('Текст для перевірки друку для учня.');
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });

    await page.evaluate(() => { window.print = () => {}; });
    await page.getByTestId('print-student').click();
    await expect(page.locator('.teacher-app')).toHaveClass(/print-variant-student/);

    const keyCount = await page.locator('[data-testid="teacher-answer-key"]:visible').count();
    const keyPhrase = await page.locator('[data-testid="teacher-answer-key"] strong:visible').count();
    expect(keyCount).toBe(0);
    expect(keyPhrase).toBe(0);

    await page.getByTestId('print-teacher').click();
    await expect(page.locator('.teacher-app')).toHaveClass(/print-variant-teacher/);
    const teacherKeys = await page.locator('[data-testid="teacher-answer-key"]:visible').count();
    expect(teacherKeys).toBeGreaterThan(0);
  });

  test('URL import flow creates lesson and failure keeps form state', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByRole('tab', { name: 'З посилання' }).click();
    await page.getByTestId('anchor-url-input').fill('https://stub.example.test/article');
    await page.getByTestId('fetch-anchor-url-btn').click();

    const textarea = page.getByTestId('anchor-text-input');
    await expect(textarea).toHaveValue(/Текст, отриманий із посилання/);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('[data-activity-player], .block', { timeout: 15000 });

    await page.getByTestId('copy-lesson-as-new').click();
    await expect(page.getByRole('heading', { name: 'Створити новий урок' })).toBeVisible();
    await expect(page.getByRole('tab', { name: 'З посилання' })).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByTestId('anchor-url-input')).toBeVisible();

    const failUrl = 'https://stub.example.test/fail-fetch';
    await page.getByTestId('anchor-url-input').fill(failUrl);
    await page.getByTestId('fetch-anchor-url-btn').click();
    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.getByTestId('anchor-url-input')).toHaveValue(failUrl);
    await expect(textarea).toHaveValue(/Текст, отриманий із посилання/);
  });

  test('failed status subline never shows stale «готово» step (regression)', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill('bad bake text');
    await page.evaluate(() => {
      const BAD = '00000000-0000-0000-0000-000000000bad';
      // @ts-ignore
      crypto.randomUUID = () => BAD;
    });
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    await expect(page.getByTestId('failure-recovery')).toBeVisible({ timeout: 10000 });
    const subline = page.getByTestId('baking-subline');
    await expect(subline).toBeVisible();
    const text = await subline.innerText();
    expect(text).not.toMatch(/готово|готовий/i);
    expect(text).toMatch(/Постачальник|Не вдалося/i);
    // restore native uuid so subsequent tests (incl. catalog open states) get real bakes, not forced-fail
    await page.evaluate(() => { try { delete (crypto as any).randomUUID; } catch {} });
  });

  // Catalog open states (PR #111 hardened per review F3): prove ready (200), baking (409 + poll), failed (409 + failure card)
  // from catalog after returning to hub. Use distinctive text + filter (no first() fallback).
  // Baking uses slow marker so it reliably stays non-ready across back+reopen.
  test('open ready lesson from catalog', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    // ensure native uuid (previous tests may have left BAD override)
    await page.evaluate(() => { try { delete (crypto as any).randomUUID; } catch {} });

    const text = 'Текст для відкриття готового уроку з каталогу.';
    await page.getByPlaceholder(/Вставте український текст/).fill(text);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.block', { timeout: 15000 });

    // back to hub; catalog has the ready item
    await page.getByRole('button', { name: '← До списку' }).click();
    await expect(page.locator('.catalog')).toBeVisible({ timeout: 5000 });
    await page.getByRole('button', { name: /Оновити список/i }).click();
    await expect(page.locator('.catalog li')).toBeVisible({ timeout: 3000 });

    // target a ready row by chip label (titles are not unique); click proves openLesson 200 path from catalog
    const row = page.locator('.catalog li').filter({ hasText: /готово/ }).first();
    await row.getByTestId('catalog-open-btn').click();

    // lands in review with 9 blocks (real 200)
    await expect(page.locator('.lesson-view .block')).toHaveCount(9, { timeout: 10000 });
  });

  test('open baking lesson from catalog (exercises 409 path reliably)', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    // ensure native uuid (previous tests may have left BAD override)
    await page.evaluate(() => { try { delete (crypto as any).randomUUID; } catch {} });

    const slowText = '__SLOW_BAKE__ Текст для відкриття baking-стану з каталогу.';
    await page.getByPlaceholder(/Вставте український текст/).fill(slowText);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    // ensure we entered baking path
    await expect(page.getByTestId('baking-status-view')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('baking-polling')).toBeVisible({ timeout: 8000 });

    // back to hub/catalog quickly; slow requires 8 polls so it stays baking
    await page.getByRole('button', { name: '← До списку' }).click();
    await expect(page.locator('.catalog')).toBeVisible({ timeout: 5000 });
    await page.getByRole('button', { name: /Оновити список/i }).click();

    // target the baking row via chip (reliable, independent of title/id text); no blind first()
    const row = page.locator('.catalog li').filter({ hasText: /готується/ }).first();
    await expect(row).toBeVisible({ timeout: 3000 });
    await row.getByTestId('catalog-open-btn').click();

    // must land on baking UI (not ready blocks), via 409 path + bakeStatus && !lesson
    await expect(page.getByTestId('baking-status-view')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('baking-polling')).toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId('baking-subline')).toBeVisible();
    await expect(page.locator('.block')).toHaveCount(0);
    // still baking, not promoted yet (slow)
    await expect(page.getByText(/готується|статус/i).first()).toBeVisible();
  });

  test('open failed lesson from catalog shows failure card + re-submit wired', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const text = 'Текст для failed-уроку з каталогу.';
    await page.getByPlaceholder(/Вставте український текст/).fill(text);

    // force the BAD id so stub marks failed on POST (and saveLastBakeRequest stores it)
    await page.evaluate(() => {
      // @ts-ignore
      crypto.randomUUID = () => '00000000-0000-0000-0000-000000000bad';
    });
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();

    // wait via baking then failure (poll corrects status); some timing from prior state
    await expect(page.getByTestId('baking-status-view')).toBeVisible({ timeout: 8000 });
    const recovery = page.getByTestId('failure-recovery');
    await expect(recovery).toBeVisible({ timeout: 10000 });

    // back to catalog via list button (use the header one with arrow; recovery card has different "Повернутися...")
    await page.getByRole('button', { name: '← До списку' }).click();
    await expect(page.locator('.catalog')).toBeVisible({ timeout: 5000 });
    await page.getByRole('button', { name: /Оновити список/i }).click();

    // the failed row exists (chip bad or помилка)
    const failedRow = page.locator('.catalog li').filter({ hasText: /помилка|bad|00000000/ });
    await expect(failedRow).toBeVisible({ timeout: 3000 });

    // click the failed catalog row -> must land on failure card directly (not baking spinner)
    await failedRow.getByTestId('catalog-open-btn').click();

    const recovery2 = page.getByTestId('failure-recovery');
    await expect(recovery2).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('baking-polling')).toHaveCount(0);
    await expect(recovery2).toContainText('Створити урок ще раз із цим текстом');

    // retry via server recreate with EMPTY localStorage (no client-side source)
    await page.evaluate(() => localStorage.clear());
    const retryBtn = recovery2.getByTestId('failure-retry-btn');
    await expect(retryBtn).toBeVisible();
    await expect(retryBtn).toBeEnabled();

    // restore real uuid; click re-submit -> should produce a new ready lesson from server request
    await page.evaluate(() => {
      // @ts-ignore
      delete crypto.randomUUID;
    });
    await retryBtn.click();
    await page.waitForSelector('.block', { timeout: 15000 });
    await expect(page.locator('.lesson-view .block')).toHaveCount(9, { timeout: 5000 });
  });

  // Sol P1-5: clipboard lesson export — student variant never includes answers/«Ключ відповіді».
  test('clipboard student copy yields text with tasks but no answer_key content', async ({ page, context }) => {
    // Grant clipboard-permission so navigator.clipboard.writeText works headlessly.
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);

    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    // Use a distinct anchor phrase so we can assert the body is captured.
    await page.getByPlaceholder(/Вставте/).fill('Текст для перевірки експорту в буфер обміну.');
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });

    await page.getByTestId('copy-clipboard-student').click();

    // UA success banner surfaces.
    await expect(page.getByTestId('clipboard-notice')).toContainText(/Скопійовано.*учня/i);

    // Read the actual captured clipboard text.
    const captured = await page.evaluate(async () => {
      const txt = await navigator.clipboard.readText();
      return txt;
    });

    // Positive: a real task body was captured (the golden lesson title + a task chip).
    expect(captured.length).toBeGreaterThan(0);
    expect(captured).toContain('Золотий урок');
    expect(captured).toContain('ТЕКСТ ДЛЯ ЧИТАННЯ');
    expect(captured).toContain('Тривалість: ≈ 60 хв');
    // Sanity: any task chip header is present.
    expect(/\[true-false\]|\[cloze\]|\[quiz\]|\[match-up\]/.test(captured)).toBe(true);

    // Negative: student copy MUST NOT contain answer-key content or its header.
    expect(captured).not.toContain('Ключ відповіді');
    // The golden true-false answer_key is the literal «правда» — student variant excludes it.
    expect(captured).not.toMatch(/Примітка|Походження/);
  });

  test('clipboard student copy is available from the run-mode student toolbar', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);

    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill('Текст для перевірки буфера в режимі учня.');
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });

    await page.getByTestId('enter-student-mode-btn').click();
    await expect(page.getByTestId('student-surface')).toBeVisible();

    await page.getByTestId('copy-clipboard-student-run').click();
    await expect(page.getByTestId('clipboard-notice')).toContainText(/Скопійовано.*учня/i);

    const captured = await page.evaluate(async () => await navigator.clipboard.readText());
    expect(captured).toContain('Золотий урок');
    expect(captured).toContain('ТЕКСТ ДЛЯ ЧИТАННЯ');
    expect(captured).not.toContain('Ключ відповіді');
  });

  test('teacher copy includes «Ключ відповіді» (verify teacher variant differs)', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);

    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill('Текст для перевірки вчительського буфера.');
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });

    await page.getByTestId('copy-clipboard-teacher').click();
    await expect(page.getByTestId('clipboard-notice')).toContainText(/Скопійовано.*вчителя/i);

    const captured = await page.evaluate(async () => await navigator.clipboard.readText());
    expect(captured).toContain('Ключ відповіді');
  });

  test('clone-to-form action is preserved as «Створити інший урок із цього тексту»', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    await page.getByPlaceholder(/Вставте/).fill('Текст для перевірки кнопки клонування.');
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });

    const cloneBtn = page.getByTestId('copy-lesson-as-new');
    await expect(cloneBtn).toHaveText(/Створити інший урок із цього тексту/);
    await cloneBtn.click();
    // Form is restored on the paste view, with the anchor text pre-filled.
    await expect(page.getByPlaceholder(/Вставте/)).toHaveValue('Текст для перевірки кнопки клонування.');
  });

  test('copy-as-new is not silently disabled while another lesson bakes and a review save is in flight', async ({ page }) => {
    await page.goto(`${APP}/teacher/#invite=${TEST_TOKEN}`);
    await page.reload();
    await page.waitForURL(/\/teacher\/?$/);

    const anchorOne = '__CLONE_BUSY__ Текст готового уроку для клонування під час бакінгу.';
    await page.getByPlaceholder(/Вставте/).fill(anchorOne);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await page.waitForSelector('.teacher-key', { timeout: 15000 });
    await page.waitForURL(/#\/lessons\//);
    const lessonOneUrl = page.url();

    // Leave for My Lessons, then start a slow bake so another lesson stays in-flight.
    await page.getByRole('button', { name: '← До списку' }).click();
    await expect(page.locator('.catalog')).toBeVisible({ timeout: 5000 });
    await page.getByRole('main').getByRole('button', { name: '+ Нове заняття' }).click();

    await page.evaluate(() => { try { delete (crypto as any).randomUUID; } catch {} });
    const slowText = '__SLOW_BAKE__ Текст другого уроку, що залишається в бакінгу.';
    await page.getByPlaceholder(/Вставте/).fill(slowText);
    await page.getByRole('button', { name: /Згенерувати урок/ }).click();
    await expect(page.getByTestId('baking-status-view')).toBeVisible({ timeout: 8000 });

    await page.goto(lessonOneUrl);
    await expect(page.locator('.lesson-view .block')).toHaveCount(9, { timeout: 10000 });

    // Hold the next warning-ack mutation open so global `loading` stays true briefly.
    let releaseAck: (() => void) | null = null;
    const ackGate = new Promise<void>((resolve) => { releaseAck = resolve; });
    await page.route('**/api/lessons/*/blocks/*/accept', async (route) => {
      await ackGate;
      try {
        await route.continue();
      } catch {
        /* page may navigate away before the held ack completes */
      }
    });

    const ackBtn = page.locator('.ack-btn').first();
    await expect(ackBtn).toBeVisible({ timeout: 5000 });
    await ackBtn.click();

    const cloneBtn = page.getByTestId('copy-lesson-as-new');
    await expect(cloneBtn).toBeEnabled({ timeout: 1000 });
    await cloneBtn.click();

    releaseAck?.();
    await page.unroute('**/api/lessons/*/blocks/*/accept').catch(() => {});

    await expect(page.getByPlaceholder(/Вставте/)).toHaveValue(anchorOne, { timeout: 5000 });
  });
});
