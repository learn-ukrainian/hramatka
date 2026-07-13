import { describe, it, expect, beforeEach } from 'vitest';
import {
  statusLabel,
  bakeStatusSubline,
  saveLastBakeRequest,
  loadLastBakeRequest,
  clearLastBakeRequest,
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
});
