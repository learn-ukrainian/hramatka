import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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

  it('renders My Lessons as a separate empty page with no delete affordance', async () => {
    installFetch();
    renderApp();

    fireEvent.click(await screen.findByRole('button', { name: 'Мої заняття' }));
    expect(await screen.findByTestId('lessons-empty-state')).toHaveTextContent('Створіть перше заняття');
    expect(screen.queryByTestId('catalog-delete-btn')).not.toBeInTheDocument();
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
});
