/**
 * Teacher/student split for the «Навмисна помилка» affordance (#164).
 *
 * Error-correction blocks print deliberately wrong Ukrainian. Review must mark that
 * intent so teachers stop reading it as an engine defect; the student render must show
 * the instruction and the task and nothing else.
 *
 * The student assertions below are the point of the file: they check the DOM, not CSS,
 * because "hidden by a stylesheet" is not "absent from a printed handout".
 */
import { render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import LessonBlocks, { type LessonBlocksBlock, type LessonViewMode } from './LessonBlocks';
import { LangProvider, saveLang, translate, type Lang } from './i18n';

const BADGE_UK = translate('uk', 'blocks.deliberateError');
const BADGE_EN = translate('en', 'blocks.deliberateError');

const SENTENCE = 'Я живу в Києв.';
const ERROR = 'Києв';
const CORRECTION = 'Києві';
const CORRECTED_SENTENCE = 'Я живу в Києві.';

function ecBlock(answerKey: string | object | null): LessonBlocksBlock {
  return {
    id: 'block-ec-1',
    phase: 2,
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
      answer_key: { items: [CORRECTED_SENTENCE] },
      provenance: { source: 'generated', generator: 'gemma', gates: ['vesum'] },
    },
    answer_key: answerKey,
    mark: 'ok',
    note: null,
    edited: false,
    provenance: { source: 'generated', generator: 'gemma', gates: ['vesum'] },
  };
}

const ENRICHED_KEY = {
  items: [CORRECTED_SENTENCE],
  corrections: [{ sentence: SENTENCE, error: ERROR, correction: CORRECTION }],
};

beforeEach(() => {
  localStorage.clear();
});

function renderBlocks(
  block: LessonBlocksBlock,
  viewMode: LessonViewMode,
  { showAnswers = true, lang = 'uk' as Lang } = {},
) {
  saveLang(lang); // LangProvider seeds its state from storage on mount.
  return render(
    <LangProvider>
      <LessonBlocks
        blocks={[block]}
        viewMode={viewMode}
        showAnswers={showAnswers}
        acknowledgedIds={[]}
        onAck={() => {}}
        loading={false}
      />
    </LangProvider>,
  );
}

describe('review mode — teacher sees the intent (#164)', () => {
  it('badges the block so the wrong Ukrainian is not read as an engine bug', () => {
    renderBlocks(ecBlock(ENRICHED_KEY), 'review');
    expect(screen.getByTestId('deliberate-error-badge')).toHaveTextContent(BADGE_UK);
  });

  it('highlights the erroneous form and shows its correction', () => {
    renderBlocks(ecBlock(ENRICHED_KEY), 'review');

    const mark = screen.getByTestId('deliberate-error-mark');
    expect(mark.tagName).toBe('MARK');
    expect(mark).toHaveTextContent(ERROR);

    const item = within(screen.getByTestId('deliberate-error-list'));
    expect(item.getByText(CORRECTION)).toBeInTheDocument();
  });

  it('still shows the corrected sentence key alongside the highlight', () => {
    renderBlocks(ecBlock(ENRICHED_KEY), 'review');
    expect(screen.getByTestId('teacher-answer-key')).toHaveTextContent(CORRECTED_SENTENCE);
  });

  it('keeps the badge visible with answers hidden — it answers "is this a bug?", not "what is the answer?"', () => {
    renderBlocks(ecBlock(ENRICHED_KEY), 'review', { showAnswers: false });

    expect(screen.getByTestId('deliberate-error-badge')).toBeInTheDocument();
    // The highlight reveals WHERE the error is, so it stays with the rest of the key.
    expect(screen.queryByTestId('deliberate-error-list')).not.toBeInTheDocument();
    expect(screen.queryByTestId('teacher-answer-key')).not.toBeInTheDocument();
  });

  it('translates the badge — it is chrome, while the marked forms stay Ukrainian', () => {
    renderBlocks(ecBlock(ENRICHED_KEY), 'review', { lang: 'en' });

    expect(screen.getByTestId('deliberate-error-badge')).toHaveTextContent(BADGE_EN);
    expect(screen.getByTestId('deliberate-error-mark')).toHaveTextContent(ERROR);
  });
});

describe('student view — the affordance must not leak (#164)', () => {
  it('renders neither the badge nor the highlight markup', () => {
    const { container } = renderBlocks(ecBlock(ENRICHED_KEY), 'run');

    expect(screen.getByTestId('student-block')).toBeInTheDocument();
    expect(screen.queryByTestId('deliberate-error-badge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('deliberate-error-list')).not.toBeInTheDocument();
    expect(screen.queryByTestId('deliberate-error-mark')).not.toBeInTheDocument();

    // Absent from the DOM, not merely painted away: a stylesheet does not survive
    // a copy-paste, and `display:none` would still leak the answer to the page source.
    expect(container.querySelector('mark')).toBeNull();
    expect(container.textContent).not.toContain(BADGE_UK);
    expect(container.textContent).not.toContain(BADGE_EN);
  });

  it('never leaks the correction, even with showAnswers left on', () => {
    // `showAnswers` is teacher toolbar state; the student route must not honour it.
    const { container } = renderBlocks(ecBlock(ENRICHED_KEY), 'run', { showAnswers: true });

    expect(screen.queryByTestId('teacher-answer-key')).not.toBeInTheDocument();
    expect(container.textContent).not.toContain(CORRECTED_SENTENCE);
    expect(container.textContent).not.toContain(BADGE_UK);
  });

  it('keeps the instruction and the task itself', () => {
    renderBlocks(ecBlock(ENRICHED_KEY), 'run');

    const block = within(screen.getByTestId('student-block'));
    expect(block.getByText('Виправте помилку в кожному реченні.')).toBeInTheDocument();
    expect(screen.getByTestId('student-block').textContent).toContain(SENTENCE);
  });

  it('hides the badge in the English chrome too', () => {
    const { container } = renderBlocks(ecBlock(ENRICHED_KEY), 'run', { lang: 'en' });
    expect(container.textContent).not.toContain(BADGE_EN);
  });
});

describe('lessons without intent metadata are unchanged (#164)', () => {
  it('renders no badge for a pre-#164 error-correction key', () => {
    renderBlocks(ecBlock({ items: [CORRECTED_SENTENCE] }), 'review');

    expect(screen.queryByTestId('deliberate-error-badge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('deliberate-error-list')).not.toBeInTheDocument();
    // The plain corrected-sentence key still renders exactly as before.
    expect(screen.getByTestId('teacher-answer-key')).toHaveTextContent(CORRECTED_SENTENCE);
  });

  it('ignores a corrections entry whose error is not in its sentence', () => {
    const bogus = {
      items: [CORRECTED_SENTENCE],
      corrections: [{ sentence: SENTENCE, error: 'Львов', correction: 'Львові' }],
    };
    renderBlocks(ecBlock(bogus), 'review');

    expect(screen.queryByTestId('deliberate-error-badge')).not.toBeInTheDocument();
  });

  it('leaves a non-error-correction block alone', () => {
    const tf: LessonBlocksBlock = {
      ...ecBlock({ items: [{ index: 0, correct: true }] }),
      type: 'true-false',
      activity: {
        id: 'activity-tf-1',
        type: 'true-false',
        title: 'Правда чи неправда',
        level: 'b1',
        payload: {
          type: 'true-false',
          instruction: 'Правда чи неправда?',
          items: [{ statement: 'Це правда.', correct: true }],
        },
        answer_key: { items: [{ index: 0, correct: true }] },
        provenance: { source: 'generated', generator: 'gemma', gates: ['vesum'] },
      },
    };
    renderBlocks(tf, 'review');

    expect(screen.queryByTestId('deliberate-error-badge')).not.toBeInTheDocument();
    expect(screen.getByTestId('teacher-answer-key')).toBeInTheDocument();
  });
});

describe('engine-flagged blocks (#402)', () => {
  const FLAG_REASON = 'Двигун вважає цю вправу неякісною.';

  function flaggedBlock(): LessonBlocksBlock {
    return {
      ...ecBlock({ items: [CORRECTED_SENTENCE] }),
      id: 'block-flagged-1',
      quality: 'engine_flagged',
      flag_reason_uk: FLAG_REASON,
    };
  }

  it('review mode shows the engine-authored reason verbatim on a visually distinct card', () => {
    const { container } = renderBlocks(flaggedBlock(), 'review');

    expect(screen.getByText(FLAG_REASON)).toBeInTheDocument();
    const card = container.querySelector('.block') as HTMLElement;
    expect(card.className).toContain('flagged');

    const { container: okContainer } = renderBlocks(
      { ...flaggedBlock(), quality: 'engine_ok', flag_reason_uk: null },
      'review',
    );
    const okCard = okContainer.querySelector('.block') as HTMLElement;
    // Not just "an extra span exists": the card classes themselves differ.
    expect(card.className).not.toBe(okCard.className);
    expect(okCard.className).not.toContain('flagged');
  });

  it('the student run view never renders a flagged block at all', () => {
    render(
      <LangProvider>
        <LessonBlocks
          blocks={[flaggedBlock(), ecBlock({ items: [CORRECTED_SENTENCE] })]}
          viewMode="run"
          showAnswers={false}
          acknowledgedIds={[]}
          onAck={() => {}}
          loading={false}
        />
      </LangProvider>,
    );

    // Exactly one student block: the ok one. The flagged block is absent from
    // the DOM, not hidden by a stylesheet.
    expect(screen.getAllByTestId('student-block')).toHaveLength(1);
    expect(screen.queryByText(FLAG_REASON)).not.toBeInTheDocument();
    expect(document.querySelector('[data-block-id="block-flagged-1"]')).toBeNull();
  });
});
