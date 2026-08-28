import { translate, statusKey, type ChromeKey } from './i18n';

export type LessonState = 'draft' | 'baking' | 'ready' | 'failed' | 'cancelled';

/** The sole duration currently qualified for newly generated lessons. */
export const NEW_LESSON_DURATION = 45 as const;
export type NewLessonDuration = typeof NEW_LESSON_DURATION;

export interface CatalogLessonItem {
  id: string;
  title: string | null;
  anchor_snippet?: string | null;
  status: LessonState;
  level?: 'B1';
  duration: number;
  methodology?: 'ttt';
  grammar_focus?: string | null;
  /** Legacy client-only field retained while old cached rows expire. */
  focus?: string | null;
  revision: number;
  accepted: boolean;
  accepted_at: string | null;
  accepted_revision: number | null;
  failure_code: string | null;
  created_at: string;
  updated_at: string;
}

export type BakeProgressStep = 'generation' | 'gates' | 'assembly' | null;

/** Optional status payload field (may be absent until engine PR lands). */
export interface BakeProgress {
  phase: number;
  phases_total: number;
  step: BakeProgressStep;
  calls_done: number | null;
  /** Legacy provider-plan field; conditional checks make it non-total. */
  calls_planned: number | null;
  updated_at: string;
}

export interface BakeRequestPayload {
  text: string;
  duration: NewLessonDuration;
  grammarFocus?: string;
  /** Legacy sessionStorage field accepted when restoring a pre-wiring request. */
  focus?: string;
  lessonId: string;
  /** New requests are paste-only; unknown legacy storage fields are ignored. */
  anchorSource?: 'teacher-paste';
  logicalModelId?: string;
}

const LAST_BAKE_KEY = 'hramatka:last-bake-request';

/** UA status chip labels — delegates to i18n (UA default for standalone tests). */
export function statusLabel(s: LessonState): string {
  return translate('uk', statusKey(s));
}

export type BakeStatusTranslator = (
  key: ChromeKey,
  params?: Record<string, string | number>,
) => string;

/**
 * When failed, show only the failure message — never the last step label.
 * `failFallback` lets the caller pass a language-aware fallback (chrome); the UA
 * default keeps this helper usable standalone (and stable for its unit test).
 */
export function bakeStatusSubline(
  status: LessonState,
  step: string,
  failure?: string,
  failFallback = translate('uk', 'bake.failFallback' as ChromeKey),
): string {
  if (status === 'failed') {
    return failure || failFallback;
  }
  return step || '';
}

/** Honest line when API exposes `progress` — never a fake percent. */
export function formatBakeProgressLine(progress: BakeProgress, t: BakeStatusTranslator): string {
  const phase = Math.max(1, progress.phase);
  const total = Math.max(phase, progress.phases_total);
  const stepKey = progress.step;
  const stepLabels: Record<Exclude<BakeProgressStep, null>, string> = {
    generation: t('bake.step.generation'),
    gates: t('bake.step.gates'),
    assembly: t('bake.step.assembly'),
  };
  const stepLabel = stepKey ? stepLabels[stepKey] : t('bake.step.prep');
  let line = t('bake.progressLine', { phase: String(phase), total: String(total), step: stepLabel });
  if (progress.calls_done != null) {
    line += t('bake.providerCalls', { count: String(progress.calls_done) });
  }
  return line;
}

/** Staged reassuring copy keyed to elapsed time when `progress` is absent. */
export function formatBakeElapsedFallback(elapsedMs: number, t: BakeStatusTranslator): string {
  const min = elapsedMs / 60000;
  if (min < 2) {
    return t('bake.elapsed.lt2');
  }
  if (min < 5) {
    return t('bake.elapsed.lt5');
  }
  if (min < 15) {
    return t('bake.elapsed.lt15');
  }
  return t('bake.elapsed.gte15');
}

/** Elapsed mm:ss for the baking status header (not a progress percent). */
export function formatBakeElapsedClock(elapsedMs: number): string {
  const totalSec = Math.max(0, Math.floor(elapsedMs / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

/**
 * Persist last submitted bake request for failure recovery.
 * The status API does not expose anchor text on failed lessons (openapi LessonStatus).
 */
export function saveLastBakeRequest(payload: BakeRequestPayload): void {
  try {
    sessionStorage.setItem(LAST_BAKE_KEY, JSON.stringify(payload));
  } catch {
    /* quota / private mode — in-memory fallback handled by caller state */
  }
}

export function loadLastBakeRequest(lessonId?: string | null): BakeRequestPayload | null {
  try {
    const raw = sessionStorage.getItem(LAST_BAKE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw) as Partial<BakeRequestPayload>;
    if (!data?.text) return null;
    if (lessonId && data.lessonId && data.lessonId !== lessonId) return null;
    // A session can contain a pre-45-only request.  Its text remains useful
    // for recovery, but its old duration must never revive a new 60/90 bake.
    return { ...data, duration: NEW_LESSON_DURATION } as BakeRequestPayload;
  } catch {
    return null;
  }
}

/** Clear saved bake request (session boundary — no cross-teacher text leak). */
export function clearLastBakeRequest(): void {
  try {
    sessionStorage.removeItem(LAST_BAKE_KEY);
  } catch {
    /* private mode / quota */
  }
}

/** Build an optimistic catalog row from a saved bake request (in-flight create/recreate). */
export function catalogItemFromBakeRequest(payload: BakeRequestPayload): CatalogLessonItem {
  const now = new Date().toISOString();
  return {
    id: payload.lessonId,
    title: null,
    status: 'baking',
    duration: payload.duration,
    methodology: 'ttt',
    grammar_focus: (payload.grammarFocus ?? payload.focus ?? '').trim() || null,
    focus: (payload.grammarFocus ?? payload.focus ?? '').trim() || null,
    revision: 1,
    accepted: false,
    accepted_at: null,
    accepted_revision: null,
    failure_code: null,
    created_at: now,
    updated_at: now,
  };
}

/** Pending catalog rows from sessionStorage (+ optional in-memory fallback). */
export function loadLocalCatalogEntries(fallback?: BakeRequestPayload | null): CatalogLessonItem[] {
  const payload = loadLastBakeRequest() ?? fallback ?? null;
  if (!payload?.lessonId) return [];
  return [catalogItemFromBakeRequest(payload)];
}

/**
 * Merge server catalog rows with optimistic local rows.
 * Server rows win on id collision; local-only rows (not yet listed by API) are kept.
 */
export function mergeCatalogLessons(
  server: CatalogLessonItem[],
  local: CatalogLessonItem[],
): CatalogLessonItem[] {
  const serverById = new Map<string, CatalogLessonItem>();
  for (const item of server) {
    if (item?.id && !serverById.has(item.id)) {
      serverById.set(item.id, item);
    }
  }

  const merged: CatalogLessonItem[] = [];
  const seen = new Set<string>();

  for (const item of local) {
    if (!item?.id || serverById.has(item.id) || seen.has(item.id)) continue;
    seen.add(item.id);
    merged.push(item);
  }

  for (const item of server) {
    if (!item?.id || seen.has(item.id)) continue;
    seen.add(item.id);
    merged.push(serverById.get(item.id)!);
  }

  return merged;
}

/** Resolve persisted teacher pref to a valid duration (preselect for new-lesson form). */
export function resolveDefaultDurationFromPref(_pref: unknown): NewLessonDuration {
  // The server also normalizes historical preference rows. Keeping this
  // client-side guard prevents a stale cached response from restoring 60/90.
  return NEW_LESSON_DURATION;
}

// ===== Clipboard lesson export (Sol P1-5) =====
// Plain-text rendering of a lesson document for clipboard copy. Pure + unit-tested.
// Teacher variant: appends «Ключ відповіді» per task + notes/provenance.
// Student variant: NEVER includes answers, notes, or provenance — safe to share in Zoom.
// NOTE: the rendered TEXT is lesson content (UA); only app chrome strings were i18n-folded.

export interface ClipboardLessonBlock {
  id: string;
  phase: 1 | 2 | 3;
  type: string;
  mode: string;
  activity: any;
  answer_key: any;
  note: string | null;
  provenance?: any;
}

export interface ClipboardLesson {
  title: string;
  duration: number;
  focus?: string | null;
  anchor?: { text?: string } | null;
  blocks: ClipboardLessonBlock[];
}

export type ClipboardMode = 'teacher' | 'student';

/** OpenAPI LessonBlock.mode enum value — lesson data, not UI chrome. */
const HOMEWORK_MODE = 'вдома' as const;

/** Render a single activity payload as a readable plain-text task body. */
function renderActivityBody(activity: any): string {
  if (!activity || typeof activity !== 'object') return '';
  const p = activity.payload || {};
  const lines: string[] = [];
  const title = typeof activity.title === 'string' && activity.title ? activity.title : '';
  if (title) lines.push(title);

  const instruction =
    typeof p.instruction === 'string' && p.instruction
      ? p.instruction
      : typeof p.prompt === 'string' && p.prompt
        ? p.prompt
        : '';
  if (instruction && instruction !== title) lines.push(instruction);

  if (Array.isArray(p.items) && p.items.length) {
    p.items.forEach((item: any, i: number) => {
      if (item == null) return;
      const n = i + 1;
      if (typeof item === 'string') {
        lines.push(`${n}. ${item}`);
        return;
      }
      const stmt =
        typeof item.statement === 'string' ? item.statement
        : typeof item.question === 'string' ? item.question
        : typeof item.sentence === 'string' ? item.sentence
        : '';
      if (stmt) lines.push(`${n}. ${stmt}`);
      if (Array.isArray(item.options)) {
        item.options.forEach((opt: any, j: number) => {
          const letter = String.fromCharCode(65 + j); // A, B, C...
          const text = typeof opt === 'string' ? opt : (opt?.text ?? '');
          if (text) lines.push(`   ${letter}) ${text}`);
        });
      }
    });
  }

  if (typeof p.text === 'string' && p.text && !lines.includes(p.text)) {
    lines.push(p.text);
  }
  if (Array.isArray(p.pairs) && p.pairs.length) {
    p.pairs.forEach((pair: any, i: number) => {
      if (!pair) return;
      const l = pair.left ?? '';
      const r = pair.right ?? '';
      lines.push(`${i + 1}. ${l} — ${r}`);
    });
  }

  if (typeof p.source_ref === 'string' && p.source_ref) {
    lines.push(`(За текстом: ${p.source_ref})`);
  }

  return lines.filter(Boolean).join('\n');
}

/** Render the answer_key field as plain text (teacher-only). */
function renderAnswerKey(answerKey: any): string {
  if (answerKey == null || answerKey === '') return '';
  if (typeof answerKey === 'string') return answerKey.trim();
  try {
    return JSON.stringify(answerKey, null, 2).trim();
  } catch {
    return '';
  }
}

/**
 * Render a lesson as plain text for the clipboard.
 * Layout: title + duration header → anchor text → phase headers with planned tasks
 * (mode !== 'вдома') → reserve/homework section (mode === 'вдома') → in teacher mode
 * each task carries its «Ключ відповіді», note, and provenance appended inline.
 */
export function formatLessonForClipboard(
  lesson: ClipboardLesson,
  opts: { mode: ClipboardMode },
): string {
  const isTeacher = opts.mode === 'teacher';
  const out: string[] = [];

  out.push(lesson.title || 'Урок');
  out.push(`Тривалість: ≈ ${lesson.duration} хв`);
  if (lesson.focus) out.push(`Фокус: ${lesson.focus}`);
  out.push('');

  const anchorText = lesson.anchor?.text;
  if (anchorText && anchorText.trim()) {
    out.push('ТЕКСТ ДЛЯ ЧИТАННЯ');
    out.push(anchorText.trim());
    out.push('');
  }

  // Split blocks: planned (in-class) vs reserve/homework (mode === 'вдома').
  const planned = [1, 2, 3].map((ph) => ({
    phase: ph as 1 | 2 | 3,
    blocks: lesson.blocks.filter((b) => b.phase === ph && b.mode !== HOMEWORK_MODE),
  }));
  const reserve = lesson.blocks.filter((b) => b.mode === HOMEWORK_MODE);

  const renderBlock = (b: ClipboardLessonBlock): string[] => {
    const body = renderActivityBody(b.activity);
    const header = `• [${b.type}]`;
    const block: string[] = [header];
    if (body) block.push(body);
    if (isTeacher) {
      const key = renderAnswerKey(b.answer_key);
      if (key) {
        block.push(`Ключ відповіді: ${key}`);
      }
      if (b.note) block.push(`Примітка: ${b.note}`);
      if (b.provenance) {
        const src = b.provenance.source ?? '';
        const gen = b.provenance.generator ?? '';
        if (src || gen) block.push(`Походження: ${src} • ${gen}`);
      }
    }
    return block;
  };

  for (const grp of planned) {
    if (!grp.blocks.length) continue;
    out.push(`Фаза ${grp.phase}`);
    for (const b of grp.blocks) {
      out.push(...renderBlock(b));
      out.push('');
    }
  }

  if (reserve.length) {
    out.push('РЕЗЕРВ / ДОМАШНЄ ЗАВДАННЯ');
    for (const b of reserve) {
      out.push(...renderBlock(b));
      out.push('');
    }
  }

  return out.join('\n').trim() + '\n';
}
