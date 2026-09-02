import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
          onMoveBlock={vi.fn()}
          onRemoveBlock={vi.fn()}
          onIncludeReserve={vi.fn()}
          onRestoreRejected={vi.fn()}
          onAckWarning={vi.fn()}
          onSaveActivity={vi.fn()}
          onActivityFeedback={vi.fn()}
          onRegenerateActivity={vi.fn()}
          onRetryRegeneration={vi.fn()}
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
          onMoveBlock={vi.fn()}
          onRemoveBlock={vi.fn()}
          onIncludeReserve={vi.fn()}
          onRestoreRejected={vi.fn()}
          onAckWarning={vi.fn()}
          onSaveActivity={vi.fn()}
          onActivityFeedback={vi.fn()}
          onRegenerateActivity={vi.fn()}
          onRetryRegeneration={vi.fn()}
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
          onMoveBlock={vi.fn()}
          onRemoveBlock={vi.fn()}
          onIncludeReserve={vi.fn()}
          onRestoreRejected={vi.fn()}
          onAckWarning={vi.fn()}
          onSaveActivity={vi.fn()}
          onActivityFeedback={vi.fn()}
          onRegenerateActivity={vi.fn()}
          onRetryRegeneration={vi.fn()}
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
    onMoveBlock: vi.fn(),
    onRemoveBlock: vi.fn(),
    onIncludeReserve: vi.fn(),
    onRestoreRejected: vi.fn(),
    onAckWarning: vi.fn(),
    onSaveActivity: vi.fn(),
    onActivityFeedback: vi.fn(),
    onRegenerateActivity: vi.fn(),
    onRetryRegeneration: vi.fn(),
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

describe('engine-flagged blocks — badge, ack flow, and teacher feedback (#402)', () => {
  const FLAG_REASON = 'Двигун вважає цю вправу неякісною.';

  function flaggedResource(
    feedback?: LessonResourceView['activity_feedback'],
  ): LessonResourceView {
    const blocks = mockResource.lesson.blocks.map((block, index) =>
      index === 0
        ? {
            ...block,
            quality: 'engine_flagged' as const,
            flag_reason_uk: FLAG_REASON,
            flagged_content_hash: 'a'.repeat(64),
            engine_reason_class: 'deterministic_gate: fixture',
          }
        : block,
    );
    return {
      ...mockResource,
      activity_feedback: feedback,
      lesson: { ...mockResource.lesson, blocks },
    };
  }

  it('renders the engine-authored reason verbatim, a distinct card, and the ack button', () => {
    renderWorkbench(flaggedResource());

    expect(screen.getByTestId('flagged-badge')).toHaveTextContent(FLAG_REASON);
    const card = document.querySelector('[data-block-id="block-1"]') as HTMLElement;
    expect(card.className).toContain('flagged');
    const okCard = document.querySelector('[data-block-id="block-2"]') as HTMLElement;
    expect(okCard.className).not.toContain('flagged');
    // The flagged block joins the ordinary acknowledge-then-accept flow.
    expect(card.querySelector('[data-action="b-accept"]')).not.toBeNull();
  });

  it('submits «вправа погана» with the optional comment', () => {
    const props = renderWorkbench(flaggedResource());

    fireEvent.change(screen.getByTestId('feedback-comment'), {
      target: { value: 'Завдання не пасує до тексту.' },
    });
    fireEvent.click(screen.getByText('Вправа погана'));

    expect(props.onActivityFeedback).toHaveBeenCalledWith(
      'block-1',
      'bad',
      'Завдання не пасує до тексту.',
    );
  });

  it('submits «вправа добра» with a null comment when none is typed', () => {
    const props = renderWorkbench(flaggedResource());

    fireEvent.click(screen.getByText('Вправа добра'));

    expect(props.onActivityFeedback).toHaveBeenCalledWith('block-1', 'good', null);
  });

  it('shows the saved applicable verdict and reopens the form on change', () => {
    renderWorkbench(
      flaggedResource({
        'block-1': { verdict: 'bad', comment: null, updated_at: '2026-08-09T00:00:00Z' },
      }),
    );

    expect(screen.getByTestId('feedback-saved')).toBeInTheDocument();
    expect(screen.queryByTestId('feedback-form')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Змінити оцінку'));

    expect(screen.getByTestId('feedback-form')).toBeInTheDocument();
  });

  it('never renders feedback chrome on unflagged blocks', () => {
    renderWorkbench(mockResource);

    expect(screen.queryByTestId('feedback-form')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flagged-badge')).not.toBeInTheDocument();
  });
});

describe('one-block regeneration (#418)', () => {
  const generatedResource = (): LessonResourceView => ({
    ...mockResource,
    lesson: {
      ...mockResource.lesson,
      blocks: [
        {
          ...mockResource.lesson.blocks[0],
          provenance: { source: 'generated', generator: 'gemini', gates: ['v3'] },
        },
      ],
    },
  });

  const regeneration = (status: 'queued' | 'running' | 'succeeded' | 'failed') => ({
    id: '11111111-1111-4111-8111-111111111111',
    lesson_id: mockResource.lesson_id,
    block_id: 'block-1',
    base_revision: 1,
    status,
    attempt: 1,
    failure_code: status === 'failed' ? 'generation_failed' : null,
    failure_message: status === 'failed' ? 'Попередню вправу збережено.' : null,
    prompt_version: 'HramatkaBlockRegeneration.v1',
    prompt_sha256: 'a'.repeat(64),
    old_block_hash: 'b'.repeat(64),
    new_block_hash: status === 'succeeded' ? 'c'.repeat(64) : null,
    applied_revision: status === 'succeeded' ? 2 : null,
    created_at: '2026-08-13T00:00:00Z',
    updated_at: '2026-08-13T00:00:00Z',
    started_at: status === 'queued' ? null : '2026-08-13T00:00:01Z',
    completed_at: status === 'succeeded' || status === 'failed'
      ? '2026-08-13T00:00:02Z'
      : null,
  });

  it('submits optional teacher guidance for exactly the selected generated block', () => {
    const props = renderWorkbench(generatedResource());

    fireEvent.click(screen.getByText('Створити інший варіант'));
    fireEvent.change(screen.getByPlaceholderText(/менше очевидних підказок/i), {
      target: { value: '  Природніші формулювання.  ' },
    });
    fireEvent.click(screen.getByText('Створити новий варіант'));

    expect(props.onRegenerateActivity).toHaveBeenCalledWith(
      'block-1',
      'Природніші формулювання.',
    );
  });

  it('keeps the old activity visible during work and exposes a durable failed retry', () => {
    const active = generatedResource();
    renderWorkbench({ ...active, activity_regenerations: [regeneration('running')] }, {
      loading: true,
    });

    expect(screen.getByTestId('regeneration-active')).toHaveTextContent(
      'Ця вправа лишається в уроці',
    );
    expect(screen.getByText('Це правда.')).toBeInTheDocument();

    const failed = generatedResource();
    const retry = vi.fn();
    renderWorkbench(
      { ...failed, activity_regenerations: [regeneration('failed')] },
      { onRetryRegeneration: retry },
    );
    fireEvent.click(screen.getByText('Спробувати ще раз'));

    expect(retry).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111');
  });

  it('keeps unrelated blocks and lesson controls usable while the selected block regenerates', () => {
    const generated = generatedResource();
    const resource = {
      ...generated,
      activity_regenerations: [regeneration('running')],
      lesson: {
        ...generated.lesson,
        blocks: [generated.lesson.blocks[0], mockResource.lesson.blocks[1]],
      },
    };
    renderWorkbench(resource);

    const selected = document.querySelector('[data-block-id="block-1"]') as HTMLElement;
    const unrelated = document.querySelector('[data-block-id="block-2"]') as HTMLElement;
    expect((selected.querySelector('[data-action="b-edit"]') as HTMLButtonElement).disabled).toBe(true);
    expect((selected.querySelector('[data-action="b-del"]') as HTMLButtonElement).disabled).toBe(true);
    expect((unrelated.querySelector('[data-action="b-edit"]') as HTMLButtonElement).disabled).toBe(false);
    expect((unrelated.querySelector('[data-action="b-del"]') as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByTestId('review-duration-readonly')).toHaveTextContent('60 хв');
    expect(document.querySelector('[data-action="review-duration"]')).toBeNull();
    expect((document.querySelector('[data-action="accept-lesson"]') as HTMLButtonElement).disabled).toBe(false);
  });
});

/**
 * «Друк для учня» from teacher review (#587).
 *
 * The leak boundary is DOM ABSENCE, not visibility: a stylesheet that paints the
 * teacher material away still loses to a copy-paste of the print DOM and to any
 * print path that drops CSS. Every assertion below therefore queries the document,
 * never computed styles.
 */
describe('ReviewWorkbench student print (#587)', () => {
  const SENTENCE = 'Я живу в Києв.';
  const ERROR = 'Києв';
  const CORRECTION = 'Києві';
  const CORRECTED = 'Я живу в Києві.';
  const FLAG_REASON = 'Двигун вважає цю вправу неякісною.';
  const REJECTED_REASON = 'Не пройшло ґейт.';
  const NOTE = 'Уточніть відмінок разом з класом.';

  function printResource(): LessonResourceView {
    const [tf, cloze, shortWriting, questions, modelled] = mockResource.lesson.blocks;
    return {
      ...mockResource,
      lesson: {
        ...mockResource.lesson,
        focus_status: UNSUPPORTED_FOCUS_STATUS,
        rejected: [{ type: 'quiz', activity: null, reason: REJECTED_REASON }],
        blocks: [
          { ...tf, note: NOTE },
          {
            ...cloze,
            quality: 'engine_flagged' as const,
            flag_reason_uk: FLAG_REASON,
          },
          shortWriting,
          {
            ...shortWriting,
            id: 'block-ec',
            type: 'error-correction' as const,
            activity: {
              id: 'activity-ec',
              type: 'error-correction',
              title: 'Виправте помилку',
              level: 'b1',
              payload: { type: 'error-correction', instruction: 'Виправте.', items: [SENTENCE] },
              answer_key: { items: [CORRECTED] },
              provenance: { source: 'generated', generator: 'gemma', gates: ['vesum'] },
            },
            answer_key: {
              items: [CORRECTED],
              corrections: [{ sentence: SENTENCE, error: ERROR, correction: CORRECTION }],
            },
          },
          questions,
          modelled,
        ],
      },
    };
  }

  const MODEL_ANSWER = 'Моя родина любить вареники.';
  const RUBRIC = 'Правильне використання лексики.';
  const MODEL_ANSWERS_ITEM = 'Відповідь 1.';

  it('keeps every teacher affordance when the teacher is the one printing', () => {
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: false });

    expect(screen.getAllByTestId('teacher-answer-key').length).toBeGreaterThan(0);
    expect(screen.getByTestId('deliberate-error-badge')).toBeTruthy();
    expect(screen.getByTestId('deliberate-error-list')).toBeTruthy();
    expect(screen.getByTestId('rejected-tray')).toBeTruthy();
    expect(screen.getByTestId('focus-notice-print')).toBeTruthy();
    expect(screen.getByTestId('flagged-badge')).toHaveTextContent(FLAG_REASON);
    expect(document.querySelector('[data-block-id="block-2"]')).not.toBeNull();
    expect(document.body.textContent).toContain(NOTE);
    expect(document.body.textContent).toContain(MODEL_ANSWER);
    expect(document.body.textContent).toContain(RUBRIC);
  });

  it('leaves no answer key, model answer or rubric in the student print DOM', () => {
    // showAnswers stays true: the teacher was reading keys on screen when they
    // reached for «Друк для учня», which is exactly when the leak used to happen.
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: true });

    expect(screen.queryAllByTestId('teacher-answer-key')).toHaveLength(0);
    expect(document.querySelectorAll('.teacher-key')).toHaveLength(0);
    expect(document.body.textContent).not.toContain('Ключ відповіді');
    // The frozen kit prints these into its own teacher-guidance aside, so the
    // activity handed to it must no longer carry them.
    expect(document.querySelectorAll('[data-activity-model-answers]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-activity-rubric]')).toHaveLength(0);
    expect(document.body.textContent).not.toContain(MODEL_ANSWER);
    expect(document.body.textContent).not.toContain(RUBRIC);
    expect(document.body.textContent).not.toContain(MODEL_ANSWERS_ITEM);
  });

  it('leaves no deliberate-error badge, list or correction in the student print DOM', () => {
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: true });

    expect(screen.queryByTestId('deliberate-error-badge')).toBeNull();
    expect(screen.queryByTestId('deliberate-error-list')).toBeNull();
    expect(screen.queryByTestId('deliberate-error-mark')).toBeNull();
    // The wrong sentence is the exercise and stays; the correction is the answer.
    expect(document.body.textContent).toContain(SENTENCE);
    expect(document.body.textContent).not.toContain(CORRECTED);
  });

  it('drops engine-flagged blocks and the rejected tray, matching the student run view', () => {
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: true });

    expect(document.querySelector('[data-block-id="block-2"]')).toBeNull();
    expect(screen.queryByTestId('flagged-badge')).toBeNull();
    expect(document.body.textContent).not.toContain(FLAG_REASON);
    expect(screen.queryByTestId('rejected-tray')).toBeNull();
    expect(document.body.textContent).not.toContain(REJECTED_REASON);
  });

  it('leaves no teacher note, provenance line or review margin in the student print DOM', () => {
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: true });

    expect(document.querySelectorAll('.dmargin')).toHaveLength(0);
    expect(document.body.textContent).not.toContain(NOTE);
    expect(document.body.textContent).not.toContain('Джерело');
    expect(document.body.textContent).not.toContain('генератор');
    expect(document.body.textContent).not.toContain('gemma');
  });

  it('omits the printed focus notice, which is addressed to the teacher', () => {
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: true });

    expect(screen.queryByTestId('focus-notice-print')).toBeNull();
    // The screen banner repeats the same caveat, so it goes with it — `.noprint`
    // keeps it off paper but not out of a copy-paste of the print DOM.
    expect(screen.queryByTestId('focus-notice')).toBeNull();
    expect(document.body.textContent).not.toContain(NOTICE_UK);
  });

  it('still prints the lesson the student has to work through', () => {
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: true });

    expect(document.body.textContent).toContain('Українська кухня: вареники');
    expect(document.body.textContent).toContain('Текст про вареники.');
    expect(document.body.textContent).toContain('Це правда.');
    expect(screen.getByTestId('honesty-footer')).toBeTruthy();
    // `guidance` is student-safe by the boundary the run view already drew.
    expect(document.body.textContent).toContain('Вільна відповідь.');
  });

  it('omits per-block type/mode chips on the student print, matching the student run view (#590)', () => {
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: true });

    expect(document.querySelectorAll('.type')).toHaveLength(0);
    expect(document.querySelectorAll('.mode-chip')).toHaveLength(0);
    expect(document.body.textContent).not.toContain('Правда чи ні');
    expect(document.body.textContent).not.toContain('усно');
    expect(document.body.textContent).not.toContain('письмово');
  });

  it('keeps type/mode chips when the teacher prints or reviews on screen (#590)', () => {
    renderWorkbench(printResource(), { showAnswers: true, studentPrint: false });

    expect(document.querySelectorAll('.type').length).toBeGreaterThan(0);
    expect(document.querySelectorAll('.mode-chip').length).toBeGreaterThan(0);
    expect(document.body.textContent).toContain('Правда чи ні');
    expect(document.body.textContent).toContain('усно');
  });

  it('omits the reserve tray heading on the student print while still printing reserve blocks (#590)', () => {
    // Duration 45 budgets phase 1 at 2 blocks; a third phase-1 block is reserve.
    const base = printResource();
    const overflow = {
      ...base.lesson.blocks[0],
      id: 'block-reserve-overflow',
      phase: 1 as const,
      mode: 'вдома',
      activity: {
        ...base.lesson.blocks[0].activity,
        id: 'activity-reserve-overflow',
        title: 'Запасна вправа',
        payload: {
          type: 'true-false',
          instruction: 'Т/Ф запас',
          items: [{ statement: 'Запасний блок для друку.', correct: true }],
        },
      },
    };
    const resource: LessonResourceView = {
      ...base,
      lesson: {
        ...base.lesson,
        duration: 45,
        blocks: [...base.lesson.blocks, overflow],
      },
    };

    renderWorkbench(resource, { showAnswers: true, studentPrint: false });
    expect(screen.getByTestId('reserve-tray')).toBeTruthy();
    expect(document.querySelector('.reserve-head')).not.toBeNull();
    expect(document.body.textContent).toContain('У запасі');
    expect(document.body.textContent).toContain('Запасний блок для друку.');
    cleanup();

    renderWorkbench(resource, { showAnswers: true, studentPrint: true });
    expect(screen.getByTestId('reserve-tray')).toBeTruthy();
    expect(document.querySelector('.reserve-head')).toBeNull();
    expect(document.body.textContent).not.toContain('У запасі');
    expect(document.body.textContent).not.toContain('не входить у');
    expect(document.body.textContent).toContain('Запасний блок для друку.');
    expect(document.querySelector('[data-block-id="block-reserve-overflow"]')).not.toBeNull();
  });

  it('renders the saved activity instead of an open editor, keeping its raw key fields off the sheet', () => {
    const resource = printResource();
    const props = {
      resource,
      showAnswers: true,
      loading: false,
      onMoveBlock: vi.fn(),
      onRemoveBlock: vi.fn(),
      onIncludeReserve: vi.fn(),
      onRestoreRejected: vi.fn(),
      onAckWarning: vi.fn(),
      onSaveActivity: vi.fn(),
      onActivityFeedback: vi.fn(),
      onRegenerateActivity: vi.fn(),
      onRetryRegeneration: vi.fn(),
      onAcceptLesson: vi.fn(),
      onReturnToDraft: vi.fn(),
      allWarningsAcked: true,
    };
    const { rerender } = render(
      <LangProvider><ReviewWorkbench {...props} studentPrint={false} /></LangProvider>,
    );
    fireEvent.click(document.querySelector('[data-action="b-edit"]') as HTMLElement);
    expect(document.querySelector('.activity-editor')).not.toBeNull();

    // Same instance, print variant on: the editor must not ride onto the sheet.
    rerender(<LangProvider><ReviewWorkbench {...props} studentPrint /></LangProvider>);
    expect(document.querySelector('.activity-editor')).toBeNull();

    // Clearing the variant brings the teacher straight back to editing.
    rerender(<LangProvider><ReviewWorkbench {...props} studentPrint={false} /></LangProvider>);
    expect(document.querySelector('.activity-editor')).not.toBeNull();
  });
});
