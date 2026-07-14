import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ReviewWorkbench, { type LessonResourceView } from './ReviewWorkbench';
import { LangProvider } from './i18n';

const mockResource: LessonResourceView = {
  lesson_id: 'test-lesson-id',
  revision: 1,
  warning_acknowledgements: [],
  lesson: {
    title: 'Українська кухня: вареники',
    level: 'B1',
    duration: 60,
    focus: 'cases',
    anchor: { text: 'Текст про вареники.' },
    accepted: false,
    rejected: [],
    blocks: [
      {
        id: 'block-1',
        phase: 1,
        type: 'true-false',
        mode: 'усно',
        activity: {
          id: 'activity-tf-1',
          type: 'true-false',
          title: 'Перевірмо розуміння',
          level: 'b1',
          payload: { type: 'true-false', instruction: 'Т/Ф', items: [{ statement: 'Це правда.', correct: true }] },
          answer_key: { items: [{ index: 0, correct: true }] },
          provenance: { source: 'gen', generator: 'gemma', gates: [] }
        },
        answer_key: { items: [{ index: 0, correct: true }] },
        mark: 'ok',
        note: null,
        edited: false,
      },
      {
        id: 'block-2',
        phase: 1,
        type: 'cloze',
        mode: 'письмово',
        activity: {
          id: 'activity-cloze-2',
          type: 'cloze',
          title: 'Заповніть пропуски',
          level: 'b1',
          payload: { type: 'cloze', instruction: 'Пропуски', text: 'Замісіть мʼяке тісто й залиште його під [___:1].', blanks: [{ id: 1, answer: 'рушником', options: ['рушником', 'столом'] }] },
          answer_key: { blanks: [{ id: 1, answer: 'рушником' }] },
          provenance: { source: 'gen', generator: 'gemma', gates: [] }
        },
        answer_key: { blanks: [{ id: 1, answer: 'рушником' }] },
        mark: 'ok',
        note: null,
        edited: false,
      },
      {
        id: 'block-3',
        phase: 2,
        type: 'short-writing',
        mode: 'усно',
        activity: {
          id: 'activity-sw-3',
          type: 'short-writing',
          title: 'Коротке письмо',
          level: 'b1',
          payload: { type: 'short-writing', prompt: 'Напишіть.' },
          answer_key: {
            guidance: 'Вільна відповідь.',
            rubric: 'Правильне використання лексики.',
            model_answer: 'Моя родина любить вареники.'
          },
          provenance: { source: 'gen', generator: 'gemma', gates: [] }
        },
        answer_key: 'Підтвердьте ключ разом з учителем.',
        mark: 'ok',
        note: null,
        edited: false,
      },
      {
        id: 'block-4',
        phase: 2,
        type: 'text-questions',
        mode: 'усно',
        activity: {
          id: 'activity-tq-4',
          type: 'text-questions',
          title: 'Питання',
          level: 'b1',
          payload: { type: 'text-questions', instruction: 'Питання', items: ['Питання 1?'] },
          answer_key: { guidance: 'Перевірте.' },
          provenance: { source: 'gen', generator: 'gemma', gates: [] }
        },
        answer_key: 'Підтвердьте ключ разом з учителем.',
        mark: 'ok',
        note: null,
        edited: false,
      },
      {
        id: 'block-5',
        phase: 3,
        type: 'text-questions',
        mode: 'вдома',
        activity: {
          id: 'activity-tq-5',
          type: 'text-questions',
          title: 'Питання',
          level: 'b1',
          payload: { type: 'text-questions', instruction: 'Питання', items: ['Питання 1?'] },
          answer_key: { guidance: 'Перевірте.', model_answers: ['Відповідь 1.'] },
          provenance: { source: 'gen', generator: 'gemma', gates: [] }
        },
        answer_key: { guidance: 'Перевірте.', model_answers: ['Відповідь 1.'] },
        mark: 'ok',
        note: null,
        edited: false,
      }
    ]
  }
};

describe('ReviewWorkbench answer key rendering', () => {
  it('renders correct keys and placeholder configurations', () => {
    render(
      <LangProvider>
        <ReviewWorkbench
          resource={mockResource}
          showAnswers={true}
          loading={false}
          onDurationChange={vi.fn()}
          onMoveBlock={vi.fn()}
          onRemoveBlock={vi.fn()}
          onIncludeReserve={vi.fn()}
          onRestoreRejected={vi.fn()}
          onAckWarning={vi.fn()}
          onSaveActivity={vi.fn()}
          onAcceptLesson={vi.fn()}
          onReturnToDraft={vi.fn()}
          allWarningsAcked={true}
        />
      </LangProvider>
    );

    const keys = screen.getAllByTestId('teacher-answer-key');
    expect(keys).toHaveLength(5);

    // block-1 (true-false): should render real key json
    expect(keys[0].textContent).toContain('items');
    expect(keys[0].textContent).toContain('correct');
    expect(keys[0].textContent).not.toContain('Підтвердьте ключ разом з учителем.');

    // block-2 (cloze): should render real key json
    expect(keys[1].textContent).toContain('blanks');
    expect(keys[1].textContent).toContain('рушником');
    expect(keys[1].textContent).not.toContain('Підтвердьте ключ разом з учителем.');

    // block-3 (short-writing): placeholder string block.answer_key, but rubric + model answer rendered instead
    expect(keys[2].textContent).toContain('Рубрика');
    expect(keys[2].textContent).toContain('Правильне використання лексики.');
    expect(keys[2].textContent).toContain('Модельна відповідь');
    expect(keys[2].textContent).toContain('Моя родина любить вареники.');
    expect(keys[2].textContent).not.toContain('Підтвердьте ключ разом з учителем.');

    // block-4 (text-questions with no key): should render placeholder
    expect(keys[3].textContent).toContain('Підтвердьте ключ разом з учителем.');

    // block-5 (text-questions with key): should render real key
    expect(keys[4].textContent).toContain('model_answers');
    expect(keys[4].textContent).toContain('Відповідь 1.');
    expect(keys[4].textContent).not.toContain('Підтвердьте ключ разом з учителем.');
  });
});
