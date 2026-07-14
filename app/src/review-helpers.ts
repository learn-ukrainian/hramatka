/** Review assembly helpers — mirrors hramatka/api/lesson.py budgets and split logic. */
import type { ErrorObject } from 'ajv';
import { translate, type ChromeKey, type TFn } from './i18n';

// CSP-safe precompiled validator (generated at build time via ajv standalone).
// Replaces the previous top-level `new Ajv().compile()` which used runtime `new Function()`
// and violated the production CSP (`default-src 'self'`, no `unsafe-eval`).
import activityValidator from './generated/activityValidator.js';

export type PilotActivityType =
  | 'true-false' | 'cloze' | 'match-up' | 'quiz' | 'mark-the-words'
  | 'fill-in' | 'error-correction' | 'text-questions' | 'short-writing';

export type LessonDuration = 45 | 60 | 90;

export const PILOT_ACTIVITY_TYPES: readonly PilotActivityType[] = [
  'true-false', 'cloze', 'match-up', 'quiz', 'mark-the-words',
  'fill-in', 'error-correction', 'text-questions', 'short-writing',
];

// Must stay byte-for-byte equal to hramatka/sizing_policy.py.  The Python
// sizing-policy test parses this JSON literal and rejects drift before build.
const REVIEW_PHASE_BUDGETS_RAW = {
  "45": { "1": 3, "2": 4, "3": 1 },
  "60": { "1": 3, "2": 5, "3": 2 },
  "90": { "1": 4, "2": 5, "3": 3 }
} as const;

export const REVIEW_PHASE_BUDGETS: Record<LessonDuration, Record<1 | 2 | 3, number>> = REVIEW_PHASE_BUDGETS_RAW;

export const PHASE_LABELS: Record<1 | 2 | 3, { pnKey: ChromeKey; titleKey: ChromeKey; pd: Record<LessonDuration, number> }> = {
  1: { pnKey: 'phase.roman1', titleKey: 'phase.test1', pd: { 45: 8, 60: 10, 90: 12 } },
  2: { pnKey: 'phase.roman2', titleKey: 'phase.teach', pd: { 45: 18, 60: 22, 90: 28 } },
  3: { pnKey: 'phase.roman3', titleKey: 'phase.test2', pd: { 45: 8, 60: 10, 90: 12 } },
};

export interface ReviewBlock {
  id: string;
  phase: 1 | 2 | 3;
  type: PilotActivityType;
  mode: string;
  activity: Record<string, unknown>;
  answer_key: string | object | null;
  mark: 'ok' | 'warn';
  note: string | null;
  edited: boolean;
  provenance?: {
    source?: string;
    generator?: string;
    gates?: string[];
    external_options?: boolean;
  };
}

export interface RejectedEntry {
  type: string;
  activity: Record<string, unknown>;
  reason: string;
}

export function splitReviewBlocks(
  blocks: ReviewBlock[],
  duration: LessonDuration,
): { visible: ReviewBlock[]; reserve: ReviewBlock[]; byPhase: Record<1 | 2 | 3, { visible: ReviewBlock[]; reserve: ReviewBlock[] }> } {
  const budgets = REVIEW_PHASE_BUDGETS[duration];
  const seen: Record<1 | 2 | 3, number> = { 1: 0, 2: 0, 3: 0 };
  const visible: ReviewBlock[] = [];
  const reserve: ReviewBlock[] = [];
  const byPhase: Record<1 | 2 | 3, { visible: ReviewBlock[]; reserve: ReviewBlock[] }> = {
    1: { visible: [], reserve: [] },
    2: { visible: [], reserve: [] },
    3: { visible: [], reserve: [] },
  };

  for (const block of blocks) {
    const phase = block.phase;
    if (seen[phase] < budgets[phase]) {
      visible.push(block);
      byPhase[phase].visible.push(block);
      seen[phase] += 1;
    } else {
      reserve.push(block);
      byPhase[phase].reserve.push(block);
    }
  }

  return { visible, reserve, byPhase };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => typeof item === 'string' ? item : '') : [];
}

function pilotTypeError(type: unknown, t: TFn): string | null {
  return PILOT_ACTIVITY_TYPES.includes(type as PilotActivityType)
    ? null
    : t('err.unsupportedType', { type: String(type || t('editor.valLabel.taskDefault')) });
}

function formatSchemaError(error: ErrorObject, t: TFn): string {
  const path = error.instancePath || 'activity';
  return t('err.schemaPath', { path, message: error.message || t('err.schemaDefault') });
}

function nonBlankTextErrors(activity: Record<string, unknown>, t: TFn): string[] {
  const payload = asRecord(activity.payload);
  const requiredText = [
    ['editor.valLabel.title', activity.title],
    ['editor.valLabel.instruction', payload.instruction],
    ['editor.valLabel.text', payload.text],
    ['editor.valLabel.prompt', payload.prompt],
  ] as const;
  return requiredText.flatMap(([labelKey, value]) =>
    typeof value === 'string' && value.trim().length === 0
      ? [t('err.blankField', { label: t(labelKey as ChromeKey) })]
      : [],
  );
}

/**
 * Rebuild answer keys that are deterministically encoded by the activity payload.
 * Teacher-authored keys are deliberately retained, but normalized to their schema shape.
 */
export function regenerateAnswerKey(activity: Record<string, unknown>): Record<string, unknown> {
  const payload = asRecord(activity.payload);
  const previousKey = asRecord(activity.answer_key);
  let answerKey: Record<string, unknown>;

  switch (activity.type as PilotActivityType) {
    case 'true-false':
      answerKey = {
        items: (Array.isArray(payload.items) ? payload.items : []).map((item, index) => ({
          index,
          correct: asRecord(item).correct,
        })),
      };
      break;
    case 'cloze':
      answerKey = {
        blanks: (Array.isArray(payload.blanks) ? payload.blanks : []).map((blank) => ({
          id: asRecord(blank).id,
          answer: asRecord(blank).answer,
        })),
      };
      break;
    case 'match-up':
      answerKey = {
        pairs: (Array.isArray(payload.pairs) ? payload.pairs : []).map((_, index) => ({
          left_index: index,
          right_index: index,
        })),
      };
      break;
    case 'quiz':
      answerKey = {
        items: (Array.isArray(payload.items) ? payload.items : []).map((item, index) => ({
          index,
          correct: asRecord(item).correct,
        })),
      };
      break;
    case 'mark-the-words':
      answerKey = { target_words: asStringArray(payload.target_words) };
      break;
    case 'fill-in':
      answerKey = {
        items: (Array.isArray(payload.items) ? payload.items : []).map((item) => asRecord(item).answer),
      };
      break;
    case 'error-correction': {
      const items = Array.isArray(payload.items) ? payload.items : [];
      answerKey = { items: items.map((_, index) => asStringArray(previousKey.items)[index] || '') };
      break;
    }
    case 'text-questions': {
      const items = Array.isArray(payload.items) ? payload.items : [];
      const modelAnswers = asStringArray(previousKey.model_answers);
      answerKey = {
        guidance: previousKey.guidance || '',
        ...(modelAnswers.length > 0 ? { model_answers: items.map((_, index) => modelAnswers[index] || '') } : {}),
        ...(typeof previousKey.model_answer === 'string' ? { model_answer: previousKey.model_answer } : {}),
        ...(typeof previousKey.rubric === 'string' ? { rubric: previousKey.rubric } : {}),
      };
      break;
    }
    case 'short-writing':
      answerKey = {
        guidance: previousKey.guidance || '',
        ...(typeof previousKey.model_answer === 'string' ? { model_answer: previousKey.model_answer } : {}),
        ...(typeof previousKey.rubric === 'string' ? { rubric: previousKey.rubric } : {}),
      };
      break;
    default:
      answerKey = previousKey;
  }

  return { ...activity, answer_key: answerKey };
}

/** Validate the full activity envelope using the pinned vendored lu.activity.v1 schema. */
export function validateActivityDocument(
  activity: Record<string, unknown>,
  t: TFn = (key, params) => translate('uk', key as ChromeKey, params),
): { valid: boolean; errors: string[] } {
  const pilotError = pilotTypeError(activity.type, t);
  const valid = activityValidator(activity);
  const errors = [
    ...(pilotError ? [pilotError] : []),
    ...(valid ? [] : (activityValidator.errors || []).map((error) => formatSchemaError(error, t))),
    ...nonBlankTextErrors(activity, t),
  ];
  return { valid: errors.length === 0, errors };
}

export function activityTypeLabel(type: string): string {
  const keys: Record<string, string> = {
    'true-false': 'type.true-false',
    'cloze': 'type.cloze',
    'match-up': 'type.match-up',
    'quiz': 'type.quiz',
    'mark-the-words': 'type.mark-the-words',
    'fill-in': 'type.fill-in',
    'error-correction': 'type.error-correction',
    'text-questions': 'type.text-questions',
    'short-writing': 'type.short-writing',
  };
  return keys[type] || type;
}

export function blockNeedsReview(block: ReviewBlock): boolean {
  return block.mark === 'warn' || block.provenance?.external_options === true;
}

export function marginStateChip(block: ReviewBlock, acked: boolean): { className: string; key: string } {
  if (block.edited) return { className: 'ok', key: 'chip.edited' };
  if (blockNeedsReview(block) && !acked) return { className: 'warn', key: 'chip.look' };
  if (blockNeedsReview(block) && acked) return { className: 'ok', key: 'chip.confirmed' };
  return { className: 'ok', key: 'chip.verified' };
}
