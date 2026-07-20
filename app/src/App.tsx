import { useEffect, useState, useCallback, useRef } from 'react';
// Import the kit styles (resolved via package subpath export)
import '@learn-ukrainian/activity-kit/styles.css';
import './teacher.css';
import {
  bakeStatusSubline,
  formatBakeProgressLine,
  formatBakeElapsedFallback,
  formatBakeElapsedClock,
  saveLastBakeRequest,
  loadLastBakeRequest,
  clearLastBakeRequest,
  mergeCatalogLessons,
  loadLocalCatalogEntries,
  resolveDefaultDurationFromPref,
  formatLessonForClipboard,
  type BakeRequestPayload,
  type BakeProgress,
  type ClipboardMode,
} from './app-helpers';
import Conductor from './Conductor';
import { useT, statusKey, recoveryBodyKey, type ChromeKey } from './i18n';
import LessonBlocks from './LessonBlocks';
import ReviewWorkbench from './ReviewWorkbench';
import QualifiedModelPicker, {
  resolveQualifiedModelId,
  type QualifiedModelChoice,
} from './QualifiedModelPicker';
import {
  splitReviewBlocks,
  blockNeedsReview,
  focusStatusNeedsReview,
  FOCUS_STATUS_ACK_ID,
  type LessonDuration,
  type FocusStatus,
} from './review-helpers';

/**
 * The error banner holds either translated client chrome (`key`) or a raw server
 * message (`raw`, e.g. API `e.message` — content, stays as-is). Storing the key
 * (not a pre-rendered string) keeps the banner reversible when chrome is toggled.
 */
type AppError = { key: ChromeKey } | { raw: string } | null;
const errKey = (key: ChromeKey): { key: ChromeKey } => ({ key });
const errOr = (serverMsg: string | null | undefined, key: ChromeKey): AppError =>
  serverMsg ? { raw: serverMsg } : { key };

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
  // Absent (never an explicit null) when the teacher requested no focus.
  focus_status?: FocusStatus;
  anchor: { text: string; source: 'teacher-paste' | 'teacher-url'; chars: number; source_url?: string };
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
  logical_model_id: string | null;
  lesson: LessonDocument;
}

interface QualifiedModelList {
  registry_version: string;
  models: QualifiedModelChoice[];
  unavailable_message: string | null;
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
  const { t, lang, toggleLang, resetLang } = useT();

  const [session, setSession] = useState<Session | null>(null);
  const [csrf, setCsrf] = useState<string | null>(null); // kept in memory only
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<AppError>(null);

  // Paste form state (level fixed B1)
  const [anchorTab, setAnchorTab] = useState<'text' | 'url'>('text');
  const [pasteText, setPasteText] = useState('');
  const [urlInput, setUrlInput] = useState('');
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [fetchingUrl, setFetchingUrl] = useState(false);
  const [duration, setDuration] = useState<45 | 60 | 90>(60);
  const [focus, setFocus] = useState('');
  const [qualifiedModels, setQualifiedModels] = useState<QualifiedModelChoice[]>([]);
  const [selectedModelId, setSelectedModelId] = useState('');
  const [modelUnavailableMessage, setModelUnavailableMessage] = useState<string | null>(null);

  // Baking / lesson state
  const [currentLessonId, setCurrentLessonId] = useState<string | null>(null);
  const [lesson, setLesson] = useState<LessonResource | null>(null);
  const [catalog, setCatalog] = useState<LessonCatalogItem[]>([]);
  const [bakeStatus, setBakeStatus] = useState<{
    status: LessonState;
    step: string;
    failure?: string;
    failure_code?: string | null;
    progress?: BakeProgress;
    startedAt?: string;
  } | null>(null);
  const [polling, setPolling] = useState(false);
  const [bakeElapsedMs, setBakeElapsedMs] = useState(0);

  // Per-lesson local acks (until server confirms)
  const [localAcks, setLocalAcks] = useState<string[]>([]);

  // Session ready gate (#93 item 1): single "session ready" state/promise proxy.
  // Gate loads; when ready, (re)run pending route action. UA loading instead of blank.
  const [sessionReady, setSessionReady] = useState(false);
  const [initLoading, setInitLoading] = useState(true);

  // Poll ref (prepared; used robust in item 3)
  const pollTimerRef = useRef<number | null>(null);
  // Track the id we are actively polling for. Used to make clear-on-id-change + start
  // reliable when switching catalog rows (F2). Prevents late clear from killing a fresh poll.
  const activePollIdRef = useRef<string | null>(null);

  // Last bake request: sessionStorage (+ in-memory fallback when storage unavailable).
  // Recovery path: API status does not expose anchor on failed lessons — see app-helpers.
  const lastBakeRef = useRef<BakeRequestPayload | null>(null);

  const [showAnswers, setShowAnswers] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const [restoredTextNotice, setRestoredTextNotice] = useState(false);
  const [anchorOpen, setAnchorOpen] = useState(false);
  const [printVariant, setPrintVariant] = useState<'teacher' | 'student' | null>(null);
  const [conductStudentPreview, setConductStudentPreview] = useState(false);
  // Clipboard export notice (Sol P1-5 folded to i18n): stores key so t() reflects current lang.
  const [clipboardNotice, setClipboardNotice] = useState<{ kind: 'ok' | 'fail'; key: ChromeKey } | null>(null);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);

  const clearPoll = useCallback(() => {
    if (pollTimerRef.current != null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    activePollIdRef.current = null;
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
    setQualifiedModels([]);
    setSelectedModelId('');
    setModelUnavailableMessage(null);
    setCurrentLessonId(null);
    setLesson(null);
    setCatalog([]);
    setBakeStatus(null);
    setLocalAcks([]);
    setShowAnswers(true);
    setError(null);
    setRestoredTextNotice(false);
    setPendingDeleteId(null);
    setAnchorOpen(false);
    setPrintVariant(null);
    setBakeElapsedMs(0);
    resetLang(); // #106 boundary: UI language back to default UA + clear persisted choice
    setClipboardNotice(null);
  }, [clearPoll, resetLang]);

  const restoreFormFromPayload = useCallback((payload: BakeRequestPayload) => {
    setPasteText(payload.text);
    setDuration(payload.duration);
    setFocus(payload.focus || '');
    setSelectedModelId(resolveQualifiedModelId(qualifiedModels, payload.logicalModelId));
    setSourceUrl(payload.sourceUrl || null);
    setAnchorTab(payload.anchorSource === 'teacher-url' ? 'url' : 'text');
    setRestoredTextNotice(true);
  }, [qualifiedModels]);

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
        await loadTeacherDefaultDuration();
        await loadQualifiedModels();
        // Load catalog so paste view is fully populated (used by some flows)
        try { await loadCatalog(); } catch {}
      } else if (res.status === 410) {
        const e: ErrorEnvelope = await res.json();
        setError(errOr(e.message, 'err.inviteGone'));
      } else if (res.status === 422) {
        setError(errKey('err.inviteBadToken'));
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({ code: 'error', message: '', retryable: false }));
        setError(errOr(e.message, 'err.loginFailed'));
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

  const loadTeacherDefaultDuration = useCallback(async () => {
    // Silent load; preselects new-lesson duration from teacher-owned preference (P2-6).
    try {
      const res = await apiFetch('/api/teacher/preferences');
      if (res.ok) {
        const p = await res.json();
        setDuration(resolveDefaultDurationFromPref(p));
      }
    } catch {
      /* silent; keep current/60 */
    }
  }, []);

  const loadQualifiedModels = useCallback(async () => {
    try {
      const res = await apiFetch('/api/lesson-models');
      if (!res.ok) throw new Error('model list unavailable');
      const payload: QualifiedModelList = await res.json();
      const models = Array.isArray(payload.models) ? payload.models : [];
      setQualifiedModels(models);
      setModelUnavailableMessage(payload.unavailable_message || null);
      setSelectedModelId(previous => resolveQualifiedModelId(models, previous));
    } catch {
      // Availability is fail-closed: a failed list read never revives cached choices.
      setQualifiedModels([]);
      setSelectedModelId('');
      setModelUnavailableMessage(null);
    }
  }, []);

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
            await loadTeacherDefaultDuration();
            await loadQualifiedModels();
          }
        }
      } catch {
        if (!cancelled) {
          setError(errKey('err.initSession'));
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
      const haveContent = !!lesson && lesson.lesson_id === route.lessonId;
      const haveBakeForIt = !!bakeStatus && currentLessonId === route.lessonId;
      // F1/F2 stability: do not re-invoke openLesson (which would re-seed 'baking' on 409)
      // if we already have either lesson content or active bakeStatus for this exact id.
      // This prevents double 409 on catalog open of non-ready, and keeps failure card stable.
      if (currentLessonId !== route.lessonId || (!haveContent && !haveBakeForIt)) {
        openLesson(route.lessonId, (route.mode as 'review' | 'run' | 'conduct') || 'review').catch(() => {});
      }
    } else if (route.view !== 'lesson') {
      // #93 item 2: auto-load catalog once session ready (keep manual «Оновити список»)
      loadCatalog().catch(() => {});
    }
  }, [sessionReady, route.view, route.lessonId, route.mode, lesson, bakeStatus, currentLessonId]);

  // #93 item 3 cleanup: never orphan poll loops
  useEffect(() => {
    return () => { clearPoll(); };
  }, [clearPoll]);
  // F2 fix: only clear if the active poll id is *not* the one we just switched to.
  // The pollStatus(new) call (which sets activePollIdRef) happens before this effect runs,
  // so we avoid cancelling the freshly started poll for the target lesson when switching
  // catalog rows mid-bake. Still stops orphans from prior lessons.
  useEffect(() => {
    if (activePollIdRef.current && activePollIdRef.current !== currentLessonId) {
      clearPoll();
    }
  }, [currentLessonId, clearPoll]);

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
  const submitNewLesson = async (source: {
    text: string;
    duration: 45 | 60 | 90;
    focus: string | null;
    anchorSource: 'teacher-paste' | 'teacher-url';
    sourceUrl?: string | null;
    logicalModelId: string;
  }) => {
    if (!session || !csrf) {
      setError(errKey('err.sessionRequired'));
      return;
    }
    const text = source.text.trim();
    if (!text || text.length > 100000) {
      setError(errKey('err.badTextLen'));
      return;
    }
    if (source.anchorSource === 'teacher-url' && !source.sourceUrl) {
      setError(errKey('err.urlSourceRequired'));
      return;
    }
    if (!qualifiedModels.some(model => model.id === source.logicalModelId)) {
      setError(errKey('paste.modelUnavailable'));
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
      anchorSource: source.anchorSource,
      ...(source.sourceUrl ? { sourceUrl: source.sourceUrl } : {}),
      logicalModelId: source.logicalModelId,
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
          anchor: {
            text,
            source: source.anchorSource,
            ...(source.anchorSource === 'teacher-url' && source.sourceUrl
              ? { source_url: source.sourceUrl }
              : {}),
          },
          level: 'B1',
          duration: source.duration,
          focus: source.focus?.trim() || null,
          logical_model_id: source.logicalModelId,
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
        const startedAt = new Date().toISOString();
        setBakeStatus({ status: data.status || 'baking', step: 'bake.step.textReceived', startedAt });
        setBakeElapsedMs(0);
        navigate({ view: 'lesson', lessonId: id, mode: 'review' });
        pollStatus(id);
      } else {
        const e: ErrorEnvelope = data;
        setError(errOr(e.message, 'err.bakeFailed'));
      }
    } catch {
      setError(errKey('err.bakeFailed'));
    } finally {
      setLoading(false);
    }
  };

  const startBake = async () => {
    await submitNewLesson({
      text: pasteText,
      duration,
      focus: focus || null,
      anchorSource: sourceUrl ? 'teacher-url' : 'teacher-paste',
      sourceUrl,
      logicalModelId: selectedModelId,
    });
  };

  const fetchAnchorUrl = async () => {
    if (!session || !csrf) {
      setError(errKey('err.sessionRequired'));
      return;
    }
    const url = urlInput.trim();
    if (!url) {
      setError(errKey('err.urlNeedAddress'));
      return;
    }
    setError(null);
    setFetchingUrl(true);
    try {
      const res = await apiFetch('/api/anchor/import-url', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf,
        },
        body: JSON.stringify({ url }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        setPasteText(data.text || '');
        setSourceUrl(data.source_url || url);
        setAnchorTab('text');
      } else {
        const e: ErrorEnvelope = data;
        setError(errOr(e.message, 'err.urlFetchFailed'));
      }
    } catch {
      setError(errKey('err.urlFetchFailed'));
    } finally {
      setFetchingUrl(false);
    }
  };

  const getRetrySource = (lessonId: string | null): BakeRequestPayload | null => {
    return loadLastBakeRequest(lessonId) ?? lastBakeRef.current;
  };

  const beginRecreateFromServer = async (sourceLessonId: string, data: { id: string; status?: string }) => {
    const newId = data.id;
    setCurrentLessonId(newId);
    const startedAt = new Date().toISOString();
    setBakeStatus({
      status: (data.status as LessonState) || 'baking',
      step: 'bake.step.textReceived',
      startedAt,
    });
    setBakeElapsedMs(0);
    setLesson(null);
    navigate({ view: 'lesson', lessonId: newId, mode: 'review' });
    pollStatus(newId);
    void sourceLessonId;
  };

  const retryFailedLesson = async () => {
    const lid = currentLessonId || route.lessonId || null;
    if (!lid) return;
    if (!session || !csrf) {
      setError(errKey('err.sessionRequired'));
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const res = await apiFetch(`/api/lessons/${lid}/recreate`, {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf },
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 202) {
        await beginRecreateFromServer(lid, data);
        return;
      }
      if (res.status === 422) {
        // Server is the source of truth for the stored request. If it has none,
        // surface the server's Ukrainian error — do NOT recreate from client cache
        // (that would bypass the missing-request_json contract). (review #127)
        const e: ErrorEnvelope = data;
        setError(errOr(e.message, 'err.noRetrySource'));
        return;
      }
      const e: ErrorEnvelope = data;
      handleApiError(e);
    } catch {
      setError(errKey('err.bakeFailed'));
    } finally {
      setLoading(false);
    }
  };

  const deleteLesson = async (lessonId: string) => {
    if (!session || !csrf) {
      setError(errKey('err.sessionRequired'));
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const res = await apiFetch(`/api/lessons/${lessonId}`, {
        method: 'DELETE',
        headers: { 'X-CSRF-Token': csrf },
      });
      if (res.status === 204) {
        setPendingDeleteId(null);
        if (loadLastBakeRequest(lessonId)) {
          clearLastBakeRequest();
          lastBakeRef.current = null;
        }
        if (currentLessonId === lessonId || route.lessonId === lessonId) {
          clearPoll();
          setLesson(null);
          setBakeStatus(null);
          setCurrentLessonId(null);
          navigate({ view: 'paste' });
        }
        await loadCatalog();
        return;
      }
      if (res.status === 404) {
        setError(errKey('err.lessonNotFound'));
        return;
      }
      const data = await res.json().catch(() => ({}));
      const e: ErrorEnvelope = data;
      handleApiError(e);
    } catch {
      setError(errKey('err.deleteFailed'));
    } finally {
      setLoading(false);
    }
  };

  const copyLessonAsNew = () => {
    if (!lesson) {
      setError(errKey('err.lessonNotFound'));
      return;
    }
    const anchorText = lesson.lesson.anchor?.text?.trim();
    if (!anchorText) {
      setError(errKey('err.copyAsNewNoAnchor'));
      return;
    }
    setError(null);
    restoreFormFromPayload({
      text: anchorText,
      duration: lesson.lesson.duration,
      focus: lesson.lesson.focus || '',
      lessonId: lesson.lesson_id,
      anchorSource: lesson.lesson.anchor.source,
      ...(lesson.lesson.anchor.source_url ? { sourceUrl: lesson.lesson.anchor.source_url } : {}),
      ...(lesson.logical_model_id ? { logicalModelId: lesson.logical_model_id } : {}),
    });
    navigate({ view: 'paste' });
  };

  // Sol P1-5: clipboard lesson export (teacher + student variants).
  // Student variant is safe for Zoom share — never includes answers / notes / provenance.
  // The *text* produced by formatLessonForClipboard is lesson content (stays UA always).
  // Chrome notices use t() keys (added to i18n).
  const copyLessonToClipboard = async (mode: ClipboardMode) => {
    if (!lesson) return;
    const text = formatLessonForClipboard(
      {
        title: lesson.lesson.title,
        duration: lesson.lesson.duration,
        focus: lesson.lesson.focus,
        anchor: lesson.lesson.anchor,
        blocks: lesson.lesson.blocks,
      },
      { mode },
    );
    const okKey: ChromeKey = mode === 'teacher' ? 'clipboard.copiedTeacher' : 'clipboard.copiedStudent';
    const failKey: ChromeKey = 'clipboard.copyFailed';
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        setClipboardNotice({ kind: 'ok', key: okKey });
      } else {
        setClipboardNotice({ kind: 'fail', key: failKey });
      }
    } catch {
      setClipboardNotice({ kind: 'fail', key: failKey });
    }
    window.setTimeout(() => setClipboardNotice(null), 5000);
  };

  // #93 item 3: robust poll (F2 hardened)
  // - update step on every tick (no stale)
  // - converge on ready/failed
  // - resume after refresh if baking (via 409 + route effect)
  // - cleanup on unmount/lesson switch
  // - active id tracking + tick guards + conditional clear so catalog row switches don't kill polls
  const pollStatus = (id: string) => {
    clearPoll();
    activePollIdRef.current = id;
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
      // F2: guard against a stale timer from a previous lesson that wasn't cleared in time.
      // If we switched catalog rows, active will be the new id; old scheduled tick aborts.
      if (activePollIdRef.current !== id) {
        return;
      }
      attempts++;
      let st: any = null;
      try {
        const res = await apiFetch(`/api/lessons/${id}/status`);
        st = await res.json();
        // Re-check after await in case switch happened during network
        if (activePollIdRef.current !== id) {
          return;
        }
        setBakeStatus((prev) => ({
          status: st.status,
          step: st.step || '',
          failure: st.failure_message || undefined,
          failure_code: st.failure_code ?? null,
          progress: st.progress || undefined,
          startedAt: prev?.startedAt || st.created_at,
        }));
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
        if (activePollIdRef.current !== id) {
          return;
        }
        setBakeStatus((prev) => prev || { status: 'baking', step: 'bake.step.updating' });
      }
      if (attempts < max) {
        schedule(attempts < 30 ? 800 : 2500);
      } else {
        clearPoll();
        if (st && st.status !== 'ready' && st.status !== 'failed') {
          setError(errKey('err.longWait'));
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
        setError(errKey('err.sessionRequired'));
        return;
      }
    }
    setLoading(true);
    setError(null);
    setLocalAcks([]);
    // F1 fix: clear stale `lesson` (from a prior ready lesson) at start of open when
    // targeting a different lesson. This guarantees that when catalog opens a failed
    // (or baking) item after viewing a ready one, the 409 path's bakeStatus + !lesson
    // condition renders the failure card instead of old blocks. Clear only on id switch
    // to avoid unnecessary flicker on re-checks of same lesson.
    if (currentLessonId !== id) {
      setLesson(null);
      setBakeStatus(null);
    }
    try {
      const res = await apiFetch(`/api/lessons/${id}`);
      if (res.status === 200) {
        const lr: LessonResource = await res.json();
        setLesson(lr);
        setBakeStatus(null);
        setCurrentLessonId(id);
        setLocalAcks(lr.warning_acknowledgements || []);
        setAnchorOpen(false);
        navigate({ view: 'lesson', lessonId: id, mode });
      } else if (res.status === 409) {
        await res.json().catch(() => ({}));
        setLesson(null);
        setCurrentLessonId(id);
        navigate({ view: 'lesson', lessonId: id, mode });
        try {
          const statusRes = await apiFetch(`/api/lessons/${id}/status`);
          if (statusRes.ok) {
            const st = await statusRes.json();
            if (st.status === 'failed') {
              setBakeStatus({
                status: 'failed',
                step: st.step || '',
                failure: st.failure_message || undefined,
                failure_code: st.failure_code ?? null,
                progress: st.progress || undefined,
                startedAt: st.created_at,
              });
              setBakeElapsedMs(0);
              return;
            }
            const startedAt = st.created_at || new Date().toISOString();
            setBakeStatus({
              status: st.status === 'ready' ? 'baking' : (st.status as LessonState),
              step: st.step || 'bake.step.tasksComposed',
              progress: st.progress || undefined,
              startedAt,
            });
            setBakeElapsedMs(0);
            pollStatus(id);
            return;
          }
        } catch {
          // fall through to generic baking poll
        }
        const startedAt = new Date().toISOString();
        setBakeStatus({ status: 'baking', step: 'bake.step.tasksComposed', startedAt });
        setBakeElapsedMs(0);
        pollStatus(id);
      } else if (res.status === 404) {
        setError(errKey('err.lessonNotFound'));
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({ code: '', message: '' }));
        handleApiError(e);
      }
    } catch {
      setError(errKey('err.loadLesson'));
    } finally {
      setLoading(false);
    }
  };

  const loadCatalog = async () => {
    // #93 item 2: auto-loaded once session ready; guard removed, manual button remains
    const res = await apiFetch('/api/lessons');
    if (res.ok) {
      const data = await res.json();
      const local = loadLocalCatalogEntries(lastBakeRef.current);
      setCatalog(mergeCatalogLessons(data.lessons || [], local));
    }
  };

  // ===== Warning ack (REVIEW only) =====
  const ackWarning = async (blockId: string) => {
    await reviewMutation(`/api/lessons/${currentLessonId}/blocks/${blockId}/accept`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ expected_revision: lesson!.revision }),
    });
  };

  const allVisibleWarningsAcked = (l: LessonResource | null) => {
    if (!l) return true;
    const required = splitReviewBlocks(l.lesson.blocks, l.lesson.duration)
      .visible
      .filter((block) => blockNeedsReview(block))
      .map((block) => block.id);
    // Mirrors the server's _visible_needs_review_ids: an unsupported focus is
    // acknowledged under a reserved lesson-level id, so the accept button and
    // the accept endpoint agree on what is still outstanding.
    if (focusStatusNeedsReview(l.lesson.focus_status)) required.push(FOCUS_STATUS_ACK_ID);
    const acks = [...(l.warning_acknowledgements || []), ...localAcks];
    return required.every(id => acks.includes(id));
  };

  // ===== Accept / Draft =====
  const acceptLesson = async () => {
    if (!lesson || !currentLessonId || !csrf) return;
    if (!allVisibleWarningsAcked(lesson)) {
      setError(errKey('err.ackAllBeforeAccept'));
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
        applyLessonResource(lr);
        await loadCatalog();
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({} as any));
        if (e.code === 'warning_acknowledgements_required') {
          setError(errKey('err.ackAllBeforeAcceptShort'));
        } else if (e.code === 'revision_conflict') {
          setError(errKey('err.lessonChangedReload2'));
          await openLesson(currentLessonId, 'review');
        } else {
          setError(errOr(e.message, 'err.acceptFailed'));
        }
      }
    } finally {
      setLoading(false);
    }
  };

  const returnToDraft = async () => {
    if (!lesson || !currentLessonId || !csrf) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/lessons/${currentLessonId}/draft`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ expected_revision: lesson.revision }),
      });
      if (res.ok) {
        const lr: LessonResource = await res.json();
        applyLessonResource(lr);
        await loadCatalog();
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({} as ErrorEnvelope));
        if (e.code === 'revision_conflict') {
          setError(errKey('err.lessonChangedReload'));
          await handleRevisionConflict();
        } else {
          setError(errOr(e.message, 'err.draftFailed'));
        }
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

  // Browser print (with stylesheet) — teacher keeps keys; student worksheet has none
  const printLesson = (variant: 'teacher' | 'student') => {
    setPrintVariant(variant);
    requestAnimationFrame(() => {
      window.print();
      window.setTimeout(() => setPrintVariant(null), 500);
    });
  };

  // Live elapsed clock while baking (honest wait — not a fake percent)
  useEffect(() => {
    if (!bakeStatus || bakeStatus.status !== 'baking' || !bakeStatus.startedAt) {
      setBakeElapsedMs(0);
      return undefined;
    }
    const started = new Date(bakeStatus.startedAt).getTime();
    const tick = () => setBakeElapsedMs(Math.max(0, Date.now() - started));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [bakeStatus?.status, bakeStatus?.startedAt]);

  const CLIENT_BAKE_STEP_KEYS = new Set<ChromeKey>([
    'bake.step.textReceived',
    'bake.step.updating',
    'bake.step.tasksComposed',
  ]);

  const resolveBakeStep = (step: string): string => {
    if (CLIENT_BAKE_STEP_KEYS.has(step as ChromeKey)) return t(step as ChromeKey);
    return step;
  };

  const bakingSubline = () => {
    if (!bakeStatus) return '';
    const step = resolveBakeStep(bakeStatus.step);
    if (bakeStatus.status === 'failed') {
      return bakeStatusSubline(bakeStatus.status, step, bakeStatus.failure, t('bake.failFallback'));
    }
    if (bakeStatus.progress) {
      return formatBakeProgressLine(bakeStatus.progress, t);
    }
    if (bakeStatus.status === 'baking') {
      return formatBakeElapsedFallback(bakeElapsedMs, t);
    }
    return bakeStatusSubline(bakeStatus.status, step, bakeStatus.failure, t('bake.failFallback'));
  };

  const renderAnchorBody = (text: string, testId?: string) => (
    <div className="anchorbody" {...(testId ? { 'data-testid': testId } : {})}>{text}</div>
  );

  function handleApiError(e: ErrorEnvelope) {
    if (e.code === 'session_required') {
      setSession(null);
      setCsrf(null);
      setSessionReady(false);
      resetSessionScopedState();
      setError(errKey('err.sessionExpired'));
      navigate({ view: 'invite' });
    } else {
      setError(errOr(e.message, 'err.genericDot'));
    }
  }

  const applyLessonResource = (lr: LessonResource) => {
    setLesson(lr);
    setLocalAcks(lr.warning_acknowledgements || []);
  };

  const handleRevisionConflict = async () => {
    if (currentLessonId) await openLesson(currentLessonId, 'review');
  };

  const reviewMutation = async (
    path: string,
    init: RequestInit,
    opts?: { reloadOnConflict?: boolean },
  ): Promise<LessonResource | null> => {
    if (!lesson || !currentLessonId || !csrf) return null;
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(path, init);
      if (res.ok) {
        const lr: LessonResource = await res.json();
        applyLessonResource(lr);
        return lr;
      }
      const e: ErrorEnvelope = await res.json().catch(() => ({} as ErrorEnvelope));
      if (e.code === 'revision_conflict') {
        setError(errKey('err.lessonChangedReload'));
        if (opts?.reloadOnConflict !== false && currentLessonId) {
          await handleRevisionConflict();
        }
      } else {
        setError(errOr(e.message, 'err.generic'));
      }
      return null;
    } catch {
      setError(errKey('err.generic'));
      return null;
    } finally {
      setLoading(false);
    }
  };

  const moveBlock = (blockId: string, direction: 'up' | 'down') =>
    reviewMutation(`/api/lessons/${currentLessonId}/blocks/${blockId}/move`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ expected_revision: lesson!.revision, direction }),
    });

  const removeBlock = (blockId: string) =>
    reviewMutation(`/api/lessons/${currentLessonId}/blocks/${blockId}/remove`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ expected_revision: lesson!.revision }),
    });

  const includeReserveBlock = (blockId: string) =>
    reviewMutation(`/api/lessons/${currentLessonId}/blocks/${blockId}/include`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ expected_revision: lesson!.revision }),
    });

  const restoreRejected = (rejectedIndex: number) =>
    reviewMutation(`/api/lessons/${currentLessonId}/rejected/${rejectedIndex}/restore`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ expected_revision: lesson!.revision, phase: 2 }),
    });

  const selectDuration = (duration: LessonDuration) =>
    reviewMutation(`/api/lessons/${currentLessonId}/duration`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ expected_revision: lesson!.revision, duration }),
    });

  const replaceActivity = (blockId: string, activity: Record<string, unknown>) =>
    reviewMutation(`/api/lessons/${currentLessonId}/blocks/${blockId}/activity`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ expected_revision: lesson!.revision, activity }),
    });

  const renderBlocks = (l: LessonResource, viewMode: 'review' | 'run' | 'conduct') => (
    <LessonBlocks
      blocks={l.lesson.blocks}
      viewMode={viewMode}
      showAnswers={showAnswers}
      acknowledgedIds={[...(l.warning_acknowledgements || []), ...localAcks]}
      onAck={ackWarning}
      loading={loading}
    />
  );

  // ===== Render =====
  const currentMode = (route.mode as 'review' | 'run' | 'conduct') || 'review';
  const isStudentSurface =
    currentMode === 'run' || (currentMode === 'conduct' && conductStudentPreview);
  const showTeacherLessonChrome = route.view === 'lesson' && currentMode !== 'conduct' && currentMode !== 'run';

  const returnToTeacherView = () => {
    if (currentMode === 'run') {
      openLesson(currentLessonId || route.lessonId!, 'review');
      return;
    }
    setConductStudentPreview(false);
  };

  return (
    <div className={`teacher-app${printVariant ? ` print-variant-${printVariant}` : ''}${isStudentSurface ? ' studentframe' : ''}`} data-testid={isStudentSurface ? 'student-surface' : 'teacher-surface'}>
      <header className="appbar">
        <div className="brand">{t('brand')}</div>
        {session && isStudentSurface && (
          <span className="modechip student" data-testid="student-mode-badge">{t('chip.student')}</span>
        )}
        <div className="session">
          {/* Header EN/УКР toggle (demo langbtn): default UA, instant + reversible, persisted.
              Stays on the student surface too — the demo keeps interface-language switching there. */}
          <button
            type="button"
            className="helpbtn langbtn"
            onClick={toggleLang}
            title={t('lang.title')}
            aria-label={t('lang.title')}
            data-testid="lang-toggle"
          >
            {lang === 'en' ? t('lang.switchUk') : t('lang.switchEn')}
          </button>
          {session && !isStudentSurface && (
            <>
              <button
                type="button"
                className="helpbtn"
                onClick={() => setHelpOpen(true)}
                title={t('help.aria')}
                aria-label={t('help.aria')}
              >
                ?
              </button>
              <span data-testid="teacher-display-name">{session.teacher.display_name}</span>
              <button onClick={logout} className="link" data-testid="logout-btn">{t('logout')}</button>
            </>
          )}
        </div>
      </header>

      {helpOpen && (
        <div
          className="help-overlay"
          role="dialog"
          aria-labelledby="help-title"
          onClick={(e) => { if (e.target === e.currentTarget) setHelpOpen(false); }}
        >
          <div className="help-card">
            <h2 id="help-title">{t('help.title')}</h2>
            <p><strong>{t('help.p1.b')}</strong> {t('help.p1.t')}</p>
            <p><strong>{t('help.p2.b')}</strong> {t('help.p2.t')}</p>
            <p><strong>{t('help.p3.b')}</strong> {t('help.p3.t')}</p>
            <p><strong>{t('help.p4.b')}</strong> {t('help.p4.t')}</p>
            <div className="help-actions">
              <button type="button" className="btn primary" onClick={() => setHelpOpen(false)}>{t('help.gotIt')}</button>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="banner fail error" role="alert">
          <span className="ic">!</span>
          <span>{'raw' in error ? error.raw : t(error.key)}</span>
          <button onClick={() => setError(null)} style={{marginLeft:'auto',fontSize:13,opacity:.7}}>✕</button>
        </div>
      )}

      {clipboardNotice && (
        <div
          className={`banner ${clipboardNotice.kind === 'ok' ? 'honest' : 'fail'} clipboard-notice`}
          role="status"
          data-testid="clipboard-notice"
        >
          <span className="ic">{clipboardNotice.kind === 'ok' ? '✓' : '!'}</span>
          <span>{t(clipboardNotice.key)}</span>
          <button
            type="button"
            onClick={() => setClipboardNotice(null)}
            style={{ marginLeft: 'auto', fontSize: 13, opacity: 0.7 }}
            aria-label={t('close.aria')}
          >
            ✕
          </button>
        </div>
      )}

      {/* #93 item1: localized loading state (never blank page) */}
      {initLoading && (
        <main style={{ padding: 40, color: 'var(--ink-soft)' }}>{t('loading')}</main>
      )}

      {!session && !initLoading && (
        <main className="invite">
          <h1>{t('invite.title')}</h1>
          <p>{t('invite.lead')}</p>
          <button
            className="btn primary"
            onClick={async () => {
              // Dev helper: allow manual redeem with test token
              const tok = prompt(t('invite.prompt')) || 'TEST' + 'A'.repeat(39) + 'Q';
              if (!isValidToken(tok)) { setError(errKey('invite.badToken')); return; }
              window.location.hash = `#invite=${tok}`;
              await redeemFromFragment();
            }}
            disabled={loading}
          >
            {t('invite.testBtn')}
          </button>
          <p className="small">{t('invite.small')}</p>
        </main>
      )}

      {session && (
        <>
          {/* Paste / Hub */}
          {route.view !== 'lesson' && (
            <main className="hub">
              <section className="paste">
                <h2>{t('paste.title')}</h2>
                <div className="banner honest"><span className="ic">ℹ︎</span><span>{t(anchorTab === 'url' ? 'paste.discloseUrl' : 'paste.disclose')}</span></div>

                <div className="choices" role="tablist" aria-label={t('anchor.sourceAria')}>
                  <button
                    type="button"
                    className={`choice${anchorTab === 'text' ? ' on' : ''}`}
                    role="tab"
                    aria-selected={anchorTab === 'text'}
                    onClick={() => setAnchorTab('text')}
                  >
                    {t('anchor.tabText')}
                  </button>
                  <button
                    type="button"
                    className={`choice${anchorTab === 'url' ? ' on' : ''}`}
                    role="tab"
                    aria-selected={anchorTab === 'url'}
                    onClick={() => setAnchorTab('url')}
                  >
                    {t('anchor.tabUrl')}
                  </button>
                </div>

                {anchorTab === 'url' && (
                  <div className="urlrow">
                    <input
                      className="inputbox"
                      type="url"
                      value={urlInput}
                      onChange={e => setUrlInput(e.target.value)}
                      placeholder={t('anchor.urlPh')}
                      data-testid="anchor-url-input"
                    />
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={fetchAnchorUrl}
                      disabled={fetchingUrl || loading}
                      data-testid="fetch-anchor-url-btn"
                    >
                      {fetchingUrl ? t('anchor.fetching') : t('anchor.fetchBtn')}
                    </button>
                  </div>
                )}

                <div className="formgrid">
                  {/* B1 and TTT are fixed-pilot constraints and remain clearly labelled (no port of demo editing). */}
                  <div className="field">
                    <label>{t('paste.levelPre')}<strong>B1</strong>{t('paste.levelPost')}</label>
                  </div>

                  <QualifiedModelPicker
                    models={qualifiedModels}
                    selectedId={selectedModelId}
                    unavailableMessage={modelUnavailableMessage}
                    disabled={loading}
                    onChange={setSelectedModelId}
                    t={t}
                  />

                  <div className="field">
                    <label>{t('paste.duration')}</label>
                    <select className="inputbox" value={duration} onChange={e => {
                      const d = Number(e.target.value) as 45 | 60 | 90;
                      setDuration(d);
                      // Silent persist (no extra UI, per P2-6); owner-scoped via csrf.
                      if (csrf) {
                        apiFetch('/api/teacher/preferences', {
                          method: 'PUT',
                          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                          body: JSON.stringify({ default_duration: d }),
                        }).catch(() => { /* silent */ });
                      }
                    }}>
                      <option value={45}>45</option>
                      <option value={60}>60</option>
                      <option value={90}>90</option>
                    </select>
                  </div>

                  <div className="field">
                    <label>{t('paste.focus')}</label>
                    <input
                      className="inputbox"
                      type="text"
                      value={focus}
                      onChange={e => setFocus(e.target.value)}
                      placeholder={t('paste.focusPh')}
                      maxLength={500}
                    />
                  </div>

                  <div className="field">
                    <label>{t(anchorTab === 'url' ? 'paste.textLabelReview' : 'paste.textLabel')}</label>
                    {restoredTextNotice && (
                      <div
                        className="banner honest restored-notice"
                        role="status"
                        data-testid="restored-text-notice"
                      >
                        <span className="ic">ℹ︎</span>
                        <span>{t('paste.restored')}</span>
                        <button
                          type="button"
                          onClick={() => setRestoredTextNotice(false)}
                          style={{ marginLeft: 'auto', fontSize: 13, opacity: 0.7 }}
                          aria-label={t('close.aria')}
                        >
                          ✕
                        </button>
                      </div>
                    )}
                    <textarea
                      className="inputbox"
                      value={pasteText}
                      onChange={e => {
                        setPasteText(e.target.value);
                        if (!e.target.value.trim()) setSourceUrl(null);
                      }}
                      rows={8}
                      placeholder={t('paste.textPh')}
                      data-testid="anchor-text-input"
                    />
                  </div>

                  <button
                    className="btn primary"
                    onClick={startBake}
                    disabled={
                      loading
                      || !pasteText.trim()
                      || !qualifiedModels.some(model => model.id === selectedModelId)
                    }
                  >
                    {loading ? t('paste.submitting') : t('paste.submit')}
                  </button>
                </div>
              </section>

              <section className="catalog">
                <h2>{t('catalog.title')}</h2>
                <button className="btn ghost" onClick={loadCatalog}>{t('catalog.refresh')}</button>
                {catalog.length === 0 && <p className="hint">{t('catalog.empty')}</p>}
                <ul>
                  {catalog.map(item => {
                    const st = item.status;
                    const chipClass = st === 'ready' ? 'ok' : st === 'baking' ? 'info' : st === 'failed' ? 'bad' : 'muted';
                    return (
                      <li key={item.id} className="listrow">
                        <button data-testid="catalog-open-btn" onClick={() => openLesson(item.id, 'review')}>
                          <span className="title">{item.title || item.id.slice(0, 8)}</span>
                          <span className={`chip ${chipClass}`}>{t(statusKey(item.status))}</span>
                          {item.accepted && <span className="chip ok">{t('accepted')}</span>}
                        </button>
                        {pendingDeleteId === item.id ? (
                          <div className="inline-confirm" data-testid="catalog-delete-confirm">
                            <span className="hint">{t('recovery.deleteConfirm')}</span>
                            <button
                              type="button"
                              className="btn ghost"
                              data-testid="delete-cancel-btn"
                              onClick={() => setPendingDeleteId(null)}
                              disabled={loading}
                            >
                              {t('recovery.deleteCancelBtn')}
                            </button>
                            <button
                              type="button"
                              className="btn ghost bad"
                              data-testid="delete-confirm-btn"
                              onClick={() => deleteLesson(item.id)}
                              disabled={loading}
                            >
                              {t('recovery.deleteConfirmBtn')}
                            </button>
                          </div>
                        ) : (
                          <button
                            type="button"
                            className="btn ghost catalog-delete"
                            data-testid="catalog-delete-btn"
                            aria-label={t('catalog.deleteAria')}
                            onClick={() => setPendingDeleteId(item.id)}
                            disabled={loading}
                          >
                            ✕
                          </button>
                        )}
                        <span className="when">{new Date(item.updated_at).toLocaleString(lang === 'en' ? 'en' : 'uk')}</span>
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
            <main className={`lesson-view${currentMode === 'run' ? ' student-run-view' : ''}`}>
              {showTeacherLessonChrome && (
              <div className="lesson-header">
                <button className="btn ghost" onClick={() => navigate({ view: 'paste' })}>{t('lesson.back')}</button>
                {lesson && <h2>{lesson.lesson.title}</h2>}
                <div className="lesson-actions">
                  <button className="btn ghost" onClick={() => openLesson(currentLessonId || route.lessonId!, 'review')} disabled>{t('lesson.reviewMode')}</button>
                  {lesson && lesson.lesson.status === 'ready' && (
                    <button
                      type="button"
                      className="btn ghost"
                      data-testid="enter-student-mode-btn"
                      onClick={() => openLesson(currentLessonId || route.lessonId!, 'run')}
                    >
                      {t('lesson.showAsStudent')}
                    </button>
                  )}
                  {lesson && currentMode === 'review' && lesson.lesson.status === 'ready' && (
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() => setShowAnswers(v => !v)}
                    >
                      {showAnswers ? t('lesson.hideAnswers') : t('lesson.showAnswers')}
                    </button>
                  )}
                  {lesson && lesson.lesson.status === 'ready' && (
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={copyLessonAsNew}
                      data-testid="copy-lesson-as-new"
                    >
                      {t('lesson.copyAsNew')}
                    </button>
                  )}
                  {lesson && currentMode === 'review' && lesson.lesson.status === 'ready' && (
                    <>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => copyLessonToClipboard('teacher')}
                        disabled={loading}
                        data-testid="copy-clipboard-teacher"
                      >
                        {t('lesson.copyTeacher')}
                      </button>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => copyLessonToClipboard('student')}
                        disabled={loading}
                        data-testid="copy-clipboard-student"
                      >
                        {t('lesson.copyStudent')}
                      </button>
                    </>
                  )}
                  {lesson && lesson.lesson.accepted && (
                    <button className="btn primary" onClick={() => openLesson(currentLessonId || route.lessonId!, 'conduct')}>{t('lesson.conduct')}</button>
                  )}
                  {lesson && lesson.lesson.accepted && <span className="chip ok">{t('accepted')}</span>}
                  {lesson && lesson.lesson.status === 'ready' && (
                    <>
                      <button
                        type="button"
                        className="btn ghost"
                        data-testid="print-teacher"
                        onClick={() => printLesson('teacher')}
                      >
                        {t('print.teacher')}
                      </button>
                      <button
                        type="button"
                        className="btn ghost"
                        data-testid="print-student"
                        onClick={() => printLesson('student')}
                      >
                        {t('print.student')}
                      </button>
                    </>
                  )}
                  <button className="btn ghost" onClick={downloadJSON} disabled={!lesson || !lesson.lesson.accepted}>{t('lesson.downloadJson')}</button>
                </div>
              </div>
              )}

              {bakeStatus && !lesson && (
                <div className="baking" data-testid="baking-status-view">
                  <div className="steps">
                    <div className={`step ${bakeStatus.status === 'baking' ? 'now' : bakeStatus.status === 'failed' ? 'fail' : 'done'}`}>
                      <div className="dot">{bakeStatus.status === 'baking' ? '⋯' : bakeStatus.status === 'failed' ? '!' : '✓'}</div>
                      <div>
                        <b>{t('bake.statusPrefix')}{t(statusKey(bakeStatus.status))}</b>
                        <div className="sd" data-testid="baking-subline">{bakingSubline()}</div>
                        {bakeStatus.status === 'baking' && (
                          <div className="sd bake-elapsed" data-testid="baking-elapsed">
                            {t('bake.elapsed', { clock: formatBakeElapsedClock(bakeElapsedMs) })}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                  {bakeStatus.status === 'failed' && (
                    <div className="recovery-card banner fail" data-testid="failure-recovery">
                      <span className="ic">!</span>
                      <div className="recovery-body">
                        <p data-testid="failure-recovery-body" data-failure-code={bakeStatus.failure_code || ''}>
                          {t(recoveryBodyKey(bakeStatus.failure_code))}
                        </p>
                        <div className="recovery-actions">
                          <button
                            type="button"
                            className="btn primary"
                            onClick={retryFailedLesson}
                            disabled={loading}
                            data-testid="failure-retry-btn"
                          >
                            {t('recovery.retry')}
                          </button>
                          {pendingDeleteId === (currentLessonId || route.lessonId) ? (
                            <div className="inline-confirm" data-testid="failure-delete-confirm">
                              <span className="hint">{t('recovery.deleteConfirm')}</span>
                              <button
                                type="button"
                                className="btn ghost"
                                data-testid="delete-cancel-btn"
                                onClick={() => setPendingDeleteId(null)}
                                disabled={loading}
                              >
                                {t('recovery.deleteCancelBtn')}
                              </button>
                              <button
                                type="button"
                                className="btn ghost bad"
                                data-testid="delete-confirm-btn"
                                onClick={() => deleteLesson(currentLessonId || route.lessonId!)}
                                disabled={loading}
                              >
                                {t('recovery.deleteConfirmBtn')}
                              </button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              className="btn ghost"
                              data-testid="failure-delete-btn"
                              onClick={() => setPendingDeleteId(currentLessonId || route.lessonId || null)}
                              disabled={loading}
                            >
                              {t('recovery.delete')}
                            </button>
                          )}
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
                            {t('recovery.back')}
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                  {bakeStatus.status === 'baking' && polling && (
                    <p className="hint" style={{marginTop:6}} data-testid="baking-polling">{t('bake.updating')}</p>
                  )}
                  {bakeStatus.status === 'baking' && (
                    <button className="btn ghost" style={{marginTop:8}} onClick={() => (currentLessonId || route.lessonId) && openLesson(currentLessonId || route.lessonId!)}>{t('bake.checkNow')}</button>
                  )}
                </div>
              )}

              {!lesson && !bakeStatus && (
                <p style={{ color: 'var(--ink-soft)', padding: '12px 0' }}>{t('loading')}</p>
              )}

              {lesson && (
                <>
                  {showTeacherLessonChrome && (
                  <div className="meta">
                    <div>{t('meta.level', { level: lesson.lesson.level, duration: lesson.lesson.duration })}</div>
                    <div>{t('meta.revision', { rev: lesson.revision, acc: lesson.lesson.accepted ? t('meta.yes') : t('meta.no') })}</div>
                    {lesson.lesson.focus && <div>{t('meta.focus')}{lesson.lesson.focus}</div>}
                  </div>
                  )}

                  {currentMode === 'run' && (
                    <>
                      <div className="rolebanner student" data-testid="student-banner">
                        {t('run.studentBanner')}
                      </div>
                      <div className="student-toolbar noprint">
                        <span className="chip info">
                          {t('run.toolbarChip', { level: lesson.lesson.level, duration: lesson.lesson.duration })}
                        </span>
                        <span className="student-toolbar-spacer" />
                        <button
                          type="button"
                          className="btn ghost"
                          data-testid="copy-clipboard-student-run"
                          onClick={() => copyLessonToClipboard('student')}
                        >
                          {t('lesson.copyStudent')}
                        </button>
                        <button
                          type="button"
                          className="btn primary"
                          data-testid="teacher-return-btn"
                          onClick={returnToTeacherView}
                        >
                          {t('run.backToTeacher')}
                        </button>
                      </div>
                      {lesson.lesson.anchor?.text && (
                        <div className="anchor-run-bar noprint">
                          <button
                            type="button"
                            className="btn ghost"
                            data-testid="anchor-toggle-run"
                            onClick={() => setAnchorOpen((v) => !v)}
                          >
                            {anchorOpen ? t('anchor.hide') : t('anchor.summary')}
                          </button>
                          {anchorOpen && (
                            <div className="anchor-panel run-open" data-testid="anchor-panel-run">
                              <h4 className="dochead"><span className="pn">☰</span>{t('anchor.readingHead')}</h4>
                              {renderAnchorBody(lesson.lesson.anchor.text, 'anchor-text-body')}
                            </div>
                          )}
                        </div>
                      )}
                      <div className="student-sheet paper">
                        <p className="docsheet-title">{lesson.lesson.title}</p>
                        <p className="student-sheet-sub">
                          {t('run.sheetSub', { duration: lesson.lesson.duration })}
                        </p>
                        <div className={`modes run`}>
                          {renderBlocks(lesson, 'run')}
                        </div>
                        <p className="docsheet-footer" data-testid="honesty-footer">
                          {t('print.honestyFooter')}
                        </p>
                      </div>
                    </>
                  )}

                  {lesson.lesson.anchor?.text && (
                    <div className="anchor-print" data-testid="anchor-print">
                      <h4 className="dochead"><span className="pn">☰</span>{t('anchor.readingHead')}</h4>
                      {renderAnchorBody(lesson.lesson.anchor.text)}
                    </div>
                  )}

                  {currentMode === 'review' && (
                    <ReviewWorkbench
                      resource={lesson}
                      showAnswers={showAnswers}
                      loading={loading}
                      onDurationChange={selectDuration}
                      onMoveBlock={moveBlock}
                      onRemoveBlock={removeBlock}
                      onIncludeReserve={includeReserveBlock}
                      onRestoreRejected={restoreRejected}
                      onAckWarning={ackWarning}
                      onSaveActivity={replaceActivity}
                      onAcceptLesson={acceptLesson}
                      onReturnToDraft={returnToDraft}
                      allWarningsAcked={allVisibleWarningsAcked(lesson)}
                    />
                  )}

                  {currentMode === 'conduct' && lesson && (
                    <Conductor
                      lessonDoc={lesson.lesson}
                      onExit={() => {
                        setConductStudentPreview(false);
                        openLesson(currentLessonId || route.lessonId!, 'review');
                      }}
                      onStudentPreviewChange={setConductStudentPreview}
                    />
                  )}
                </>
              )}
            </main>
          )}
        </>
      )}

      {!isStudentSurface && (
      <footer className="footer">
        <small>{t('footer')}</small>
      </footer>
      )}
    </div>
  );
}
