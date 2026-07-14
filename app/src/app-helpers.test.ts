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
  type ClipboardLesson,
} from './app-helpers';

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

  it('formatBakeProgressLine renders honest phase/step copy (no percent)', () => {
    expect(
      formatBakeProgressLine({
        phase: 2,
        phases_total: 3,
        step: 'generation',
        calls_done: 4,
        calls_planned: 12,
        updated_at: '2026-07-14T10:00:00Z',
      }),
    ).toBe('Фаза 2 із 3 — створення завдань… (4 з 12)');
    expect(
      formatBakeProgressLine({
        phase: 3,
        phases_total: 3,
        step: 'assembly',
        calls_done: null,
        calls_planned: null,
        updated_at: '2026-07-14T10:00:00Z',
      }),
    ).toBe('Фаза 3 із 3 — збирання заняття…');
  });

  it('formatBakeElapsedFallback stages UA copy by elapsed time', () => {
    expect(formatBakeElapsedFallback(30_000)).toContain('Текст отримано');
    expect(formatBakeElapsedFallback(4 * 60_000)).toContain('Генерація триває');
    expect(formatBakeElapsedFallback(10 * 60_000)).toContain('Ще працюємо');
    expect(formatBakeElapsedFallback(20 * 60_000)).toContain('до пів години');
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
