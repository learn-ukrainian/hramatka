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
  type BakeRequestPayload,
  type BakeProgress,
  type ClipboardMode,
} from './app-helpers';
import Conductor from './Conductor';
import { useT, statusKey, recoveryBodyKey, modelUnavailableKey, type ChromeKey } from './i18n';
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

interface LessonAttempt {
  attempt: number;
  status: 'ready' | 'failed' | 'cancelled';
  failure_code: string | null;
  started_at: string | null;
  completed_at: string;
  retry_requested_revision: number | null;
}

export interface LessonStatus {
  id: string;
  status: LessonState;
  step: string;
  revision: number;
  attempt: number;
  attempt_history: LessonAttempt[];
  failure_code: string | null;
  failure_message: string | null;
  created_at: string;
  updated_at: string;
  /** Current attempt start; absent only when reading an older API deployment. */
  started_at?: string | null;
  progress?: BakeProgress;
}

export function statusCardFromApi(
  status: LessonStatus,
  previous?: { attempt?: number; startedAt?: string },
) {
  const isNewAttempt = previous?.attempt !== status.attempt;
  // Older API deployments do not expose started_at.  Keep their same-attempt
  // clock stable, but never carry an earlier attempt into a retry.  updated_at
  // is the least stale durable fallback at the current attempt transition.
  const fallbackStartedAt = status.status === 'baking'
    ? (isNewAttempt
      ? status.updated_at || status.created_at
      : previous?.startedAt || status.updated_at || status.created_at)
    : (isNewAttempt ? undefined : previous?.startedAt);
  return {
    status: status.status,
    step: status.step || '',
    revision: status.revision,
    attempt: status.attempt,
    attempt_history: status.attempt_history || [],
    failure: status.failure_message || undefined,
    failure_code: status.failure_code ?? null,
    progress: status.progress || undefined,
    startedAt: status.started_at ?? fallbackStartedAt,
  };
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
type View = 'invite' | 'paste' | 'catalog' | 'settings' | 'lesson';

interface RouteState {
  view: View;
  lessonId?: string;
  mode?: 'review' | 'run' | 'conduct';
}

function useSimpleRouter() {
  const [route, setRoute] = useState<RouteState>({ view: 'invite' });

  const navigate = useCallback((r: RouteState) => {
    const hash = r.lessonId
      ? `#/lessons/${r.lessonId}${r.mode ? '?mode=' + r.mode : ''}`
      : r.view === 'catalog'
        ? '#/lessons'
        : r.view === 'settings'
          ? '#/settings'
          : '#/';
    history.pushState(null, '', hash);
    setRoute(r);
  }, []);

  useEffect(() => {
    const parse = () => {
      const h = window.location.hash || '#/';
      if (h === '#/lessons') {
        setRoute({ view: 'catalog' });
      } else if (h === '#/settings') {
        setRoute({ view: 'settings' });
      } else if (h.startsWith('#/lessons/')) {
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

  // Baking / lesson state
  const [currentLessonId, setCurrentLessonId] = useState<string | null>(null);
  const [lesson, setLesson] = useState<LessonResource | null>(null);
  const [catalog, setCatalog] = useState<LessonCatalogItem[]>([]);
  const [bakeStatus, setBakeStatus] = useState<{
    status: LessonState;
    step: string;
    revision: number;
    attempt: number;
    attempt_history: LessonAttempt[];
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
  const currentLessonIdRef = useRef<string | null>(null);
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
  const [pendingPrintLessonId, setPendingPrintLessonId] = useState<string | null>(null);
  const [conductStudentPreview, setConductStudentPreview] = useState(false);

  const lessonApiFetch = (path: string, init?: RequestInit, ownerId = selectedOwnerId): Promise<Response> => {
    if (session?.role !== 'admin' || !ownerId || !path.startsWith('/api/lessons')) {
      return apiFetch(path, init);
    }
    const separator = path.includes('?') ? '&' : '?';
    return apiFetch(`${path}${separator}owner_id=${encodeURIComponent(ownerId)}`, init);
  };
  // Clipboard export notice (Sol P1-5 folded to i18n): stores key so t() reflects current lang.
  const [clipboardNotice, setClipboardNotice] = useState<{ kind: 'ok' | 'fail'; key: ChromeKey } | null>(null);

  const activeActivityRegeneration = lesson?.activity_regenerations?.find(
    (item) => item.status === 'queued' || item.status === 'running',
  );

  useEffect(() => {
    currentLessonIdRef.current = currentLessonId;
  }, [currentLessonId]);

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
    setDuration(NEW_LESSON_DURATION);
    setGrammarFocus('');
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
    setAnchorOpen(false);
    setPrintVariant(null);
    setPendingPrintLessonId(null);
    setBakeElapsedMs(0);
    resetLang(); // #106 boundary: UI language back to default UA + clear persisted choice
    setClipboardNotice(null);
    setRecoveryCodes(null);
    setRecoveryCodesCopyStatus(null);
    setRecoveryCodeInput('');
  }, [clearPoll, resetLang]);

  const restoreFormFromPayload = useCallback((payload: BakeRequestPayload) => {
    setPasteText(payload.text);
    setDuration(NEW_LESSON_DURATION);
    setGrammarFocus(payload.grammarFocus ?? payload.focus ?? '');
    setSelectedModelId(resolveQualifiedModelId(qualifiedModels, payload.logicalModelId));
    setRestoredTextNotice(true);
  }, [qualifiedModels]);

  const refreshSession = async () => {
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
  };

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
    };
    let promise: Promise<boolean>;
    promise = run().finally(() => {
      if (redeemInFlightRef.current === promise) redeemInFlightRef.current = null;
    });
    redeemInFlightRef.current = promise;
    return promise;
  }, [resetSessionScopedState, navigate]);

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
      try { await loadCatalog(); } catch {}
    } catch {
      setError(errKey('recovery.redeemFailed'));
    } finally {
      setLoading(false);
    }
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
    duration: typeof NEW_LESSON_DURATION;
    grammarFocus: string | null;
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
      if (res.status === 202) {
        saveLastBakeRequest(payload);
        lastBakeRef.current = payload;
        setPasteText(text);
        setDuration(NEW_LESSON_DURATION);
        setGrammarFocus(source.grammarFocus?.trim() || '');
        setCurrentLessonId(id);
        const startedAt = new Date().toISOString();
        setBakeStatus({
          status: data.status || 'baking', step: 'bake.step.textReceived',
          revision: data.revision || 1, attempt: 1, attempt_history: [], startedAt,
        });
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
      grammarFocus: grammarFocus || null,
      logicalModelId: selectedModelId,
    });
  };

  const mutateBake = async (operation: 'cancel' | 'retry') => {
    const lid = currentLessonId || route.lessonId || null;
    const current = bakeStatus;
    if (
      !lid || !current || bakeMutationInFlightRef.current
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
      if (res.ok) {
        const next = statusCardFromApi(data as LessonStatus, current);
        setBakeStatus(next);
        setCurrentLessonId(lid);
        setLesson(null);
        await loadCatalog();
        if (next.status === 'draft' || next.status === 'baking') {
          setBakeElapsedMs(0);
          pollStatus(lid);
        } else {
          clearPoll();
        }
        return;
      }
      // Another tab may have completed the same idempotent intent or advanced
      // the revision.  Re-read before offering the next valid action.
      if (res.status === 409) {
        const statusRes = await lessonApiFetch(`/api/lessons/${lid}/status`);
        if (statusRes.ok) {
          const refreshed = statusCardFromApi(await statusRes.json() as LessonStatus, current);
          setBakeStatus(refreshed);
          if (refreshed.status === 'draft' || refreshed.status === 'baking') pollStatus(lid);
        }
      }
      const e: ErrorEnvelope = data;
      handleApiError(e);
    } catch {
      setError(errKey('err.bakeFailed'));
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
        const res = await lessonApiFetch(`/api/lessons/${id}/status`);
        st = await res.json();
        // Re-check after await in case switch happened during network
        if (activePollIdRef.current !== id) {
          return;
        }
        setBakeStatus((prev) => statusCardFromApi(st as LessonStatus, prev || undefined));
        if (st.status === 'ready') {
          clearPoll();
          await openLesson(id, 'review');
          await loadCatalog();
          return;
        }
        if (st.status === 'failed' || st.status === 'cancelled') {
          clearPoll();
          return;
        }
      } catch {
        if (activePollIdRef.current !== id) {
          return;
        }
        setBakeStatus((prev) => prev || {
          status: 'baking', step: 'bake.step.updating', revision: 1, attempt: 1, attempt_history: [],
        });
      }
      if (attempts < max) {
        schedule(attempts < 30 ? 800 : 2500);
      } else {
        clearPoll();
        if (st && st.status !== 'ready' && st.status !== 'failed' && st.status !== 'cancelled') {
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
      const res = await lessonApiFetch(`/api/lessons/${id}`);
      if (res.status === 200) {
        const lr: LessonResource = await res.json();
        setLesson(lr);
        setBakeStatus(null);
        setCurrentLessonId(id);
        setLocalAcks(lr.warning_acknowledgements || []);
        // The student must receive the source text before the exercises; keep the
        // teacher's review view compact, but open the reading on the run surface.
        setAnchorOpen(mode === 'run');
        navigate({ view: 'lesson', lessonId: id, mode });
      } else if (res.status === 409) {
        await res.json().catch(() => ({}));
        setLesson(null);
        setCurrentLessonId(id);
        navigate({ view: 'lesson', lessonId: id, mode });
        try {
          const statusRes = await lessonApiFetch(`/api/lessons/${id}/status`);
          if (statusRes.ok) {
            const st = await statusRes.json();
            if (st.status === 'failed' || st.status === 'cancelled') {
              setBakeStatus(statusCardFromApi(st as LessonStatus));
              setBakeElapsedMs(0);
              return;
            }
            const mappedStatus = statusCardFromApi(st as LessonStatus);
            setBakeStatus({
              ...mappedStatus,
              status: st.status === 'ready' ? 'baking' : (st.status as LessonState),
              step: st.step || 'bake.step.tasksComposed',
            });
            setBakeElapsedMs(0);
            pollStatus(id);
            return;
          }
        } catch {
          // fall through to generic baking poll
        }
        const startedAt = new Date().toISOString();
        setBakeStatus({
          status: 'baking', step: 'bake.step.tasksComposed', revision: 1, attempt: 1, attempt_history: [], startedAt,
        });
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

  const loadCatalog = async (ownerId?: string) => {
    // #93 item 2: auto-loaded once session ready; guard removed, manual button remains
    const res = await lessonApiFetch('/api/lessons', undefined, ownerId);
    if (res.ok) {
      const data = await res.json();
      const local = loadLocalCatalogEntries(lastBakeRef.current);
      setCatalog(mergeCatalogLessons(data.lessons || [], local));
    }
  };

  useEffect(() => {
    if (sessionReady && session?.role === 'admin' && selectedOwnerId) {
      void loadCatalog();
    }
    // Owner selection changes the complete request scope; reload only after
    // that state has committed, never by trusting a lesson id alone.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedOwnerId, session?.role, sessionReady]);

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
      const res = await lessonApiFetch(`/api/lessons/${currentLessonId}/accept`, {
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
      const res = await lessonApiFetch(`/api/lessons/${currentLessonId}/draft`, {
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

  const openLessonForPrint = (id: string) => {
    setPendingPrintLessonId(id);
    void openLesson(id, 'review');
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

  const applyActivityRegeneration = (entry: ActivityRegenerationEntry) => {
    setLesson((current) => {
      if (!current || current.lesson_id !== entry.lesson_id) return current;
      return {
        ...current,
        activity_regenerations: [
          entry,
          ...(current.activity_regenerations || []).filter((item) => item.id !== entry.id),
        ],
      };
    });
  };

  const reconcileActivityRegeneration = async (entry: ActivityRegenerationEntry) => {
    if (entry.status !== 'succeeded' && entry.status !== 'failed') {
      applyActivityRegeneration(entry);
      return;
    }
    const resourceResponse = await lessonApiFetch(`/api/lessons/${entry.lesson_id}`);
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
    const resource: LessonResource = await resourceResponse.json();
    if (currentLessonIdRef.current !== entry.lesson_id) return;
    applyLessonResource(resource);
    if (entry.status === 'succeeded') await loadCatalog();
  };

  // A replacement is a separate durable job. Reloading the page reconstructs
  // this poll from `activity_regenerations`; the old block stays visible meanwhile.
  useEffect(() => {
    const regeneration = activeActivityRegeneration;
    const lessonId = lesson?.lesson_id;
    if (!regeneration || !lessonId) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const response = await lessonApiFetch(
          `/api/lessons/${lessonId}/regenerations/${regeneration.id}`,
        );
        if (!response.ok) {
          if (response.status === 401) {
            const envelope: ErrorEnvelope = await response.json().catch(() => ({} as ErrorEnvelope));
            if (!cancelled) handleApiError(envelope);
            return;
          }
          throw new Error('regeneration poll unavailable');
        }
        const status: ActivityRegenerationEntry = await response.json();
        if (cancelled) return;
        if (status.status === 'succeeded' || status.status === 'failed') {
          const resourceResponse = await lessonApiFetch(`/api/lessons/${lessonId}`);
          if (!cancelled && resourceResponse.ok) {
            const resource: LessonResource = await resourceResponse.json();
            if (cancelled) return;
            applyLessonResource(resource);
            if (status.status === 'succeeded') await loadCatalog();
            return;
          }
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
  }, [lesson?.lesson_id, activeActivityRegeneration?.id]);

  const handleRevisionConflict = async (expectedLessonId?: string) => {
    const lessonId = expectedLessonId || currentLessonIdRef.current;
    if (!lessonId || currentLessonIdRef.current !== lessonId) return;
    await openLesson(lessonId, 'review');
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
      const res = await lessonApiFetch(path, init);
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
    if (
      !lesson || !currentLessonId || !csrf || activeActivityRegeneration
      || regenerationMutationInFlightRef.current
    ) return;
    regenerationMutationInFlightRef.current = true;
    const requestLessonId = currentLessonId;
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
        await reconcileActivityRegeneration(await response.json());
        return;
      }
      const envelope: ErrorEnvelope = await response.json().catch(() => ({} as ErrorEnvelope));
      if (currentLessonIdRef.current !== requestLessonId) return;
      if (envelope.code === 'revision_conflict') {
        setError(errKey('err.lessonChangedReload'));
        await handleRevisionConflict(requestLessonId);
      } else {
        setError(errOr(envelope.message, 'err.generic'));
      }
    } catch {
      if (currentLessonIdRef.current === requestLessonId) setError(errKey('err.generic'));
    } finally {
      regenerationMutationInFlightRef.current = false;
      setLoading(false);
    }
  };

  const retryActivityRegeneration = async (regenerationId: string) => {
    if (
      !lesson || !currentLessonId || !csrf || activeActivityRegeneration
      || regenerationMutationInFlightRef.current
    ) return;
    regenerationMutationInFlightRef.current = true;
    const requestLessonId = currentLessonId;
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
        await reconcileActivityRegeneration(await response.json());
        return;
      }
      const envelope: ErrorEnvelope = await response.json().catch(() => ({} as ErrorEnvelope));
      if (currentLessonIdRef.current !== requestLessonId) return;
      if (envelope.code === 'revision_conflict') {
        setError(errKey('err.lessonChangedReload'));
        await handleRevisionConflict(requestLessonId);
      } else {
        setError(errOr(envelope.message, 'err.generic'));
      }
    } catch {
      if (currentLessonIdRef.current === requestLessonId) setError(errKey('err.generic'));
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
                      clearPoll();
                      setSelectedOwnerId(event.target.value);
                      setLesson(null);
                      setCurrentLessonId(null);
                      setCatalog([]);
                      void loadCatalog(event.target.value);
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
                {lesson && <h2>{lesson.lesson.title}</h2>}
                {lesson && (
                  <span className="lesson-model" data-testid="lesson-model">
                    {t('lesson.model', { model: lesson.logical_model_id ?? t('lesson.modelUnknown') })}
                  </span>
                )}
                <div className="lesson-actions">
                  <button
                    type="button"
                    className="btn ghost selected"
                    aria-current="page"
                    onClick={() => openLesson(currentLessonId || route.lessonId!, 'review')}
                  >{t('lesson.reviewMode')}</button>
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
                    <div className={`step ${bakeStatus.status === 'baking' || bakeStatus.status === 'draft' ? 'now' : bakeStatus.status === 'failed' ? 'fail' : 'done'}`}>
                      <div className="dot">{bakeStatus.status === 'baking' || bakeStatus.status === 'draft' ? '⋯' : bakeStatus.status === 'failed' ? '!' : '✓'}</div>
                      <div>
                        <b>{t('bake.statusPrefix')}{t(statusKey(bakeStatus.status))}</b>
                        <div className="sd" data-testid="bake-attempt">{t('bake.attempt', { attempt: bakeStatus.attempt })}</div>
                        <div className="sd" data-testid="baking-subline">{bakingSubline()}</div>
                        {bakeStatus.status === 'baking' && (
                          <div className="sd bake-elapsed" data-testid="baking-elapsed">
                            {t('bake.elapsed', { clock: formatBakeElapsedClock(bakeElapsedMs) })}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                  {(bakeStatus.status === 'failed' || bakeStatus.status === 'cancelled') && (
                    <div className="recovery-card banner fail" data-testid="failure-recovery">
                      <span className="ic">!</span>
                      <div className="recovery-body">
                        <p data-testid="failure-recovery-body" data-failure-code={bakeStatus.failure_code || ''}>
                          {bakeStatus.status === 'cancelled' ? t('bake.cancelledBody') : t(recoveryBodyKey(bakeStatus.failure_code))}
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
                  {(bakeStatus.status === 'baking' || bakeStatus.status === 'draft') && polling && (
                    <p className="hint" style={{marginTop:6}} data-testid="baking-polling">{t('bake.updating')}</p>
                  )}
                  {(bakeStatus.status === 'baking' || bakeStatus.status === 'draft') && (
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
                      <button className="btn ghost" onClick={() => (currentLessonId || route.lessonId) && openLesson(currentLessonId || route.lessonId!)} disabled={bakeMutation !== null}>{t('bake.checkNow')}</button>
                      <p className="hint bake-cancel-hint">{t('bake.cancelHint')}</p>
                    </div>
                  )}
                  {(bakeStatus.status === 'failed' || bakeStatus.status === 'cancelled') && (
                    <section className="bake-attempt-history" aria-label={t('bake.history')}>
                      <h3>{t('bake.history')}</h3>
                      {bakeStatus.attempt_history.length === 0 ? <p>{t('bake.historyEmpty')}</p> : (
                        <ol>
                          {bakeStatus.attempt_history.map((entry) => (
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

              {!lesson && !bakeStatus && (
                <p style={{ color: 'var(--ink-soft)', padding: '12px 0' }}>{t('loading')}</p>
              )}

              {lesson && (
                <>
                  {showTeacherLessonChrome && (
                  <div className="meta">
                    <div>{t('meta.level', { level: lesson.lesson.level, duration: lesson.lesson.duration })}</div>
                    <div>{t('meta.revision', { rev: lesson.revision, acc: lesson.lesson.accepted ? t('meta.yes') : t('meta.no') })}</div>
                    {(lesson.grammar_focus ?? lesson.lesson.focus) && <div>{t('meta.focus')}{lesson.grammar_focus ?? lesson.lesson.focus}</div>}
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
                      allWarningsAcked={allVisibleWarningsAcked(lesson)}
                      studentPrint={printVariant === 'student'}
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
