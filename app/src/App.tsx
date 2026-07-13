import { useEffect, useState, useCallback, useRef } from 'react';
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';
// Import the kit styles (resolved via package subpath export)
import '@learn-ukrainian/activity-kit/styles.css';
import './teacher.css';
import {
  statusLabel,
  bakeStatusSubline,
  saveLastBakeRequest,
  loadLastBakeRequest,
  clearLastBakeRequest,
  type BakeRequestPayload,
} from './app-helpers';
import Conductor from './Conductor';

// ===== Types derived from openapi + lesson contract =====
type LessonState = 'draft' | 'baking' | 'ready' | 'failed';
type PilotActivityType =
  | 'true-false' | 'cloze' | 'match-up' | 'quiz' | 'mark-the-words'
  | 'fill-in' | 'error-correction' | 'text-questions' | 'short-writing';

interface Session {
  teacher: { id: string; display_name: string };
  expires_at: string;
  csrf_token: string;
}

interface ErrorEnvelope {
  code: string;
  message: string;
  retryable: boolean;
  lesson_id?: string;
}

interface LessonBlock {
  id: string;
  phase: 1 | 2 | 3;
  type: PilotActivityType;
  mode: string;
  activity: any; // LuActivityV1 shape from kit
  answer_key: string | object | null;
  mark: 'ok' | 'warn';
  note: string | null;
  edited: boolean;
  provenance: any;
}

interface LessonDocument {
  schema: 'lu.lesson.v1';
  id: string;
  title: string;
  level: 'B1';
  method: 'ttt';
  focus: string | null;
  anchor: { text: string; source: 'teacher-paste'; chars: number };
  duration: 45 | 60 | 90;
  version: 1;
  status: LessonState;
  last_error: string | null;
  accepted: boolean;
  blocks: LessonBlock[];
  rejected: any[];
  created_at: string;
  updated_at: string;
}

interface LessonResource {
  lesson_id: string;
  revision: number;
  accepted_at: string | null;
  accepted_revision: number | null;
  warning_acknowledgements: string[];
  lesson: LessonDocument;
}

interface LessonCatalogItem {
  id: string;
  title: string | null;
  status: LessonState;
  duration: number;
  focus: string | null;
  revision: number;
  accepted: boolean;
  accepted_at: string | null;
  accepted_revision: number | null;
  failure_code: string | null;
  created_at: string;
  updated_at: string;
}

const API_BASE = ''; // same-origin in prod; in dev we proxy or use full for stub via fetch override in E2E
const STUB_BASE = 'http://localhost:8787'; // used when ?useStub or in dev e2e

function getApiBase() {
  // For local dev: use relative /api (vite dev server proxies /api -> stub on 8787).
  // ?stub=1 forces direct to stub (rare, for standalone).
  if (typeof window !== 'undefined' && window.location.search.includes('stub=1')) {
    return STUB_BASE;
  }
  return API_BASE; // '' or proxied in dev
}

function isValidToken(t: string): boolean {
  return /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/.test(t);
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const base = getApiBase();
  const url = base ? `${base}${path}` : path;
  const headers = new Headers(init?.headers || {});
  // Always send Origin for mutations (contract)
  if (!headers.has('Origin')) {
    headers.set('Origin', window.location.origin);
  }
  return fetch(url, { ...init, headers, credentials: 'include' });
}

// ===== Simple SPA router (lightweight, no extra deps) =====
type View = 'invite' | 'paste' | 'catalog' | 'lesson';

interface RouteState {
  view: View;
  lessonId?: string;
  mode?: 'review' | 'run' | 'conduct';
}

function useSimpleRouter() {
  const [route, setRoute] = useState<RouteState>({ view: 'invite' });

  const navigate = useCallback((r: RouteState) => {
    const hash = r.lessonId ? `#/lessons/${r.lessonId}${r.mode ? '?mode=' + r.mode : ''}` : '#/';
    history.pushState(null, '', hash);
    setRoute(r);
  }, []);

  useEffect(() => {
    const parse = () => {
      const h = window.location.hash || '#/';
      if (h.startsWith('#/lessons/')) {
        const id = h.split('/')[2]?.split('?')[0];
        const params = new URLSearchParams(h.split('?')[1] || '');
        setRoute({ view: 'lesson', lessonId: id, mode: (params.get('mode') as any) || 'review' });
      } else {
        setRoute({ view: 'paste' });
      }
    };
    parse();
    const onPop = () => parse();
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  return { route, navigate };
}

// ===== Main App =====
export default function TeacherApp() {
  const { route, navigate } = useSimpleRouter();

  const [session, setSession] = useState<Session | null>(null);
  const [csrf, setCsrf] = useState<string | null>(null); // kept in memory only
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Paste form state (level fixed B1)
  const [pasteText, setPasteText] = useState('');
  const [duration, setDuration] = useState<45 | 60 | 90>(60);
  const [focus, setFocus] = useState('');

  // Baking / lesson state
  const [currentLessonId, setCurrentLessonId] = useState<string | null>(null);
  const [lesson, setLesson] = useState<LessonResource | null>(null);
  const [catalog, setCatalog] = useState<LessonCatalogItem[]>([]);
  const [bakeStatus, setBakeStatus] = useState<{ status: LessonState; step: string; failure?: string } | null>(null);
  const [polling, setPolling] = useState(false);

  // Per-lesson local acks (until server confirms)
  const [localAcks, setLocalAcks] = useState<string[]>([]);

  // Session ready gate (#93 item 1): single "session ready" state/promise proxy.
  // Gate loads; when ready, (re)run pending route action. UA loading instead of blank.
  const [sessionReady, setSessionReady] = useState(false);
  const [initLoading, setInitLoading] = useState(true);

  // Poll ref (prepared; used robust in item 3)
  const pollTimerRef = useRef<number | null>(null);

  // Last bake request: sessionStorage (+ in-memory fallback when storage unavailable).
  // Recovery path: API status does not expose anchor on failed lessons — see app-helpers.
  const lastBakeRef = useRef<BakeRequestPayload | null>(null);

  const [showAnswers, setShowAnswers] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const [restoredTextNotice, setRestoredTextNotice] = useState(false);

  const clearPoll = useCallback(() => {
    if (pollTimerRef.current != null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setPolling(false);
  }, []);

  /** Reset all lesson/session-scoped client state (logout + invite redeem boundaries). */
  const resetSessionScopedState = useCallback(() => {
    clearPoll();
    clearLastBakeRequest();
    lastBakeRef.current = null;
    setPasteText('');
    setDuration(60);
    setFocus('');
    setCurrentLessonId(null);
    setLesson(null);
    setCatalog([]);
    setBakeStatus(null);
    setLocalAcks([]);
    setShowAnswers(true);
    setError(null);
    setRestoredTextNotice(false);
  }, [clearPoll]);

  const restoreFormFromPayload = useCallback((payload: BakeRequestPayload) => {
    setPasteText(payload.text);
    setDuration(payload.duration);
    setFocus(payload.focus || '');
    setRestoredTextNotice(true);
  }, []);

  // Invite redemption (token only in memory)
  const redeemFromFragment = useCallback(async () => {
    const hash = window.location.hash || '';
    const m = hash.match(/invite=([A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]+)/);
    if (!m) return false;
    const token = m[1];
    // scrub immediately in finally regardless of outcome
    try {
      setLoading(true);
      setError(null);
      const res = await apiFetch('/api/session/redeem', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });
      if (res.status === 200) {
        resetSessionScopedState();
        const data = await res.json();
        const s: Session = { teacher: data.teacher, expires_at: data.expires_at, csrf_token: data.csrf_token };
        setSession(s);
        setCsrf(data.csrf_token);
        setSessionReady(true);
        navigate({ view: 'paste' });
        // Fetch full session for display
        await refreshSession();
        // Load catalog so paste view is fully populated (used by some flows)
        try { await loadCatalog(); } catch {}
      } else if (res.status === 410) {
        const e: ErrorEnvelope = await res.json();
        setError(e.message || 'Запрошення більше недоступне.');
      } else if (res.status === 422) {
        setError('Некоректний токен запрошення.');
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({ code: 'error', message: 'Помилка входу', retryable: false }));
        setError(e.message);
      }
    } finally {
      // SCRUB — token never stays in URL (contract)
      try {
        history.replaceState(null, '', '/teacher/');
      } catch {}
      setLoading(false);
    }
    return true;
  }, [resetSessionScopedState, navigate]);

  const refreshSession = async () => {
    const res = await apiFetch('/api/session');
    if (res.ok) {
      const data = await res.json();
      const s: Session = { teacher: data.teacher, expires_at: data.expires_at, csrf_token: data.csrf_token };
      setSession(s);
      setCsrf(data.csrf_token);
      setSessionReady(true);
      return s;
    } else if (res.status === 401) {
      setSession(null);
      setCsrf(null);
      setSessionReady(false);
      resetSessionScopedState();
    }
    return null;
  };

  // On mount (and on hashchange for invite fragment): try redeem from fragment.
  // This ensures E2E direct-goto with hash (or late hash set) triggers redeem without requiring full reload.
  // #93 item1: drive sessionReady; UA «Завантаження…» during init, never blank.
  useEffect(() => {
    let cancelled = false;
    const tryRedeemIfInvite = async () => {
      const h = window.location.hash || '';
      if (h.includes('invite=')) {
        await redeemFromFragment();
      }
    };
    (async () => {
      setInitLoading(true);
      setError(null);
      try {
        const h = window.location.hash || '';
        let did = false;
        if (h.includes('invite=')) {
          did = await redeemFromFragment();
        }
        if (!did && !cancelled) {
          const s = await refreshSession();
          if (s) {
            did = true;
          }
        }
      } catch (e) {
        if (!cancelled) {
          setError('Помилка ініціалізації сесії. Спробуйте перезавантажити сторінку.');
        }
      } finally {
        if (!cancelled) setInitLoading(false);
      }
    })();
    const onHash = () => { tryRedeemIfInvite(); };
    window.addEventListener('hashchange', onHash);
    return () => {
      cancelled = true;
      window.removeEventListener('hashchange', onHash);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // #93 item 1: gate route action on sessionReady. When resolves, re-run openLesson for direct-link/refresh.
  // Prevents empty page (route lesson but no currentLessonId/lesson rendered).
  useEffect(() => {
    if (!sessionReady) return;
    if (route.view === 'lesson' && route.lessonId) {
      if (!currentLessonId) {
        setCurrentLessonId(route.lessonId);
      }
      if (currentLessonId !== route.lessonId || !lesson) {
        openLesson(route.lessonId, (route.mode as 'review' | 'run' | 'conduct') || 'review').catch(() => {});
      }
    } else if (route.view !== 'lesson') {
      // #93 item 2: auto-load catalog once session ready (keep manual «Оновити список»)
      loadCatalog().catch(() => {});
    }
  }, [sessionReady, route.view, route.lessonId, route.mode]);

  // #93 item 3 cleanup: never orphan poll loops
  useEffect(() => {
    return () => { clearPoll(); };
  }, [clearPoll]);
  useEffect(() => { clearPoll(); }, [currentLessonId, clearPoll]);

  const logout = async () => {
    if (!csrf) return;
    await apiFetch('/api/session', {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).catch(() => {});
    setSession(null);
    setCsrf(null);
    setSessionReady(false);
    resetSessionScopedState();
    navigate({ view: 'invite' });
  };

  // ===== Paste + Bake =====
  const disclose = 'Вставлений текст буде надіслано зовнішньому провайдеру (Gemma). Не використовуйте чутливі або персональні дані.';

  const submitNewLesson = async (source: {
    text: string;
    duration: 45 | 60 | 90;
    focus: string | null;
  }) => {
    if (!session || !csrf) {
      setError('Потрібна сесія викладача.');
      return;
    }
    const text = source.text.trim();
    if (!text || text.length > 100000) {
      setError('Текст має бути від 1 до 100000 символів.');
      return;
    }
    setError(null);
    setLoading(true);
    setLesson(null);
    const id = crypto.randomUUID();
    const payload: BakeRequestPayload = {
      text,
      duration: source.duration,
      focus: source.focus?.trim() || '',
      lessonId: id,
    };
    try {
      const res = await apiFetch('/api/lessons', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf,
        },
        body: JSON.stringify({
          id,
          anchor: { text, source: 'teacher-paste' },
          level: 'B1',
          duration: source.duration,
          focus: source.focus?.trim() || null,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 202) {
        saveLastBakeRequest(payload);
        lastBakeRef.current = payload;
        setPasteText(text);
        setDuration(source.duration);
        setFocus(source.focus?.trim() || '');
        setCurrentLessonId(id);
        setBakeStatus({ status: data.status || 'baking', step: 'текст отримано' });
        navigate({ view: 'lesson', lessonId: id, mode: 'review' });
        pollStatus(id);
      } else {
        const e: ErrorEnvelope = data;
        setError(e.message || 'Не вдалося скласти урок. Спробуйте, будь ласка, ще раз.');
      }
    } catch {
      setError('Не вдалося скласти урок. Спробуйте, будь ласка, ще раз.');
    } finally {
      setLoading(false);
    }
  };

  const startBake = async () => {
    await submitNewLesson({ text: pasteText, duration, focus: focus || null });
  };

  const getRetrySource = (lessonId: string | null): BakeRequestPayload | null => {
    return loadLastBakeRequest(lessonId) ?? lastBakeRef.current;
  };

  const retryFailedLesson = async () => {
    const lid = currentLessonId || route.lessonId || null;
    const stored = getRetrySource(lid);
    if (!stored) {
      setError('Текст уроку недоступний. Вставте текст знову на головній сторінці.');
      return;
    }
    await submitNewLesson({
      text: stored.text,
      duration: stored.duration,
      focus: stored.focus || null,
    });
  };

  const copyLessonAsNew = async () => {
    if (!lesson) return;
    restoreFormFromPayload({
      text: lesson.lesson.anchor.text,
      duration: lesson.lesson.duration,
      focus: lesson.lesson.focus || '',
      lessonId: lesson.lesson_id,
    });
    navigate({ view: 'paste' });
  };

  // #93 item 3: robust poll
  // - update step on every tick (no stale)
  // - converge on ready/failed
  // - resume after refresh if baking (via 409 + route effect)
  // - cleanup on unmount/lesson switch
  const pollStatus = (id: string) => {
    clearPoll();
    setPolling(true);
    let attempts = 0;
    // Real bakes run ~4 min and provider hiccups stretch them further: poll
    // fast for the first ~25 s, then back off to 2.5 s ticks for ~10 min
    // total before surfacing the long-wait hint.
    const max = 260;
    const schedule = (delay: number) => {
      pollTimerRef.current = window.setTimeout(tick, delay);
    };
    const tick = async () => {
      attempts++;
      let st: any = null;
      try {
        const res = await apiFetch(`/api/lessons/${id}/status`);
        st = await res.json();
        setBakeStatus({ status: st.status, step: st.step || '', failure: st.failure_message || undefined });
        if (st.status === 'ready') {
          clearPoll();
          await openLesson(id, 'review');
          await loadCatalog();
          return;
        }
        if (st.status === 'failed') {
          clearPoll();
          return;
        }
      } catch {
        setBakeStatus((prev) => prev || { status: 'baking', step: 'оновлення…' });
      }
      if (attempts < max) {
        schedule(attempts < 30 ? 800 : 2500);
      } else {
        clearPoll();
        if (st && st.status !== 'ready' && st.status !== 'failed') {
          setError('Тривале очікування. Спробуйте оновити сторінку.');
        }
      }
    };
    schedule(400);
  };

  // ===== Lesson load + modes =====
  const openLesson = async (id: string, mode: 'review' | 'run' | 'conduct' = 'review') => {
    // #93 item1: no silent return on !session. Gate was the race; callers use sessionReady.
    if (!session || !csrf) {
      const s = await refreshSession();
      if (!s) {
        setError('Потрібна сесія викладача.');
        return;
      }
    }
    setLoading(true);
    setError(null);
    setLocalAcks([]);
    try {
      const res = await apiFetch(`/api/lessons/${id}`);
      if (res.status === 200) {
        const lr: LessonResource = await res.json();
        setLesson(lr);
        setCurrentLessonId(id);
        setLocalAcks(lr.warning_acknowledgements || []);
        navigate({ view: 'lesson', lessonId: id, mode });
      } else if (res.status === 409) {
        const e: ErrorEnvelope = await res.json();
        setBakeStatus({ status: 'baking', step: e.message || 'завдання складено' });
        setCurrentLessonId(id);
        pollStatus(id); // resume/ start poll for baking lesson (refresh case)
      } else if (res.status === 404) {
        setError('Урок не знайдено.');
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({ code: '', message: 'Помилка' }));
        handleApiError(e);
      }
    } catch {
      setError('Не вдалося завантажити урок. Спробуйте, будь ласка, ще раз.');
    } finally {
      setLoading(false);
    }
  };

  const loadCatalog = async () => {
    // #93 item 2: auto-loaded once session ready; guard removed, manual button remains
    const res = await apiFetch('/api/lessons');
    if (res.ok) {
      const data = await res.json();
      setCatalog(data.lessons || []);
    }
  };

  // ===== Warning ack (REVIEW only) =====
  const ackWarning = async (blockId: string) => {
    if (!lesson || !currentLessonId || !csrf) return;
    const expected = lesson.revision;
    setLoading(true);
    try {
      const res = await apiFetch(`/api/lessons/${currentLessonId}/blocks/${blockId}/accept`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ expected_revision: expected }),
      });
      if (res.ok) {
        await res.json();
        // reload to get fresh
        await openLesson(currentLessonId, 'review');
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({} as any));
        if (e.code === 'revision_conflict') {
          setError('Урок змінився. Перезавантажте.');
          await openLesson(currentLessonId, 'review');
        } else {
          setError(e.message || 'Не вдалося підтвердити.');
        }
      }
    } finally {
      setLoading(false);
    }
  };

  const allVisibleWarningsAcked = (l: LessonResource | null) => {
    if (!l) return true;
    const warns = l.lesson.blocks.filter(b => b.mark === 'warn').map(b => b.id);
    const acks = [...(l.warning_acknowledgements || []), ...localAcks];
    return warns.every(w => acks.includes(w));
  };

  // ===== Accept / Draft =====
  const acceptLesson = async () => {
    if (!lesson || !currentLessonId || !csrf) return;
    if (!allVisibleWarningsAcked(lesson)) {
      setError('Підтвердіть усі попередження перед прийняттям уроку.');
      return;
    }
    setLoading(true);
    try {
      const res = await apiFetch(`/api/lessons/${currentLessonId}/accept`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ expected_revision: lesson.revision }),
      });
      if (res.ok) {
        const lr: LessonResource = await res.json();
        setLesson(lr);
        await loadCatalog();
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({} as any));
        if (e.code === 'warning_acknowledgements_required') {
          setError('Підтвердіть усі попередження перед прийняттям.');
        } else if (e.code === 'revision_conflict') {
          setError('Урок змінився — перезавантажте.');
          await openLesson(currentLessonId, 'review');
        } else {
          setError(e.message || 'Не вдалося прийняти.');
        }
      }
    } finally {
      setLoading(false);
    }
  };

  const returnToDraft = async () => {
    if (!lesson || !currentLessonId || !csrf) return;
    setLoading(true);
    try {
      const res = await apiFetch(`/api/lessons/${currentLessonId}/draft`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ expected_revision: lesson.revision }),
      });
      if (res.ok) {
        const lr: LessonResource = await res.json();
        setLesson(lr);
        await loadCatalog();
      } else {
        const e = await res.json().catch(() => ({}));
        setError((e as any).message || 'Не вдалося повернути в чернетку.');
      }
    } finally { setLoading(false); }
  };

  // Download accepted JSON
  const downloadJSON = () => {
    if (!lesson) return;
    const blob = new Blob([JSON.stringify(lesson.lesson, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `hramatka-lesson-${lesson.lesson_id}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // Browser print (with stylesheet)
  const printLesson = () => {
    window.print();
  };

  function handleApiError(e: ErrorEnvelope) {
    if (e.code === 'session_required') {
      setSession(null);
      setCsrf(null);
      setSessionReady(false);
      resetSessionScopedState();
      setError('Сесія закінчилась. Увійдіть знову.');
      navigate({ view: 'invite' });
    } else {
      setError(e.message || 'Помилка.');
    }
  }

  // Render blocks grouped by phase using REAL ActivityPlayer
  const renderBlocks = (l: LessonResource, viewMode: 'review' | 'run' | 'conduct') => {
    const phases: Record<number, LessonBlock[]> = { 1: [], 2: [], 3: [] };
    l.lesson.blocks.forEach(b => {
      if (phases[b.phase]) phases[b.phase].push(b);
    });

    return [1, 2, 3].map(phase => {
      const bs = phases[phase];
      if (!bs.length) return null;
      return (
        <section key={phase} className="phase">
          <h3>Фаза {phase}</h3>
          {bs.map(block => {
            const isWarn = block.mark === 'warn';
            const acked = (l.warning_acknowledgements || []).includes(block.id) || localAcks.includes(block.id);
            const showKey = viewMode === 'review' && showAnswers;
            const typeChip = isWarn ? 'warn' : 'info';
            return (
              <div key={block.id} className={`block ${isWarn ? 'warn' : 'ok'} ${block.edited ? 'edited' : ''}`}>
                <div className="block-meta">
                  <span className={`chip ${typeChip}`}>{block.type}</span>
                  <span className="mode">{block.mode}</span>
                  {isWarn && <span className="warn-badge">⚠️ попередження</span>}
                  {block.provenance?.external_options && <span className="prov">зовнішні варіанти</span>}
                </div>

                {/* REAL WIDGET — zero fallback (container styled only; kit kept untouched) */}
                <div className="activity-wrapper" data-activity-type={block.type}>
                  <ActivityPlayer
                    activity={block.activity}
                    isUkrainian={true}
                    // onComplete omitted for teacher review/run
                  />
                </div>

                {showKey && (
                  <div className="teacher-key">
                    <strong>Ключ відповіді:</strong>
                    <pre>{typeof block.answer_key === 'string' ? block.answer_key : JSON.stringify(block.answer_key, null, 2)}</pre>
                    {block.note && <div className="note">Примітка: {block.note}</div>}
                    {block.provenance && (
                      <div className="prov-detail">Походження: {block.provenance.source} • {block.provenance.generator}</div>
                    )}
                  </div>
                )}

                {viewMode === 'review' && isWarn && !acked && (
                  <button
                    className="ack-btn btn"
                    onClick={() => ackWarning(block.id)}
                    disabled={loading}
                  >
                    Підтвердити попередження
                  </button>
                )}
                {viewMode === 'review' && isWarn && acked && (
                  <span className="acked">✓ Підтверджено</span>
                )}
              </div>
            );
          })}
        </section>
      );
    });
  };

  // ===== Render =====
  const currentMode = (route.mode as 'review' | 'run' | 'conduct') || 'review';

  return (
    <div className="teacher-app">
      <header className="appbar">
        <div className="brand">Граматка</div>
        {session && (
          <div className="session">
            <button
              type="button"
              className="helpbtn"
              onClick={() => setHelpOpen(true)}
              title="Довідка"
              aria-label="Довідка"
            >
              ?
            </button>
            <span>{session.teacher.display_name}</span>
            <button onClick={logout} className="link">Вийти</button>
          </div>
        )}
      </header>

      {helpOpen && (
        <div
          className="help-overlay"
          role="dialog"
          aria-labelledby="help-title"
          onClick={(e) => { if (e.target === e.currentTarget) setHelpOpen(false); }}
        >
          <div className="help-card">
            <h2 id="help-title">Як користуватися «Граматкою»</h2>
            <p><strong>Створення уроку.</strong> Вставте український текст, оберіть тривалість і натисніть «Згенерувати урок». Рівень B1 фіксований для пілоту.</p>
            <p><strong>Скільки чекати.</strong> Генерація зазвичай триває кілька хвилин. Можна повернутися до списку — урок з’явиться, коли буде готовий.</p>
            <p><strong>«Перевірте».</strong> Попередження означає, що завдання варто переглянути. Підтвердіть кожне перед прийняттям уроку.</p>
            <p><strong>Якщо сталася помилка.</strong> Текст зберігається — натисніть «Створити урок ще раз із цим текстом» або поверніться до списку.</p>
            <div className="help-actions">
              <button type="button" className="btn primary" onClick={() => setHelpOpen(false)}>Зрозуміло</button>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="banner fail error" role="alert">
          <span className="ic">!</span>
          <span>{error}</span>
          <button onClick={() => setError(null)} style={{marginLeft:'auto',fontSize:13,opacity:.7}}>✕</button>
        </div>
      )}

      {/* #93 item1: UA loading state (never blank page) */}
      {initLoading && (
        <main style={{ padding: 40, color: 'var(--ink-soft)' }}>Завантаження…</main>
      )}

      {!session && !initLoading && (
        <main className="invite">
          <h1>Вхід для викладача</h1>
          <p>Використайте посилання-запрошення. Токен обробляється лише в пам’яті.</p>
          <button
            className="btn primary"
            onClick={async () => {
              // Dev helper: allow manual redeem with test token
              const t = prompt('Тестовий токен (або залиште порожнім для автоматичного):') || 'TEST' + 'A'.repeat(39) + 'Q';
              if (!isValidToken(t)) { setError('Некоректний формат токена.'); return; }
              window.location.hash = `#invite=${t}`;
              await redeemFromFragment();
            }}
            disabled={loading}
          >
            Увійти за тестовим запрошенням (тест)
          </button>
          <p className="small">У реальному сценарії — відкрийте посилання з #invite=...</p>
        </main>
      )}

      {session && (
        <>
          {/* Paste / Hub */}
          {route.view !== 'lesson' && (
            <main className="hub">
              <section className="paste">
                <h2>Створити новий урок</h2>
                <div className="banner honest"><span className="ic">ℹ︎</span><span>{disclose}</span></div>

                <div className="formgrid">
                  <div className="field">
                    <label>Рівень: <strong>B1</strong> (фіксовано для пілоту)</label>
                  </div>

                  <div className="field">
                    <label>Тривалість (хв)</label>
                    <select className="inputbox" value={duration} onChange={e => setDuration(Number(e.target.value) as any)}>
                      <option value={45}>45</option>
                      <option value={60}>60</option>
                      <option value={90}>90</option>
                    </select>
                  </div>

                  <div className="field">
                    <label>Фокус (необов’язково)</label>
                    <input
                      className="inputbox"
                      type="text"
                      value={focus}
                      onChange={e => setFocus(e.target.value)}
                      placeholder="напр. вищий ступінь прикметників"
                      maxLength={500}
                    />
                  </div>

                  <div className="field">
                    <label>Текст для уроку (вставте)</label>
                    {restoredTextNotice && (
                      <div
                        className="banner honest restored-notice"
                        role="status"
                        data-testid="restored-text-notice"
                      >
                        <span className="ic">ℹ︎</span>
                        <span>Текст попереднього запиту відновлено</span>
                        <button
                          type="button"
                          onClick={() => setRestoredTextNotice(false)}
                          style={{ marginLeft: 'auto', fontSize: 13, opacity: 0.7 }}
                          aria-label="Закрити"
                        >
                          ✕
                        </button>
                      </div>
                    )}
                    <textarea
                      className="inputbox"
                      value={pasteText}
                      onChange={e => setPasteText(e.target.value)}
                      rows={8}
                      placeholder="Вставте український текст..."
                    />
                  </div>

                  <button className="btn primary" onClick={startBake} disabled={loading || !pasteText.trim()}>
                    {loading ? 'Надсилаємо…' : 'Згенерувати урок'}
                  </button>
                </div>
              </section>

              <section className="catalog">
                <h2>Ваші уроки</h2>
                <button className="btn ghost" onClick={loadCatalog}>Оновити список</button>
                {catalog.length === 0 && <p className="hint">Поки немає уроків.</p>}
                <ul>
                  {catalog.map(item => {
                    const st = item.status;
                    const chipClass = st === 'ready' ? 'ok' : st === 'baking' ? 'info' : st === 'failed' ? 'bad' : 'muted';
                    return (
                      <li key={item.id} className="listrow">
                        <button onClick={() => openLesson(item.id, 'review')}>
                          <span className="title">{item.title || item.id.slice(0, 8)}</span>
                          <span className={`chip ${chipClass}`}>{statusLabel(item.status)}</span>
                          {item.accepted && <span className="chip ok">Прийнято</span>}
                        </button>
                        <span className="when">{new Date(item.updated_at).toLocaleString('uk')}</span>
                      </li>
                    );
                  })}
                </ul>
              </section>
            </main>
          )}

          {/* Lesson view */}
          {/* #93 item1: render on route.lessonId to avoid blank on direct/refresh; set current early via ready effect */}
          {route.view === 'lesson' && route.lessonId && (
            <main className="lesson-view">
              <div className="lesson-header">
                <button className="btn ghost" onClick={() => navigate({ view: 'paste' })}>← До списку</button>
                {lesson && <h2>{lesson.lesson.title}</h2>}
                <div className="lesson-actions">
                  <button className="btn ghost" onClick={() => openLesson(currentLessonId || route.lessonId!, 'review')} disabled={currentMode === 'review'}>Режим огляду</button>
                  <button className="btn ghost" onClick={() => openLesson(currentLessonId || route.lessonId!, 'run')} disabled={currentMode === 'run'}>Режим запуску (для учня)</button>
                  {lesson && currentMode === 'review' && lesson.lesson.status === 'ready' && (
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() => setShowAnswers(v => !v)}
                    >
                      {showAnswers ? 'Сховати відповіді' : 'Показати відповіді'}
                    </button>
                  )}
                  {lesson && lesson.lesson.status === 'ready' && (
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={copyLessonAsNew}
                      disabled={loading}
                    >
                      Копіювати урок
                    </button>
                  )}
                  {lesson && lesson.lesson.accepted && (
                    <button className="btn primary" onClick={() => openLesson(currentLessonId || route.lessonId!, 'conduct')} disabled={currentMode === 'conduct'}>▶ Провести заняття</button>
                  )}
                  {lesson && lesson.lesson.accepted && <span className="chip ok">Прийнято</span>}
                  <button className="btn ghost" onClick={printLesson}>Друк</button>
                  <button className="btn ghost" onClick={downloadJSON} disabled={!lesson || !lesson.lesson.accepted}>Завантажити JSON</button>
                </div>
              </div>

              {bakeStatus && !lesson && (
                <div className="baking">
                  <div className="steps">
                    <div className={`step ${bakeStatus.status === 'baking' ? 'now' : bakeStatus.status === 'failed' ? 'fail' : 'done'}`}>
                      <div className="dot">{bakeStatus.status === 'baking' ? '⋯' : bakeStatus.status === 'failed' ? '!' : '✓'}</div>
                      <div>
                        <b>Статус: {statusLabel(bakeStatus.status)}</b>
                        <div className="sd">{bakeStatusSubline(bakeStatus.status, bakeStatus.step, bakeStatus.failure)}</div>
                      </div>
                    </div>
                  </div>
                  {bakeStatus.status === 'failed' && (
                    <div className="recovery-card banner fail" data-testid="failure-recovery">
                      <span className="ic">!</span>
                      <div className="recovery-body">
                        <p>Не вдалося створити урок. Таке інколи трапляється, коли сервіс перевантажений. Спробуйте ще раз — текст уже збережено.</p>
                        <div className="recovery-actions">
                          <button
                            type="button"
                            className="btn primary"
                            onClick={retryFailedLesson}
                            disabled={loading || !getRetrySource(currentLessonId || route.lessonId || null)}
                          >
                            Створити урок ще раз із цим текстом
                          </button>
                          <button
                            type="button"
                            className="btn ghost link-back"
                            onClick={() => {
                              const lid = currentLessonId || route.lessonId || null;
                              const stored = getRetrySource(lid);
                              if (stored) {
                                restoreFormFromPayload(stored);
                              }
                              navigate({ view: 'paste' });
                            }}
                          >
                            Повернутися до списку
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                  {bakeStatus.status === 'baking' && polling && <p className="hint" style={{marginTop:6}}>Оновлення…</p>}
                  {bakeStatus.status === 'baking' && (
                    <button className="btn ghost" style={{marginTop:8}} onClick={() => (currentLessonId || route.lessonId) && openLesson(currentLessonId || route.lessonId!)}>Перевірити зараз</button>
                  )}
                </div>
              )}

              {!lesson && !bakeStatus && (
                <p style={{ color: 'var(--ink-soft)', padding: '12px 0' }}>Завантаження…</p>
              )}

              {lesson && (
                <>
                  <div className="meta">
                    <div>Рівень: {lesson.lesson.level} • Тривалість: {lesson.lesson.duration} хв</div>
                    <div>Ревізія: {lesson.revision} • Прийнято: {lesson.lesson.accepted ? 'так' : 'ні'}</div>
                    {lesson.lesson.focus && <div>Фокус: {lesson.lesson.focus}</div>}
                  </div>

                  {currentMode !== 'conduct' && (
                    <div className={`modes ${currentMode}`}>
                      {renderBlocks(lesson, currentMode as 'review' | 'run')}
                    </div>
                  )}

                  {currentMode === 'review' && (
                    <div className="accept-bar">
                      {!allVisibleWarningsAcked(lesson) && (
                        <div className="warn-note">Потрібно підтвердити всі попередження (⚠️), щоб прийняти урок.</div>
                      )}
                      <button
                        className="btn primary"
                        onClick={acceptLesson}
                        disabled={loading || !allVisibleWarningsAcked(lesson) || lesson.lesson.accepted}
                      >
                        Прийняти урок (ревізія {lesson.revision})
                      </button>
                      <button className="btn ghost" onClick={returnToDraft} disabled={loading || !lesson.lesson.accepted}>
                        Повернути в чернетку
                      </button>
                    </div>
                  )}

                  {currentMode === 'run' && (
                    <div className="run-note">Це режим для демонстрації учню — відповіді та ключі приховані (залежно від віджета).</div>
                  )}

                  {currentMode === 'conduct' && lesson && (
                    <Conductor
                      lessonDoc={lesson.lesson}
                      onExit={() => openLesson(currentLessonId || route.lessonId!, 'review')}
                    />
                  )}
                </>
              )}
            </main>
          )}
        </>
      )}

      <footer className="footer">
        <small>Приватний пілот • Тільки для запрошених викладачів • B1</small>
      </footer>
    </div>
  );
}
