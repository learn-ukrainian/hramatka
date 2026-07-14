import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ActivityEditor from './ActivityEditor';
import { LangProvider } from './i18n';

function baseActivity(type: 'quiz' | 'fill-in' | 'error-correction' | 'text-questions') {
  const common = {
    id: `editor-${type}`,
    type,
    title: 'Редагування',
    level: 'b1',
    provenance: { source: 'test', generator: 'vitest', gates: ['schema'] },
  };
  if (type === 'quiz') return {
    ...common,
    payload: { type, instruction: 'Оберіть.', items: [
      { question: 'Перше?', options: ['а', 'б'], correct: 0 },
      { question: 'Друге?', options: ['в', 'г'], correct: 1 },
    ] },
    answer_key: { items: [{ index: 0, correct: 0 }, { index: 1, correct: 1 }] },
  };
  if (type === 'fill-in') return {
    ...common,
    payload: { type, instruction: 'Вставте.', items: [
      { sentence: 'Перше ____', answer: 'перша' },
      { sentence: 'Друге ____', answer: 'друга' },
    ] },
    answer_key: { items: ['перша', 'друга'] },
  };
  if (type === 'error-correction') return {
    ...common,
    payload: { type, instruction: 'Виправте.', items: ['Перша помилка.', 'Друга помилка.'] },
    answer_key: { items: ['Перше виправлення.', 'Друге виправлення.'] },
  };
  return {
    ...common,
    payload: { type, instruction: 'Відповідайте.', items: ['Перше питання?', 'Друге питання?'] },
    answer_key: { guidance: 'За текстом.', model_answers: ['Перша відповідь.', 'Друга відповідь.'] },
  };
}

describe('ActivityEditor collection editing', () => {
  it.each([
    ['quiz', 5, 'Оновлене друге питання?'],
    ['fill-in', 4, 'Оновлене друге речення ____'],
    ['error-correction', 4, 'Оновлена друга помилка.'],
    ['text-questions', 4, 'Оновлене друге питання?'],
  ] as const)('preserves every %s item when item two changes', (type, inputIndex, replacement) => {
    const onSave = vi.fn();
    render(
      <LangProvider>
        <ActivityEditor activity={baseActivity(type)} onSave={onSave} onCancel={vi.fn()} />
      </LangProvider>
    );

    fireEvent.change(screen.getAllByRole('textbox')[inputIndex], { target: { value: replacement } });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти зміни' }));

    expect(onSave).toHaveBeenCalledOnce();
    const saved = onSave.mock.calls[0][0] as Record<string, any>;
    expect(saved.payload.items).toHaveLength(2);
    expect(saved.answer_key.items ?? saved.answer_key.model_answers).toHaveLength(2);
    expect(JSON.stringify(saved.payload.items)).toContain(replacement);
  });
});
