export type LessonState = 'draft' | 'baking' | 'ready' | 'failed';

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
