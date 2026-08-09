import { useMemo, useState } from 'react';
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';
import { useT } from './i18n';

type Activity = {
  title?: string;
  type?: string;
  payload?: Record<string, any>;
  answer_key?: Record<string, any>;
};

type Correction = {
  sentence: string;
  error: string;
  correction: string;
};

function normalize(value: string | undefined) {
  return (value ?? '').normalize('NFC').replace(/[’‘]/gu, "'").trim().toLocaleLowerCase('uk-UA');
}

function normalizeSentence(value: string | undefined) {
  return normalize(value).replace(/\s+/gu, ' ');
}

function Heading({ activity }: { activity: Activity }) {
  return <h2 className="student-activity-title">{activity.title ?? activity.type}</h2>;
}

function CheckButton({ disabled, onClick }: { disabled: boolean; onClick: () => void }) {
  const { t } = useT();
  return <button type="button" className="student-check" disabled={disabled} onClick={onClick}>{t('studentActivity.check')}</button>;
}

function RetryButton({ onClick }: { onClick: () => void }) {
  const { t } = useT();
  return <button type="button" className="student-retry" onClick={onClick}>{t('studentActivity.retry')}</button>;
}

function TrueFalse({ activity }: { activity: Activity }) {
  const { t } = useT();
  const items = activity.payload?.items ?? [];
  const [answers, setAnswers] = useState<Record<number, boolean>>({});
  const [checked, setChecked] = useState(false);
  const reset = () => { setAnswers({}); setChecked(false); };

  return <section className="student-activity" data-student-activity="true-false">
    <Heading activity={activity} />
    {activity.payload?.instruction && <p className="student-instruction">{activity.payload.instruction}</p>}
    {items.map((item: any, index: number) => {
      const selected = answers[index];
      const isCorrect = selected === item.correct;
      return <div className="student-question" key={index}>
        <p>{item.statement}</p>
        <div className="student-choice-row">
          {[true, false].map((value) => {
            const isSelected = selected === value;
            const state = checked && isSelected ? (value === item.correct ? ' correct' : ' wrong') : '';
            return <button
              type="button"
              key={String(value)}
              disabled={checked}
              className={`student-choice${isSelected ? ' selected' : ''}${state}`}
              aria-pressed={isSelected}
              onClick={() => setAnswers((previous) => ({ ...previous, [index]: value }))}
            >{value ? t('studentActivity.true') : t('studentActivity.false')}</button>;
          })}
        </div>
        {checked && <div
          className={`student-feedback ${isCorrect ? 'correct' : 'wrong'}`}
          data-activity="tf-row-feedback"
          data-correct={isCorrect ? 'true' : 'false'}
        >
          {isCorrect ? t('studentActivity.correct') : t('studentActivity.incorrect')}
        </div>}
      </div>;
    })}
    {checked ? <RetryButton onClick={reset} /> : <CheckButton disabled={Object.keys(answers).length !== items.length} onClick={() => setChecked(true)} />}
  </section>;
}

function Quiz({ activity }: { activity: Activity }) {
  const items = activity.payload?.items ?? [];
  const [answers, setAnswers] = useState<Record<number, number>>({});
  const [checked, setChecked] = useState(false);
  const reset = () => { setAnswers({}); setChecked(false); };

  return <section className="student-activity" data-student-activity="quiz">
    <Heading activity={activity} />
    {activity.payload?.instruction && <p className="student-instruction">{activity.payload.instruction}</p>}
    {items.map((item: any, index: number) => <div className="student-question" key={index}>
      <p>{item.question}</p>
      <div className="student-choice-stack">
        {(item.options ?? []).map((option: string, optionIndex: number) => {
          const selected = answers[index] === optionIndex;
          const state = checked && selected ? (optionIndex === item.correct ? ' correct' : ' wrong') : '';
          return <button
            type="button"
            key={optionIndex}
            disabled={checked}
            className={`student-choice${selected ? ' selected' : ''}${state}`}
            aria-pressed={selected}
            onClick={() => setAnswers((previous) => ({ ...previous, [index]: optionIndex }))}
          >{option}</button>;
        })}
      </div>
    </div>)}
    {checked ? <RetryButton onClick={reset} /> : <CheckButton disabled={Object.keys(answers).length !== items.length} onClick={() => setChecked(true)} />}
  </section>;
}

function FillIn({ activity }: { activity: Activity }) {
  const { t } = useT();
  const items = activity.payload?.items ?? [];
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const [checked, setChecked] = useState(false);
  const reset = () => { setAnswers({}); setChecked(false); };

  return <section className="student-activity" data-student-activity="fill-in">
    <Heading activity={activity} />
    {activity.payload?.instruction && <p className="student-instruction">{activity.payload.instruction}</p>}
    {items.map((item: any, index: number) => {
      const correct = normalize(answers[index]) === normalize(item.answer);
      const parts = String(item.sentence ?? '').split(/(?:_{3,}|\{[^{}]+\})/);
      return <label className="student-fill-row" key={index}>
        <span>{parts[0]}</span>
        <select
          aria-label={t('studentActivity.fillBlank', { n: index + 1 })}
          className={`student-select${checked ? (correct ? ' correct' : ' wrong') : ''}`}
          disabled={checked}
          value={answers[index] ?? ''}
          onChange={(event) => setAnswers((previous) => ({ ...previous, [index]: event.target.value }))}
        >
          <option value="">{t('studentActivity.choose')}</option>
          {(item.options ?? [item.answer]).map((option: string) => <option value={option} key={option}>{option}</option>)}
        </select>
        <span>{parts.slice(1).join('')}</span>
      </label>;
    })}
    {checked ? <RetryButton onClick={reset} /> : <CheckButton disabled={Object.keys(answers).length !== items.length} onClick={() => setChecked(true)} />}
  </section>;
}

function Cloze({ activity }: { activity: Activity }) {
  const { t } = useT();
  const blanks = activity.payload?.blanks ?? [];
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const [checked, setChecked] = useState(false);
  const reset = () => { setAnswers({}); setChecked(false); };
  const nodes = useMemo(() => {
    const text = String(activity.payload?.text ?? '');
    const segments = text.split(/(\[___:\d+\]|\{(?:gap|blank)\}|(?<![\p{L}\p{N}_])\{[1-9]\d*\}(?![\p{L}\p{N}_]))/gu);
    let implicitIndex = 0;
    return segments.map((segment, segmentIndex) => {
      const token = segment ?? '';
      const indexed = token.match(/^\[___:(\d+)\]$/);
      const singleBraceIndex = token.match(/^\{(\d+)\}$/);
      const numberedIndex = indexed ?? singleBraceIndex;
      if (!numberedIndex && token !== '{gap}' && token !== '{blank}') return <span key={segmentIndex}>{token}</span>;
      const blankIndex = numberedIndex
        ? Number(numberedIndex[1])
        : (blanks[implicitIndex++]?.id ?? implicitIndex);
      const blank = blanks.find((candidate: any) => Number(candidate.id) === blankIndex) ?? blanks[blankIndex - 1];
      if (!blank) return <span key={segmentIndex}>{token}</span>;
      const correct = normalize(answers[blankIndex]) === normalize(blank.answer);
      return <select
        key={segmentIndex}
        aria-label={t('studentActivity.clozeBlank', { n: blankIndex })}
        className={`student-select student-cloze${checked ? (correct ? ' correct' : ' wrong') : ''}`}
        disabled={checked}
        value={answers[blankIndex] ?? ''}
        onChange={(event) => setAnswers((previous) => ({ ...previous, [blankIndex]: event.target.value }))}
      >
        <option value="">{t('studentActivity.choose')}</option>
        {(blank.options ?? []).map((option: string) => <option key={option} value={option}>{option}</option>)}
      </select>;
    });
  }, [activity.payload?.text, answers, blanks, checked, t]);

  return <section className="student-activity" data-student-activity="cloze">
    <Heading activity={activity} />
    {activity.payload?.instruction && <p className="student-instruction">{activity.payload.instruction}</p>}
    <p className="student-cloze-passage">{nodes}</p>
    {checked ? <RetryButton onClick={reset} /> : <CheckButton disabled={Object.keys(answers).length !== blanks.length} onClick={() => setChecked(true)} />}
  </section>;
}

function ErrorCorrection({ activity, corrections }: { activity: Activity; corrections: Correction[] }) {
  const { t } = useT();
  const items = activity.payload?.items ?? [];
  const correctionFor = (sentence: string) => corrections.find(
    (entry) => normalizeSentence(entry.sentence) === normalizeSentence(sentence),
  );
  const interactiveItemCount = items.filter((sentence: string) => {
    const entry = correctionFor(sentence);
    return entry?.error && entry.correction;
  }).length;
  const [identified, setIdentified] = useState<Record<number, boolean>>({});
  const [wrongAttempts, setWrongAttempts] = useState<Record<number, string[]>>({});
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const [checked, setChecked] = useState(false);
  const reset = () => { setIdentified({}); setWrongAttempts({}); setAnswers({}); setChecked(false); };

  return <section className="student-activity" data-student-activity="error-correction">
    <Heading activity={activity} />
    {activity.payload?.instruction && <p className="student-instruction">{activity.payload.instruction}</p>}
    {items.map((sentence: string, index: number) => {
      const correctionEntry = correctionFor(sentence);
      const error = correctionEntry?.error;
      const correction = correctionEntry?.correction;
      if (!error || !correction) {
        return <div className="student-question" key={index}>
          <p className="student-error-sentence">{sentence}</p>
        </div>;
      }
      const choices: string[] = error && correction ? [...new Set([error, correction])] : [];
      const isCorrect = correction !== undefined && normalize(answers[index]) === normalize(correction);
      return <div className="student-question" key={index}>
        {!identified[index] ? <>
          <p className="student-step-one">{t('studentActivity.findError')}</p>
          <p className="student-error-sentence">
            {(sentence.match(/[\p{L}'’-]+|[^\p{L}'’-]+/gu) ?? []).map((part, partIndex) => {
              if (!/[\p{L}]/u.test(part)) return <span key={partIndex}>{part}</span>;
              const isWrong = wrongAttempts[index]?.some((attempt) => normalize(attempt) === normalize(part));
              return <button
                type="button"
                key={partIndex}
                className={`student-word${isWrong ? ' wrong' : ''}`}
                onClick={() => {
                  if (error !== undefined && normalize(part) === normalize(error)) {
                    setIdentified((previous) => ({ ...previous, [index]: true }));
                  } else {
                    setWrongAttempts((previous) => ({ ...previous, [index]: [...(previous[index] ?? []), part] }));
                  }
                }}
              >{part}</button>;
            })}
          </p>
        </> : <>
          <p className="student-step-two">{t('studentActivity.chooseCorrection', { word: error ?? '' })}</p>
          <div className="student-choice-row">
            {choices.map((choice) => {
              const selected = answers[index] === choice;
              const state = checked && selected ? (isCorrect ? ' correct' : ' wrong') : '';
              return <button type="button" key={choice} disabled={checked} className={`student-choice${selected ? ' selected' : ''}${state}`} onClick={() => setAnswers((previous) => ({ ...previous, [index]: choice }))}>{choice}</button>;
            })}
          </div>
        </>}
      </div>;
    })}
    {interactiveItemCount > 0 && (checked
      ? <RetryButton onClick={reset} />
      : <CheckButton disabled={Object.keys(answers).length !== interactiveItemCount} onClick={() => setChecked(true)} />)}
  </section>;
}

function TextQuestions({ activity }: { activity: Activity }) {
  const { t } = useT();
  const questions = activity.payload?.items ?? [];
  const guidance = activity.answer_key?.guidance;
  return <section className="student-activity" data-student-activity="text-questions">
    <Heading activity={activity} />
    {guidance && <p className="student-guidance"><strong>{t('studentActivity.guidance')}</strong> {guidance}</p>}
    {activity.payload?.instruction && <p className="student-instruction">{activity.payload.instruction}</p>}
    <ol className="student-questions">{questions.map((question: string) => <li key={question}>{question}</li>)}</ol>
  </section>;
}

function ShortWriting({ activity }: { activity: Activity }) {
  const { t } = useT();
  return <section className="student-activity" data-student-activity="short-writing">
    <Heading activity={activity} />
    {activity.answer_key?.guidance && <p className="student-guidance"><strong>{t('studentActivity.guidance')}</strong> {activity.answer_key.guidance}</p>}
    <p className="student-instruction">{activity.payload?.prompt}</p>
    <label className="student-writing-label" htmlFor={`student-writing-${activity.title}`}>{t('studentActivity.yourResponse')}</label>
    <textarea id={`student-writing-${activity.title}`} className="student-writing" rows={8} placeholder={t('studentActivity.writingPlaceholder')} />
  </section>;
}

/**
 * Student-only adapter around the frozen package.  The package remains the teacher
 * renderer; this small UI layer only handles baked payload shapes the package cannot
 * represent without exposing answer keys or leaving the learner at a dead end.
 */
export default function StudentActivityPlayer({ activity, corrections = [] }: { activity: Activity; corrections?: Correction[] }) {
  switch (activity.type) {
    case 'true-false': return <TrueFalse activity={activity} />;
    case 'quiz': return <Quiz activity={activity} />;
    case 'fill-in': return <FillIn activity={activity} />;
    case 'cloze': return <Cloze activity={activity} />;
    case 'error-correction': return <ErrorCorrection activity={activity} corrections={corrections} />;
    case 'text-questions': return <TextQuestions activity={activity} />;
    case 'short-writing': return <ShortWriting activity={activity} />;
    default: return <ActivityPlayer activity={activity} isUkrainian />;
  }
}
