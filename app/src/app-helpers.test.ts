import { describe, it, expect, beforeEach } from 'vitest';
import {
  statusLabel,
  bakeStatusSubline,
  formatBakeProgressLine,
  formatBakeElapsedFallback,
  formatBakeElapsedClock,
  saveLastBakeRequest,
  loadLastBakeRequest,
  clearLastBakeRequest,
  resolveDefaultDurationFromPref,
  formatLessonForClipboard,
  mergeCatalogLessons,
  loadLocalCatalogEntries,
  type CatalogLessonItem,
  type ClipboardLesson,
} from './app-helpers';
import { translate, type ChromeKey } from './i18n';

const tUk = (key: string, params?: Record<string, string | number>) =>
  translate('uk', key as ChromeKey, params);

const GOLDEN_BLOCK_BASE = {
  id: 'b1',
  phase: 1 as const,
  type: 'true-false',
  mode: 'усно',
  activity: {
    id: 'a1',
    type: 'true-false',
    title: 'Перевірка тверджень',
    level: 'b1',
    payload: {
      type: 'true-false',
      instruction: 'Прочитайте твердження й оберіть правильну відповідь.',
      items: [
        { statement: 'Це правдиве твердження.', correct: true },
      ],
    },
  },
  answer_key: 'правда',
  mark: 'ok' as const,
  note: 'Перевірити форму умовного способу.' ,
  edited: false,
  provenance: {
    source: 'anchor',
    generator: 'hramatka-engine',
    gates: ['schema'],
    external_options: false,
  },
};

function buildLesson(overrides: Partial<ClipboardLesson> = {}): ClipboardLesson {
  return {
    title: 'Урок про квартиру',
    duration: 60,
    focus: 'порівняння прикметників',
    anchor: { text: 'Це текст для читання. Він короткий.' },
    blocks: [{ ...GOLDEN_BLOCK_BASE }],
    ...overrides,
  };
}

describe('app-helpers', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('statusLabel uses «готово» for ready (demo chip wording)', () => {
    expect(statusLabel('ready')).toBe('готово');
    expect(statusLabel('baking')).toBe('готується');
    expect(statusLabel('failed')).toBe('помилка');
  });

  it('bakeStatusSubline shows only failure message when failed (no step suffix)', () => {
    expect(bakeStatusSubline('failed', 'готово', 'Постачальник тимчасово недоступний.')).toBe(
      'Постачальник тимчасово недоступний.',
    );
    expect(bakeStatusSubline('failed', 'готово — застарілий крок', undefined)).toBe(
      'Не вдалося створити урок.',
    );
    expect(bakeStatusSubline('baking', 'завдання складено', undefined)).toBe('завдання складено');
    expect(bakeStatusSubline('failed', 'готовий', 'Збій генерації.')).toBe('Збій генерації.');
  });

  it('formatBakeProgressLine renders phase progress and provider-call count separately', () => {
    expect(
      formatBakeProgressLine({
        phase: 2,
        phases_total: 3,
        step: 'generation',
        calls_done: 4,
        calls_planned: 12,
        updated_at: '2026-07-14T10:00:00Z',
      }, tUk),
    ).toBe('Фаза 2 із 3 — створення завдань… (виклики постачальника: 4)');
    expect(
      formatBakeProgressLine({
        phase: 3,
        phases_total: 3,
        step: 'gates',
        calls_done: 5,
        calls_planned: 4,
        updated_at: '2026-07-14T10:00:00Z',
      }, tUk),
    ).toBe('Фаза 3 із 3 — перевірка… (виклики постачальника: 5)');
    expect(
      formatBakeProgressLine({
        phase: 3,
        phases_total: 3,
        step: 'assembly',
        calls_done: null,
        calls_planned: null,
        updated_at: '2026-07-14T10:00:00Z',
      }, tUk),
    ).toBe('Фаза 3 із 3 — збирання заняття…');
  });

  it('formatBakeElapsedFallback stages UA copy by elapsed time', () => {
    expect(formatBakeElapsedFallback(30_000, tUk)).toContain('Текст отримано');
    expect(formatBakeElapsedFallback(4 * 60_000, tUk)).toContain('Генерація триває');
    expect(formatBakeElapsedFallback(10 * 60_000, tUk)).toContain('Ще працюємо');
    expect(formatBakeElapsedFallback(20 * 60_000, tUk)).toContain('до пів години');
  });

  it('formatBakeElapsedClock shows mm:ss without implying progress percent', () => {
    expect(formatBakeElapsedClock(125_000)).toBe('2:05');
  });

  it('persists last bake request in sessionStorage for failure recovery', () => {
    const payload = {
      text: 'Тестовий текст',
      duration: 60 as const,
      focus: 'граматика',
      lessonId: '00000000-0000-0000-0000-000000000bad',
    };
    saveLastBakeRequest(payload);
    expect(loadLastBakeRequest('00000000-0000-0000-0000-000000000bad')).toEqual(payload);
    expect(loadLastBakeRequest('other-id')).toBeNull();
  });

  it('clearLastBakeRequest removes saved bake request from sessionStorage', () => {
    saveLastBakeRequest({
      text: 'Текст',
      duration: 45,
      focus: '',
      lessonId: 'id-1',
    });
    clearLastBakeRequest();
    expect(loadLastBakeRequest()).toBeNull();
  });

  it('resolveDefaultDurationFromPref preselects from teacher pref (falls back to 60)', () => {
    expect(resolveDefaultDurationFromPref({ default_duration: 45 })).toBe(45);
    expect(resolveDefaultDurationFromPref({ default_duration: 60 })).toBe(60);
    expect(resolveDefaultDurationFromPref({ default_duration: 90 })).toBe(90);
    expect(resolveDefaultDurationFromPref({ default_duration: 30 })).toBe(60);
    expect(resolveDefaultDurationFromPref(null)).toBe(60);
    expect(resolveDefaultDurationFromPref({})).toBe(60);
    expect(resolveDefaultDurationFromPref({ default_duration: '60' })).toBe(60);
  });
});

function catalogFixture(
  id: string,
  overrides: Partial<CatalogLessonItem> = {},
): CatalogLessonItem {
  return {
    id,
    title: null,
    status: 'baking',
    duration: 60,
    focus: null,
    revision: 1,
    accepted: false,
    accepted_at: null,
    accepted_revision: null,
    failure_code: null,
    created_at: '2026-07-14T10:00:00Z',
    updated_at: '2026-07-14T10:00:00Z',
    ...overrides,
  };
}

describe('mergeCatalogLessons', () => {
  it('dedups overlapping local+server entries by lesson id (server row wins)', () => {
    const id = '11111111-1111-1111-1111-111111111111';
    const server = [
      catalogFixture(id, {
        title: 'Server title',
        focus: null,
        updated_at: '2026-07-14T10:05:00Z',
      }),
    ];
    const local = [
      catalogFixture(id, {
        focus: 'local-only focus',
        updated_at: '2026-07-14T10:00:01Z',
      }),
    ];

    const merged = mergeCatalogLessons(server, local);

    expect(merged).toHaveLength(1);
    expect(merged[0].title).toBe('Server title');
    expect(merged[0].focus).toBeNull();
  });

  it('keeps local-only entries not yet on the server', () => {
    const localId = '22222222-2222-2222-2222-222222222222';
    const merged = mergeCatalogLessons([], [catalogFixture(localId)]);

    expect(merged).toHaveLength(1);
    expect(merged[0].id).toBe(localId);
  });

  it('prefers server failed status over stale local baking row after recreate', () => {
    const oldId = '33333333-3333-3333-3333-333333333333';
    const newId = '44444444-4444-4444-4444-444444444444';
    const server = [
      catalogFixture(oldId, { status: 'failed', title: 'Failed lesson' }),
      catalogFixture(newId, { status: 'baking', title: 'Recreated lesson' }),
    ];
    const local = [catalogFixture(oldId, { status: 'baking' })];

    const merged = mergeCatalogLessons(server, local);

    expect(merged.filter((row) => row.id === oldId)).toHaveLength(1);
    expect(merged.find((row) => row.id === oldId)?.status).toBe('failed');
    expect(merged.find((row) => row.id === newId)?.status).toBe('baking');
  });
});

describe('loadLocalCatalogEntries', () => {
  it('derives a baking catalog row from the saved last-bake request', () => {
    saveLastBakeRequest({
      text: 'Текст',
      duration: 45,
      focus: 'граматика',
      lessonId: '55555555-5555-5555-5555-555555555555',
    });

    const rows = loadLocalCatalogEntries();

    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe('55555555-5555-5555-5555-555555555555');
    expect(rows[0].status).toBe('baking');
    expect(rows[0].duration).toBe(45);
    expect(rows[0].focus).toBe('граматика');
  });
});

describe('formatLessonForClipboard', () => {
  it('teacher variant includes title, duration, anchor, phase header, task body and answer key', () => {
    const text = formatLessonForClipboard(buildLesson(), { mode: 'teacher' });
    expect(text).toContain('Урок про квартиру');
    expect(text).toContain('≈ 60 хв');
    expect(text).toContain('ТЕКСТ ДЛЯ ЧИТАННЯ');
    expect(text).toContain('Це текст для читання. Він короткий.');
    expect(text).toContain('Фаза 1');
    expect(text).toContain('Перевірка тверджень');
    expect(text).toContain('Це правдиве твердження.');
    // Teacher-only content:
    expect(text).toContain('Ключ відповіді');
    expect(text).toContain('правда');
  });

  it('student variant NEVER includes answer key, note, provenance, or «Ключ відповідей»', () => {
    const text = formatLessonForClipboard(buildLesson(), { mode: 'student' });
    expect(text).toContain('Урок про квартиру');
    expect(text).toContain('Це правдиве твердження.');
    // Student-safe content must NOT include:
    expect(text).not.toContain('Ключ відповіді');
    expect(text).not.toContain('правда');
    expect(text).not.toContain('Примітка');
    expect(text).not.toContain('Перевірити форму умовного способу');
    expect(text).not.toContain('Походження');
    expect(text).not.toContain('hramatka-engine');
  });

  it('reserve/homework section is rendered separately under a «РЕЗЕРВ» header', () => {
    const lesson = buildLesson({
      blocks: [
        { ...GOLDEN_BLOCK_BASE, id: 'b1', phase: 1, mode: 'усно' },
        {
          ...GOLDEN_BLOCK_BASE,
          id: 'b2',
          phase: 3,
          mode: 'вдома',
          type: 'short-writing',
          activity: {
            id: 'a2',
            type: 'short-writing',
            title: 'Коротке письмо',
            level: 'b1',
            payload: {
              type: 'short-writing',
              prompt: 'Опишіть квартиру вашої мрії (7–8 речень).',
            },
          },
          answer_key: 'Моя квартира мрії світла й тиха.',
          note: null,
        },
      ],
    });
    const text = formatLessonForClipboard(lesson, { mode: 'student' });
    // Homework block goes into a separate section:
    expect(text).toContain('РЕЗЕРВ / ДОМАШНЄ ЗАВДАННЯ');
    expect(text).toContain('Коротке письмо');
    expect(text).toContain('Опишіть квартиру вашої мрії');
    // And is NOT listed under any «Фаза N» header:
    expect(text).not.toMatch(/Фаза 3/);
  });

  it('teacher variant keeps reserve section with answer key', () => {
    const lesson = buildLesson({
      blocks: [
        { ...GOLDEN_BLOCK_BASE, id: 'b1', phase: 1, mode: 'усно' },
        {
          ...GOLDEN_BLOCK_BASE,
          id: 'b2',
          phase: 3,
          mode: 'вдома',
          type: 'short-writing',
          activity: {
            id: 'a2',
            type: 'short-writing',
            title: 'Коротке письмо',
            level: 'b1',
            payload: {
              type: 'short-writing',
              prompt: 'Опишіть квартиру.',
            },
          },
          answer_key: 'Моя квартира мрії світла.',
          note: null,
        },
      ],
    });
    const text = formatLessonForClipboard(lesson, { mode: 'teacher' });
    expect(text).toContain('РЕЗЕРВ / ДОМАШНЄ ЗАВДАННЯ');
    expect(text).toContain('Ключ відповіді: Моя квартира мрії світла.');
  });

  it('empty-anchor edge: omits the «ТЕКСТ ДЛЯ ЧИТАННЯ» section but still emits title + phase', () => {
    const lesson = buildLesson({ anchor: { text: '' } });
    const text = formatLessonForClipboard(lesson, { mode: 'student' });
    expect(text).not.toContain('ТЕКСТ ДЛЯ ЧИТАННЯ');
    expect(text).toContain('Урок про квартиру');
    expect(text).toContain('Фаза 1');
  });

  it('null anchor: same as empty — no crash, header suppressed', () => {
    const lesson = buildLesson({ anchor: null });
    const text = formatLessonForClipboard(lesson, { mode: 'teacher' });
    expect(text).not.toContain('ТЕКСТ ДЛЯ ЧИТАННЯ');
    expect(text).toContain('Урок про квартиру');
  });

  it('handles quiz/multiple-choice options in reading order (A/B/C)', () => {
    const lesson = buildLesson({
      blocks: [
        {
          ...GOLDEN_BLOCK_BASE,
          id: 'b1',
          phase: 1,
          mode: 'усно',
          type: 'quiz',
          activity: {
            id: 'a1',
            type: 'quiz',
            title: 'Тест',
            level: 'b1',
            payload: {
              type: 'quiz',
              instruction: 'Оберіть правильну відповідь.',
              items: [
                {
                  question: 'Яке слово правильне?',
                  options: ['книга', 'книгу'],
                  correct: 0,
                },
              ],
            },
          },
          answer_key: 'A',
        },
      ],
    });
    const text = formatLessonForClipboard(lesson, { mode: 'student' });
    expect(text).toContain('Яке слово правильне?');
    expect(text).toContain('A) книга');
    expect(text).toContain('B) книгу');
    // Student copy never reveals the correct index:
    expect(text).not.toContain('Ключ відповіді: A');
  });

  it('does not duplicate the instruction line when it equals the activity title', () => {
    const lesson = buildLesson({
      blocks: [
        {
          ...GOLDEN_BLOCK_BASE,
          id: 'b1',
          phase: 1,
          mode: 'усно',
          type: 'short-writing',
          activity: {
            id: 'a1',
            type: 'short-writing',
            title: 'Опишіть квартиру.',
            level: 'b1',
            payload: { type: 'short-writing', prompt: 'Опишіть квартиру.' },
          },
          answer_key: '',
        },
      ],
    });
    const text = formatLessonForClipboard(lesson, { mode: 'student' });
    const occurrences = (text.match(/Опишіть квартиру\./g) || []).length;
    expect(occurrences).toBe(1);
  });
});
