import '@testing-library/jest-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LangProvider } from './i18n';
import StudentActivityPlayer from './StudentActivityPlayer';

function renderActivity(
  activity: Record<string, any>,
  corrections?: Array<{ sentence: string; error: string; correction: string }>,
) {
  return render(<LangProvider><StudentActivityPlayer activity={activity} corrections={corrections} /></LangProvider>);
}

describe('student activity player (#410)', () => {
  it('accepts baked {gap} cloze markers and colours checked answers', () => {
    renderActivity({
      type: 'cloze', title: 'Заповніть пропуски',
      payload: { instruction: 'Доберіть слова.', text: 'Учні {gap} текст.', blanks: [{ id: 1, answer: 'читають', options: ['читають', 'пишуть'] }] },
    });

    const blank = screen.getByLabelText('Пропуск 1');
    fireEvent.change(blank, { target: { value: 'читають' } });
    fireEvent.click(screen.getByRole('button', { name: 'Перевірити' }));
    expect(blank).toHaveClass('correct');
  });

  it('renders numbered single-brace cloze markers as every gap and colours checked answers', () => {
    const blanks = Array.from({ length: 8 }, (_, index) => ({
      id: index + 1,
      answer: `слово${index + 1}`,
      options: [`слово${index + 1}`, `інше${index + 1}`],
    }));
    renderActivity({
      type: 'cloze', title: 'Заповніть пропуски',
      payload: {
        text: '{1} {2} {3} {4} {5}, {6} {7} {8} потрібна велика і світла вітальня.',
        blanks,
      },
    });

    const check = screen.getByRole('button', { name: 'Перевірити' });
    expect(check).toBeDisabled();
    for (const blank of blanks) {
      const value = blank.id === 8 ? blank.options[1] : blank.answer;
      fireEvent.change(screen.getByLabelText(`Пропуск ${blank.id}`), { target: { value } });
    }
    expect(screen.getAllByRole('combobox')).toHaveLength(8);
    expect(check).toBeEnabled();

    fireEvent.click(check);
    expect(screen.getByLabelText('Пропуск 1')).toHaveClass('correct');
    expect(screen.getByLabelText('Пропуск 8')).toHaveClass('wrong');
  });

  it('keeps word-adjacent and out-of-range brace expressions as prose', () => {
    const { container } = renderActivity({
      type: 'cloze', title: 'Заповніть пропуски',
      payload: {
        text: 'Позначення formula{1} і {9} лишаються в тексті, а {1} — пропуск.',
        blanks: [{ id: 1, answer: 'слово', options: ['слово', 'інше'] }],
      },
    });

    expect(container).toHaveTextContent('formula{1}');
    expect(container).toHaveTextContent('{9}');
    expect(screen.getAllByRole('combobox')).toHaveLength(1);
  });

  it('colours checked fill-in answers instead of only reporting a result', () => {
    renderActivity({
      type: 'fill-in', title: 'Вставте слово',
      payload: { items: [{ sentence: 'Це ___ текст.', answer: 'український', options: ['український', 'інший'] }] },
    });

    const blank = screen.getByLabelText('Пропуск 1');
    fireEvent.change(blank, { target: { value: 'інший' } });
    fireEvent.click(screen.getByRole('button', { name: 'Перевірити' }));
    expect(blank).toHaveClass('wrong');
  });

  it('retains true-false choices and colours the chosen button after checking', () => {
    renderActivity({
      type: 'true-false', title: 'Перевірмо розуміння',
      payload: { items: [{ statement: 'Текст український.', correct: true }] },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Неправда' }));
    fireEvent.click(screen.getByRole('button', { name: 'Перевірити' }));
    expect(screen.getByRole('button', { name: 'Неправда' })).toHaveClass('selected', 'wrong');
    expect(document.querySelector('[data-activity="tf-row-feedback"]')).toHaveAttribute('data-correct', 'false');
  });

  it('colours the learner-selected quiz option when it is checked', () => {
    renderActivity({
      type: 'quiz', title: 'Тест',
      payload: { items: [{ question: 'Яка відповідь?', options: ['Перша', 'Друга'], correct: 0 }] },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Друга' }));
    fireEvent.click(screen.getByRole('button', { name: 'Перевірити' }));
    expect(screen.getByRole('button', { name: 'Друга' })).toHaveClass('selected', 'wrong');
  });

  it('uses the block correction triple when the evidence quote is word-misaligned', () => {
    renderActivity({
      type: 'error-correction', title: 'Виправте помилку',
      payload: { instruction: 'Виправте форму.', items: ['Я живу в Києв.'] },
      answer_key: { items: ['У Києві багато старовинних будинків.'] },
    }, [{ sentence: 'Я живу в Києв.', error: 'Києв', correction: 'Києві' }]);

    fireEvent.click(screen.getByRole('button', { name: 'Я' }));
    expect(screen.getByRole('button', { name: 'Я' })).toHaveClass('wrong');
    expect(screen.queryByText('Оберіть правильну форму для «Києв»')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Києв' }));
    expect(screen.getByText('Оберіть правильну форму для «Києв»')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Києві' }));
    fireEvent.click(screen.getByRole('button', { name: 'Перевірити' }));
    expect(screen.getByRole('button', { name: 'Києві' })).toHaveClass('correct');
  });

  it('matches correction triples after Unicode, apostrophe, and whitespace normalization', () => {
    renderActivity({
      type: 'error-correction', title: 'Виправте помилку',
      payload: { items: ['  Я п’ю чаї.  '] },
    }, [{ sentence: "Я п'ю чаї.", error: 'чаї', correction: 'чай' }]);

    fireEvent.click(screen.getByRole('button', { name: 'чаї' }));
    expect(screen.getByText('Оберіть правильну форму для «чаї»')).toBeInTheDocument();
  });

  it('renders a legacy error-correction item without a triple non-interactively', () => {
    renderActivity({
      type: 'error-correction', title: 'Виправте помилку',
      payload: { items: ['Я живу в Києв.'] },
      answer_key: { items: ['Я живу в Києві.'] },
    });

    expect(screen.getByText('Я живу в Києв.')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('shows text questions, hides model answers, and puts guidance first', () => {
    const { container } = renderActivity({
      type: 'text-questions', title: 'Питання до тексту',
      payload: { instruction: 'Дайте відповіді.', items: ['Що сталося?'] },
      answer_key: { guidance: 'У відповіді має бути три слова.', model_answers: ['Прихована відповідь'] },
    });

    const guidance = screen.getByText('У відповіді має бути три слова.').parentElement!;
    const question = screen.getByText('Що сталося?');
    expect(guidance.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(container).not.toHaveTextContent('Прихована відповідь');
  });

  it('puts short-writing guidance above the learner input without exposing a model answer', () => {
    renderActivity({
      type: 'short-writing', title: 'Напишіть текст',
      payload: { prompt: 'Опишіть подію.' },
      answer_key: { guidance: 'Використайте щонайменше три речення.', model_answer: 'Не показувати учневі.' },
    });

    const guidance = screen.getByText('Використайте щонайменше три речення.').parentElement!;
    const input = screen.getByLabelText('Ваша відповідь:');
    expect(guidance.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText('Не показувати учневі.')).not.toBeInTheDocument();
  });
});
