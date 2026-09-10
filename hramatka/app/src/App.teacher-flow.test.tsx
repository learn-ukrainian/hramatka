import '@testing-library/jest-dom';
import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App, { statusCardFromApi, type LessonStatus } from './App';
import { LangProvider } from './i18n';

const teacher = {
  teacher: { id: 'teacher-1', display_name: 'Марія' },
  expires_at: '2030-01-01T00:00:00Z',
  csrf_token: 'csrf-token',
};

function response(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as Response;
}

function errorResponse(status: number, body: unknown) {
  return { ok: false, status, json: async () => body } as Response;
}

function jobStatus(status: 'draft' | 'baking' | 'failed' | 'cancelled', revision: number, attempt = 1) {
  return {
    id: 'lesson-1', status, step: status === 'baking' ? 'завдання складено' : '', revision, attempt,
    attempt_history: status === 'cancelled'
      ? [{ attempt: 1, status: 'cancelled', failure_code: 'cancelled', started_at: '2026-08-13T00:00:00Z', completed_at: '2026-08-13T00:00:02Z', retry_requested_revision: null }]
      : [],
    failure_code: status === 'cancelled' ? 'cancelled' : null,
    failure_message: null,
    created_at: '2026-08-13T00:00:00Z', updated_at: '2026-08-13T00:00:02Z',
  };
}

function statusForTimer(overrides: Partial<LessonStatus> = {}): LessonStatus {
  return {
    id: 'lesson-1',
    status: 'baking',
    step: 'завдання складено',
    revision: 1,
    attempt: 1,
    attempt_history: [],
    failure_code: null,
    failure_message: null,
    created_at: '2026-08-26T08:00:00Z',
    updated_at: '2026-08-26T08:00:00Z',
    ...overrides,
  };
}

function generatedLessonResource(lessonId: string, title = 'Урок для перевірки') {
  return {
    lesson_id: lessonId,
    revision: 1,
    accepted_at: null,
    accepted_revision: null,
    warning_acknowledgements: [],
    activity_regenerations: [],
    logical_model_id: 'pilot',
    methodology: 'ttt',
    grammar_focus: null,
    lesson: {
      schema: 'lu.lesson.v1',
      id: lessonId,
      title,
      level: 'B1',
      method: 'ttt',
      focus: null,
      anchor: {
        text: 'Учні уважно читають український текст.',
        source: 'teacher-paste',
        chars: 39,
      },
      duration: 45,
      version: 1,
      status: 'ready',
      last_error: null,
      accepted: false,
      blocks: [{
        id: 'block-1',
        phase: 1,
        type: 'true-false',
        mode: 'усно',
        activity: {
          id: `${lessonId}-activity-1`,
          type: 'true-false',
          title: 'Перевірмо розуміння',
          level: 'b1',
          payload: {
            type: 'true-false',
            instruction: 'Визначте, чи правильне твердження.',
            items: [{ statement: 'Учні читають текст.', correct: true }],
          },
          answer_key: { items: [{ index: 0, correct: true }] },
          provenance: { source: 'generated', generator: 'pilot', gates: ['v3'] },
        },
        answer_key: { items: [{ index: 0, correct: true }] },
        mark: 'ok',
        note: null,
        edited: false,
        provenance: { source: 'generated', generator: 'pilot', gates: ['v3'] },
      }],
      rejected: [],
      created_at: '2026-08-13T00:00:00Z',
      updated_at: '2026-08-13T00:00:00Z',
    },
  };
}

function regenerationEntry(lessonId: string, status: 'succeeded' | 'failed') {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    lesson_id: lessonId,
    block_id: 'block-1',
    base_revision: 1,
    status,
    attempt: 1,
    failure_code: status === 'failed' ? 'generation_failed' : null,
    failure_message: status === 'failed' ? 'Попередню вправу збережено.' : null,
    prompt_version: 'HramatkaBlockRegeneration.test',
    prompt_sha256: 'a'.repeat(64),
    old_block_hash: 'b'.repeat(64),
    new_block_hash: status === 'succeeded' ? 'c'.repeat(64) : null,
    applied_revision: status === 'succeeded' ? 2 : null,
    created_at: '2026-08-13T00:00:00Z',
    updated_at: '2026-08-13T00:00:02Z',
    started_at: '2026-08-13T00:00:01Z',
    completed_at: '2026-08-13T00:00:02Z',
  };
}

function installFetch(
  lessons: unknown[] = [],
  modelId: string | null = 'pilot',
  sessionPayload: typeof teacher & { local_auth_disabled?: boolean; google_linked?: boolean } = teacher,
  googleOptions: { client_id: string; nonce: string; login_uri: string } | null = null,
) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/session')) return response(sessionPayload);
    if (url.endsWith('/api/auth/google/options')) {
      return googleOptions ? response(googleOptions) : errorResponse(404, {});
    }
    if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 60 });
    if (url.endsWith('/api/lesson-models')) {
      return response({ registry_version: 'test', models: [{ id: 'pilot', label: 'Pilot', description: '' }], unavailable_message: null });
    }
    if (url.endsWith('/api/lessons/lesson-1')) {
      return response({
        lesson_id: 'lesson-1', revision: 1, accepted_at: '2026-07-27T10:00:00Z', accepted_revision: 1,
        warning_acknowledgements: [], logical_model_id: modelId, methodology: 'ttt', grammar_focus: 'вищий ступінь прикметників',
        lesson: {
          schema: 'lu.lesson.v1', id: 'lesson-1', title: 'Порівнюємо квартири', level: 'B1', method: 'ttt',
          focus: 'вищий ступінь прикметників', anchor: { text: 'Квартира була світліша за іншу.', source: 'teacher-paste', chars: 35 },
          duration: 60, version: 1, status: 'ready', last_error: null, accepted: true, blocks: [], rejected: [],
          created_at: '2026-07-27T10:00:00Z', updated_at: '2026-07-27T10:00:00Z',
        },
      });
    }
    if (url.endsWith('/api/lessons')) return response({ lessons });
    return response({});
  }));
}

function renderApp() {
  return render(<LangProvider><App /></LangProvider>);
}

describe('teacher lesson creation and list chrome', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, '', '#/');
  });

  it('continues a valid session when an already-used invite fragment is reopened', async () => {
    installFetch();
    window.history.replaceState(null, '', '#invite=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
    renderApp();

    await screen.findByTestId('anchor-text-input');
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith('/api/session/redeem'))).toBe(false);
    expect(window.location.pathname).toBe('/teacher/');
  });

  it('lets an administrator select a durable owner scope for the lesson catalog', async () => {
    const admin = { ...teacher, role: 'admin' as const };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(admin);
      if (url.endsWith('/api/lesson-owners')) {
        return response({ owners: [
          { id: 'teacher-1', display_name: 'Марія', role: 'admin' },
          { id: 'teacher-2', display_name: 'Тетяна', role: 'teacher' },
        ] });
      }
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 45 });
      if (url.endsWith('/api/lesson-models')) {
        return response({ registry_version: 'test', models: [], unavailable_message: null });
      }
      if (url.includes('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));

    renderApp();
    const picker = await screen.findByTestId('admin-owner-picker');
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => (
      String(url).includes('/api/lessons?owner_id=teacher-1')
    ))).toBe(true));
    const select = picker.querySelector('select')!;
    await act(async () => {
      fireEvent.change(select, { target: { value: 'teacher-2' } });
    });
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => (
      String(url).includes('/api/lessons?owner_id=teacher-2')
    ))).toBe(true));
  });

  it('scrubs an invite fragment when the initial session refresh fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/api/session')) throw new Error('offline');
      return response({});
    }));
    window.history.replaceState(null, '', '#invite=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
    renderApp();

    await waitFor(() => expect(window.location.pathname).toBe('/teacher/'));
    expect(window.location.hash).toBe('');
  });

  it('shows the qualified 45-minute path, fixed TTT methodology, and optional grammar focus', async () => {
    installFetch();
    renderApp();

    const methodology = await screen.findByTestId('methodology-ttt');
    expect(methodology).toBeDisabled();
    expect(methodology).toHaveTextContent('Тест → Навчання → Тест');
    expect(methodology).toHaveTextContent('перевірити → навчити → перевірити. Радимо для B1.');
    expect(screen.getByTestId('qualified-duration')).toHaveTextContent('45 хв');
    expect(screen.queryByRole('combobox', { name: 'Тривалість (хв)' })).not.toBeInTheDocument();

    const grammarFocus = screen.getByTestId('grammar-focus-input');
    expect(grammarFocus).toHaveAttribute('placeholder', 'напр., вищий ступінь прикметників');
    expect(grammarFocus).toHaveAttribute('maxlength', '120');

    fireEvent.change(grammarFocus, { target: { value: 'вищий ступінь прикметників' } });
    fireEvent.change(screen.getByTestId('anchor-text-input'), { target: { value: 'Місто стало тихішим увечері.' } });
    const submit = screen.getByRole('button', { name: 'Згенерувати урок' });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith('/api/lessons'))).toBe(true));
    const request = vi.mocked(fetch).mock.calls.find(([url, init]) =>
      String(url).endsWith('/api/lessons') && (init as RequestInit | undefined)?.method === 'POST',
    );
    expect(request).toBeDefined();
    const [, init] = request!;
    expect(JSON.parse(String((init as RequestInit).body))).toMatchObject({
      duration: 45,
      methodology: 'ttt',
      grammar_focus: 'вищий ступінь прикметників',
    });
  });

  it('keeps the lesson library available when an operator pauses generation (#44)', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 45 });
      if (url.endsWith('/api/lesson-models')) {
        return response({
          registry_version: 'test',
          models: [],
          unavailable_message: 'raw server message must not be rendered',
          unavailable_code: 'generation_disabled',
        });
      }
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));
    renderApp();

    expect(await screen.findByTestId('no-qualified-models')).toHaveTextContent(
      'Створення уроків тимчасово вимкнено',
    );
    expect(screen.getByRole('button', { name: 'Згенерувати урок' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Мої заняття' }));
    expect(await screen.findByRole('heading', { name: 'Мої заняття' })).toBeVisible();
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith('/api/lessons'))).toBe(true);
  });

  it('explains the required paste input for empty and whitespace-only text without starting a bake', async () => {
    installFetch();
    renderApp();

    const textarea = await screen.findByTestId('anchor-text-input');
    const submit = screen.getByRole('button', { name: 'Згенерувати урок' });
    const postRequests = () => vi.mocked(fetch).mock.calls.filter(([url, init]) =>
      String(url).endsWith('/api/lessons') && (init as RequestInit | undefined)?.method === 'POST',
    );

    expect(submit).toBeDisabled();
    expect(textarea).toBeRequired();
    expect(textarea).toHaveAttribute('aria-describedby', 'paste-text-required-guidance');
    expect(screen.getByTestId('paste-text-required-guidance')).toHaveTextContent(
      'Щоб згенерувати урок, вставте український текст.',
    );

    fireEvent.change(textarea, { target: { value: '   ' } });
    expect(submit).toBeDisabled();
    expect(screen.getByTestId('paste-text-required-guidance')).toBeVisible();
    expect(postRequests()).toHaveLength(0);

    fireEvent.click(screen.getByTestId('lang-toggle'));
    expect(screen.getByTestId('paste-text-required-guidance')).toHaveTextContent(
      'To generate a lesson, paste Ukrainian text.',
    );

    fireEvent.click(screen.getByTestId('lang-toggle'));
    expect(screen.getByTestId('paste-text-required-guidance')).toHaveTextContent(
      'Щоб згенерувати урок, вставте український текст.',
    );
  });

  it('renders My Lessons as a separate empty page with no delete affordance', async () => {
    installFetch();
    renderApp();

    fireEvent.click(await screen.findByRole('button', { name: 'Мої заняття' }));
    expect(await screen.findByTestId('lessons-empty-state')).toHaveTextContent('Створіть перше заняття');
    expect(screen.queryByTestId('catalog-delete-btn')).not.toBeInTheDocument();
  });

  it('marks only the current header navigation item as active', async () => {
    installFetch();
    renderApp();

    const lessons = await screen.findByRole('button', { name: 'Мої заняття' });
    const newLesson = screen.getByRole('button', { name: '+ Нове заняття' });
    const settings = screen.getByRole('button', { name: 'Налаштування' });

    expect(lessons).not.toHaveClass('on');
    expect(settings).not.toHaveClass('on');
    expect(newLesson).toHaveClass('on');
  });

  it('lists the teacher lesson metadata and its two drive paths', async () => {
    installFetch([{
      id: 'lesson-1',
      title: 'Порівнюємо квартири',
      anchor_snippet: 'Квартира була світліша за іншу.',
      status: 'ready',
      level: 'B1',
      duration: 60,
      methodology: 'ttt',
      grammar_focus: 'вищий ступінь прикметників',
      revision: 1,
      accepted: true,
      accepted_at: '2026-07-27T10:00:00Z',
      accepted_revision: 1,
      failure_code: null,
      created_at: '2026-07-27T10:00:00Z',
      updated_at: '2026-07-27T10:00:00Z',
    }]);
    renderApp();

    fireEvent.click(await screen.findByRole('button', { name: 'Мої заняття' }));
    await waitFor(() => expect(screen.getByText('Порівнюємо квартири')).toBeInTheDocument());
    expect(screen.getByText('Якір: Квартира була світліша за іншу.')).toBeInTheDocument();
    expect(screen.getByText('B1 · 60 хв · Тест → Навчання → Тест')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Проведення' })).toBeInTheDocument();
    const print = vi.spyOn(window, 'print').mockImplementation(() => undefined);
    fireEvent.click(screen.getByRole('button', { name: 'Друк' }));
    await waitFor(() => expect(print).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('catalog-delete-btn')).not.toBeInTheDocument();
  });

  it('shows which model baked the lesson on the lesson view (#401)', async () => {
    installFetch();
    window.history.replaceState(null, '', '#/lessons/lesson-1');
    renderApp();

    const provenance = await screen.findByTestId('lesson-model', undefined, { timeout: 5000 });
    expect(provenance).toHaveTextContent('Модель: pilot');
  });

  it('shows an honest «невідомо» fallback for lessons without a stored model (#401)', async () => {
    installFetch([], null);
    window.history.replaceState(null, '', '#/lessons/lesson-1');
    renderApp();

    const provenance = await screen.findByTestId('lesson-model', undefined, { timeout: 5000 });
    expect(provenance).toHaveTextContent('Модель: невідомо');
    expect(provenance).not.toHaveTextContent('pilot');
  });

  it('marks review mode as the active, available lesson view', async () => {
    installFetch();
    window.history.replaceState(null, '', '#/lessons/lesson-1');
    renderApp();

    const reviewMode = await screen.findByRole('button', { name: 'Режим огляду' });
    expect(reviewMode).toBeEnabled();
    expect(reviewMode).toHaveAttribute('aria-current', 'page');
    expect(reviewMode).toHaveClass('selected');
  });

  it('opens the reading text when the student run screen loads (#410)', async () => {
    installFetch();
    window.history.replaceState(null, '', '#/lessons/lesson-1');
    renderApp();

    fireEvent.click(await screen.findByTestId('enter-student-mode-btn'));
    expect(await screen.findByTestId('anchor-panel-run')).toHaveTextContent('Квартира була світліша за іншу.');
  });
});

describe('cancel and retry in place (#415)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, '', '#/lessons/lesson-1');
  });

  it('uses the retry attempt clock instead of carrying an earlier attempt forward', () => {
    const firstAttempt = statusCardFromApi(statusForTimer({
      attempt: 2,
      started_at: '2026-08-26T08:10:00Z',
    }));
    const retryQueued = statusCardFromApi(statusForTimer({
      status: 'draft',
      attempt: 3,
      started_at: null,
      updated_at: '2026-08-26T10:00:00Z',
    }), firstAttempt);
    const retryBaking = statusCardFromApi(statusForTimer({
      attempt: 3,
      started_at: '2026-08-26T10:00:05Z',
      updated_at: '2026-08-26T10:00:05Z',
    }), retryQueued);

    expect(retryQueued.startedAt).toBeUndefined();
    expect(retryBaking.startedAt).toBe('2026-08-26T10:00:05Z');

    const legacyRetry = statusCardFromApi(statusForTimer({
      attempt: 4,
      started_at: undefined,
      updated_at: '2026-08-26T12:00:00Z',
    }), retryBaking);
    expect(legacyRetry.startedAt).toBe('2026-08-26T12:00:00Z');
  });

  it('cancels once and retries the same job at its returned revision', async () => {
    let cancelRequests = 0;
    let retryRequests = 0;
    const mutationBodies: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 60 });
      if (url.endsWith('/api/lesson-models')) return response({ registry_version: 'test', models: [{ id: 'pilot', label: 'Pilot', description: '' }], unavailable_message: null });
      if (url.endsWith('/api/lessons/lesson-1') && (!init?.method || init.method === 'GET')) return errorResponse(409, { code: 'lesson_not_ready', message: 'Ще не готово.' });
      if (url.endsWith('/api/lessons/lesson-1/status')) return response(jobStatus('baking', 7));
      if (url.endsWith('/api/lessons/lesson-1/cancel') && init?.method === 'POST') {
        cancelRequests += 1;
        mutationBodies.push(JSON.parse(String(init.body)));
        return response(jobStatus('cancelled', 8));
      }
      if (url.endsWith('/api/lessons/lesson-1/retry') && init?.method === 'POST') {
        retryRequests += 1;
        mutationBodies.push(JSON.parse(String(init.body)));
        return { ok: true, status: 202, json: async () => jobStatus('baking', 9, 2) } as Response;
      }
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));

    renderApp();
    const cancel = await screen.findByTestId('bake-cancel-btn');
    fireEvent.click(cancel);
    fireEvent.click(cancel);
    await screen.findByTestId('failure-recovery');
    expect(cancelRequests).toBe(1);
    expect(mutationBodies[0]).toEqual({ expected_revision: 7 });
    expect(screen.getByTestId('failure-retry-btn')).toHaveTextContent('Повторити в цьому самому занятті');
    expect(screen.getByRole('heading', { name: 'Історія спроб' })).toBeVisible();

    const retry = screen.getByTestId('failure-retry-btn');
    fireEvent.click(retry);
    fireEvent.click(retry);
    await waitFor(() => expect(retryRequests).toBe(1));
    expect(mutationBodies[1]).toEqual({ expected_revision: 8 });
    expect(screen.getByTestId('bake-attempt')).toHaveTextContent('Спроба 2');
    expect(window.location.hash).toContain('/lessons/lesson-1');
  });

  it('keeps cancellation guidance and status understandable after switching chrome locale', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 60 });
      if (url.endsWith('/api/lesson-models')) return response({ registry_version: 'test', models: [{ id: 'pilot', label: 'Pilot', description: '' }], unavailable_message: null });
      if (url.endsWith('/api/lessons/lesson-1') && (!init?.method || init.method === 'GET')) return errorResponse(409, { code: 'lesson_not_ready', message: 'Ще не готово.' });
      if (url.endsWith('/api/lessons/lesson-1/status')) return response(jobStatus('baking', 7));
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));
    renderApp();
    await screen.findByTestId('bake-cancel-btn');
    fireEvent.click(screen.getByTestId('lang-toggle'));
    expect(screen.getByTestId('bake-cancel-btn')).toHaveTextContent('Cancel lesson build');
    expect(screen.getByText(/may finish remotely, but its result cannot be published/i)).toBeVisible();
    fireEvent.click(screen.getByTestId('lang-toggle'));
  });

  it('retries a failed job in place instead of creating a replacement lesson', async () => {
    const retryBodies: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 60 });
      if (url.endsWith('/api/lesson-models')) return response({ registry_version: 'test', models: [{ id: 'pilot', label: 'Pilot', description: '' }], unavailable_message: null });
      if (url.endsWith('/api/lessons/lesson-1') && (!init?.method || init.method === 'GET')) return errorResponse(409, { code: 'lesson_not_ready', message: 'Ще не готово.' });
      if (url.endsWith('/api/lessons/lesson-1/status')) return response({ ...jobStatus('failed', 12), failure_code: 'bake_timeout' });
      if (url.endsWith('/api/lessons/lesson-1/retry') && init?.method === 'POST') {
        retryBodies.push(JSON.parse(String(init.body)));
        return { ok: true, status: 202, json: async () => jobStatus('baking', 13, 2) } as Response;
      }
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));
    renderApp();
    const retry = await screen.findByTestId('failure-retry-btn');
    fireEvent.click(retry);
    await waitFor(() => expect(retryBodies).toEqual([{ expected_revision: 12 }]));
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('/recreate'))).toBe(false);
    expect(screen.getByTestId('bake-attempt')).toHaveTextContent('Спроба 2');
  });
});

describe('activity regeneration reconciliation (#418)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, '', '#/lessons/lesson-1');
  });

  it('never paints terminal success over stale content when lesson refetch fails', async () => {
    const resource = generatedLessonResource('lesson-1');
    let lessonReads = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 60 });
      if (url.endsWith('/api/lesson-models')) {
        return response({ registry_version: 'test', models: [{ id: 'pilot', label: 'Pilot', description: '' }], unavailable_message: null });
      }
      if (url.endsWith('/api/lessons/lesson-1') && (!init?.method || init.method === 'GET')) {
        lessonReads += 1;
        return lessonReads === 1
          ? response(resource)
          : errorResponse(503, { code: 'temporarily_unavailable', message: 'retry' });
      }
      if (url.endsWith('/blocks/block-1/regenerations') && init?.method === 'POST') {
        return response(regenerationEntry('lesson-1', 'succeeded'));
      }
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));
    renderApp();

    fireEvent.click(await screen.findByText('Створити інший варіант'));
    fireEvent.click(screen.getByText('Створити новий варіант'));

    expect(await screen.findByText('Помилка')).toBeInTheDocument();
    expect(screen.getByText('Учні читають текст.')).toBeInTheDocument();
    expect(screen.queryByTestId('regeneration-succeeded')).not.toBeInTheDocument();
    expect(lessonReads).toBe(2);
  });

  it('ignores a late conflict after the teacher opens another lesson', async () => {
    const first = generatedLessonResource('lesson-1', 'Перший урок');
    const second = generatedLessonResource('lesson-2', 'Другий урок');
    let resolveRegeneration!: (value: Response) => void;
    const pendingRegeneration = new Promise<Response>((resolve) => {
      resolveRegeneration = resolve;
    });
    const catalog = [{
      id: 'lesson-2',
      title: 'Другий урок',
      anchor_snippet: 'Учні уважно читають український текст.',
      status: 'ready',
      level: 'B1',
      duration: 45,
      methodology: 'ttt',
      grammar_focus: null,
      revision: 1,
      accepted: false,
      accepted_at: null,
      accepted_revision: null,
      failure_code: null,
      created_at: '2026-08-13T00:00:00Z',
      updated_at: '2026-08-13T00:00:00Z',
    }];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 60 });
      if (url.endsWith('/api/lesson-models')) {
        return response({ registry_version: 'test', models: [{ id: 'pilot', label: 'Pilot', description: '' }], unavailable_message: null });
      }
      if (url.endsWith('/api/lessons/lesson-1')) return response(first);
      if (url.endsWith('/api/lessons/lesson-2')) return response(second);
      if (url.endsWith('/blocks/block-1/regenerations') && init?.method === 'POST') {
        return pendingRegeneration;
      }
      if (url.endsWith('/api/lessons')) return response({ lessons: catalog });
      return response({});
    }));
    renderApp();

    fireEvent.click(await screen.findByText('Створити інший варіант'));
    fireEvent.click(screen.getByText('Створити новий варіант'));
    fireEvent.click(screen.getByRole('button', { name: 'Мої заняття' }));
    await screen.findByText('Другий урок');
    fireEvent.click(screen.getByTestId('catalog-open-btn'));
    await screen.findByTestId('lesson-model');
    expect(window.location.hash).toContain('/lessons/lesson-2');

    await act(async () => {
      resolveRegeneration(errorResponse(409, {
        code: 'revision_conflict',
        message: 'Урок змінився.',
      }));
      await pendingRegeneration;
    });

    await waitFor(() => expect(window.location.hash).toContain('/lessons/lesson-2'));
    expect(screen.getAllByText('Другий урок').length).toBeGreaterThan(0);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('coalesces a same-frame double submit before disabled state paints', async () => {
    const resource = generatedLessonResource('lesson-1');
    let regenerationRequests = 0;
    let resolveRegeneration!: (value: Response) => void;
    const pendingRegeneration = new Promise<Response>((resolve) => {
      resolveRegeneration = resolve;
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 60 });
      if (url.endsWith('/api/lesson-models')) {
        return response({ registry_version: 'test', models: [{ id: 'pilot', label: 'Pilot', description: '' }], unavailable_message: null });
      }
      if (url.endsWith('/api/lessons/lesson-1')) return response(resource);
      if (url.endsWith('/blocks/block-1/regenerations') && init?.method === 'POST') {
        regenerationRequests += 1;
        return pendingRegeneration;
      }
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));
    renderApp();

    fireEvent.click(await screen.findByText('Створити інший варіант'));
    const submit = screen.getByText('Створити новий варіант');
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(regenerationRequests).toBe(1);

    await act(async () => {
      resolveRegeneration(response(regenerationEntry('lesson-1', 'failed')));
      await pendingRegeneration;
    });
  });
});

describe('passkey chrome for local-auth vs invite sessions (#514)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, '', '#/');
    Object.defineProperty(navigator, 'credentials', {
      configurable: true,
      value: {},
    });
  });

  afterEach(() => {
    Reflect.deleteProperty(navigator, 'credentials');
  });

  it('does not render enroll or sign-in chrome for a local-auth-disabled session', async () => {
    installFetch([], 'pilot', {
      ...teacher,
      teacher: { id: 'local-1', display_name: 'Локальний викладач' },
      local_auth_disabled: true,
    });
    renderApp();

    expect(await screen.findByTestId('teacher-display-name')).toHaveTextContent('Локальний викладач');
    expect(screen.getByTestId('local-auth-disabled-banner')).toBeInTheDocument();
    expect(screen.queryByTestId('passkey-enroll-btn')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Додати ключ доступу' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('passkey-sign-in-btn')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Увійти за ключем доступу' })).not.toBeInTheDocument();
  });

  it('still renders enroll chrome for an invite session', async () => {
    installFetch();
    renderApp();

    expect(await screen.findByTestId('passkey-enroll-btn')).toHaveTextContent('Додати ключ доступу');
    expect(screen.queryByTestId('local-auth-disabled-banner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('passkey-sign-in-btn')).not.toBeInTheDocument();
  });

  it('shows a visible Google failure from the OAuth callback query', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/api/session')) {
        return errorResponse(401, { code: 'session_required', message: 'session required' });
      }
      return response({});
    }));
    window.history.replaceState(null, '', '/teacher/?google=failed');
    renderApp();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Не вдалося увійти через Google');
    expect(await screen.findByTestId('sign-in-alternatives')).toBeInTheDocument();
    expect(window.location.search).toBe('?google=failed');
    expect(window.location.pathname).toBe('/teacher/');
  });

  it.each(['signed-in', 'linked'])('shows a persistent error when Google returns %s without a session cookie', async (result) => {
    vi.stubGlobal('fetch', vi.fn(async () => errorResponse(401, {})));
    window.history.replaceState(null, '', `/teacher/?google=${result}`);
    const app = renderApp();
    expect(await screen.findByRole('alert')).toHaveTextContent('Не вдалося увійти через Google');
    expect(window.location.search).toBe('?google=failed');
    app.unmount();
    renderApp();
    expect(await screen.findByRole('alert')).toHaveTextContent('Не вдалося увійти через Google');
  });

  it('opens the teacher workspace when Google returns with a usable session', async () => {
    installFetch([], 'pilot', { ...teacher, google_linked: true });
    window.history.replaceState(null, '', '/teacher/?google=signed-in');
    renderApp();
    expect(await screen.findByTestId('anchor-text-input')).toBeInTheDocument();
    expect(screen.queryByTestId('sign-in-alternatives')).not.toBeInTheDocument();
    expect(window.location.search).toBe('');
  });

  it('shows a Google error when session verification fails to load', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network unavailable'); }));
    window.history.replaceState(null, '', '/teacher/?google=signed-in');
    renderApp();
    expect(await screen.findByRole('alert')).toHaveTextContent('Не вдалося увійти через Google');
    expect(window.location.search).toBe('?google=failed');
  });

  it('still renders sign-in chrome on the unauthenticated invite page', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/api/session')) {
        return errorResponse(401, { code: 'session_required', message: 'session required' });
      }
      return response({});
    }));
    renderApp();

    const alternatives = await screen.findByTestId('sign-in-alternatives');
    expect(alternatives.tagName).toBe('DETAILS');
    expect(alternatives).not.toHaveAttribute('open');
    expect(alternatives).toHaveTextContent('Інші способи входу');
    expect(await screen.findByTestId('passkey-sign-in-btn')).toHaveTextContent('Увійти за ключем доступу');
    expect(screen.getByTestId('recovery-code-sign-in')).toBeInTheDocument();
    expect(screen.queryByTestId('passkey-enroll-btn')).not.toBeInTheDocument();
  });

  it('explains one-time Google setup only to an invited teacher without a durable link', async () => {
    const googleOptions = {
      client_id: '123456789-test.apps.googleusercontent.com',
      nonce: 'A'.repeat(43),
      login_uri: 'https://pilot.example.test/api/auth/google/complete',
    };
    installFetch([], 'pilot', { ...teacher, google_linked: false }, googleOptions);
    renderApp();
    const setup = await screen.findByTestId('google-setup-card');
    expect(setup).toHaveTextContent('Зробіть наступний вхід простим');
    expect(setup).toHaveTextContent('Підключіть Google один раз');
    expect(screen.getByTestId('google-setup-button')).toBeInTheDocument();
    expect(screen.queryByTestId('google-link-button')).not.toBeInTheDocument();
  });

  it('does not offer Google setup again once the server marks the teacher linked', async () => {
    installFetch([], 'pilot', { ...teacher, google_linked: true });
    renderApp();
    await screen.findByTestId('teacher-display-name');
    expect(screen.queryByTestId('google-setup-card')).not.toBeInTheDocument();
  });
});

function installGoogleIdentityStub() {
  const initialize = vi.fn();
  const renderButton = vi.fn();
  Object.defineProperty(window, 'google', {
    configurable: true,
    writable: true,
    value: { accounts: { id: { initialize, renderButton } } },
  });
  if (!document.querySelector('script[data-google-identity-services]')) {
    const script = document.createElement('script');
    script.dataset.googleIdentityServices = 'true';
    document.head.appendChild(script);
  }
  return { initialize, renderButton };
}

function installSignedOutGoogleFetch() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/session')) {
      return errorResponse(401, { code: 'session_required', message: 'session required' });
    }
    if (url.endsWith('/api/auth/google/options')) {
      return response({
        client_id: '123456789-test.apps.googleusercontent.com',
        nonce: 'A'.repeat(43),
        login_uri: 'https://pilot.example.test/api/auth/google/complete',
      });
    }
    return response({});
  }));
}

function firstGisCallback(initialize: ReturnType<typeof vi.fn>) {
  const config = initialize.mock.calls[0]?.[0] as {
    callback?: (response: unknown) => void;
    use_fedcm_for_button?: boolean;
    ux_mode?: string;
  } | undefined;
  if (!config?.callback) {
    throw new Error('GIS initialize was not called with a callback');
  }
  return config.callback;
}

function submittedGoogleCompleteForm() {
  return document.querySelector<HTMLFormElement>('form[action="/api/auth/google/complete"]');
}

describe('teacher GIS popup callback lifetime (#595)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, '', '/teacher/');
    document.querySelectorAll('form[action="/api/auth/google/complete"]').forEach((form) => form.remove());
    document.querySelectorAll('script[data-google-identity-services]').forEach((script) => script.remove());
    Reflect.deleteProperty(window, 'google');
  });

  afterEach(() => {
    vi.useRealTimers();
    document.querySelectorAll('form[action="/api/auth/google/complete"]').forEach((form) => form.remove());
    document.querySelectorAll('script[data-google-identity-services]').forEach((script) => script.remove());
    Reflect.deleteProperty(window, 'google');
    // GIS tests stubGlobal('location'); restoreAllMocks does not undo that, and a
    // frozen location stub breaks later hash replaceState (#587 student-print).
    vi.unstubAllGlobals();
  });

  it('still POSTs a GIS credential after the sign-in effect is cancelled', async () => {
    const { initialize } = installGoogleIdentityStub();
    installSignedOutGoogleFetch();
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {});

    const view = render(
      <StrictMode>
        <LangProvider><App /></LangProvider>
      </StrictMode>,
    );
    await screen.findByTestId('google-sign-in-button');
    await waitFor(() => expect(initialize).toHaveBeenCalled());
    expect(initialize).toHaveBeenCalledWith(expect.objectContaining({
      use_fedcm_for_button: true,
      ux_mode: 'popup',
    }));
    const callback = firstGisCallback(initialize);

    view.unmount();
    await act(async () => {
      callback({ credential: 'aaa.bbb.ccc' });
    });

    expect(submit).toHaveBeenCalled();
    const form = submittedGoogleCompleteForm();
    expect(form?.method.toLowerCase()).toBe('post');
    expect(form?.querySelector('input[name="credential"]')).toHaveValue('aaa.bbb.ccc');
  });

  it('does not swallow a later GIS credential after cancel without one', async () => {
    const { initialize } = installGoogleIdentityStub();
    installSignedOutGoogleFetch();
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {});
    const assign = vi.fn();
    vi.stubGlobal('location', {
      ...window.location,
      assign,
      pathname: '/teacher/',
      search: '',
      href: 'http://localhost/teacher/',
    });

    const view = render(
      <StrictMode>
        <LangProvider><App /></LangProvider>
      </StrictMode>,
    );
    await screen.findByTestId('google-sign-in-button');
    await waitFor(() => expect(initialize).toHaveBeenCalled());
    const callback = firstGisCallback(initialize);

    view.unmount();
    expect(submit).not.toHaveBeenCalled();
    expect(assign).not.toHaveBeenCalled();

    await act(async () => {
      callback({});
    });
    expect(assign).not.toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();

    await act(async () => {
      callback({ credential: 'aaa.bbb.ccc' });
    });
    expect(submit).toHaveBeenCalled();
    expect(submittedGoogleCompleteForm()?.querySelector('input[name="credential"]')).toHaveValue('aaa.bbb.ccc');
    expect(assign).not.toHaveBeenCalled();
  });

  it('POSTs a GIS credential delivered as a bare JWT string', async () => {
    const { initialize } = installGoogleIdentityStub();
    installSignedOutGoogleFetch();
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {});

    render(
      <StrictMode>
        <LangProvider><App /></LangProvider>
      </StrictMode>,
    );
    await screen.findByTestId('google-sign-in-button');
    await waitFor(() => expect(initialize).toHaveBeenCalled());

    await act(async () => {
      firstGisCallback(initialize)('aaa.bbb.ccc');
    });

    expect(submit).toHaveBeenCalled();
    expect(submittedGoogleCompleteForm()?.querySelector('input[name="credential"]')).toHaveValue('aaa.bbb.ccc');
  });

  it('does not send an empty GIS callback to ?google=failed after 250ms', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { initialize } = installGoogleIdentityStub();
    installSignedOutGoogleFetch();
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {});
    const assign = vi.fn();
    vi.stubGlobal('location', {
      ...window.location,
      assign,
      pathname: '/teacher/',
      search: '',
      href: 'http://localhost/teacher/',
    });

    render(
      <StrictMode>
        <LangProvider><App /></LangProvider>
      </StrictMode>,
    );
    await screen.findByTestId('google-sign-in-button');
    await waitFor(() => expect(initialize).toHaveBeenCalled());

    await act(async () => {
      firstGisCallback(initialize)({});
    });
    expect(assign).not.toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(assign).not.toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();

    await act(async () => {
      firstGisCallback(initialize)({ credential: 'aaa.bbb.ccc' });
    });
    expect(submit).toHaveBeenCalled();
    expect(submittedGoogleCompleteForm()?.action).toContain('/api/auth/google/complete');
    expect(submittedGoogleCompleteForm()?.querySelector('input[name="credential"]')).toHaveValue('aaa.bbb.ccc');
    expect(assign).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('logs GIS response shape without the JWT and still POSTs complete', async () => {
    const jwt = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJtdXN0LW5vdC1sb2cifQ.signature';
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    const { initialize } = installGoogleIdentityStub();
    installSignedOutGoogleFetch();
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {});

    render(
      <StrictMode>
        <LangProvider><App /></LangProvider>
      </StrictMode>,
    );
    await screen.findByTestId('google-sign-in-button');
    await waitFor(() => expect(initialize).toHaveBeenCalled());

    await act(async () => {
      firstGisCallback(initialize)({ credential: jwt, select_by: 'btn' });
    });

    const shapeLogs = info.mock.calls.filter((call) => call[0] === 'gis_callback_shape');
    expect(shapeLogs.length).toBeGreaterThan(0);
    const logged = JSON.stringify(shapeLogs);
    expect(logged).toContain('has_credential');
    expect(logged).toContain('credential_len_bucket');
    expect(logged).toContain('select_by');
    expect(logged).not.toContain(jwt);
    expect(logged).not.toContain('eyJhbGciOiJSUzI1NiJ9');
    expect(submit).toHaveBeenCalled();
    expect(submittedGoogleCompleteForm()?.action).toContain('/api/auth/google/complete');
    expect(submittedGoogleCompleteForm()?.querySelector('input[name="credential"]')).toHaveValue(jwt);
  });
});

/**
 * «Друк для учня» wiring (#587).
 *
 * The assertions read the DOM *from inside the `window.print` stub*, which is the
 * only moment that matters: it is the document the print dialog serialises, and it
 * proves the variant re-render lands before printing rather than after.
 */
describe('student print omits teacher material from the print DOM (#587)', () => {
  beforeEach(() => {
    // Undo any leftover stubGlobal('location') from GIS (#595) before routing.
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.history.replaceState(null, '', '#/lessons/lesson-1');
  });

  function installLessonFetch(resource: unknown) {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 60 });
      if (url.endsWith('/api/lesson-models')) {
        return response({ registry_version: 'test', models: [{ id: 'pilot', label: 'Pilot', description: '' }], unavailable_message: null });
      }
      if (url.endsWith('/api/lessons/lesson-1')) return response(resource);
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));
  }

  async function domSeenBy(printTestId: 'print-student' | 'print-teacher') {
    installLessonFetch(generatedLessonResource('lesson-1'));
    renderApp();
    // Answers are on by default — the teacher is reading keys when they print.
    await screen.findByTestId('teacher-answer-key');

    let printed = '';
    const print = vi.spyOn(window, 'print').mockImplementation(() => {
      printed = document.body.innerHTML;
    });
    fireEvent.click(screen.getByTestId(printTestId));
    await waitFor(() => expect(print).toHaveBeenCalledTimes(1));
    return printed;
  }

  it('serialises no answer key, note or provenance margin for the student', async () => {
    const printed = await domSeenBy('print-student');

    expect(printed).toContain('print-variant-student');
    expect(printed).not.toContain('teacher-answer-key');
    expect(printed).not.toContain('teacher-key');
    expect(printed).not.toContain('Ключ відповіді');
    expect(printed).not.toContain('dmargin');
    // The activity the student has to do is still on the sheet.
    expect(printed).toContain('Учні читають текст.');
  });

  it('still serialises the keys for the teacher', async () => {
    const printed = await domSeenBy('print-teacher');

    expect(printed).toContain('print-variant-teacher');
    expect(printed).toContain('teacher-answer-key');
    expect(printed).toContain('Ключ відповіді');
    expect(printed).toContain('dmargin');
  });
});
