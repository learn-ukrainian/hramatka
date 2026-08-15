import '@testing-library/jest-dom';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
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

function installFetch(lessons: unknown[] = [], modelId: string | null = 'pilot') {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/session')) return response(teacher);
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

  it('shows the fixed TTT methodology and the optional grammar-focus field on the new lesson form', async () => {
    installFetch();
    renderApp();

    const methodology = await screen.findByTestId('methodology-ttt');
    expect(methodology).toBeDisabled();
    expect(methodology).toHaveTextContent('Тест → Навчання → Тест');
    expect(methodology).toHaveTextContent('перевірити → навчити → перевірити. Радимо для B1.');

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
      methodology: 'ttt',
      grammar_focus: 'вищий ступінь прикметників',
    });
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
