export type LessonState = 'draft' | 'baking' | 'ready' | 'failed';

export type BakeProgressStep = 'generation' | 'gates' | 'assembly' | null;

/** Optional status payload field (may be absent until engine PR lands). */
export interface BakeProgress {
  phase: number;
  phases_total: number;
  step: BakeProgressStep;
  calls_done: number | null;
  calls_planned: number | null;
  updated_at: string;
}

export interface BakeRequestPayload {
  text: string;
  duration: 45 | 60 | 90;
  focus: string;
  lessonId: string;
}

const LAST_BAKE_KEY = 'hramatka:last-bake-request';

/** UA status chip labels (demo-reference: «готово» not «готовий»). */
export function statusLabel(s: LessonState): string {
  if (s === 'baking') return 'готується';
  if (s === 'ready') return 'готово';
  if (s === 'failed') return 'помилка';
  if (s === 'draft') return 'чернетка';
  return s;
}

/** When failed, show only the failure message — never the last step label. */
export function bakeStatusSubline(
  status: LessonState,
  step: string,
  failure?: string,
): string {
  if (status === 'failed') {
    return failure || 'Не вдалося створити урок.';
  }
  return step || '';
}

const PROGRESS_STEP_UA: Record<Exclude<BakeProgressStep, null>, string> = {
  generation: 'створення завдань',
  gates: 'перевірка',
  assembly: 'збирання заняття',
};

/** Honest line when API exposes `progress` — never a fake percent. */
export function formatBakeProgressLine(progress: BakeProgress): string {
  const phase = Math.max(1, progress.phase);
  const total = Math.max(phase, progress.phases_total);
  const stepKey = progress.step;
  const stepLabel = stepKey ? PROGRESS_STEP_UA[stepKey] : 'приготування';
  let line = `Фаза ${phase} із ${total} — ${stepLabel}…`;
  if (
    progress.calls_done != null &&
    progress.calls_planned != null &&
    progress.calls_planned > 0
  ) {
    line += ` (${progress.calls_done} з ${progress.calls_planned})`;
  }
  return line;
}

/** Staged reassuring UA copy keyed to elapsed time when `progress` is absent. */
export function formatBakeElapsedFallback(elapsedMs: number): string {
  const min = elapsedMs / 60000;
  if (min < 2) {
    return 'Текст отримано — складаємо завдання з вашого тексту.';
  }
  if (min < 5) {
    return 'Генерація триває — це нормально. Зазвичай кілька хвилин.';
  }
  if (min < 15) {
    return 'Ще працюємо над завданнями. Можна повернутися до списку — ми продовжимо тут.';
  }
  return 'Це може тривати до пів години. Можна повернутися до «Моїх занять» — заняття дочекається вас.';
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
    const data = JSON.parse(raw) as BakeRequestPayload;
    if (!data?.text) return null;
    if (lessonId && data.lessonId && data.lessonId !== lessonId) return null;
    return data;
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

/** Resolve persisted teacher pref to a valid duration (preselect for new-lesson form). */
export function resolveDefaultDurationFromPref(pref: unknown): 45 | 60 | 90 {
  if (pref && typeof pref === 'object') {
    const d = (pref as { default_duration?: unknown }).default_duration;
    if (d === 45 || d === 60 || d === 90) return d;
  }
  return 60;
}
