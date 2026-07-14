import { useState, type ReactNode } from 'react';
import {
  type PilotActivityType,
  validateActivityDocument,
  regenerateAnswerKey,
  activityTypeLabel,
} from './review-helpers';
import { useT, type ChromeKey } from './i18n';

interface ActivityEditorProps {
  activity: Record<string, unknown>;
  onSave: (activity: Record<string, unknown>) => void;
  onCancel: () => void;
  disabled?: boolean;
}

function cloneActivity(activity: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(activity));
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function objectItems(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(asRecord) : [];
}

function stringItems(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => typeof item === 'string' ? item : '') : [];
}

function commaSeparated(value: unknown): string[] {
  return String(value).split(',').map((item) => item.trim()).filter(Boolean);
}

export default function ActivityEditor({ activity, onSave, onCancel, disabled }: ActivityEditorProps) {
  const [draft, setDraft] = useState(() => cloneActivity(activity));
  const [errors, setErrors] = useState<string[]>([]);

  const { t } = useT();

  const type = draft.type as PilotActivityType;
  const payload = asRecord(draft.payload);
  const answerKey = asRecord(draft.answer_key);

  const updateDraft = (update: (previous: Record<string, unknown>) => Record<string, unknown>) => {
    setDraft((previous) => regenerateAnswerKey(update(previous)));
    setErrors([]);
  };

  const updatePayload = (update: (previous: Record<string, unknown>) => Record<string, unknown>) => {
    updateDraft((previous) => ({ ...previous, payload: update(asRecord(previous.payload)) }));
  };

  const updateAnswerKey = (update: (previous: Record<string, unknown>) => Record<string, unknown>) => {
    updateDraft((previous) => ({ ...previous, answer_key: update(asRecord(previous.answer_key)) }));
  };

  const updateObjectPayloadItem = (
    field: 'items' | 'pairs' | 'blanks',
    index: number,
    update: (item: Record<string, unknown>) => Record<string, unknown>,
  ) => {
    updatePayload((previous) => {
      const items = objectItems(previous[field]);
      return { ...previous, [field]: items.map((item, itemIndex) => itemIndex === index ? update(item) : item) };
    });
  };

  const updateStringPayloadItem = (field: 'items', index: number, value: string) => {
    updatePayload((previous) => {
      const items = stringItems(previous[field]);
      return { ...previous, [field]: items.map((item, itemIndex) => itemIndex === index ? value : item) };
    });
  };

  const updateAnswerKeyItem = (index: number, value: string) => {
    updateAnswerKey((previous) => {
      const items = stringItems(previous.items);
      return { ...previous, items: items.map((item, itemIndex) => itemIndex === index ? value : item) };
    });
  };

  const handleSubmit = () => {
    const completeActivity = regenerateAnswerKey(draft);
    const result = validateActivityDocument(completeActivity);
    if (!result.valid) {
      setErrors(result.errors);
      return;
    }
    setErrors([]);
    onSave(completeActivity);
  };

  const renderFields = () => {
    switch (type) {
      case 'true-false': {
        const items = objectItems(payload.items);
        return <>
          <Instruction value={payload.instruction} onChange={(value) => updatePayload((previous) => ({ ...previous, instruction: value }))} />
          {items.map((item, index) => <div key={index} className="editor-group">
            <Field label={`Твердження ${index + 1}`}><textarea className="inputbox" rows={2} value={String(item.statement || '')} onChange={(event) => updateObjectPayloadItem('items', index, (current) => ({ ...current, statement: event.target.value }))} /></Field>
            <Field label="Правильна відповідь"><select className="inputbox" value={item.correct === false ? 'false' : 'true'} onChange={(event) => updateObjectPayloadItem('items', index, (current) => ({ ...current, correct: event.target.value === 'true' }))}><option value="true">Правда</option><option value="false">Ні</option></select></Field>
          </div>)}
        </>;
      }
      case 'cloze': {
        const blanks = objectItems(payload.blanks);
        return <>
          <Instruction value={payload.instruction} onChange={(value) => updatePayload((previous) => ({ ...previous, instruction: value }))} />
          <Field label="Текст із прогалинами"><textarea className="inputbox" rows={4} value={String(payload.text || '')} onChange={(event) => updatePayload((previous) => ({ ...previous, text: event.target.value }))} /></Field>
          {blanks.map((blank, index) => <div key={index} className="editor-group">
            <Field label={`Прогалина ${index + 1} — номер`}><input className="inputbox" type="number" min="1" value={String(blank.id || '')} onChange={(event) => updateObjectPayloadItem('blanks', index, (current) => ({ ...current, id: Number(event.target.value) }))} /></Field>
            <Field label="Відповідь"><input className="inputbox" value={String(blank.answer || '')} onChange={(event) => updateObjectPayloadItem('blanks', index, (current) => ({ ...current, answer: event.target.value }))} /></Field>
            <Field label="Варіанти (через кому)"><input className="inputbox" value={stringItems(blank.options).join(', ')} onChange={(event) => updateObjectPayloadItem('blanks', index, (current) => ({ ...current, options: commaSeparated(event.target.value) }))} /></Field>
          </div>)}
        </>;
      }
      case 'match-up': {
        const pairs = objectItems(payload.pairs);
        return <>
          <Instruction value={payload.instruction} onChange={(value) => updatePayload((previous) => ({ ...previous, instruction: value }))} />
          {pairs.map((pair, index) => <div key={index} className="editor-group">
            <Field label={`Пара ${index + 1} — ліворуч`}><input className="inputbox" value={String(pair.left || '')} onChange={(event) => updateObjectPayloadItem('pairs', index, (current) => ({ ...current, left: event.target.value }))} /></Field>
            <Field label={`Пара ${index + 1} — праворуч`}><input className="inputbox" value={String(pair.right || '')} onChange={(event) => updateObjectPayloadItem('pairs', index, (current) => ({ ...current, right: event.target.value }))} /></Field>
          </div>)}
        </>;
      }
      case 'quiz': {
        const items = objectItems(payload.items);
        return <>
          <Instruction value={payload.instruction} onChange={(value) => updatePayload((previous) => ({ ...previous, instruction: value }))} />
          {items.map((item, index) => {
            const options = stringItems(item.options);
            return <div key={index} className="editor-group">
              <Field label={`Питання ${index + 1}`}><textarea className="inputbox" rows={2} value={String(item.question || '')} onChange={(event) => updateObjectPayloadItem('items', index, (current) => ({ ...current, question: event.target.value }))} /></Field>
              {options.map((option, optionIndex) => <Field key={optionIndex} label={`Варіант ${String.fromCharCode(65 + optionIndex)}`}><input className="inputbox" value={option} onChange={(event) => updateObjectPayloadItem('items', index, (current) => ({ ...current, options: stringItems(current.options).map((currentOption, currentIndex) => currentIndex === optionIndex ? event.target.value : currentOption) }))} /></Field>)}
              <Field label="Правильний варіант"><select className="inputbox" value={String(item.correct ?? 0)} onChange={(event) => updateObjectPayloadItem('items', index, (current) => ({ ...current, correct: Number(event.target.value) }))}>{options.map((option, optionIndex) => <option key={optionIndex} value={optionIndex}>{String.fromCharCode(65 + optionIndex)} — {option || `варіант ${optionIndex + 1}`}</option>)}</select></Field>
            </div>;
          })}
        </>;
      }
      case 'mark-the-words':
        return <>
          <Instruction value={payload.instruction} onChange={(value) => updatePayload((previous) => ({ ...previous, instruction: value }))} />
          <Field label="Текст"><textarea className="inputbox" rows={3} value={String(payload.text || '')} onChange={(event) => updatePayload((previous) => ({ ...previous, text: event.target.value }))} /></Field>
          <Field label="Цільові слова (через кому)"><input className="inputbox" value={stringItems(payload.target_words).join(', ')} onChange={(event) => updatePayload((previous) => ({ ...previous, target_words: commaSeparated(event.target.value) }))} /></Field>
          {typeof payload.criteria === 'string' && <Field label="Критерій"><input className="inputbox" value={payload.criteria} onChange={(event) => updatePayload((previous) => ({ ...previous, criteria: event.target.value }))} /></Field>}
        </>;
      case 'fill-in': {
        const items = objectItems(payload.items);
        return <>
          <Instruction value={payload.instruction} onChange={(value) => updatePayload((previous) => ({ ...previous, instruction: value }))} />
          {items.map((item, index) => <div key={index} className="editor-group">
            <Field label={`Речення ${index + 1}`}><textarea className="inputbox" rows={2} value={String(item.sentence || '')} onChange={(event) => updateObjectPayloadItem('items', index, (current) => ({ ...current, sentence: event.target.value }))} /></Field>
            <Field label="Відповідь"><input className="inputbox" value={String(item.answer || '')} onChange={(event) => updateObjectPayloadItem('items', index, (current) => ({ ...current, answer: event.target.value }))} /></Field>
            {Array.isArray(item.options) && <Field label="Варіанти (через кому)"><input className="inputbox" value={stringItems(item.options).join(', ')} onChange={(event) => updateObjectPayloadItem('items', index, (current) => ({ ...current, options: commaSeparated(event.target.value) }))} /></Field>}
          </div>)}
        </>;
      }
      case 'error-correction': {
        const items = stringItems(payload.items);
        const corrections = stringItems(answerKey.items);
        return <>
          <Instruction value={payload.instruction} onChange={(value) => updatePayload((previous) => ({ ...previous, instruction: value }))} />
          {items.map((item, index) => <div key={index} className="editor-group">
            <Field label={`Речення з помилкою ${index + 1}`}><textarea className="inputbox" rows={2} value={item} onChange={(event) => updateStringPayloadItem('items', index, event.target.value)} /></Field>
            <Field label="Виправлене речення"><textarea className="inputbox" rows={2} value={corrections[index] || ''} onChange={(event) => updateAnswerKeyItem(index, event.target.value)} /></Field>
          </div>)}
        </>;
      }
      case 'text-questions': {
        const items = stringItems(payload.items);
        const modelAnswers = stringItems(answerKey.model_answers);
        return <>
          <Instruction value={payload.instruction} onChange={(value) => updatePayload((previous) => ({ ...previous, instruction: value }))} />
          {typeof payload.source_ref === 'string' && <Field label="Посилання на текст"><input className="inputbox" value={payload.source_ref} onChange={(event) => updatePayload((previous) => ({ ...previous, source_ref: event.target.value }))} /></Field>}
          {items.map((item, index) => <div key={index} className="editor-group">
            <Field label={`Питання ${index + 1}`}><textarea className="inputbox" rows={2} value={item} onChange={(event) => updateStringPayloadItem('items', index, event.target.value)} /></Field>
            {modelAnswers.length > 0 && <Field label="Модельна відповідь"><textarea className="inputbox" rows={2} value={modelAnswers[index] || ''} onChange={(event) => updateAnswerKey((previous) => ({ ...previous, model_answers: stringItems(previous.model_answers).map((answer, answerIndex) => answerIndex === index ? event.target.value : answer) }))} /></Field>}
          </div>)}
          <GuidanceFields answerKey={answerKey} updateAnswerKey={updateAnswerKey} />
        </>;
      }
      case 'short-writing':
        return <>
          <Field label="Завдання"><textarea className="inputbox" rows={3} value={String(payload.prompt || '')} onChange={(event) => updatePayload((previous) => ({ ...previous, prompt: event.target.value }))} /></Field>
          {typeof payload.source_ref === 'string' && <Field label="Посилання на текст"><input className="inputbox" value={payload.source_ref} onChange={(event) => updatePayload((previous) => ({ ...previous, source_ref: event.target.value }))} /></Field>}
          <GuidanceFields answerKey={answerKey} updateAnswerKey={updateAnswerKey} />
        </>;
      default:
        return <p className="hint">{t('review.unavailable')}</p>;
    }
  };

  return <div className="activity-editor" data-testid="activity-editor">
    <p className="editor-type-label">{t(activityTypeLabel(type) as ChromeKey)}</p>
    <Field label="Назва"><input className="inputbox" value={String(draft.title || '')} onChange={(event) => updateDraft((previous) => ({ ...previous, title: event.target.value }))} /></Field>
    {renderFields()}
    {errors.length > 0 && <div className="banner fail editor-errors" role="alert" data-testid="editor-validation-errors"><span className="ic">!</span><ul>{errors.map((error) => <li key={error}>{error}</li>)}</ul></div>}
    <div className="editor-actions">
      <button type="button" className="btn primary" onClick={handleSubmit} disabled={disabled}>{t('editor.save')}</button>
      <button type="button" className="btn ghost" onClick={onCancel} disabled={disabled}>{t('editor.cancel')}</button>
    </div>
  </div>;
}

function Instruction({ value, onChange }: { value: unknown; onChange: (value: string) => void }) {
  return <Field label="Інструкція"><textarea className="inputbox" rows={2} value={String(value || '')} onChange={(event) => onChange(event.target.value)} /></Field>;
}

function GuidanceFields({ answerKey, updateAnswerKey }: { answerKey: Record<string, unknown>; updateAnswerKey: (update: (previous: Record<string, unknown>) => Record<string, unknown>) => void }) {
  return <div className="editor-group">
    <Field label="Вказівка для вчителя"><textarea className="inputbox" rows={2} value={String(answerKey.guidance || '')} onChange={(event) => updateAnswerKey((previous) => ({ ...previous, guidance: event.target.value }))} /></Field>
    {typeof answerKey.model_answer === 'string' && <Field label="Модельна відповідь"><textarea className="inputbox" rows={2} value={answerKey.model_answer} onChange={(event) => updateAnswerKey((previous) => ({ ...previous, model_answer: event.target.value }))} /></Field>}
    {typeof answerKey.rubric === 'string' && <Field label="Рубрика"><textarea className="inputbox" rows={2} value={answerKey.rubric} onChange={(event) => updateAnswerKey((previous) => ({ ...previous, rubric: event.target.value }))} /></Field>}
  </div>;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="field"><label>{label}</label>{children}</div>;
}
