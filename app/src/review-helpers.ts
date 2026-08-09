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
  /**
   * The engine's honest verdict (lu.lesson.v1 >= 1.3.0, #402): `engine_flagged`
   * content shipped in place after failing gates or exhausting repair.
   * `flag_reason_uk` is engine-authored teacher-facing Ukrainian — render it
   * verbatim, never translate it; non-null exactly when flagged.
   */
  quality?: 'engine_ok' | 'engine_flagged';
  flag_reason_uk?: string | null;
  flagged_content_hash?: string | null;
  engine_reason_class?: string | null;
}

export interface RejectedEntry {
  type: string;
  /** Exactly null for the contentless `flagged-notice` shape (#402). */
  activity: Record<string, unknown> | null;
  reason: string;
}

/** The teacher's applicable verdict on one flagged block (#402). */
export interface ActivityFeedbackEntry {
  verdict: 'good' | 'bad';
  comment: string | null;
  updated_at: string;
}

/**
 * The lesson document's honest focus outcome (lu.lesson.v1 >= 1.1.0).
 * Absent — never an explicit null — when the teacher requested no focus.
 * `notice_uk` is engine-authored learner-facing Ukrainian: render it verbatim,
 * never translate it, and it is non-null exactly when `supported` is false.
 */
export interface FocusStatus {
  requested: string;
  supported: boolean;
  notice_uk: string | null;
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

/**
 * One deliberately-wrong sentence and the form that was broken on purpose (#164).
 * `error` is guaranteed by the engine to occur in `sentence`.
 */
export interface DeliberateError {
  sentence: string;
  error: string;
  correction: string;
}

/**
 * Read the engine's teacher-only intent metadata off an error-correction BLOCK key.
 *
 * It lives on the block key rather than the inner activity envelope because the pinned
 * `lu.activity.v1` `itemsAnswerKey` is `additionalProperties: false`. Older lessons
 * (and any lesson whose items lacked a full triple) simply have none — callers then
 * render today's plain answer key, so this is additive for stored documents too.
 */
export function deliberateErrors(answerKey: string | object | null | undefined): DeliberateError[] {
  const raw = asRecord(answerKey).corrections;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((entry) => {
    const { sentence, error, correction } = asRecord(entry);
    return typeof sentence === 'string' && sentence !== ''
      && typeof error === 'string' && error !== ''
      && typeof correction === 'string' && correction !== ''
      && sentence.includes(error)
      ? [{ sentence, error, correction }]
      : [];
  });
}

/**
 * Split a sentence around the FIRST occurrence of its deliberate error.
 * Returns the literal segments to render — never markup — so callers stay
 * injection-safe and the erroneous form can be wrapped in `<mark>`.
 */
export function splitOnDeliberateError(
  entry: DeliberateError,
): { before: string; error: string; after: string } {
  const at = entry.sentence.indexOf(entry.error);
  if (at < 0) return { before: entry.sentence, error: '', after: '' };
  return {
    before: entry.sentence.slice(0, at),
    error: entry.error,
    after: entry.sentence.slice(at + entry.error.length),
  };
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
    'flagged-notice': 'type.flagged-notice',
  };
  return keys[type] || type;
}

/**
 * Render an answer_key for teacher review display (#186).
 * - string → as-is (lesson content, may already be a human placeholder)
 * - object → human-readable lines (never raw JSON)
 * - null/empty → empty string (caller keeps any existing empty/placeholder UI)
 *
 * Chrome labels go through `t`; resolved option/answer text is lesson content and stays UA.
 */
export function formatAnswerKeyDisplay(
  answerKey: string | object | null | undefined,
  activity?: Record<string, unknown> | null,
  t: TFn = (key, params) => translate('uk', key as ChromeKey, params),
): string {
  if (answerKey == null || answerKey === '') return '';
  if (typeof answerKey === 'string') return answerKey.trim();
  if (typeof answerKey !== 'object' || Array.isArray(answerKey)) {
    return Array.isArray(answerKey)
      ? answerKey.map((item, i) => t('answerKey.simpleLine', { n: i + 1, value: String(item ?? '') })).join('\n')
      : '';
  }

  const key = asRecord(answerKey);
  const payload = asRecord(activity?.payload);
  const lines: string[] = [];

  if (Array.isArray(key.items)) {
    key.items.forEach((raw, i) => {
      const n = i + 1;
      if (typeof raw === 'string') {
        lines.push(t('answerKey.simpleLine', { n, value: raw }));
        return;
      }
      const item = asRecord(raw);
      const index = typeof item.index === 'number' ? item.index : i;
      const displayN = index + 1;
      const correct = item.correct;

      if (typeof correct === 'boolean') {
        const value = correct ? t('editor.option.true') : t('editor.option.false');
        lines.push(t('answerKey.itemLine', { n: displayN, value }));
        return;
      }

      if (typeof correct === 'number') {
        const payloadItems = Array.isArray(payload.items) ? payload.items : [];
        const payloadItem = asRecord(payloadItems[index]);
        const options = Array.isArray(payloadItem.options) ? payloadItem.options : [];
        const opt = options[correct];
        const value =
          typeof opt === 'string'
            ? opt
            : opt != null && typeof asRecord(opt).text === 'string'
              ? String(asRecord(opt).text)
              : t('editor.field.optionFallback', { n: correct + 1 });
        lines.push(t('answerKey.itemLine', { n: displayN, value }));
        return;
      }

      // Unknown item shape — render a compact non-JSON summary of known fields.
      const parts = Object.entries(item)
        .filter(([, v]) => v != null && typeof v !== 'object')
        .map(([k, v]) => `${k}=${String(v)}`);
      if (parts.length) {
        lines.push(t('answerKey.simpleLine', { n: displayN, value: parts.join(', ') }));
      }
    });
  }

  if (Array.isArray(key.blanks)) {
    key.blanks.forEach((raw, i) => {
      const blank = asRecord(raw);
      const n = typeof blank.id === 'number' ? blank.id : i + 1;
      const value = typeof blank.answer === 'string' ? blank.answer : String(blank.answer ?? '');
      lines.push(t('answerKey.blankLine', { n, value }));
    });
  }

  if (Array.isArray(key.pairs)) {
    const payloadPairs = Array.isArray(payload.pairs) ? payload.pairs : [];
    key.pairs.forEach((raw, i) => {
      const pair = asRecord(raw);
      const li = typeof pair.left_index === 'number' ? pair.left_index : i;
      const ri = typeof pair.right_index === 'number' ? pair.right_index : i;
      const leftSrc = asRecord(payloadPairs[li]);
      const rightSrc = asRecord(payloadPairs[ri]);
      const left =
        typeof leftSrc.left === 'string'
          ? leftSrc.left
          : typeof pair.left === 'string'
            ? pair.left
            : String(li + 1);
      const right =
        typeof rightSrc.right === 'string'
          ? rightSrc.right
          : typeof pair.right === 'string'
            ? pair.right
            : String(ri + 1);
      lines.push(t('answerKey.pairLine', { n: i + 1, left, right }));
    });
  }

  if (Array.isArray(key.target_words)) {
    const words = key.target_words.filter((w): w is string => typeof w === 'string');
    if (words.length) {
      lines.push(t('answerKey.wordsLine', { value: words.join(', ') }));
    }
  }

  if (typeof key.guidance === 'string' && key.guidance.trim()) {
    lines.push(t('answerKey.guidanceLine', { value: key.guidance.trim() }));
  }
  if (Array.isArray(key.model_answers)) {
    const answers = key.model_answers
      .map((a, i) => (typeof a === 'string' && a.trim() ? `${i + 1}. ${a.trim()}` : ''))
      .filter(Boolean);
    if (answers.length) {
      lines.push(t('answerKey.modelAnswersLine', { value: answers.join(' · ') }));
    }
  }
  if (typeof key.model_answer === 'string' && key.model_answer.trim()) {
    lines.push(t('answerKey.modelLine', { value: key.model_answer.trim() }));
  }
  if (typeof key.rubric === 'string' && key.rubric.trim()) {
    lines.push(t('answerKey.rubricLine', { value: key.rubric.trim() }));
  }

  if (lines.length > 0) return lines.join('\n');

  // Last resort for unexpected object shapes: surface primitive fields, never JSON braces dump.
  const fallback = Object.entries(key)
    .filter(([, v]) => v != null && (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'))
    .map(([k, v]) => `${k}: ${String(v)}`);
  return fallback.join('\n');
}

export function blockNeedsReview(block: ReviewBlock): boolean {
  // Mirrors `block_needs_review` in hramatka/api/store.py — keep in lockstep.
  return (
    block.mark === 'warn'
    || block.quality === 'engine_flagged'
    || block.provenance?.external_options === true
  );
}

/**
 * Reserved lesson-level pseudo-block id acknowledging an unsupported focus.
 * Must stay equal to `FOCUS_STATUS_ACK_ID` in hramatka/api/store.py: the ack
 * posts it to the ordinary block-ack endpoint, and the server requires it
 * before accept. Real block ids are generated (`block-<n>`, `restored-<hex>`),
 * so it cannot collide with one.
 */
export const FOCUS_STATUS_ACK_ID = 'focus-status';

/**
 * Whether the lesson's focus outcome needs teacher acknowledgement — mirrors
 * `focus_status_needs_review` in hramatka/api/store.py. An unsupported focus is
 * a caveat the teacher accepts explicitly; a supported focus, and an absent
 * carrier (no focus requested), need nothing.
 */
export function focusStatusNeedsReview(focusStatus: FocusStatus | null | undefined): boolean {
  return focusStatus?.supported === false;
}

export function marginStateChip(block: ReviewBlock, acked: boolean): { className: string; key: string } {
  if (block.edited) return { className: 'ok', key: 'chip.edited' };
  if (blockNeedsReview(block) && !acked) return { className: 'warn', key: 'chip.look' };
  if (blockNeedsReview(block) && acked) return { className: 'ok', key: 'chip.confirmed' };
  return { className: 'ok', key: 'chip.verified' };
}
