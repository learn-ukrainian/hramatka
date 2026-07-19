import { fireEvent, render, screen } from '@testing-library/react';
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

describe('ReviewWorkbench deliberate-error affordance (#208 / #164)', () => {
  it('renders the existing badge for an enriched error-correction block key', () => {
    const SENTENCE = 'Я живу в Києв.';
    const ERROR = 'Києв';
    const CORRECTION = 'Києві';
    const CORRECTED = 'Я живу в Києві.';
    const resource: LessonResourceView = {
      ...mockResource,
      lesson: {
        ...mockResource.lesson,
        blocks: [
          {
            id: 'block-ec-enriched',
            phase: 1,
            type: 'error-correction',
            mode: 'письмово',
            activity: {
              id: 'activity-error-correction-1',
              type: 'error-correction',
              title: 'Виправте помилку',
              level: 'b1',
              payload: {
                type: 'error-correction',
                instruction: 'Виправте помилку в кожному реченні.',
                items: [SENTENCE],
              },
              answer_key: { items: [CORRECTED] },
              provenance: { source: 'generated', generator: 'gemma', gates: ['vesum'] },
            },
            answer_key: {
              items: [CORRECTED],
              corrections: [{ sentence: SENTENCE, error: ERROR, correction: CORRECTION }],
            },
            mark: 'ok',
            note: null,
            edited: false,
          },
        ],
      },
    };

    render(
      <LangProvider>
        <ReviewWorkbench
          resource={resource}
          showAnswers={false}
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
      </LangProvider>,
    );

    // Production workbench wiring: badge before activity content, answers hidden.
    expect(screen.getByTestId('deliberate-error-badge')).toBeTruthy();
    expect(screen.queryByTestId('deliberate-error-list')).toBeNull();
    expect(screen.queryByTestId('teacher-answer-key')).toBeNull();
  });
});

describe('ReviewWorkbench honesty flags', () => {
  it('surfaces external_options blocks with warn chip and margin note', () => {
    const resource: LessonResourceView = {
      ...mockResource,
      lesson: {
        ...mockResource.lesson,
        blocks: [
          {
            id: 'block-cloze-warn',
            phase: 1,
            type: 'cloze',
            mode: 'письмово',
            activity: mockResource.lesson.blocks[1].activity,
            answer_key: mockResource.lesson.blocks[1].answer_key,
            mark: 'warn',
            note: 'Згенеровано з опори; перевірте: є варіанти поза текстом опори.',
            edited: false,
            provenance: {
              source: 'generated',
              generator: 'gemma',
              gates: ['external_options'],
              external_options: true,
            },
          },
        ],
      },
    };

    render(
      <LangProvider>
        <ReviewWorkbench
          resource={resource}
          showAnswers={false}
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
          allWarningsAcked={false}
        />
      </LangProvider>
    );

    expect(screen.getByText(/⚠ погляньте/i)).toBeTruthy();
    expect(screen.getByText(/зовнішні варіанти/i)).toBeTruthy();
  });
});

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

    // block-1 (true-false): human-readable key, not raw JSON
    expect(keys[0].textContent).toContain('Питання 1 → правильна відповідь: Правда');
    expect(keys[0].textContent).not.toContain('"items"');
    expect(keys[0].textContent).not.toContain('{');
    expect(keys[0].textContent).not.toContain('Підтвердьте ключ разом з учителем.');

    // block-2 (cloze): human-readable blank answer
    expect(keys[1].textContent).toContain('Прогалина 1: рушником');
    expect(keys[1].textContent).toContain('рушником');
    expect(keys[1].textContent).not.toContain('"blanks"');
    expect(keys[1].textContent).not.toContain('Підтвердьте ключ разом з учителем.');

    // block-3 (short-writing): placeholder string block.answer_key, but rubric + model answer rendered instead
    expect(keys[2].textContent).toContain('Рубрика');
    expect(keys[2].textContent).toContain('Правильне використання лексики.');
    expect(keys[2].textContent).toContain('Модельна відповідь');
    expect(keys[2].textContent).toContain('Моя родина любить вареники.');
    expect(keys[2].textContent).not.toContain('Підтвердьте ключ разом з учителем.');

    // block-4 (text-questions with no key): should render placeholder string as-is
    expect(keys[3].textContent).toContain('Підтвердьте ключ разом з учителем.');

    // block-5 (text-questions with key): human-readable guidance + model answers
    expect(keys[4].textContent).toContain('Відповідь 1.');
    expect(keys[4].textContent).toMatch(/Вказівка:|Модельн/);
    expect(keys[4].textContent).not.toContain('"model_answers"');
    expect(keys[4].textContent).not.toContain('{');
    expect(keys[4].textContent).not.toContain('Підтвердьте ключ разом з учителем.');
  });
});

const NOTICE_UK =
  'Опора не містить достатньо перевіреного матеріалу для фокусу «умовний спосіб». ' +
  'Вправи спираються лише на текст-опору; додайте приклади або змініть фокус.';

const UNSUPPORTED_FOCUS_STATUS = {
  requested: 'умовний спосіб',
  supported: false,
  notice_uk: NOTICE_UK,
};

function renderWorkbench(
  resource: LessonResourceView,
  overrides: Partial<React.ComponentProps<typeof ReviewWorkbench>> = {},
) {
  const props = {
    resource,
    showAnswers: false,
    loading: false,
    onDurationChange: vi.fn(),
    onMoveBlock: vi.fn(),
    onRemoveBlock: vi.fn(),
    onIncludeReserve: vi.fn(),
    onRestoreRejected: vi.fn(),
    onAckWarning: vi.fn(),
    onSaveActivity: vi.fn(),
    onAcceptLesson: vi.fn(),
    onReturnToDraft: vi.fn(),
    allWarningsAcked: true,
    ...overrides,
  };
  render(
    <LangProvider>
      <ReviewWorkbench {...props} />
    </LangProvider>,
  );
  return props;
}

function withFocusStatus(focusStatus?: LessonResourceView['lesson']['focus_status']) {
  return {
    ...mockResource,
    lesson: { ...mockResource.lesson, focus_status: focusStatus },
  };
}

describe('ReviewWorkbench focus notice (#191)', () => {
  it('renders an unsupported focus as its own banner, never as a rejected draft', () => {
    renderWorkbench(withFocusStatus(UNSUPPORTED_FOCUS_STATUS));

    const banner = screen.getByTestId('focus-notice');
    expect(banner).toBeTruthy();
    // Chrome label is translated; the notice body is engine UA content, verbatim.
    expect(banner.textContent).toContain('Фокус не підкріплено опорою');
    expect(banner.textContent).toContain('умовний спосіб');
    expect(screen.getByTestId('focus-notice-body').textContent).toBe(NOTICE_UK);
    // Nothing was rejected, so the tray must stay out of it.
    expect(screen.queryByTestId('rejected-tray')).toBeNull();
  });

  it('acks the notice under the reserved lesson-level id', () => {
    const props = renderWorkbench(withFocusStatus(UNSUPPORTED_FOCUS_STATUS), {
      allWarningsAcked: false,
    });

    fireEvent.click(screen.getByText('зрозуміло, підтверджую'));

    expect(props.onAckWarning).toHaveBeenCalledWith('focus-status');
  });

  it('keeps accept disabled while the notice is outstanding', () => {
    renderWorkbench(withFocusStatus(UNSUPPORTED_FOCUS_STATUS), { allWarningsAcked: false });

    const accept = document.querySelector('[data-action="accept-lesson"]') as HTMLButtonElement;
    expect(accept.disabled).toBe(true);
    // The caveat is stated and still awaiting its explicit acknowledgement.
    expect(screen.getByTestId('focus-notice')).toBeTruthy();
    expect(screen.queryByTestId('focus-notice-acked')).toBeNull();
    expect(screen.getByText('зрозуміло, підтверджую')).toBeTruthy();
  });

  it('shows the acked chip and no ack button once acknowledged', () => {
    const resource = withFocusStatus(UNSUPPORTED_FOCUS_STATUS);
    renderWorkbench({ ...resource, warning_acknowledgements: ['focus-status'] });

    expect(screen.getByTestId('focus-notice-acked')).toBeTruthy();
    expect(screen.queryByText('зрозуміло, підтверджую')).toBeNull();
    const accept = document.querySelector('[data-action="accept-lesson"]') as HTMLButtonElement;
    expect(accept.disabled).toBe(false);
  });

  it('carries the notice onto the printed sheet, which drops the banner chrome', () => {
    renderWorkbench(withFocusStatus(UNSUPPORTED_FOCUS_STATUS));

    const printed = screen.getByTestId('focus-notice-print');
    expect(printed.textContent).toContain(NOTICE_UK);
    // The banner is chrome (.noprint); the sheet line is what survives printing.
    expect(screen.getByTestId('focus-notice').className).toContain('noprint');
    expect(printed.className).toContain('focus-notice-print');
    expect(printed.className).not.toContain('noprint');
  });

  it('shows no banner for a supported focus', () => {
    renderWorkbench(withFocusStatus({ requested: 'читання', supported: true, notice_uk: null }));

    expect(screen.queryByTestId('focus-notice')).toBeNull();
    expect(screen.queryByTestId('focus-notice-print')).toBeNull();
  });

  it('shows no banner when no focus was requested', () => {
    renderWorkbench(withFocusStatus(undefined));

    expect(screen.queryByTestId('focus-notice')).toBeNull();
    expect(screen.queryByTestId('focus-notice-print')).toBeNull();
  });
});
