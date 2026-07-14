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
} from './app-helpers';

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
