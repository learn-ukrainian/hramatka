/**
 * Conduct-mode deliberate-error badge (#208 / #164 follow-up).
 *
 * Teachers conducting an error-correction task see the same deliberately wrong
 * Ukrainian that review mode marks with «Навмисна помилка». The badge lives only
 * in the teacher panel; student preview reuses the shared surface and must not
 * receive intent, correction, or key markup in the DOM.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import Conductor, { type ConductorLessonDoc } from './Conductor';
import { LangProvider, saveLang, translate } from './i18n';

const BADGE_UK = translate('uk', 'blocks.deliberateError');
const BADGE_EN = translate('en', 'blocks.deliberateError');

const SENTENCE = 'Я живу в Києв.';
const ERROR = 'Києв';
const CORRECTION = 'Києві';
const CORRECTED_SENTENCE = 'Я живу в Києві.';

const ENRICHED_KEY = {
  items: [CORRECTED_SENTENCE],
  corrections: [{ sentence: SENTENCE, error: ERROR, correction: CORRECTION }],
};

function ecActivity() {
  return {
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
  };
}

function lessonDoc(
  answerKey: string | object | null,
): ConductorLessonDoc {
  return {
    id: 'lesson-conduct-ec',
    title: 'Урок: навмисна помилка',
    duration: 45,
    blocks: [
      {
        id: 'block-ec-1',
        phase: 1,
        type: 'error-correction',
        mode: 'письмово',
        activity: ecActivity(),
        answer_key: answerKey,
        mark: 'ok',
        note: null,
      },
    ],
  };
}

beforeEach(() => {
  localStorage.clear();
  saveLang('uk');
});

function renderConductor(doc: ConductorLessonDoc) {
  return render(
    <LangProvider>
      <Conductor lessonDoc={doc} onExit={() => {}} />
    </LangProvider>,
  );
}

describe('Conductor deliberate-error badge (#208)', () => {
  it('shows the badge in the teacher panel before answer reveal', () => {
    renderConductor(lessonDoc(ENRICHED_KEY));

    const badge = screen.getByTestId('deliberate-error-badge');
    expect(badge).toHaveTextContent(BADGE_UK);
    // Badge answers "is this a bug?" — visible even with answers still hidden.
    expect(screen.queryByTestId('cond-answer-key')).not.toBeInTheDocument();
    // Teacher panel is the only host; shared surface stays free of the badge.
    const panel = document.querySelector('.cond-panel');
    expect(panel).toContainElement(badge);
  });

  it('omits the badge from student preview DOM entirely', () => {
    const { container } = renderConductor(lessonDoc(ENRICHED_KEY));

    fireEvent.click(screen.getByTestId('enter-student-preview-btn'));

    expect(screen.getByTestId('conductor-student-preview')).toBeInTheDocument();
    expect(screen.queryByTestId('deliberate-error-badge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('deliberate-error-list')).not.toBeInTheDocument();
    expect(screen.queryByTestId('deliberate-error-mark')).not.toBeInTheDocument();
    expect(screen.queryByTestId('cond-answer-key')).not.toBeInTheDocument();
    // Absent from the DOM, not merely CSS-hidden.
    expect(container.textContent).not.toContain(BADGE_UK);
    expect(container.textContent).not.toContain(BADGE_EN);
    expect(container.textContent).not.toContain(CORRECTED_SENTENCE);
    expect(container.textContent).not.toContain(CORRECTION);
  });

  it('renders no badge for a legacy error-correction key without intent', () => {
    renderConductor(lessonDoc({ items: [CORRECTED_SENTENCE] }));

    expect(screen.queryByTestId('deliberate-error-badge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('deliberate-error-list')).not.toBeInTheDocument();
  });
});
