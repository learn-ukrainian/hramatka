import { useEffect, useState, useCallback, useRef } from 'react';
// Import the kit styles (resolved via package subpath export)
import '@learn-ukrainian/activity-kit/styles.css';
import './teacher.css';
import './activity-kit-overrides.css';
import {
  bakeStatusSubline,
  formatBakeProgressLine,
  formatBakeElapsedFallback,
  formatBakeElapsedClock,
  saveLastBakeRequest,
  clearLastBakeRequest,
  mergeCatalogLessons,
  loadLocalCatalogEntries,
  NEW_LESSON_DURATION,
  resolveDefaultDurationFromPref,
  formatLessonForClipboard,
  resolveQualifiedModelId,
  statusCardFromApi,
  type BakeRequestPayload,
  type BakeProgress,
  type ClipboardMode,
  type LessonAttempt,
  type LessonStatus,
  type QualifiedModelChoice,
} from './app-helpers';
import Conductor from './Conductor';
import { useT, statusKey, recoveryBodyKey, modelUnavailableKey, type ChromeKey } from './i18n';
import LessonBlocks from './LessonBlocks';
import ReviewWorkbench from './ReviewWorkbench';
import QualifiedModelPicker from './QualifiedModelPicker';
import {
  splitReviewBlocks,
  blockNeedsReview,
  focusStatusNeedsReview,
  FOCUS_STATUS_ACK_ID,
  type FocusStatus,
  type ActivityRegenerationEntry,
} from './review-helpers';
import { createGoogleCredentialDelivery } from './google-sign-in';

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
type LessonState = 'draft' | 'baking' | 'ready' | 'failed' | 'cancelled';
type PilotActivityType =
  | 'true-false' | 'cloze' | 'match-up' | 'quiz' | 'mark-the-words'
  | 'fill-in' | 'error-correction' | 'text-questions' | 'short-writing';

interface Session {
  teacher: { id: string; display_name: string };
  role: 'admin' | 'teacher';
  expires_at: string;
  csrf_token: string;
  local_auth_disabled?: boolean;
  google_linked: boolean;
}

interface LessonOwner {
  id: string;
  display_name: string;
  role: 'admin' | 'teacher' | null;
}

interface ErrorEnvelope {
  code: string;
  message: string;
  retryable: boolean;
  lesson_id?: string;
}

interface GoogleSignInOptions {
  client_id: string;
  nonce: string;
  login_uri: string;
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
  /** #402: engine verdict on the block; reason is engine-authored Ukrainian. */
  quality?: 'engine_ok' | 'engine_flagged';
  flag_reason_uk?: string | null;
  flagged_content_hash?: string | null;
  engine_reason_class?: string | null;
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
  /** Applicable-only teacher verdicts on flagged blocks, keyed by block id (#402). */
  activity_feedback?: Record<string, { verdict: 'good' | 'bad'; comment: string | null; updated_at: string }>;
  activity_regenerations?: ActivityRegenerationEntry[];
  logical_model_id: string | null;
  methodology?: 'ttt';
  grammar_focus?: string | null;
  lesson: LessonDocument;
}

interface QualifiedModelList {
  registry_version: string;
  models: QualifiedModelChoice[];
  unavailable_message: string | null;
  unavailable_code?: string | null;
}

interface LessonCatalogItem {
  id: string;
  title: string | null;
  anchor_snippet?: string | null;
  status: LessonState;
  level?: 'B1';
  duration: number;
  methodology?: 'ttt';
  grammar_focus?: string | null;
  revision: number;
  accepted: boolean;
  accepted_at: string | null;
  accepted_revision: number | null;
  failure_code: string | null;
  created_at: string;
  updated_at: string;
}

function isValidToken(t: string): boolean {
  return /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/.test(t);
}

function base64urlBytes(value: string): Uint8Array {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, char => char.charCodeAt(0));
}

function bytesBase64url(value: ArrayBuffer): string {
  const binary = String.fromCharCode(...new Uint8Array(value));
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

const ENTRY_NONCE_STORAGE_KEY = 'hramatka:entry-nonce';

function entryNonce(): string {
  const existing = sessionStorage.getItem(ENTRY_NONCE_STORAGE_KEY);
  if (existing && isValidToken(existing)) return existing;
  const raw = new Uint8Array(32);
  crypto.getRandomValues(raw);
  let binary = '';
  raw.forEach(byte => { binary += String.fromCharCode(byte); });
  const nonce = btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  sessionStorage.setItem(ENTRY_NONCE_STORAGE_KEY, nonce);
  return nonce;
}

function clearEntryNonce(): void {
  sessionStorage.removeItem(ENTRY_NONCE_STORAGE_KEY);
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const origin = window.location.origin;
  const url = new URL(path, origin);
  if (url.origin !== origin || !url.pathname.startsWith('/api/') || path !== url.pathname + url.search) {
    throw new TypeError('Expected a same-origin API path');
  }
  const headers = new Headers(init?.headers || {});
  // Always send Origin for mutations (contract)
  if (!headers.has('Origin')) {
    headers.set('Origin', origin);
  }
  return fetch(path, { ...init, headers, mode: 'same-origin', credentials: 'same-origin' });
}

// ===== Simple SPA router (lightweight, no extra deps) =====
type View = 'invite' | 'paste' | 'catalog' | 'settings' | 'lesson';

interface RouteState {
  view: View;
  lessonId?: string;
  mode?: 'review' | 'run' | 'conduct';
}

// `onLeave` runs synchronously before every route change that is not a lesson commit:
// navigation to another view and every browser Back/Forward.
function useSimpleRouter(onLeave: () => void) {
  const [route, setRoute] = useState<RouteState>({ view: 'invite' });

  // `push: false` is for a commit of the route the browser already shows.
  const navigate = useCallback((r: RouteState, push = true) => {
    if (r.view !== 'lesson') onLeave();
    const hash = r.lessonId
      ? `#/lessons/${r.lessonId}${r.mode ? '?mode=' + r.mode : ''}`
      : r.view === 'catalog'
        ? '#/lessons'
        : r.view === 'settings'
          ? '#/settings'
          : '#/';
    if (push) history.pushState(null, '', hash);
    setRoute(r);
  }, [onLeave]);

  useEffect(() => {
    const parse = () => {
      onLeave();
      const h = window.location.hash || '#/';
      if (h === '#/lessons') {
        setRoute({ view: 'catalog' });
      } else if (h === '#/settings') {
        setRoute({ view: 'settings' });
      } else if (h.startsWith('#/lessons/')) {
        const [id, query] = h.slice('#/lessons/'.length).split('?', 2);
        if (!id || /[^A-Za-z0-9_-]/.test(id)) {
          setRoute({ view: 'paste' });
          return;
        }
        const mode = new URLSearchParams(query).get('mode');
        setRoute({ view: 'lesson', lessonId: id, mode: mode === 'run' || mode === 'conduct' ? mode : 'review' });
      } else {
        setRoute({ view: 'paste' });
      }
    };
    parse();
    const onPop = () => parse();
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [onLeave]);

  return { route, navigate };
}

type LessonMode = 'review' | 'run' | 'conduct';

interface BakeCard {
  status: LessonState;
  step: string;
  revision: number;
  attempt: number;
  attempt_history: LessonAttempt[];
  failure?: string;
  failure_code?: string | null;
  progress?: BakeProgress;
  startedAt?: string;
}

/**
 * One stay of the teacher on a lesson in one mode, or off lessons within one owner and
 * session scope. Its identity is the only thing a late result compares: a result applies
 * only while its visit is current. It also records the freshness of what it shows, so a
 * payload older than the shown one (a lower aggregate revision, or any bake card once a
 * document is shown) never replaces it.
 */
interface Visit {
  /** Owner and session scope; replaced only on an owner switch or a session boundary. */
  scope: object;
  lessonId: string | null;
  mode: LessonMode | null;
  revision: number;
  doc: boolean;
  card: BakeCard | null;
  /** A ready status was seen: the document exists and is read directly from now on. */
  ready: boolean;
}

const offLessonVisit = (scope: object = {}): Visit => ({
  scope, lessonId: null, mode: null, revision: 0, doc: false, card: null, ready: false,
});

/** The persistence contract's aggregate revision: an integer that starts at 1. */
const isRevision = (value: unknown): value is number => Number.isInteger(value) && (value as number) > 0;

const BAKE_STATES: readonly unknown[] = ['draft', 'baking', 'ready', 'failed', 'cancelled'];

/**
 * A status answer the apply rule can order: a supported state and a contract revision. Any
 * other answer (an empty or unreadable body, an unknown state) says nothing about the lesson.
 */
const usableStatus = (value: unknown): value is LessonStatus => {
  const status = value as Partial<LessonStatus> | null;
  return !!status && typeof status === 'object'
    && BAKE_STATES.includes(status.status) && isRevision(status.revision);
};

/** A lesson document the apply rule can order: an object with a contract revision. */
const usableDoc = (value: unknown): value is LessonResource =>
  !!value && typeof value === 'object' && isRevision((value as Partial<LessonResource>).revision);

/** A success body that is no usable document fails its action the way a network error does. */
const docFrom = async (res: Response): Promise<LessonResource> => {
  const value: unknown = await res.json();
  if (!usableDoc(value)) throw new TypeError('unusable lesson document');
  return value;
};

/**
 * A lesson read: the document, the bake status (null when a transient failure hid it), or a
 * failure. A terminal failure proves something beyond one request: `session` (401) that the
 * session it was read in has ended, `gone` (404) that the lesson no longer exists.
 */
type LessonRead =
  | { doc: LessonResource }
  | { status: LessonStatus | null }
  | { error: () => void; terminal?: 'session' | 'gone' }
  | { stale: true };

// ===== Main App =====
export default function TeacherApp() {
  // ----- Screen owner. Written only by enterScreen and leaveScreen. -----
  const visitRef = useRef<Visit>(offLessonVisit());
  // Navigations numbered at or below `floor` can no longer commit.
  const navRef = useRef({ issued: 0, floor: 0 });
  const [currentLessonId, setCurrentLessonId] = useState<string | null>(null);
  const [pendingPrintLessonId, setPendingPrintLessonId] = useState<string | null>(null);
  // The one bake status loop; its token is replaced on every start and stop.
  const pollRef = useRef<{ visit: Visit } | null>(null);
  const pollTimerRef = useRef<number | null>(null);
  const [polling, setPolling] = useState(false);

  const clearPoll = useCallback(() => {
    pollRef.current = null;
    if (pollTimerRef.current != null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setPolling(false);
  }, []);

  /**
   * The teacher is no longer on a lesson. Pending navigations are cancelled and the poll
   * stops. A new owner or session scope always starts a new visit; otherwise moving
   * between non-lesson views keeps the visit, so a catalog load in flight survives.
   */
  const leaveScreen = useCallback((newScope = false) => {
    navRef.current.floor = navRef.current.issued;
    clearPoll();
    setPendingPrintLessonId(null);
    if (!newScope && visitRef.current.lessonId === null) return;
    visitRef.current = offLessonVisit(newScope ? {} : visitRef.current.scope);
    setCurrentLessonId(null);
  }, [clearPoll]);
  const leaveForRoute = useCallback(() => leaveScreen(), [leaveScreen]);

  const { route, navigate } = useSimpleRouter(leaveForRoute);
  const { t, lang, toggleLang, resetLang } = useT();

  const [session, setSession] = useState<Session | null>(null);
  const [lessonOwners, setLessonOwners] = useState<LessonOwner[]>([]);
  const [selectedOwnerId, setSelectedOwnerId] = useState<string | null>(null);
  const [csrf, setCsrf] = useState<string | null>(null); // kept in memory only
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<AppError>(null);
  // Recovery codes are intentionally memory-only. They are shown immediately
  // after passkey enrollment and are discarded as soon as the dialog closes.
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [recoveryCodesCopyStatus, setRecoveryCodesCopyStatus] = useState<'ok' | 'fail' | null>(null);
  const [recoveryCodeInput, setRecoveryCodeInput] = useState('');
  const [googleOptions, setGoogleOptions] = useState<GoogleSignInOptions | null>(null);

  // Paste form state (level fixed B1)
  const [pasteText, setPasteText] = useState('');
  const [duration, setDuration] = useState(NEW_LESSON_DURATION);
  const [grammarFocus, setGrammarFocus] = useState('');
  const [qualifiedModels, setQualifiedModels] = useState<QualifiedModelChoice[]>([]);
  const [selectedModelId, setSelectedModelId] = useState('');
  const [modelUnavailableMessage, setModelUnavailableMessage] = useState<string | null>(null);
  const pasteTextMissing = !pasteText.trim();

  // Baking / lesson state. `lesson` and `bakeStatus` are written only through `show`.
  const [lesson, setLesson] = useState<LessonResource | null>(null);
  const [catalog, setCatalog] = useState<LessonCatalogItem[]>([]);
  const [bakeStatus, setBakeStatus] = useState<BakeCard | null>(null);
  const [bakeElapsedMs, setBakeElapsedMs] = useState(0);

  // Per-lesson local acks (until server confirms)
  const [localAcks, setLocalAcks] = useState<string[]>([]);

  // Session ready gate (#93 item 1): single "session ready" state/promise proxy.
  // Gate loads; when ready, (re)run pending route action. UA loading instead of blank.
  const [sessionReady, setSessionReady] = useState(false);
  const [initLoading, setInitLoading] = useState(true);

  const redeemInFlightRef = useRef<Promise<boolean> | null>(null);
  const regenerationMutationInFlightRef = useRef(false);
  const googleButtonRef = useRef<HTMLDivElement | null>(null);
  // GIS may deliver the credential after this effect is cleaned up (Strict
  // Mode remount, options reload, or a long "Verify it's you" popup). The
  // delivery function lives on a ref so a later credential still same-origin
  // POSTs to /api/auth/google/complete even if the sign-in tree unmounted.
  const deliverGoogleCredentialRef = useRef(createGoogleCredentialDelivery());
  // UI disables immediately, while the exact revision sent below makes a
  // repeated click from another tab a server-side idempotent replay.
  const bakeMutationInFlightRef = useRef<'cancel' | 'retry' | null>(null);
  const [bakeMutation, setBakeMutation] = useState<'cancel' | 'retry' | null>(null);

  // Last bake request: sessionStorage (+ in-memory fallback when storage unavailable).
  // Recovery path: API status does not expose anchor on failed lessons — see app-helpers.
  const lastBakeRef = useRef<BakeRequestPayload | null>(null);

  const [showAnswers, setShowAnswers] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const [restoredTextNotice, setRestoredTextNotice] = useState(false);
  const [anchorOpen, setAnchorOpen] = useState(false);
  const [printVariant, setPrintVariant] = useState<'teacher' | 'student' | null>(null);
  const [conductStudentPreview, setConductStudentPreview] = useState(false);

  const lessonApiFetch = useCallback((path: string, init?: RequestInit): Promise<Response> => {
    if (session?.role !== 'admin' || !selectedOwnerId || !path.startsWith('/api/lessons')) {
      return apiFetch(path, init);
    }
    const separator = path.includes('?') ? '&' : '?';
    return apiFetch(`${path}${separator}owner_id=${encodeURIComponent(selectedOwnerId)}`, init);
  }, [session?.role, selectedOwnerId]);
  // Clipboard export notice (Sol P1-5 folded to i18n): stores key so t() reflects current lang.
  const [clipboardNotice, setClipboardNotice] = useState<{ kind: 'ok' | 'fail'; key: ChromeKey } | null>(null);

  const activeActivityRegeneration = lesson?.activity_regenerations?.find(
    (item) => item.status === 'queued' || item.status === 'running',
  );

  /** Reset all lesson/session-scoped client state (logout + invite redeem boundaries). */
  const resetSessionScopedState = useCallback(() => {
    leaveScreen(true);
    clearLastBakeRequest();
    lastBakeRef.current = null;
    setPasteText('');
    setDuration(NEW_LESSON_DURATION);
    setGrammarFocus('');
    setQualifiedModels([]);
    setSelectedModelId('');
    setModelUnavailableMessage(null);
    setLesson(null);
    setCatalog([]);
    setBakeStatus(null);
    setLocalAcks([]);
    setShowAnswers(true);
    setError(null);
    setRestoredTextNotice(false);
    setAnchorOpen(false);
    setPrintVariant(null);
    setBakeElapsedMs(0);
    resetLang(); // #106 boundary: UI language back to default UA + clear persisted choice
    setClipboardNotice(null);
    setRecoveryCodes(null);
    setRecoveryCodesCopyStatus(null);
    setRecoveryCodeInput('');
  }, [leaveScreen, resetLang]);

  const restoreFormFromPayload = useCallback((payload: BakeRequestPayload) => {
    setPasteText(payload.text);
    setDuration(NEW_LESSON_DURATION);
    setGrammarFocus(payload.grammarFocus ?? payload.focus ?? '');
    setSelectedModelId(resolveQualifiedModelId(qualifiedModels, payload.logicalModelId));
    setRestoredTextNotice(true);
  }, [qualifiedModels]);

  const refreshSession = useCallback(async () => {
    const res = await apiFetch('/api/session');
    if (res.ok) {
      const data = await res.json();
      const s: Session = {
        teacher: data.teacher,
        role: data.role === 'admin' ? 'admin' : 'teacher',
        expires_at: data.expires_at,
        csrf_token: data.csrf_token,
        local_auth_disabled: data.local_auth_disabled === true,
        google_linked: data.google_linked === true,
      };
      setSession(s);
      setSelectedOwnerId(data.teacher?.id ?? null);
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
  }, [resetSessionScopedState]);


  const loadTeacherDefaultDuration = useCallback(async () => {
    // Silent load; preselects new-lesson duration from teacher-owned preference (P2-6).
    try {
      const res = await apiFetch('/api/teacher/preferences');
      if (res.ok) {
        const p = await res.json();
        setDuration(resolveDefaultDurationFromPref(p));
      }
    } catch {
      /* silent; keep the qualified 45-minute default */
    }
  }, []);

  const loadQualifiedModels = useCallback(async () => {
    try {
      const res = await apiFetch('/api/lesson-models');
      if (!res.ok) throw new Error('model list unavailable');
      const payload: QualifiedModelList = await res.json();
      const models = Array.isArray(payload.models) ? payload.models : [];
      setQualifiedModels(models);
      setModelUnavailableMessage(
        models.length === 0
          ? t(modelUnavailableKey(payload.unavailable_code))
          : (payload.unavailable_message || null),
      );
      setSelectedModelId(previous => resolveQualifiedModelId(models, previous));
    } catch {
      // Availability is fail-closed: a failed list read never revives cached choices.
      setQualifiedModels([]);
      setSelectedModelId('');
      setModelUnavailableMessage(null);
    }
  }, [t]);

  const handleApiError = useCallback((envelope: ErrorEnvelope | null | undefined) => {
    const e: Partial<ErrorEnvelope> = envelope && typeof envelope === 'object' ? envelope : {};
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
  }, [navigate, resetSessionScopedState]);

  /** A 401 from any request ends the session it was sent in, if that is still the session. */
  const endedSession = (status: number, scope: object): boolean => {
    if (status !== 401) return false;
    if (visitRef.current.scope === scope) handleApiError({ code: 'session_required', message: '', retryable: false });
    return true;
  };

  // Catalog rows belong to the visit (an owner switch is a new visit) and to the latest
  // successful load: an older load that answers last never replaces newer rows.
  const catalogLoadRef = useRef({ issued: 0, applied: 0 });
  const loadCatalog = useCallback(async () => {
    if (!sessionReady) return;
    const visit = visitRef.current;
    const n = ++catalogLoadRef.current.issued;
    const owned = () => visitRef.current === visit && n > catalogLoadRef.current.applied;
    try {
      const res = await lessonApiFetch('/api/lessons');
      if (endedSession(res.status, visit.scope)) return;
      if (!res.ok || !owned()) return;
      const data = await res.json();
      if (!owned()) return;
      catalogLoadRef.current.applied = n;
      const local = loadLocalCatalogEntries(lastBakeRef.current);
      setCatalog(mergeCatalogLessons(data.lessons || [], local));
    } catch {
      // A failed refresh keeps the rows already shown; «Оновити список» stays available.
    }
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- endedSession is unmemoized; it reads only refs and handleApiError
  }, [handleApiError, lessonApiFetch, sessionReady]);

  useEffect(() => {
    if (session?.role !== 'admin') {
      setLessonOwners([]);
      return;
    }
    let cancelled = false;
    void apiFetch('/api/lesson-owners').then(async (response) => {
      if (!response.ok || cancelled) return;
      const payload = await response.json();
      if (!cancelled) setLessonOwners(Array.isArray(payload.owners) ? payload.owners : []);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [session?.role, session?.teacher.id]);

  // GIS returns the signed credential to this page; we immediately POST it
  // same-origin. The browser never stores that credential in a URL,
  // localStorage, sessionStorage, or React state.
  useEffect(() => {
    let cancelled = false;
    const loadOptions = async () => {
      try {
        const res = await apiFetch('/api/auth/google/options', {
          method: 'POST',
          headers: csrf ? { 'X-CSRF-Token': csrf } : undefined,
        });
        if (!res.ok || cancelled) {
          if (!cancelled) setGoogleOptions(null);
          return;
        }
        const payload = await res.json();
        if (
          typeof payload?.client_id !== 'string'
          || typeof payload?.nonce !== 'string'
          || typeof payload?.login_uri !== 'string'
        ) {
          if (!cancelled) setGoogleOptions(null);
          return;
        }
        if (!cancelled) setGoogleOptions(payload as GoogleSignInOptions);
      } catch {
        if (!cancelled) setGoogleOptions(null);
      }
    };
    if (session?.google_linked) {
      setGoogleOptions(null);
    } else if (!initLoading) {
      void loadOptions();
    }
    return () => { cancelled = true; };
  }, [csrf, initLoading, session?.google_linked, session?.teacher.id]);

  useEffect(() => {
    const container = googleButtonRef.current;
    if (!container || !googleOptions) return;
    let cancelled = false;
    const render = () => {
      const google = (window as any).google;
      if (cancelled || !google?.accounts?.id || !googleButtonRef.current) return;
      google.accounts.id.initialize({
        client_id: googleOptions.client_id,
        // FedCM button is the Chrome path that returns an ID token to callback.
        // Classic popup remains the non-FedCM fallback. Do not use redirect.
        ux_mode: 'popup',
        use_fedcm_for_button: true,
        callback: (response: unknown) => {
          deliverGoogleCredentialRef.current(response);
        },
        nonce: googleOptions.nonce,
        auto_select: false,
      });
      googleButtonRef.current.replaceChildren();
      google.accounts.id.renderButton(googleButtonRef.current, {
        theme: 'outline', size: 'large', type: 'standard', shape: 'rectangular', text: 'continue_with',
        width: Math.min(360, Math.max(240, googleButtonRef.current.clientWidth || 320)),
      });
    };
    const existing = document.querySelector<HTMLScriptElement>('script[data-google-identity-services]');
    if (existing) {
      if ((window as any).google?.accounts?.id) render();
      else existing.addEventListener('load', render, { once: true });
      return () => { cancelled = true; existing.removeEventListener('load', render); };
    }
    const script = document.createElement('script');
    script.src = 'https://accounts.google.com/gsi/client';
    script.async = true;
    script.defer = true;
    script.dataset.googleIdentityServices = 'true';
    script.addEventListener('load', render, { once: true });
    document.head.appendChild(script);
    return () => { cancelled = true; script.removeEventListener('load', render); };
  }, [googleOptions, session?.teacher.id]);

  // Invite redemption (token only in memory)
  const redeemFromFragment = useCallback((capturedToken?: string): Promise<boolean> => {
    if (redeemInFlightRef.current) return redeemInFlightRef.current;
    const hash = window.location.hash || '';
    const m = hash.match(/invite=([A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]+)/);
    const token = capturedToken ?? m?.[1];
    if (!token) return Promise.resolve(false);
    const run = async (): Promise<boolean> => {
      // scrub immediately in finally regardless of outcome
      try {
        // A previously redeemed link can be reopened safely in its original
        // browser: the existing cookie wins and no second redemption occurs.
        if (await refreshSession()) return true;
        setLoading(true);
        setError(null);
        const res = await apiFetch('/api/session/redeem', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token, nonce: entryNonce() }),
        });
        if (res.status === 200) {
          clearEntryNonce();
          resetSessionScopedState();
          const data = await res.json();
          const s: Session = {
            teacher: data.teacher,
            role: data.role === 'admin' ? 'admin' : 'teacher',
            expires_at: data.expires_at,
            csrf_token: data.csrf_token,
            google_linked: data.google_linked === true,
          };
          setSession(s);
          setCsrf(data.csrf_token);
          setSessionReady(true);
          navigate({ view: 'paste' });
          // Fetch full session for display
          await refreshSession();
          await loadTeacherDefaultDuration();
          await loadQualifiedModels();
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
    };
    let promise: Promise<boolean>;
    promise = run().finally(() => {
      if (redeemInFlightRef.current === promise) redeemInFlightRef.current = null;
    });
    redeemInFlightRef.current = promise;
    return promise;
  }, [loadQualifiedModels, loadTeacherDefaultDuration, navigate, refreshSession, resetSessionScopedState]);

  const signInWithPasskey = async () => {
    try {
      setLoading(true);
      setError(null);
      const begin = await apiFetch('/api/passkeys/authentication/options', { method: 'POST' });
      if (!begin.ok) {
        setError(errKey('passkey.authStartFailed'));
        return;
      }
      const ceremony = await begin.json();
      const request = ceremony.publicKey;
      const publicKey: PublicKeyCredentialRequestOptions = {
        ...request,
        challenge: base64urlBytes(request.challenge),
        allowCredentials: request.allowCredentials?.map((item: any) => ({ ...item, id: base64urlBytes(item.id) })),
      };
      const credential = await navigator.credentials.get({ publicKey }) as PublicKeyCredential | null;
      if (!credential) return;
      const response = credential.response as AuthenticatorAssertionResponse;
      const complete = await apiFetch('/api/passkeys/authentication', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          challenge_id: ceremony.challenge_id,
          credential: {
            id: credential.id,
            rawId: bytesBase64url(credential.rawId),
            type: credential.type,
            response: {
              clientDataJSON: bytesBase64url(response.clientDataJSON),
              authenticatorData: bytesBase64url(response.authenticatorData),
              signature: bytesBase64url(response.signature),
              userHandle: response.userHandle ? bytesBase64url(response.userHandle) : null,
            },
          },
        }),
      });
      if (!complete.ok) {
        setError(errKey('passkey.notConfirmed'));
        return;
      }
      const data = await complete.json();
      setSession({
        teacher: data.teacher,
        role: data.role === 'admin' ? 'admin' : 'teacher',
        expires_at: data.expires_at,
        csrf_token: data.csrf_token,
        google_linked: data.google_linked === true,
      });
      setCsrf(data.csrf_token);
      setSessionReady(true);
      navigate({ view: 'paste' });
      await loadTeacherDefaultDuration();
      await loadQualifiedModels();
    } catch {
      setError(errKey('passkey.notConfirmed'));
    } finally {
      setLoading(false);
    }
  };

  const enrollPasskey = async () => {
    if (!csrf) return;
    try {
      setLoading(true);
      const begin = await apiFetch('/api/passkeys/enrollment/options', {
        method: 'POST', headers: { 'X-CSRF-Token': csrf },
      });
      if (!begin.ok) {
        setError(errKey('passkey.enrollUnavailable'));
        return;
      }
      const ceremony = await begin.json();
      const request = ceremony.publicKey;
      const publicKey: PublicKeyCredentialCreationOptions = {
        ...request,
        challenge: base64urlBytes(request.challenge),
        user: { ...request.user, id: base64urlBytes(request.user.id) },
        excludeCredentials: request.excludeCredentials?.map((item: any) => ({
          ...item, id: base64urlBytes(item.id),
        })),
      };
      const credential = await navigator.credentials.create({ publicKey }) as PublicKeyCredential | null;
      if (!credential) return;
      const response = credential.response as AuthenticatorAttestationResponse;
      const complete = await apiFetch(`/api/passkeys/enrollment/${ceremony.challenge_id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ credential: {
          id: credential.id, rawId: bytesBase64url(credential.rawId), type: credential.type,
          response: {
            clientDataJSON: bytesBase64url(response.clientDataJSON),
            attestationObject: bytesBase64url(response.attestationObject),
          },
        }}),
      });
      if (!complete.ok) {
        setError(errKey('passkey.notConfirmed'));
        return;
      }
      const { recovery_codes } = await complete.json();
      if (Array.isArray(recovery_codes) && recovery_codes.length > 0 && recovery_codes.every((code): code is string => typeof code === 'string')) {
        setRecoveryCodes(recovery_codes);
        setRecoveryCodesCopyStatus(null);
      } else {
        setError(errKey('passkey.notConfirmed'));
      }
    } catch {
      setError(errKey('passkey.notConfirmed'));
    } finally {
      setLoading(false);
    }
  };

  const copyRecoveryCodes = async () => {
    if (!recoveryCodes) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(recoveryCodes.join('\n'));
      setRecoveryCodesCopyStatus('ok');
    } catch {
      setRecoveryCodesCopyStatus('fail');
    }
  };

  const dismissRecoveryCodes = () => {
    setRecoveryCodes(null);
    setRecoveryCodesCopyStatus(null);
  };

  const downloadRecoveryCodes = () => {
    if (!recoveryCodes) return;
    const blob = new Blob([recoveryCodes.join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = 'hramatka-recovery-codes.txt';
    link.style.display = 'none';
    try {
      document.body.appendChild(link);
      link.click();
    } finally {
      link.remove();
      URL.revokeObjectURL(objectUrl);
    }
  };

  const redeemRecoveryCode = async () => {
    const code = recoveryCodeInput.trim();
    if (!code) {
      setError(errKey('recovery.redeemFailed'));
      return;
    }
    try {
      setLoading(true);
      setError(null);
      const complete = await apiFetch('/api/recovery-codes/redeem', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
      if (!complete.ok) {
        setError(errKey('recovery.redeemFailed'));
        return;
      }
      const data = await complete.json();
      resetSessionScopedState();
      setSession({
        teacher: data.teacher,
        role: data.role === 'admin' ? 'admin' : 'teacher',
        expires_at: data.expires_at,
        csrf_token: data.csrf_token,
        local_auth_disabled: data.local_auth_disabled === true,
        google_linked: data.google_linked === true,
      });
      setCsrf(data.csrf_token);
      setSessionReady(true);
      navigate({ view: 'paste' });
      await refreshSession();
      await loadTeacherDefaultDuration();
      await loadQualifiedModels();
    } catch {
      setError(errKey('recovery.redeemFailed'));
    } finally {
      setLoading(false);
    }
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
      const params = new URLSearchParams(window.location.search);
      const h = window.location.hash || '';
      // GIS redirect is a cross-site POST + 303; fragments are dropped. The
      // callback therefore uses ?google=failed so the teacher sees an error.
      const googleFailed = params.get('google') === 'failed' || h === '#google-sign-in-failed';
      const googleLinked = params.get('google') === 'linked' || h === '#google-linked';
      const googleSignedIn = params.get('google') === 'signed-in';
      const googleReturn = googleFailed || googleLinked || googleSignedIn;
      const inviteToken = h.match(/invite=([A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]+)/)?.[1];
      // Keep the fragment token only in this closure, then remove it before
      // any request, including a failing session refresh.
      if (inviteToken) history.replaceState(null, '', '/teacher/');
      try {
        // An old one-use link is harmless in a browser that already holds a
        // valid session. Confirm that state before attempting another exchange.
        const existing = await refreshSession();
        if (existing) {
          if (googleReturn && !googleFailed) history.replaceState(null, '', '/teacher/');
          if (inviteToken) history.replaceState(null, '', '/teacher/');
          await loadTeacherDefaultDuration();
          await loadQualifiedModels();
        } else if (inviteToken) {
          await redeemFromFragment(inviteToken);
        }
        if (googleFailed || (googleReturn && !existing)) {
          // Set after the 401 refresh: that path clears session-scoped error.
          history.replaceState(null, '', '/teacher/?google=failed');
          setError(errKey('google.failed'));
        }
      } catch {
        if (!cancelled) {
          if (googleReturn) history.replaceState(null, '', '/teacher/?google=failed');
          setError(googleReturn ? errKey('google.failed') : errKey('err.initSession'));
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
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- mount-only session init; adding loadQualifiedModels/t would re-run init on language toggle
  }, []);

  // #93 item 1: once the session is ready, a lesson route the browser shows (direct link,
  // refresh, Back/Forward, sign-in) is opened unless that lesson is already the visit.
  // Nothing is written before an open commits, so this cannot re-enter for a pending open;
  // the cleanup cancels this navigation when the route or the screen changes first.
  useEffect(() => {
    if (!sessionReady || route.view !== 'lesson' || !route.lessonId) return undefined;
    if (currentLessonId === route.lessonId) return undefined;
    const nav = navRef.current; // one object for the component's lifetime
    const n = ++nav.issued;
    void goToLesson(n, route.lessonId, route.mode || 'review', { push: false });
    return () => {
      nav.floor = Math.max(nav.floor, n);
    };
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- goToLesson is unmemoized; it reads only refs and the current render's request scope
  }, [sessionReady, route.view, route.lessonId, route.mode, currentLessonId]);

  // #93 item 2: the catalog loads when the teacher enters a non-lesson view and when the
  // request scope (admin owner) changes; it is never refreshed from a lesson continuation.
  useEffect(() => {
    if (sessionReady && route.view !== 'lesson') void loadCatalog();
  }, [loadCatalog, route.view, sessionReady]);

  // #93 item 3 cleanup: never orphan poll loops
  useEffect(() => {
    return () => { clearPoll(); };
  }, [clearPoll]);

  const logout = async () => {
    if (!csrf) return;
    // Leaving is synchronous with the click: nothing that answers during the DELETE may
    // still navigate or paint, and no route effect may start a new lesson read.
    setSessionReady(false);
    leaveScreen(true);
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
    duration: typeof NEW_LESSON_DURATION;
    grammarFocus: string | null;
    logicalModelId: string;
  }) => {
    if (!session || !csrf || !sessionReady) {
      setError(errKey('err.sessionRequired'));
      return;
    }
    const text = source.text.trim();
    if (!text || text.length > 100000) {
      setError(errKey('err.badTextLen'));
      return;
    }
    if (!qualifiedModels.some(model => model.id === source.logicalModelId)) {
      setError(errKey('paste.modelUnavailable'));
      return;
    }
    setError(null);
    setLoading(true);
    // Submitting is a navigation to the new lesson: leaving Paste before the 202 cancels it.
    const n = ++navRef.current.issued;
    const latest = () => n > navRef.current.floor && n === navRef.current.issued;
    const scope = visitRef.current.scope;
    const id = crypto.randomUUID();
    const payload: BakeRequestPayload = {
      text,
      duration: source.duration,
      grammarFocus: source.grammarFocus?.trim() || '',
      lessonId: id,
      anchorSource: 'teacher-paste',
      logicalModelId: source.logicalModelId,
    };
    try {
      const res = await lessonApiFetch('/api/lessons', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf,
        },
        body: JSON.stringify({
          id,
          anchor: {
            text,
            source: 'teacher-paste',
          },
          level: 'B1',
          duration: source.duration,
          methodology: 'ttt',
          grammar_focus: source.grammarFocus?.trim() || null,
          logical_model_id: source.logicalModelId,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (endedSession(res.status, scope)) return;
      if (res.status === 202) {
        // The recovery copy is kept when the teacher has only moved on, never across an
        // owner switch or a session boundary.
        if (visitRef.current.scope !== scope) return;
        saveLastBakeRequest(payload);
        lastBakeRef.current = payload;
        if (n <= navRef.current.floor) return;
        setPasteText(text);
        setDuration(NEW_LESSON_DURATION);
        setGrammarFocus(source.grammarFocus?.trim() || '');
        const visit = enterScreen(n, id, 'review', true);
        if (usableStatus(data) && data.status === 'ready') {
          showStatus(visit, data);
        } else {
          // A creation answer without a usable status still created the lesson: the card starts
          // at the first revision and the poll learns the real state.
          const known = usableStatus(data);
          show(visit, { card: {
            status: known ? data.status : 'baking', step: 'bake.step.textReceived',
            revision: known ? data.revision : 1, attempt: 1, attempt_history: [], startedAt: new Date().toISOString(),
          } });
        }
      } else if (latest()) {
        navRef.current.floor = n;
        const e = (data && typeof data === 'object' ? data : {}) as ErrorEnvelope;
        setError(errOr(e.message, 'err.bakeFailed'));
      }
    } catch {
      if (latest()) {
        navRef.current.floor = n;
        setError(errKey('err.bakeFailed'));
      }
    } finally {
      setLoading(false);
    }
  };

  const startBake = async () => {
    await submitNewLesson({
      text: pasteText,
      duration,
      grammarFocus: grammarFocus || null,
      logicalModelId: selectedModelId,
    });
  };

  const mutateBake = async (operation: 'cancel' | 'retry') => {
    const visit = visitRef.current;
    const lid = visit.lessonId;
    const current = visit.card;
    const owned = () => visitRef.current === visit;
    if (
      !lid || !current || visit.doc || bakeMutationInFlightRef.current
      || (operation === 'cancel' && !['draft', 'baking'].includes(current.status))
      || (operation === 'retry' && !['failed', 'cancelled'].includes(current.status))
    ) return;
    if (!session || !csrf) {
      setError(errKey('err.sessionRequired'));
      return;
    }
    bakeMutationInFlightRef.current = operation;
    setBakeMutation(operation);
    setError(null);
    try {
      const res = await lessonApiFetch(`/api/lessons/${lid}/${operation}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        // The backend's revision CAS is the cross-tab guard.  Replaying this
        // exact value is intentionally safe for both cancel and retry.
        body: JSON.stringify({ expected_revision: current.revision }),
      });
      const data = await res.json().catch(() => ({}));
      if (endedSession(res.status, visit.scope) || !owned()) return;
      if (res.ok) {
        // An idempotent replay may answer with a later state, including ready. An unreadable
        // body proves only that the request landed: keep the card and re-read the visit.
        if (usableStatus(data)) showStatus(visit, data);
        else ensurePoll(visit); // the card stays; the visit's bounded poll learns the real state
        return;
      }
      // Another tab may have completed the same idempotent intent or advanced
      // the revision.  Re-read before offering the next valid action.
      if (res.status === 409) {
        const read = await readStatus(lid);
        if (endOnTerminal(read, visit) || !owned()) return;
        if ('status' in read && usableStatus(read.status)) showStatus(visit, read.status);
        else ensurePoll(visit);
      } else {
        // The outcome is unknown (the request may have landed): let the bounded poll reconcile.
        ensurePoll(visit);
      }
      handleApiError(data as ErrorEnvelope);
    } catch {
      if (owned()) {
        setError(errKey('err.bakeFailed'));
        ensurePoll(visit);
      }
    } finally {
      bakeMutationInFlightRef.current = null;
      setBakeMutation(null);
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
      duration: NEW_LESSON_DURATION,
      grammarFocus: lesson.grammar_focus ?? lesson.lesson.focus ?? '',
      lessonId: lesson.lesson_id,
      // A historic URL-derived lesson stays readable, but copying it into a
      // new lesson is deliberately a teacher-paste request.
      anchorSource: 'teacher-paste',
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
        focus: lesson.grammar_focus ?? lesson.lesson.focus,
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

  // ===== Screen owner: commit, apply rule, poll, reads =====

  /**
   * Navigation `n` commits (lesson, mode). A different lesson or mode starts a new visit;
   * the same pair keeps the visit, so a result the teacher still owns stays admitted.
   * `push` is false when the browser already shows this route (no Forward truncation).
   */
  const enterScreen = (n: number, lessonId: string, mode: LessonMode, push: boolean): Visit => {
    navRef.current.floor = Math.max(navRef.current.floor, n);
    const current = visitRef.current;
    if (current.lessonId === lessonId && current.mode === mode) return current;
    clearPoll();
    const visit: Visit = { scope: current.scope, lessonId, mode, revision: 0, doc: false, card: null, ready: false };
    visitRef.current = visit;
    setCurrentLessonId(lessonId);
    // Until this visit's own payload arrives it shows nothing of the previous one; a print
    // request belongs to the open that armed it, which sets it again after this commit.
    setLesson(null);
    setBakeStatus(null);
    setLocalAcks([]);
    setPendingPrintLessonId(null);
    setError(null);
    // The student must receive the source text before the exercises; keep the
    // teacher's review view compact, but open the reading on the run surface.
    setAnchorOpen(mode === 'run');
    setConductStudentPreview(false);
    navigate({ view: 'lesson', lessonId, mode }, push);
    return visit;
  };

  /**
   * The one apply rule and the only writer of the lesson document and bake card. A payload
   * applies only while its visit is current and only if it is not older than what the
   * visit shows. One aggregate revision per lesson rises by one per transition and `ready`
   * is terminal (persistence contract), so a lower revision is stale and a bake card never
   * replaces a shown document. An equal revision applies: the aggregate is the same, and
   * feedback rows and regeneration entries change without a new revision. A payload without a
   * contract revision (an empty or unreadable success body) cannot be ordered and never applies.
   */
  const show = (visit: Visit, payload: { doc: LessonResource } | { card: BakeCard }): boolean => {
    if (visitRef.current !== visit) return false;
    const isDoc = 'doc' in payload;
    const revision: unknown = 'doc' in payload ? payload.doc?.revision : payload.card.revision;
    if (!isRevision(revision) || revision < visit.revision || (visit.doc && !isDoc)) return false;
    visit.revision = revision;
    visit.doc = isDoc;
    if ('doc' in payload) {
      visit.card = null;
      clearPoll();
      setBakeStatus(null);
      setLesson(payload.doc);
      setLocalAcks(payload.doc.warning_acknowledgements || []);
      return true;
    }
    visit.card = payload.card;
    setLesson(null);
    setBakeStatus(payload.card);
    if (payload.card.status === 'draft' || payload.card.status === 'baking') ensurePoll(visit);
    else clearPoll();
    return true;
  };

  /**
   * A ready status: its revision becomes the visit's floor, so no older card can replace what
   * is about to be shown or stop the poll that reads the document. Returns false when stale.
   */
  const noteReady = (visit: Visit, status: LessonStatus): boolean => {
    if (visitRef.current !== visit || visit.doc || status.revision < visit.revision) return false;
    visit.revision = status.revision;
    visit.ready = true;
    return true;
  };

  /**
   * The one status handler (opens, re-reads, the poll, cancel/retry). A `ready` status is never
   * painted as a card: it raises the floor and the poll reads the document.
   */
  const showStatus = (visit: Visit, answer: LessonStatus | null) => {
    if (visitRef.current !== visit || visit.doc) return;
    // An unusable answer is no more than a transient failure that hid the status.
    const status = usableStatus(answer) ? answer : null;
    if (status?.status === 'ready') {
      if (noteReady(visit, status)) ensurePoll(visit);
      return;
    }
    if (status) {
      show(visit, { card: statusCardFromApi(status, visit.card ?? undefined) });
    } else if (visit.revision === 0) {
      // Nothing is known about this lesson yet: show a baking card and let the poll learn more.
      show(visit, { card: {
        status: 'baking', step: 'bake.step.tasksComposed', revision: 1, attempt: 1,
        attempt_history: [], startedAt: new Date().toISOString(),
      } });
    }
  };

  /** A failed lesson or status response as a read result. */
  const readFailure = async (res: Response): Promise<LessonRead> => {
    if (res.status === 404) return { error: () => setError(errKey('err.lessonNotFound')), terminal: 'gone' };
    if (res.status === 401) return { error: () => handleApiError({ code: 'session_required', message: '', retryable: false }), terminal: 'session' };
    const e = await res.json().catch(() => null) as ErrorEnvelope | null;
    return { error: () => handleApiError(e) };
  };

  /**
   * A terminal failure ends what it proves, whoever read it: a 401 expires the session it was
   * read in (if that is still the session); a 404 retires the visit of the lesson that is gone
   * (if that visit is still current). The retired visit keeps the lesson and mode, so the route
   * effect does not reopen it, accepts no payload (its floor is unreachable), and leaves
   * navigations to other screens pending.
   */
  const endOnTerminal = (read: LessonRead, visit: Visit): boolean => {
    if (!('error' in read) || !read.terminal) return false;
    if (read.terminal === 'session') {
      endedSession(401, visit.scope);
    } else if (visitRef.current === visit) {
      clearPoll();
      visitRef.current = { ...offLessonVisit(visit.scope), lessonId: visit.lessonId, mode: visit.mode, revision: Number.POSITIVE_INFINITY };
      setLesson(null);
      setBakeStatus(null);
      setPendingPrintLessonId(null);
      read.error();
    }
    return true;
  };

  /** A bake status read; 401 and 404 are terminal failures, other failures hide the status. */
  const readStatus = async (id: string): Promise<LessonRead> => {
    try {
      const res = await lessonApiFetch(`/api/lessons/${id}/status`);
      if (res.ok) return { status: await res.json() as LessonStatus };
      if (res.status === 401 || res.status === 404) return readFailure(res);
      return { status: null };
    } catch {
      return { status: null };
    }
  };

  /** One read of a lesson. The status is fetched only while `live()` still holds. */
  const readLesson = async (id: string, live: () => boolean): Promise<LessonRead> => {
    try {
      const res = await lessonApiFetch(`/api/lessons/${id}`);
      // A document without a contract revision cannot be ordered: a failed, retryable read.
      if (res.status === 200) return { doc: await docFrom(res) };
      if (res.status === 409) {
        await res.json().catch(() => ({}));
        if (!live()) return { stale: true };
        return readStatus(id);
      }
      return readFailure(res);
    } catch {
      return { error: () => setError(errKey('err.loadLesson')) };
    }
  };

  // #93 item 3: one status loop per visit. Its token decides whether it may reschedule;
  // whether a status applies is decided by `show`. Real bakes run ~4 min and provider
  // hiccups stretch them further: poll fast for ~25 s, then every 2.5 s for ~10 min.
  const ensurePoll = (visit: Visit) => {
    // Only the current visit polls, and only while it shows no document: a caller that learned
    // nothing (an unusable or failed answer) after the document landed starts no status loop.
    if (visitRef.current !== visit || visit.doc || pollRef.current?.visit === visit) return;
    clearPoll();
    const poll = { visit };
    pollRef.current = poll;
    setPolling(true);
    const id = visit.lessonId!;
    const current = () => pollRef.current === poll && visitRef.current === visit;
    let attempts = 0;
    const max = 260;
    const schedule = (delay: number) => {
      pollTimerRef.current = window.setTimeout(tick, delay);
    };
    const tick = async () => {
      if (!current()) return;
      attempts++;
      try {
        // Once readiness is known the document is read directly; a transient failure of either
        // read keeps the loop going within its bound.
        let read = visit.ready ? await readLesson(id, current) : await readStatus(id);
        if (endOnTerminal(read, visit)) {
          if (pollRef.current === poll) clearPoll();
          return;
        }
        if (!current()) return;
        if (!visit.ready && 'status' in read && read.status) {
          showStatus(visit, read.status);
          if (!current()) return;
          if (visit.ready) {
            read = await readLesson(id, current);
            if (endOnTerminal(read, visit)) {
              if (pollRef.current === poll) clearPoll();
              return;
            }
            if (!current()) return;
          }
        }
        if ('doc' in read && show(visit, { doc: read.doc })) return;
      } catch {
        // A transient failure keeps the shown card; the next tick asks again.
      }
      if (!current()) return;
      if (attempts < max) {
        schedule(attempts < 30 ? 800 : 2500);
      } else {
        clearPoll();
        setError(errKey('err.longWait'));
      }
    };
    schedule(400);
  };

  // ===== Lesson load + modes =====

  /**
   * Navigation `n` to (lesson, mode). It writes nothing before it commits; it commits only
   * while no newer navigation has committed and the teacher has not left. Only the latest
   * navigation reports a failure, and that failure cancels older ones (no late fallback).
   */
  const goToLesson = async (
    n: number,
    id: string,
    mode: LessonMode,
    opts: { push: boolean; print?: boolean },
  ) => {
    const live = () => n > navRef.current.floor;
    const scope = visitRef.current.scope;
    setLoading(true);
    try {
      const read = await readLesson(id, live);
      if ('error' in read && read.terminal === 'session') {
        endedSession(401, scope);
        return;
      }
      if (!live()) return;
      if ('error' in read) {
        if (n === navRef.current.issued) {
          navRef.current.floor = n;
          read.error();
        }
        return;
      }
      if ('stale' in read) return;
      const visit = enterScreen(n, id, mode, opts.push);
      if ('doc' in read) {
        show(visit, { doc: read.doc });
        if (opts.print) setPendingPrintLessonId(id);
      } else {
        showStatus(visit, read.status);
      }
    } finally {
      setLoading(false);
    }
  };

  /**
   * A re-read of the visit (Check now, a click on the current screen, a conflict reload).
   * Its result goes through `show`; its failure is reported only while the visit still
   * shows what it showed when the re-read began.
   */
  const refreshLesson = async (visit: Visit) => {
    const { revision, doc } = visit;
    const live = () => visitRef.current === visit;
    const unchanged = () => live() && visit.revision === revision && visit.doc === doc;
    setLoading(true);
    setError(null);
    try {
      const read = await readLesson(visit.lessonId!, live);
      if ('stale' in read || endOnTerminal(read, visit) || !live()) return;
      if ('error' in read) {
        if (unchanged()) read.error();
      } else if ('doc' in read) {
        show(visit, { doc: read.doc });
      } else if (read.status) {
        showStatus(visit, read.status);
      } else if (unchanged()) {
        setError(errKey('err.loadLesson'));
      }
    } finally {
      setLoading(false);
    }
  };

  /** Every teacher click that opens a lesson screen. */
  const openLesson = (id: string, mode: LessonMode = 'review', opts: { print?: boolean } = {}) => {
    // Sign-out (or an expired session) makes the session unavailable to lesson reads at once.
    if (!sessionReady) return Promise.resolve();
    const visit = visitRef.current;
    if (visit.lessonId === id && visit.mode === mode) {
      // The teacher chose the screen they are on: pending navigations elsewhere are cancelled.
      navRef.current.floor = navRef.current.issued;
      return refreshLesson(visit);
    }
    const n = ++navRef.current.issued;
    return goToLesson(n, id, mode, { push: true, print: opts.print });
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
  // A lesson action belongs to the visit that shows the document it acts on; its result,
  // error or conflict reload reaches the screen only while that visit is current.
  const documentVisit = (): Visit | null => {
    const visit = visitRef.current;
    return lesson && visit.doc && visit.lessonId === lesson.lesson_id ? visit : null;
  };

  const acceptLesson = async () => {
    const visit = documentVisit();
    if (!visit || !lesson || !csrf) return;
    if (!allVisibleWarningsAcked(lesson)) {
      setError(errKey('err.ackAllBeforeAccept'));
      return;
    }
    const owned = () => visitRef.current === visit;
    setLoading(true);
    try {
      const res = await lessonApiFetch(`/api/lessons/${visit.lessonId}/accept`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ expected_revision: lesson.revision }),
      });
      if (res.ok) {
        const lr = await docFrom(res);
        show(visit, { doc: lr });
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({} as any));
        if (endedSession(res.status, visit.scope) || !owned()) return;
        if (e.code === 'warning_acknowledgements_required') {
          setError(errKey('err.ackAllBeforeAcceptShort'));
        } else if (e.code === 'revision_conflict') {
          setError(errKey('err.lessonChangedReload2'));
          await refreshLesson(visit);
        } else {
          setError(errOr(e.message, 'err.acceptFailed'));
        }
      }
    } catch {
      if (owned()) setError(errKey('err.acceptFailed'));
    } finally {
      setLoading(false);
    }
  };

  const returnToDraft = async () => {
    const visit = documentVisit();
    if (!visit || !lesson || !csrf) return;
    const owned = () => visitRef.current === visit;
    setLoading(true);
    setError(null);
    try {
      const res = await lessonApiFetch(`/api/lessons/${visit.lessonId}/draft`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ expected_revision: lesson.revision }),
      });
      if (res.ok) {
        const lr = await docFrom(res);
        show(visit, { doc: lr });
      } else {
        const e: ErrorEnvelope = await res.json().catch(() => ({} as ErrorEnvelope));
        if (endedSession(res.status, visit.scope) || !owned()) return;
        if (e.code === 'revision_conflict') {
          setError(errKey('err.lessonChangedReload'));
          await refreshLesson(visit);
        } else {
          setError(errOr(e.message, 'err.draftFailed'));
        }
      }
    } catch {
      if (owned()) setError(errKey('err.draftFailed'));
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

  // Browser print — teacher keeps keys; the student worksheet has none. #587: the
  // student variant re-renders the review sheet without the teacher material rather
  // than hiding it, so the keys are absent from the print DOM, not merely painted
  // away. The variant is set first and printed from a frame callback, so React has
  // committed that render before the print dialog snapshots the document.
  const printLesson = (variant: 'teacher' | 'student') => {
    setPrintVariant(variant);
    requestAnimationFrame(() => {
      window.print();
      window.setTimeout(() => setPrintVariant(null), 500);
    });
  };

  // A list-row print action first loads the existing teacher lesson view, then
  // invokes its established print path once its document is mounted.
  useEffect(() => {
    if (!pendingPrintLessonId || lesson?.lesson_id !== pendingPrintLessonId) return;
    setPendingPrintLessonId(null);
    printLesson('teacher');
  }, [pendingPrintLessonId, lesson?.lesson_id]);

  // The print request belongs to its open: it is armed only when that open commits.
  const openLessonForPrint = (id: string) => {
    void openLesson(id, 'review', { print: true });
  };

  // Live elapsed clock while baking (honest wait — not a fake percent)
  const bakeRunning = bakeStatus?.status === 'baking';
  const bakeStartedAt = bakeStatus?.startedAt;
  useEffect(() => {
    if (!bakeRunning || !bakeStartedAt) {
      setBakeElapsedMs(0);
      return undefined;
    }
    const started = new Date(bakeStartedAt).getTime();
    const tick = () => setBakeElapsedMs(Math.max(0, Date.now() - started));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [bakeRunning, bakeStartedAt]);

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
    if (bakeStatus.status === 'cancelled') return t('bake.cancelledBody');
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

  // A regeneration entry only moves forward: a later attempt, or the same attempt at the same
  // or a later stage (queued, running, then succeeded or failed). An older entry that answers
  // last never replaces a newer one.
  const applyActivityRegeneration = useCallback((entry: ActivityRegenerationEntry) => {
    const stage = (status: ActivityRegenerationEntry['status']) =>
      status === 'queued' ? 0 : status === 'running' ? 1 : 2;
    setLesson((current) => {
      if (!current || current.lesson_id !== entry.lesson_id) return current;
      const known = current.activity_regenerations?.find((item) => item.id === entry.id);
      if (known && (entry.attempt < known.attempt
        || (entry.attempt === known.attempt && stage(entry.status) < stage(known.status)))) return current;
      return {
        ...current,
        activity_regenerations: [
          entry,
          ...(current.activity_regenerations || []).filter((item) => item.id !== entry.id),
        ],
      };
    });
  }, []);

  const reconcileActivityRegeneration = async (entry: ActivityRegenerationEntry, visit: Visit) => {
    if (visitRef.current !== visit || entry.lesson_id !== visit.lessonId) return;
    if (entry.status !== 'succeeded' && entry.status !== 'failed') {
      applyActivityRegeneration(entry);
      return;
    }
    const resourceResponse = await lessonApiFetch(`/api/lessons/${entry.lesson_id}`);
    if (endedSession(resourceResponse.status, visit.scope) || visitRef.current !== visit) return;
    if (resourceResponse.status === 404) {
      endOnTerminal({ error: () => setError(errKey('err.lessonNotFound')), terminal: 'gone' }, visit);
      return;
    }
    if (!resourceResponse.ok) {
      // A failed regeneration never changed lesson content, so its durable retry
      // affordance is safe to show even when the resource refetch is transiently
      // unavailable. Success is different: never paint a success badge over the
      // stale block that the teacher is still looking at.
      if (entry.status === 'failed') {
        applyActivityRegeneration(entry);
        return;
      }
      throw new Error('regeneration lesson refresh unavailable');
    }
    const resource = await docFrom(resourceResponse);
    show(visit, { doc: resource });
  };

  // A replacement is a separate durable job. Reloading the page reconstructs
  // this poll from `activity_regenerations`; the old block stays visible meanwhile.
  const activeRegenerationId = activeActivityRegeneration?.id;
  useEffect(() => {
    const lessonId = lesson?.lesson_id;
    if (!activeRegenerationId || !lessonId) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      // Each read belongs to the visit it starts in; a mode switch keeps this effect alive
      // (same lesson and job), so its results land only while that visit is current.
      const visit = visitRef.current;
      const owned = () => !cancelled && visitRef.current === visit && visit.lessonId === lessonId;
      try {
        const response = await lessonApiFetch(
          `/api/lessons/${lessonId}/regenerations/${activeRegenerationId}`,
        );
        if (endedSession(response.status, visit.scope)) return;
        if (!response.ok) throw new Error('regeneration poll unavailable');
        const status: ActivityRegenerationEntry = await response.json();
        if (cancelled) return;
        if (!owned()) {
          // The teacher is elsewhere: ask again from whichever visit is current then.
        } else if (status.status === 'succeeded' || status.status === 'failed') {
          // The one terminal-entry path (create, retry and this poll): a 401 ends the session, a
          // 404 retires the visit, an unavailable read keeps polling for a success.
          await reconcileActivityRegeneration(status, visit);
          return;
        } else {
          applyActivityRegeneration(status);
        }
      } catch {
        // A transient poll failure must not turn a durable server job into a UI failure.
      }
      if (!cancelled) timer = window.setTimeout(poll, 1200);
    };
    timer = window.setTimeout(poll, 500);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- endedSession and reconcileActivityRegeneration are unmemoized; they read only refs, state setters and handleApiError
  }, [lesson?.lesson_id, activeRegenerationId, applyActivityRegeneration, handleApiError, lessonApiFetch]);

  const reviewMutation = async (
    path: string,
    init: RequestInit,
    opts?: { reloadOnConflict?: boolean },
  ): Promise<LessonResource | null> => {
    const visit = documentVisit();
    if (!visit || !csrf) return null;
    const owned = () => visitRef.current === visit;
    setLoading(true);
    setError(null);
    try {
      const res = await lessonApiFetch(path, init);
      if (res.ok) {
        const lr = await docFrom(res);
        return show(visit, { doc: lr }) ? lr : null;
      }
      const e: ErrorEnvelope = await res.json().catch(() => ({} as ErrorEnvelope));
      if (endedSession(res.status, visit.scope) || !owned()) return null;
      if (e.code === 'revision_conflict') {
        setError(errKey('err.lessonChangedReload'));
        if (opts?.reloadOnConflict !== false) await refreshLesson(visit);
      } else {
        setError(errOr(e.message, 'err.generic'));
      }
      return null;
    } catch {
      if (owned()) setError(errKey('err.generic'));
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

  const replaceActivity = (blockId: string, activity: Record<string, unknown>) =>
    reviewMutation(`/api/lessons/${currentLessonId}/blocks/${blockId}/activity`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ expected_revision: lesson!.revision, activity }),
    });

  // #402: no expected_revision — staleness is hash-gated server-side against
  // the block's flag-time content hash, not the lesson revision.
  const putActivityFeedback = (blockId: string, verdict: 'good' | 'bad', comment: string | null) =>
    reviewMutation(`/api/lessons/${currentLessonId}/blocks/${blockId}/feedback`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf! },
      body: JSON.stringify({ verdict, ...(comment ? { comment } : {}) }),
    });

  const createActivityRegeneration = async (blockId: string, feedback: string | null) => {
    const visit = documentVisit();
    if (
      !visit || !lesson || !csrf || activeActivityRegeneration
      || regenerationMutationInFlightRef.current
    ) return;
    regenerationMutationInFlightRef.current = true;
    const requestLessonId = visit.lessonId;
    const owned = () => visitRef.current === visit;
    setLoading(true);
    setError(null);
    try {
      const response = await lessonApiFetch(
        `/api/lessons/${requestLessonId}/blocks/${blockId}/regenerations`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({
            id: crypto.randomUUID(),
            expected_revision: lesson.revision,
            ...(feedback ? { feedback } : {}),
          }),
        },
      );
      if (response.ok) {
        await reconcileActivityRegeneration(await response.json(), visit);
        return;
      }
      const envelope: ErrorEnvelope = await response.json().catch(() => ({} as ErrorEnvelope));
      if (endedSession(response.status, visit.scope) || !owned()) return;
      if (envelope.code === 'revision_conflict') {
        setError(errKey('err.lessonChangedReload'));
        await refreshLesson(visit);
      } else {
        setError(errOr(envelope.message, 'err.generic'));
      }
    } catch {
      if (owned()) setError(errKey('err.generic'));
    } finally {
      regenerationMutationInFlightRef.current = false;
      setLoading(false);
    }
  };

  const retryActivityRegeneration = async (regenerationId: string) => {
    const visit = documentVisit();
    if (
      !visit || !lesson || !csrf || activeActivityRegeneration
      || regenerationMutationInFlightRef.current
    ) return;
    regenerationMutationInFlightRef.current = true;
    const requestLessonId = visit.lessonId;
    const owned = () => visitRef.current === visit;
    setLoading(true);
    setError(null);
    try {
      const response = await lessonApiFetch(
        `/api/lessons/${requestLessonId}/regenerations/${regenerationId}/retry`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({ expected_revision: lesson.revision }),
        },
      );
      if (response.ok) {
        await reconcileActivityRegeneration(await response.json(), visit);
        return;
      }
      const envelope: ErrorEnvelope = await response.json().catch(() => ({} as ErrorEnvelope));
      if (endedSession(response.status, visit.scope) || !owned()) return;
      if (envelope.code === 'revision_conflict') {
        setError(errKey('err.lessonChangedReload'));
        await refreshLesson(visit);
      } else {
        setError(errOr(envelope.message, 'err.generic'));
      }
    } catch {
      if (owned()) setError(errKey('err.generic'));
    } finally {
      regenerationMutationInFlightRef.current = false;
      setLoading(false);
    }
  };

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
  const currentMode: LessonMode = route.mode || 'review';
  // Lesson content renders only for the lesson the visit owns, never a retained earlier one.
  const onRouteLesson = route.view === 'lesson' && !!route.lessonId && currentLessonId === route.lessonId;
  const viewLesson = onRouteLesson ? lesson : null;
  const viewBake = onRouteLesson ? bakeStatus : null;
  const isStudentSurface =
    currentMode === 'run' || (currentMode === 'conduct' && conductStudentPreview);
  const showTeacherLessonChrome = route.view === 'lesson' && currentMode !== 'conduct' && currentMode !== 'run';

  const returnToTeacherView = () => {
    if (currentMode === 'run') {
      openLesson(route.lessonId!, 'review');
      return;
    }
    setConductStudentPreview(false);
  };

  return (
    <div className={`teacher-app${printVariant ? ` print-variant-${printVariant}` : ''}${isStudentSurface ? ' studentframe' : ''}`} data-testid={isStudentSurface ? 'student-surface' : 'teacher-surface'}>
      <header className="appbar">
        <div className="page-shell">
          <div className="brand">{t('brand')}</div>
          {session && !isStudentSurface && (
            <nav className="appnav" aria-label={t('brand')}>
              <button
                type="button"
                className={route.view === 'catalog' ? 'on' : ''}
                onClick={() => navigate({ view: 'catalog' })}
              >
                {t('catalog.title')}
              </button>
              <button
                type="button"
                className={route.view === 'paste' ? 'on' : ''}
                onClick={() => navigate({ view: 'paste' })}
              >
                {t('catalog.new')}
              </button>
              <button
                type="button"
                className={route.view === 'settings' ? 'on' : ''}
                onClick={() => navigate({ view: 'settings' })}
              >
                {t('settings.title')}
              </button>
              {session.role === 'admin' && (
                <label className="admin-owner-picker" data-testid="admin-owner-picker">
                  <span>{t('catalog.owner')}</span>
                  <select
                    value={selectedOwnerId ?? session.teacher.id}
                    onChange={(event) => {
                      // A new owner is a new request scope: nothing pending may land in it.
                      // The catalog effect loads the new owner's rows once this commits.
                      leaveScreen(true);
                      setSelectedOwnerId(event.target.value);
                      setLesson(null);
                      setCatalog([]);
                      navigate({ view: 'catalog' });
                    }}
                  >
                    {lessonOwners.map((owner) => (
                      <option key={owner.id} value={owner.id}>{owner.display_name}</option>
                    ))}
                  </select>
                </label>
              )}
            </nav>
          )}
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
                {'credentials' in navigator && session.local_auth_disabled !== true && (
                  <button
                    type="button"
                    onClick={enrollPasskey}
                    className="link"
                    disabled={loading}
                    data-testid="passkey-enroll-btn"
                  >
                    {t('passkey.add')}
                  </button>
                )}
                <button onClick={logout} className="link" data-testid="logout-btn">{t('logout')}</button>
              </>
            )}
          </div>
        </div>
      </header>

      {session?.local_auth_disabled === true && (
        <div className="banner honest local-auth-disabled-banner" role="alert" data-testid="local-auth-disabled-banner">
          <span className="ic">!</span>
          <span>{t('localAuthDisabled.banner')}</span>
        </div>
      )}

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

      {recoveryCodes && (
        <div
          className="recovery-codes-overlay"
          role="presentation"
        >
          <section
            className="recovery-codes-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="recovery-codes-title"
            aria-describedby="recovery-codes-description"
            data-testid="recovery-codes-dialog"
          >
            <h2 id="recovery-codes-title">{t('recovery.codesTitle')}</h2>
            <p id="recovery-codes-description">{t('recovery.codesLead')}</p>
            <pre className="recovery-codes-list" data-testid="recovery-codes-list">
              {recoveryCodes.join('\n')}
            </pre>
            {recoveryCodesCopyStatus && (
              <p className={`recovery-codes-copy-status ${recoveryCodesCopyStatus}`} role="status">
                {t(recoveryCodesCopyStatus === 'ok' ? 'recovery.codesCopied' : 'recovery.codesCopyFailed')}
              </p>
            )}
            <div className="recovery-codes-actions">
              <button type="button" className="btn secondary" onClick={copyRecoveryCodes} autoFocus>
                {t('recovery.copyCodes')}
              </button>
              <button type="button" className="btn secondary" onClick={downloadRecoveryCodes}>
                {t('recovery.downloadCodes')}
              </button>
              <button
                type="button"
                className="btn primary"
                onClick={dismissRecoveryCodes}
                data-testid="recovery-codes-saved-btn"
              >
                {t('recovery.codesSaved')}
              </button>
            </div>
          </section>
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
          <p>{t('google.lead')}</p>
          {googleOptions ? (
            <div className="google-sign-in" data-testid="google-sign-in-button">
              <div ref={googleButtonRef} />
            </div>
          ) : (
            <p className="small">{t('google.unavailable')}</p>
          )}
          <details className="sign-in-alternatives" data-testid="sign-in-alternatives">
            <summary>{t('auth.otherWays')}</summary>
            <p className="small">{t('invite.lead')}</p>
            {'credentials' in navigator && (
              <button
                type="button"
                className="btn secondary"
                onClick={signInWithPasskey}
                disabled={loading}
                data-testid="passkey-sign-in-btn"
              >
                {t('passkey.signIn')}
              </button>
            )}
            <form
              className="recovery-code-sign-in"
              onSubmit={(event) => { event.preventDefault(); void redeemRecoveryCode(); }}
              data-testid="recovery-code-sign-in"
            >
              <label htmlFor="recovery-code-input">{t('recovery.signInLabel')}</label>
              <div className="recovery-code-sign-in-row">
                <input
                  id="recovery-code-input"
                  type="text"
                  value={recoveryCodeInput}
                  onChange={(event) => setRecoveryCodeInput(event.target.value)}
                  autoComplete="off"
                  autoCapitalize="characters"
                  spellCheck={false}
                  disabled={loading}
                  data-testid="recovery-code-input"
                />
                <button
                  type="submit"
                  className="btn secondary"
                  disabled={loading}
                  data-testid="recovery-code-submit"
                >
                  {t('recovery.signIn')}
                </button>
              </div>
              <p className="small">{t('recovery.signInHint')}</p>
            </form>
          </details>
        </main>
      )}

      {session && (
        <>
          {session.local_auth_disabled !== true && !isStudentSurface && !session.google_linked && googleOptions && (
            <aside className="google-setup-card page-shell" data-testid="google-setup-card">
              <div>
                <strong>{t('google.setupTitle')}</strong>
                <p>{t('google.setupLead')}</p>
              </div>
              <div ref={googleButtonRef} data-testid="google-setup-button" />
            </aside>
          )}
          {/* Paste / Hub — one-screen split create page; text is the centerpiece. */}
          {route.view === 'paste' && (
            <main className="hub create-hub page-shell">
              <section className="create-shell">
                <div className="create-left">
                  <h2>{t('paste.title')}</h2>
                  <div className="banner honest create-privacy">
                    <span className="ic">ℹ︎</span>
                    <span>{t('paste.disclose')}</span>
                  </div>

                  <div className="create-controls-stack">
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
                      <label>{t('paste.methodology')}</label>
                      <div className="pedcards">
                        <button
                          type="button"
                          className="pedcard on"
                          disabled
                          aria-disabled="true"
                          data-testid="methodology-ttt"
                        >
                          <b>{t('paste.methodology.ttt')}</b>
                          <span className="d">{t('paste.methodology.hint')}</span>
                        </button>
                      </div>
                    </div>

                    <div className="field">
                      <label>{t('paste.duration')}</label>
                      <output className="inputbox" data-testid="qualified-duration">
                        {t('paste.qualifiedDuration')}
                      </output>
                    </div>

                    <div className="field">
                      <label>{t('paste.focus')}</label>
                      <input
                        className="inputbox"
                        type="text"
                        value={grammarFocus}
                        onChange={e => setGrammarFocus(e.target.value)}
                        placeholder={t('paste.focusPh')}
                        maxLength={120}
                        data-testid="grammar-focus-input"
                      />
                    </div>
                  </div>

                  <button
                    className="btn primary create-submit"
                    onClick={startBake}
                    disabled={
                      loading
                      || pasteTextMissing
                      || !qualifiedModels.some(model => model.id === selectedModelId)
                    }
                  >
                    {loading ? t('paste.submitting') : t('paste.submit')}
                  </button>
                </div>

                <div className="create-right">
                  <label htmlFor="anchor-text-input">
                    {t('paste.textLabel')}
                  </label>
                  {pasteTextMissing && (
                    <p
                      id="paste-text-required-guidance"
                      className="paste-required-guidance"
                      role="status"
                      data-testid="paste-text-required-guidance"
                    >
                      {t('paste.textRequired')}
                    </p>
                  )}
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
                    id="anchor-text-input"
                    className="inputbox"
                    value={pasteText}
                    onChange={e => setPasteText(e.target.value)}
                    placeholder={t('paste.textPh')}
                    required
                    aria-required="true"
                    aria-describedby={pasteTextMissing ? 'paste-text-required-guidance' : undefined}
                    data-testid="anchor-text-input"
                  />
                </div>
              </section>
            </main>
          )}

          {route.view === 'catalog' && (
            <main className="lessons-page page-shell">
              <div className="lessons-page-head">
                <h1>{t('catalog.title')}</h1>
                <span className="lessons-page-actions">
                  <button className="btn ghost" onClick={() => { void loadCatalog(); }}>{t('catalog.refresh')}</button>
                  <button className="btn primary" onClick={() => navigate({ view: 'paste' })}>{t('catalog.new')}</button>
                </span>
              </div>
              {catalog.length === 0 ? (
                <section className="emptystate" data-testid="lessons-empty-state">
                  <h2>{t('catalog.emptyTitle')}</h2>
                  <p>{t('catalog.empty')}</p>
                  <button className="btn primary" onClick={() => navigate({ view: 'paste' })}>{t('catalog.new')}</button>
                </section>
              ) : (
                <section className="catalog lessons-catalog" aria-label={t('catalog.title')}>
                  <ul>
                    {catalog.map(item => {
                      const chipClass = item.status === 'ready' ? 'ok' : item.status === 'baking' ? 'warn' : item.status === 'failed' ? 'bad' : item.status === 'cancelled' ? 'muted cancelled' : 'muted';
                      const snippet = item.anchor_snippet?.trim();
                      return (
                        <li key={item.id} className="listrow lesson-list-row">
                          <button data-testid="catalog-open-btn" onClick={() => openLesson(item.id, 'review')}>
                            <span className="lesson-list-main">
                              <span className="title">{item.title || item.id.slice(0, 8)}</span>
                              {snippet && <span className="anchor-snippet">{t('catalog.anchor', { snippet })}</span>}
                              <span className="lesson-list-meta">{t('catalog.meta', { level: item.level || 'B1', duration: item.duration, methodology: t('paste.methodology.ttt') })}</span>
                            </span>
                            <span className={`chip ${chipClass}`}>{t(statusKey(item.status))}</span>
                            <span className="when">{new Date(item.created_at).toLocaleString(lang === 'en' ? 'en' : 'uk')}</span>
                          </button>
                          {item.status === 'ready' && item.accepted && (
                            <span className="lesson-drive-paths">
                              <button type="button" className="btn ghost" onClick={() => openLesson(item.id, 'conduct')}>{t('catalog.conduct')}</button>
                              <button type="button" className="btn ghost" onClick={() => openLessonForPrint(item.id)}>{t('catalog.print')}</button>
                            </span>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}
            </main>
          )}

          {route.view === 'settings' && (
            <main className="settings-page page-shell">
              <section className="settings-card">
                <h1>{t('settings.title')}</h1>
                <div className="field">
                  <label>{t('settings.defaultMethodology')}</label>
                  <div className="pedcards">
                    <button type="button" className="pedcard on" disabled aria-disabled="true">
                      <b>{t('paste.methodology.ttt')}</b>
                      <span className="d">{t('paste.methodology.hint')}</span>
                    </button>
                  </div>
                </div>
              </section>
            </main>
          )}

          {/* Lesson view */}
          {/* #93 item1: render on route.lessonId to avoid blank on direct/refresh; set current early via ready effect */}
          {route.view === 'lesson' && route.lessonId && (
            <main className={`lesson-view page-shell${currentMode === 'run' ? ' student-run-view' : ''}`}>
              {showTeacherLessonChrome && (
              <div className="lesson-header">
                <button className="btn ghost" onClick={() => navigate({ view: 'catalog' })}>{t('lesson.back')}</button>
                {viewLesson && <h2>{viewLesson.lesson.title}</h2>}
                {viewLesson && (
                  <span className="lesson-model" data-testid="lesson-model">
                    {t('lesson.model', { model: viewLesson.logical_model_id ?? t('lesson.modelUnknown') })}
                  </span>
                )}
                <div className="lesson-actions">
                  <button
                    type="button"
                    className="btn ghost selected"
                    aria-current="page"
                    onClick={() => openLesson(route.lessonId!, 'review')}
                  >{t('lesson.reviewMode')}</button>
                  {viewLesson && viewLesson.lesson.status === 'ready' && (
                    <button
                      type="button"
                      className="btn ghost"
                      data-testid="enter-student-mode-btn"
                      onClick={() => openLesson(route.lessonId!, 'run')}
                    >
                      {t('lesson.showAsStudent')}
                    </button>
                  )}
                  {viewLesson && currentMode === 'review' && viewLesson.lesson.status === 'ready' && (
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() => setShowAnswers(v => !v)}
                    >
                      {showAnswers ? t('lesson.hideAnswers') : t('lesson.showAnswers')}
                    </button>
                  )}
                  {viewLesson && viewLesson.lesson.status === 'ready' && (
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={copyLessonAsNew}
                      data-testid="copy-lesson-as-new"
                    >
                      {t('lesson.copyAsNew')}
                    </button>
                  )}
                  {viewLesson && currentMode === 'review' && viewLesson.lesson.status === 'ready' && (
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
                  {viewLesson && viewLesson.lesson.accepted && (
                    <button className="btn primary" onClick={() => openLesson(route.lessonId!, 'conduct')}>{t('lesson.conduct')}</button>
                  )}
                  {viewLesson && viewLesson.lesson.accepted && <span className="chip ok">{t('accepted')}</span>}
                  {viewLesson && viewLesson.lesson.status === 'ready' && (
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
                  <button className="btn ghost" onClick={downloadJSON} disabled={!viewLesson || !viewLesson.lesson.accepted}>{t('lesson.downloadJson')}</button>
                </div>
              </div>
              )}

              {viewBake && !viewLesson && (
                <div className="baking" data-testid="baking-status-view">
                  <div className="steps">
                    <div className={`step ${viewBake.status === 'baking' || viewBake.status === 'draft' ? 'now' : viewBake.status === 'failed' ? 'fail' : 'done'}`}>
                      <div className="dot">{viewBake.status === 'baking' || viewBake.status === 'draft' ? '⋯' : viewBake.status === 'failed' ? '!' : '✓'}</div>
                      <div>
                        <b>{t('bake.statusPrefix')}{t(statusKey(viewBake.status))}</b>
                        <div className="sd" data-testid="bake-attempt">{t('bake.attempt', { attempt: viewBake.attempt })}</div>
                        <div className="sd" data-testid="baking-subline">{bakingSubline()}</div>
                        {viewBake.status === 'baking' && (
                          <div className="sd bake-elapsed" data-testid="baking-elapsed">
                            {t('bake.elapsed', { clock: formatBakeElapsedClock(bakeElapsedMs) })}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                  {(viewBake.status === 'failed' || viewBake.status === 'cancelled') && (
                    <div className="recovery-card banner fail" data-testid="failure-recovery">
                      <span className="ic">!</span>
                      <div className="recovery-body">
                        <p data-testid="failure-recovery-body" data-failure-code={viewBake.failure_code || ''}>
                          {viewBake.status === 'cancelled' ? t('bake.cancelledBody') : t(recoveryBodyKey(viewBake.failure_code))}
                        </p>
                        <div className="recovery-actions">
                          <button
                            type="button"
                            className="btn primary"
                            onClick={() => void mutateBake('retry')}
                            disabled={bakeMutation !== null}
                            data-testid="failure-retry-btn"
                          >
                            {bakeMutation === 'retry' ? t('bake.retrying') : t('recovery.retry')}
                          </button>
                          <button
                            type="button"
                            className="btn ghost link-back"
                            onClick={() => navigate({ view: 'catalog' })}
                          >
                            {t('recovery.back')}
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                  {(viewBake.status === 'baking' || viewBake.status === 'draft') && polling && (
                    <p className="hint" style={{marginTop:6}} data-testid="baking-polling">{t('bake.updating')}</p>
                  )}
                  {(viewBake.status === 'baking' || viewBake.status === 'draft') && (
                    <div className="bake-actions">
                      <button
                        type="button"
                        className="btn danger"
                        onClick={() => void mutateBake('cancel')}
                        disabled={bakeMutation !== null}
                        data-testid="bake-cancel-btn"
                      >
                        {bakeMutation === 'cancel' ? t('bake.cancelling') : t('bake.cancel')}
                      </button>
                      <button className="btn ghost" onClick={() => openLesson(route.lessonId!, currentMode)} disabled={bakeMutation !== null}>{t('bake.checkNow')}</button>
                      <p className="hint bake-cancel-hint">{t('bake.cancelHint')}</p>
                    </div>
                  )}
                  {(viewBake.status === 'failed' || viewBake.status === 'cancelled') && (
                    <section className="bake-attempt-history" aria-label={t('bake.history')}>
                      <h3>{t('bake.history')}</h3>
                      {viewBake.attempt_history.length === 0 ? <p>{t('bake.historyEmpty')}</p> : (
                        <ol>
                          {viewBake.attempt_history.map((entry) => (
                            <li key={`${entry.attempt}-${entry.completed_at}`}>
                              {t('bake.attempt', { attempt: entry.attempt })} — {t(statusKey(entry.status))}
                            </li>
                          ))}
                        </ol>
                      )}
                    </section>
                  )}
                </div>
              )}

              {!viewLesson && !viewBake && !error && (
                <p style={{ color: 'var(--ink-soft)', padding: '12px 0' }}>{t('loading')}</p>
              )}

              {viewLesson && (
                <>
                  {showTeacherLessonChrome && (
                  <div className="meta">
                    <div>{t('meta.level', { level: viewLesson.lesson.level, duration: viewLesson.lesson.duration })}</div>
                    <div>{t('meta.revision', { rev: viewLesson.revision, acc: viewLesson.lesson.accepted ? t('meta.yes') : t('meta.no') })}</div>
                    {(viewLesson.grammar_focus ?? viewLesson.lesson.focus) && <div>{t('meta.focus')}{viewLesson.grammar_focus ?? viewLesson.lesson.focus}</div>}
                  </div>
                  )}

                  {currentMode === 'run' && (
                    <>
                      <div className="rolebanner student" data-testid="student-banner">
                        {t('run.studentBanner')}
                      </div>
                      <div className="student-toolbar noprint">
                        <span className="chip info">
                          {t('run.toolbarChip', { level: viewLesson.lesson.level, duration: viewLesson.lesson.duration })}
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
                      {viewLesson.lesson.anchor?.text && (
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
                              {renderAnchorBody(viewLesson.lesson.anchor.text, 'anchor-text-body')}
                            </div>
                          )}
                        </div>
                      )}
                      <div className="student-sheet paper">
                        <p className="docsheet-title">{viewLesson.lesson.title}</p>
                        <p className="student-sheet-sub">
                          {t('run.sheetSub', { duration: viewLesson.lesson.duration })}
                        </p>
                        <div className={`modes run`}>
                          {renderBlocks(viewLesson, 'run')}
                        </div>
                        <p className="docsheet-footer" data-testid="honesty-footer">
                          {t('print.honestyFooter')}
                        </p>
                      </div>
                    </>
                  )}

                  {viewLesson.lesson.anchor?.text && (
                    <div className="anchor-print" data-testid="anchor-print">
                      <h4 className="dochead"><span className="pn">☰</span>{t('anchor.readingHead')}</h4>
                      {renderAnchorBody(viewLesson.lesson.anchor.text)}
                    </div>
                  )}

                  {currentMode === 'review' && (
                    <ReviewWorkbench
                      resource={viewLesson}
                      showAnswers={showAnswers}
                      loading={loading}
                      onMoveBlock={moveBlock}
                      onRemoveBlock={removeBlock}
                      onIncludeReserve={includeReserveBlock}
                      onRestoreRejected={restoreRejected}
                      onAckWarning={ackWarning}
                      onSaveActivity={replaceActivity}
                      onAcceptLesson={acceptLesson}
                      onReturnToDraft={returnToDraft}
                      onActivityFeedback={putActivityFeedback}
                      onRegenerateActivity={createActivityRegeneration}
                      onRetryRegeneration={retryActivityRegeneration}
                      allWarningsAcked={allVisibleWarningsAcked(viewLesson)}
                      studentPrint={printVariant === 'student'}
                    />
                  )}

                  {currentMode === 'conduct' && viewLesson && (
                    <Conductor
                      lessonDoc={viewLesson.lesson}
                      onExit={() => {
                        setConductStudentPreview(false);
                        openLesson(route.lessonId!, 'review');
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
