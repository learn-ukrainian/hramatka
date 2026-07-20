/**
 * Dev-only stub API server for the teacher frontend.
 * Lives under hramatka/app/dev/ ONLY. Generated from openapi.yaml surface + golden fixtures.
 * Implements happy path + scripted error flows. No production logic.
 *
 * Run: node dev/server.js
 * Listens on PORT=8787 by default.
 */

import http from 'http';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import crypto from 'crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Paths to frozen contracts (relative to this dev/ dir)
const VENDOR_FIXTURES = path.resolve(__dirname, '../../vendor/lu.activity.v1@1.0.0-ffd54054/lu.activity.v1.fixtures.json');
const CONTRACTS_SCHEMA = path.resolve(__dirname, '../../contracts/pilot_activity_types.schema.json');

const PORT = process.env.PORT ? Number(process.env.PORT) : 8787;
const ORIGIN = process.env.ORIGIN || 'http://localhost:5173'; // for dev; prod uses real https origin

let goldenActivities = [];
try {
  const raw = fs.readFileSync(VENDOR_FIXTURES, 'utf8');
  goldenActivities = JSON.parse(raw);
} catch (e) {
  console.error('Failed to load golden fixtures for stub. Using minimal inline set.', e);
  goldenActivities = [];
}

/**
 * Outer block answer_key for stub lessons.
 *
 * The frozen vendored golden activity envelope stays contract-pure (items only).
 * Teacher-only deliberate-error intent (#164 / #208) lives on the BLOCK key as
 * `corrections: [{sentence, error, correction}]`. Derive a valid triple from the
 * golden error-correction payload when the legs match structurally.
 */
function blockAnswerKeyFromActivity(type, act) {
  const envelopeKey = act?.answer_key;
  if (type === 'error-correction' && envelopeKey && typeof envelopeKey === 'object') {
    const items = Array.isArray(envelopeKey.items) ? envelopeKey.items : [];
    const payloadItems = Array.isArray(act?.payload?.items) ? act.payload.items : [];
    const outer = { items: JSON.parse(JSON.stringify(items)) };
    const triple = deriveErrorCorrectionTriple(payloadItems, items);
    if (triple) outer.corrections = [triple];
    return outer;
  }
  // Other types keep the historical stub display string (not the proof surface).
  if (typeof envelopeKey === 'string') return envelopeKey;
  if (envelopeKey && typeof envelopeKey === 'object') {
    return JSON.stringify(envelopeKey).slice(0, 80);
  }
  return 'Ключ відповіді';
}

/**
 * Representative triple for the golden error-correction fixture:
 * wrong "Це моя стіл." → corrected "Це мій стіл." (моя → мій).
 * Only emits when error is a substring of sentence and replace yields a key item.
 */
function deriveErrorCorrectionTriple(payloadItems, answerItems) {
  const wrong = payloadItems.find((s) => typeof s === 'string' && s);
  const corrected = answerItems.find((s) => typeof s === 'string' && s);
  if (!wrong || !corrected) return null;
  // Smallest representative pair matching the vendored golden fixture wording.
  const candidates = [
    { error: 'моя', correction: 'мій' },
    { error: 'Києв', correction: 'Києві' },
  ];
  for (const { error, correction } of candidates) {
    if (!error || !correction) continue;
    if (!wrong.includes(error)) continue;
    if (wrong.replace(error, correction) === corrected) {
      return { sentence: wrong, error, correction };
    }
  }
  return null;
}

/**
 * #208: carry prior outer corrections only when they still structurally describe
 * the edited activity (mirrors hramatka/api/lesson.py::_preserved_error_correction_intent).
 */
function preservedErrorCorrectionIntent(previousKey, activity, newAnswerKey) {
  if (!previousKey || typeof previousKey !== 'object' || Array.isArray(previousKey)) return null;
  const raw = previousKey.corrections;
  if (!Array.isArray(raw) || raw.length === 0) return null;
  if (!activity || typeof activity !== 'object') return null;
  const payload = activity.payload;
  if (!payload || typeof payload !== 'object') return null;
  const payloadItems = Array.isArray(payload.items) ? payload.items : null;
  if (!payloadItems) return null;
  const wrongSentences = new Set(payloadItems.filter((s) => typeof s === 'string' && s));
  if (!newAnswerKey || typeof newAnswerKey !== 'object' || Array.isArray(newAnswerKey)) return null;
  const answerItems = Array.isArray(newAnswerKey.items) ? newAnswerKey.items : null;
  if (!answerItems) return null;
  const correctedSentences = new Set(answerItems.filter((s) => typeof s === 'string' && s));

  const kept = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    const sentence = entry.sentence;
    const error = entry.error;
    const correction = entry.correction;
    if (
      typeof sentence !== 'string' || !sentence
      || typeof error !== 'string' || !error
      || typeof correction !== 'string' || !correction
    ) continue;
    if (!sentence.includes(error)) continue;
    if (!wrongSentences.has(sentence)) continue;
    const derived = sentence.replace(error, correction);
    if (!correctedSentences.has(derived)) continue;
    kept.push({ sentence, error, correction });
  }
  return kept.length ? kept : null;
}

// Build a deterministic full 9-type golden lesson document for the pilot (one per type)
function buildGoldenLesson(id, opts = {}) {
  const now = new Date().toISOString();
  const anchorText = opts.anchor?.text
    || 'Це демонстраційний текст для перевірки рендеру всіх типів діяльності. Текст не чутливий.';
  const blocks = [];
  const pilotTypes = loadPilotTypes();

  // Use golden fixtures; pad or cycle if less (but 9 exactly)
  const fixtureByType = {};
  for (const f of goldenActivities) {
    if (f && f.type) fixtureByType[f.type] = f;
  }

  let phase = 1;
  let i = 0;
  for (const t of pilotTypes) {
    const act = fixtureByType[t] || {
      id: `golden-${t}`,
      type: t,
      title: t,
      level: 'b1',
      payload: { type: t, instruction: `Демонстрація ${t}` },
      answer_key: {},
      provenance: { source: 'generated', generator: 'stub', gates: [] }
    };
    const blockId = `block-${t}-${i}`;
    const isWarn = (i % 3 === 1); // two or three warns for ack flow
    const activity = {
      id: act.id || blockId,
      type: t,
      title: act.title || t,
      level: 'b1',
      payload: act.payload || { type: t, instruction: `Інструкція для ${t}` },
      // Envelope stays pure (no corrections) — intentional contract boundary.
      answer_key: act.answer_key || {},
      provenance: act.provenance || { source: 'generated', generator: 'stub', gates: ['schema'] }
    };
    blocks.push({
      id: blockId,
      phase: ((i % 3) + 1),
      type: t,
      mode: ['усно', 'письмово', 'вдома'][i % 3],
      activity,
      answer_key: blockAnswerKeyFromActivity(t, activity),
      mark: isWarn ? 'warn' : 'ok',
      note: isWarn ? 'Перевірте уважно — можливе спрощення.' : null,
      edited: false,
      provenance: { source: 'generated', generator: 'hramatka-stub', gates: ['schema', 'player-smoke'], external_options: isWarn }
    });
    i++;
  }

  return {
    schema: 'lu.lesson.v1',
    id,
    title: 'Золотий урок — всі 9 типів (stub)',
    level: 'B1',
    method: 'ttt',
    focus: opts.focus || 'демонстрація контракту',
    anchor: {
      text: anchorText,
      source: opts.anchor?.source || 'teacher-paste',
      chars: anchorText.length,
      ...(opts.anchor?.source_url ? { source_url: opts.anchor.source_url } : {}),
    },
    duration: opts.duration || 60,
    version: 1,
    status: 'ready',
    last_error: null,
    accepted: false,
    blocks,
    rejected: [],
    created_at: now,
    updated_at: now
  };
}

function loadPilotTypes() {
  try {
    const raw = fs.readFileSync(CONTRACTS_SCHEMA, 'utf8');
    const schema = JSON.parse(raw);
    const arr = schema.$defs?.pilotActivityType?.enum;
    if (Array.isArray(arr) && arr.length === 9) return arr;
  } catch {}
  // Fallback to the known frozen list (tests derive from schema; stub is dev aid)
  return [
    "true-false", "cloze", "match-up", "quiz", "mark-the-words",
    "fill-in", "error-correction", "text-questions", "short-writing"
  ];
}

const PILOT_TYPES = loadPilotTypes();
const QUALIFIED_MODEL_REGISTRY_VERSION = 'QualifiedLogicalModels.v1';
const QUALIFIED_LOGICAL_MODEL = Object.freeze({
  id: 'gemini-3.5-flash',
  label: 'Gemini 3.5 Flash',
  description: 'Детермінована тестова модель.',
});
const SLOW_BAKE_MARKER = '__SLOW_BAKE__';

function isSlowBake(l) {
  const text = l?._anchor?.text || '';
  return text.includes(SLOW_BAKE_MARKER);
}

function slowBakePollsRequired() {
  return 8;
}

function bakeReadyAfterPolls(l) {
  return isSlowBake(l) ? slowBakePollsRequired() : 1;
}

function bakeProgressFor(l) {
  const counter = l._bakeCounter || 0;
  const planned = bakeReadyAfterPolls(l) * 3;
  const phase = Math.min(3, Math.max(1, Math.ceil((counter / bakeReadyAfterPolls(l)) * 3)));
  const steps = ['generation', 'gates', 'assembly'];
  return {
    phase,
    phases_total: 3,
    step: steps[phase - 1] || 'generation',
    calls_done: counter,
    calls_planned: planned,
    updated_at: new Date().toISOString(),
  };
}

const REVIEW_PHASE_BUDGETS = {
  45: { 1: 3, 2: 4, 3: 1 },
  60: { 1: 3, 2: 5, 3: 2 },
  90: { 1: 4, 2: 5, 3: 3 },
};
const TEACHER_REMOVAL_REASON = 'вилучено вчителем';
const RESTORED_WARNING_NOTE = 'повернено з відхилених — погляньте ще раз';
const EDITED_WARNING_NOTE = 'змінено вчителем — підтвердьте ще раз перед прийняттям';

function splitReviewBlocks(lesson) {
  const budgets = REVIEW_PHASE_BUDGETS[lesson.duration] || REVIEW_PHASE_BUDGETS[60];
  const seen = { 1: 0, 2: 0, 3: 0 };
  const visible = [];
  const reserve = [];
  for (const block of lesson.blocks) {
    const phase = block.phase;
    if (seen[phase] < budgets[phase]) {
      visible.push(block);
      seen[phase] += 1;
    } else {
      reserve.push(block);
    }
  }
  return { visible, reserve };
}

function blockIndex(blocks, blockId) {
  return blocks.findIndex((b) => b.id === blockId);
}

function phaseStartIndex(blocks, phase) {
  for (let i = 0; i < blocks.length; i++) {
    if (blocks[i].phase === phase) return i;
  }
  const lower = blocks.map((b, i) => (b.phase < phase ? i : -1)).filter((i) => i >= 0);
  return lower.length ? lower[lower.length - 1] + 1 : 0;
}

function moveBlockInLesson(lesson, blockId, direction) {
  const blocks = lesson.blocks;
  const index = blockIndex(blocks, blockId);
  if (index < 0) throw new Error('block_not_found');
  const delta = direction === 'up' ? -1 : 1;
  const block = blocks[index];
  const neighborIndex = index + delta;
  if (neighborIndex >= 0 && neighborIndex < blocks.length && blocks[neighborIndex].phase === block.phase) {
    [blocks[index], blocks[neighborIndex]] = [blocks[neighborIndex], block];
    return;
  }
  const targetPhase = block.phase + delta;
  if (targetPhase < 1 || targetPhase > 3) throw new Error('invalid_move');
  block.phase = targetPhase;
}

function removeBlockToRejected(lesson, blockId) {
  const blocks = lesson.blocks;
  const index = blockIndex(blocks, blockId);
  if (index < 0) throw new Error('block_not_found');
  const removed = blocks.splice(index, 1)[0];
  lesson.rejected = lesson.rejected || [];
  lesson.rejected.push({
    type: removed.type,
    activity: JSON.parse(JSON.stringify(removed.activity)),
    reason: TEACHER_REMOVAL_REASON,
  });
}

function includeReserveBlock(lesson, blockId) {
  const { reserve } = splitReviewBlocks(lesson);
  if (!reserve.some((b) => b.id === blockId)) throw new Error('not_reserve');
  const blocks = lesson.blocks;
  const index = blockIndex(blocks, blockId);
  const block = blocks.splice(index, 1)[0];
  blocks.splice(phaseStartIndex(blocks, block.phase), 0, block);
}

function restoreRejectedEntry(lesson, rejectedIndex, phase = 2) {
  const rejected = lesson.rejected || [];
  if (rejectedIndex < 0 || rejectedIndex >= rejected.length) throw new Error('rejected_not_found');
  const entry = rejected.splice(rejectedIndex, 1)[0];
  const activity = JSON.parse(JSON.stringify(entry.activity));
  const blockId = `restored-${crypto.randomUUID().slice(0, 8)}`;
  lesson.blocks.splice(phaseStartIndex(lesson.blocks, phase), 0, {
    id: blockId,
    phase,
    type: activity.type,
    mode: 'письмово',
    activity,
    answer_key: JSON.parse(JSON.stringify(activity.answer_key || {})),
    mark: 'warn',
    note: RESTORED_WARNING_NOTE,
    edited: false,
    provenance: { source: 'teacher', generator: 'teacher-restore', gates: [], external_options: false },
  });
  return blockId;
}

function replaceBlockActivity(lesson, blockId, replacement) {
  const blocks = lesson.blocks;
  const index = blockIndex(blocks, blockId);
  if (index < 0) throw new Error('block_not_found');
  const block = blocks[index];
  // Capture outer intent before overwrite; inner envelope cannot carry corrections.
  const previousKey = block.answer_key;
  block.type = replacement.type;
  block.activity = JSON.parse(JSON.stringify(replacement));
  block.answer_key = JSON.parse(JSON.stringify(replacement.answer_key || {}));
  // #208: preserve consistent deliberate-error triples on the outer block key only.
  if (replacement.type === 'error-correction') {
    const preserved = preservedErrorCorrectionIntent(
      previousKey,
      replacement,
      replacement.answer_key,
    );
    if (preserved && block.answer_key && typeof block.answer_key === 'object' && !Array.isArray(block.answer_key)) {
      if (!Object.prototype.hasOwnProperty.call(block.answer_key, 'corrections')) {
        block.answer_key = { ...block.answer_key, corrections: preserved };
      }
    }
  }
  block.edited = true;
  if (block.mark === 'warn') {
    block.note = EDITED_WARNING_NOTE;
  }
  return block.mark === 'warn';
}

function lessonResource(l) {
  return {
    lesson_id: l.id,
    revision: l.revision,
    accepted_at: l.accepted_at,
    accepted_revision: l.accepted_revision,
    warning_acknowledgements: l.acks || [],
    logical_model_id: l._logicalModelId ?? null,
    lesson: l.lesson,
  };
}

function bumpLesson(l) {
  l.revision += 1;
  l.updated_at = new Date().toISOString();
  if (l.lesson) l.lesson.updated_at = l.updated_at;
}

// In-memory stub state (dev only)
const state = {
  session: null, // { teacher, expires_at, csrf_token, rawSecretForDev }
  lessons: {},   // id -> { status: 'baking'|'ready'|'failed', revision: number, lesson: obj|null, acks: string[], accepted: bool, ... }
  createCounters: {}, // for simulating bake progress
  errorScripts: {},   // id -> scripted error for next op
};

function makeCsrf(secret) {
  // Simplified: base64url of sha256-ish. Contract pattern 43 chars unpadded.
  const h = crypto.createHash('sha256').update('hramatka-csrf:' + secret).digest('base64url').replace(/=+$/, '');
  return h.slice(0, 43); // ensure pattern match
}

function makeSession() {
  const secret = crypto.randomBytes(32).toString('base64url').replace(/=+$/, '').slice(0, 43); // 43 char
  const csrf = makeCsrf(secret);
  const expires = new Date(Date.now() + 7 * 86400 * 1000).toISOString();
  return {
    teacher: { id: 'teacher-pilot-001', display_name: 'Тетяна' },
    expires_at: expires,
    csrf_token: csrf,
    _devSecret: secret, // only in stub memory
  };
}

function errorBody(code, message, retryable = false, lesson_id = null) {
  const body = { code, message, retryable };
  if (lesson_id) body.lesson_id = lesson_id;
  return body;
}

function setCorsLike(res, originHeader) {
  // No real CORS — same origin only in contract. For local dev allow our dev origin.
  res.setHeader('Cache-Control', 'no-store');
}

function sendJSON(res, status, obj, headers = {}) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    ...headers,
  });
  res.end(JSON.stringify(obj));
}

function getOrigin(req) {
  return req.headers.origin || req.headers['x-forwarded-origin'] || '';
}

function requireOrigin(req, res) {
  const o = getOrigin(req);
  // In dev stub be lenient; in real would be exact match.
  if (!o || (!o.startsWith('http://localhost') && !o.startsWith('http://127.0.0.1'))) {
    // allow tests running on same machine
  }
  return true;
}

function requireCsrf(req, res, session) {
  const hdr = req.headers['x-csrf-token'] || req.headers['x-csrftoken'];
  if (!session || !hdr || hdr !== session.csrf_token) {
    sendJSON(res, 403, errorBody('csrf_rejected', 'This request did not pass same-origin validation.'));
    return false;
  }
  return true;
}

function parseBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', c => data += c);
    req.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}); } catch { resolve({}); }
    });
  });
}

function getCookie(req, name) {
  const h = req.headers.cookie || '';
  const m = h.match(new RegExp(name + '=([^;]+)'));
  return m ? decodeURIComponent(m[1]) : null;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const method = req.method.toUpperCase();
  const pathname = url.pathname;

  setCorsLike(res, getOrigin(req));

  // Probes
  if (pathname === '/api/healthz' && method === 'GET') {
    return sendJSON(res, 200, { status: 'ok' });
  }
  if (pathname === '/api/readyz' && method === 'GET') {
    return sendJSON(res, 200, { status: 'ready' });
  }

  // Dev-only: allow tests to force-clear in-memory session (no effect on real API)
  if (pathname === '/api/dev/clear-session' && method === 'POST') {
    state.session = null;
    return sendJSON(res, 204, null);
  }

  // #93 item6: reset stub state between E2E for determinism
  if (pathname === '/api/dev/reset' && method === 'POST') {
    state.session = null;
    state.lessons = {};
    state.createCounters = {};
    state.errorScripts = {};
    return sendJSON(res, 204, null);
  }

  // Session redeem (no auth yet)
  if (pathname === '/api/session/redeem' && method === 'POST') {
    const body = await parseBody(req);
    const token = body && body.token;
    const pat = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/;
    if (!token || !pat.test(token)) {
      return sendJSON(res, 422, errorBody('invalid_input', 'The request is invalid.'));
    }
    // Scripted errors via special tokens
    if (token === 'fail410invite') {
      return sendJSON(res, 410, errorBody('invite_unavailable', 'This invite is no longer available.'));
    }
    if (token === 'fail503') {
      return sendJSON(res, 503, errorBody('persistence_unavailable', 'The service could not persist this request. Try again.', true));
    }
    const sess = makeSession();
    state.session = sess;
    const cookie = `__Host-hramatka_session=${encodeURIComponent(sess._devSecret)}; Path=/; Max-Age=604800; HttpOnly; Secure; SameSite=Lax`;
    return sendJSON(res, 200, {
      teacher: sess.teacher,
      expires_at: sess.expires_at,
      csrf_token: sess.csrf_token,
    }, { 'Set-Cookie': cookie });
  }

  // Current session
  if (pathname === '/api/session' && method === 'GET') {
    if (!state.session) {
      return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    }
    return sendJSON(res, 200, {
      teacher: state.session.teacher,
      expires_at: state.session.expires_at,
      csrf_token: state.session.csrf_token,
    });
  }

  if (pathname === '/api/session' && method === 'DELETE') {
    if (!state.session) {
      return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    }
    if (!requireCsrf(req, res, state.session)) return;
    // Clear
    const cleared = '__Host-hramatka_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax';
    state.session = null;
    return sendJSON(res, 204, null, { 'Set-Cookie': cleared });
  }

  // One synthetic current receipt, matching real_backend_server.py's explicit
  // qualified test registry. Provider routes are intentionally never exposed.
  if (pathname === '/api/lesson-models' && method === 'GET') {
    if (!state.session) {
      return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    }
    return sendJSON(res, 200, {
      registry_version: QUALIFIED_MODEL_REGISTRY_VERSION,
      models: [{ ...QUALIFIED_LOGICAL_MODEL }],
      unavailable_message: null,
    });
  }

  // Anchor URL import (stub — no real network)
  if (pathname === '/api/anchor/import-url' && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const body = await parseBody(req);
    const rawUrl = (body && body.url) || '';
    if (!rawUrl.trim()) {
      return sendJSON(res, 422, errorBody('url_invalid', 'Вставте адресу сторінки — і ми дістанемо з неї текст.'));
    }
    if (/localhost|127\.0\.0\.1|192\.168\.|10\./i.test(rawUrl)) {
      return sendJSON(res, 422, errorBody('url_blocked', 'Ця адреса недоступна для імпорту.'));
    }
    if (rawUrl === 'https://stub.example.test/fail-fetch') {
      return sendJSON(res, 502, errorBody('url_fetch_failed', 'Не вдалося отримати текст із цієї адреси. Перевірте посилання.', true));
    }
    const normalized = rawUrl.startsWith('https://') ? rawUrl : `https://${rawUrl}`;
    return sendJSON(res, 200, {
      text: 'Текст, отриманий із посилання для перевірки вчителем. Він містить речення для вправ.',
      source_url: normalized,
    });
  }

  // Lessons
  if (pathname === '/api/lessons' && method === 'GET') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    const list = Object.values(state.lessons).map(l => ({
      id: l.id,
      title: l.lesson ? l.lesson.title : null,
      status: l.status,
      duration: l.lesson ? l.lesson.duration : 60,
      focus: l.lesson ? l.lesson.focus : null,
      revision: l.revision || 1,
      accepted: !!l.accepted,
      accepted_at: l.accepted_at || null,
      accepted_revision: l.accepted_revision || null,
      failure_code: l.failure_code || null,
      created_at: l.created_at,
      updated_at: l.updated_at,
    }));
    return sendJSON(res, 200, { lessons: list });
  }

  if (pathname === '/api/lessons' && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const body = await parseBody(req);
    const id = body && body.id;
    const anchor = body && body.anchor;
    const logicalModelId = body && body.logical_model_id;
    if (!id || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
      return sendJSON(res, 422, errorBody('invalid_input', 'The request is invalid.'));
    }
    if (!anchor || !anchor.text || anchor.text.trim().length < 1 || anchor.text.length > 100000) {
      return sendJSON(res, 422, errorBody('invalid_input', 'The request is invalid.'));
    }
    if (logicalModelId !== QUALIFIED_LOGICAL_MODEL.id) {
      return sendJSON(res, 409, errorBody(
        'model_unavailable',
        logicalModelId
          ? 'Обрана модель зараз недоступна. Оновіть список моделей.'
          : 'Оберіть доступну кваліфіковану модель.',
      ));
    }
    // Idempotency: same id + same request returns existing
    const existing = state.lessons[id];
    if (existing) {
      // simplistic: if same anchor-ish, reuse
      return sendJSON(res, 202, { id, status: existing.status, revision: existing.revision, reused: true });
    }
    // Create
    const now = new Date().toISOString();
    state.lessons[id] = {
      id,
      status: 'baking',
      revision: 1,
      lesson: null,
      acks: [],
      accepted: false,
      accepted_at: null,
      accepted_revision: null,
      created_at: now,
      updated_at: now,
      failure_code: null,
      failure_message: null,
      _bakeCounter: 0,
      _anchor: anchor,
      _duration: body.duration || 60,
      _focus: body.focus || null,
      _logicalModelId: logicalModelId,
    };
    // Scripted: special id for errors
    if (id === '00000000-0000-0000-0000-000000000bad') {
      state.lessons[id].status = 'failed';
      state.lessons[id].failure_code = 'provider_unavailable';
      state.lessons[id].failure_message = 'Постачальник тимчасово недоступний.';
    }
    return sendJSON(res, 202, { id, status: 'baking', revision: 1, reused: false });
  }

  // Status
  const statusMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/status$/);
  if (statusMatch && method === 'GET') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    const lid = statusMatch[1];
    const l = state.lessons[lid];
    if (!l) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    // Simulate progress: after 1 poll -> ready (covers E2E polling)
    l._bakeCounter = (l._bakeCounter || 0) + 1;
    if (l.status === 'baking' && l._bakeCounter >= bakeReadyAfterPolls(l) && !l.failure_code) {
      const fullLesson = buildGoldenLesson(lid, {
        anchor: l._anchor,
        duration: l._duration,
        focus: l._focus,
      });
      l.lesson = fullLesson;
      l.status = 'ready';
      l.updated_at = new Date().toISOString();
    }
    const step = l.status === 'failed'
      ? ''
      : l.status === 'baking'
        ? 'завдання складено'
        : (l.status === 'ready' ? 'готово' : 'текст отримано');
    const payload = {
      id: lid,
      status: l.status,
      step,
      revision: l.revision,
      failure_code: l.failure_code || null,
      failure_message: l.failure_message || null,
      created_at: l.created_at,
      updated_at: l.updated_at,
    };
    if (l.status === 'baking') {
      payload.progress = bakeProgressFor(l);
    }
    return sendJSON(res, 200, payload);
  }

  // Recreate from stored request
  const recreateMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/recreate$/);
  if (recreateMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = recreateMatch[1];
    const source = state.lessons[lid];
    if (!source) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    if (!source._anchor || !source._anchor.text || !source._anchor.text.trim()) {
      return sendJSON(res, 422, errorBody('invalid_input', 'Немає збереженого запиту для повторного створення уроку.'));
    }
    const newId = crypto.randomUUID();
    const now = new Date().toISOString();
    state.lessons[newId] = {
      id: newId,
      status: 'baking',
      revision: 1,
      lesson: null,
      acks: [],
      accepted: false,
      accepted_at: null,
      accepted_revision: null,
      created_at: now,
      updated_at: now,
      failure_code: null,
      failure_message: null,
      _bakeCounter: 0,
      _anchor: source._anchor,
      _duration: source._duration || 60,
      _focus: source._focus || null,
      _logicalModelId: source._logicalModelId,
    };
    return sendJSON(res, 202, { id: newId, status: 'baking', revision: 1, reused: false });
  }

  // Get lesson (ready only) / delete lesson
  const lessonMatch = pathname.match(/^\/api\/lessons\/([^/]+)$/);
  if (lessonMatch && method === 'DELETE') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = lessonMatch[1];
    if (!state.lessons[lid]) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    delete state.lessons[lid];
    res.writeHead(204);
    return res.end();
  }

  if (lessonMatch && method === 'GET') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    const lid = lessonMatch[1];
    const l = state.lessons[lid];
    if (!l) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    if (l.status !== 'ready' || !l.lesson) {
      return sendJSON(res, 409, errorBody('lesson_not_ready', 'The lesson is not ready.'));
    }
    return sendJSON(res, 200, lessonResource(l));
  }

  // Acknowledge warning block
  const ackMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/blocks\/([^/]+)\/accept$/);
  if (ackMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = ackMatch[1];
    const bid = ackMatch[2];
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    const block = splitReviewBlocks(l.lesson).visible.find(b => b.id === bid);
    if (!block || block.mark !== 'warn' || l.acks.includes(bid)) {
      return sendJSON(res, 404, errorBody('warning_block_not_found', 'Warning block not found.'));
    }
    l.acks.push(bid);
    bumpLesson(l);
    return sendJSON(res, 200, lessonResource(l));
  }

  const moveMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/blocks\/([^/]+)\/move$/);
  if (moveMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = moveMatch[1];
    const bid = moveMatch[2];
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    try {
      moveBlockInLesson(l.lesson, bid, body.direction);
      bumpLesson(l);
      return sendJSON(res, 200, lessonResource(l));
    } catch {
      return sendJSON(res, 404, errorBody('lesson_block_not_found', 'Lesson block not found.'));
    }
  }

  const removeMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/blocks\/([^/]+)\/remove$/);
  if (removeMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = removeMatch[1];
    const bid = removeMatch[2];
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    try {
      removeBlockToRejected(l.lesson, bid);
      l.acks = (l.acks || []).filter((id) => id !== bid);
      bumpLesson(l);
      return sendJSON(res, 200, lessonResource(l));
    } catch {
      return sendJSON(res, 404, errorBody('lesson_block_not_found', 'Lesson block not found.'));
    }
  }

  const includeMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/blocks\/([^/]+)\/include$/);
  if (includeMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = includeMatch[1];
    const bid = includeMatch[2];
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    try {
      includeReserveBlock(l.lesson, bid);
      bumpLesson(l);
      return sendJSON(res, 200, lessonResource(l));
    } catch {
      return sendJSON(res, 409, errorBody('review_mutation_invalid', 'Block is not currently in reserve.', false, lid));
    }
  }

  const activityMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/blocks\/([^/]+)\/activity$/);
  if (activityMatch && method === 'PUT') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = activityMatch[1];
    const bid = activityMatch[2];
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    const activity = body.activity;
    if (!activity || !activity.type || !activity.payload) {
      return sendJSON(res, 422, errorBody('invalid_input', 'The request is invalid.'));
    }
    try {
      const wasWarn = replaceBlockActivity(l.lesson, bid, activity);
      if (wasWarn) {
        l.acks = (l.acks || []).filter((id) => id !== bid);
      }
      bumpLesson(l);
      return sendJSON(res, 200, lessonResource(l));
    } catch {
      return sendJSON(res, 404, errorBody('lesson_block_not_found', 'Lesson block not found.'));
    }
  }

  const restoreMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/rejected\/(\d+)\/restore$/);
  if (restoreMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = restoreMatch[1];
    const rejIndex = Number(restoreMatch[2]);
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    try {
      restoreRejectedEntry(l.lesson, rejIndex, body.phase || 2);
      bumpLesson(l);
      return sendJSON(res, 200, lessonResource(l));
    } catch {
      return sendJSON(res, 404, errorBody('rejected_entry_not_found', 'Rejected entry not found.'));
    }
  }

  const durationMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/duration$/);
  if (durationMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = durationMatch[1];
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    const duration = body.duration;
    if (![45, 60, 90].includes(duration)) {
      return sendJSON(res, 422, errorBody('invalid_input', 'The request is invalid.'));
    }
    l.lesson.duration = duration;
    bumpLesson(l);
    return sendJSON(res, 200, lessonResource(l));
  }

  // Accept lesson
  const acceptMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/accept$/);
  if (acceptMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = acceptMatch[1];
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    const warns = splitReviewBlocks(l.lesson).visible.filter(b => b.mark === 'warn').map(b => b.id);
    const unacked = warns.filter(w => !l.acks.includes(w));
    if (unacked.length > 0) {
      return sendJSON(res, 409, errorBody('warning_acknowledgements_required', 'Acknowledge every visible warning before accepting this lesson.', false, lid));
    }
    l.accepted = true;
    l.accepted_at = new Date().toISOString();
    l.accepted_revision = l.revision;
    bumpLesson(l);
    if (l.lesson) l.lesson.accepted = true;
    return sendJSON(res, 200, lessonResource(l));
  }

  // Return to draft
  const draftMatch = pathname.match(/^\/api\/lessons\/([^/]+)\/draft$/);
  if (draftMatch && method === 'POST') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    if (!requireCsrf(req, res, state.session)) return;
    const lid = draftMatch[1];
    const l = state.lessons[lid];
    if (!l || !l.lesson) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    const body = await parseBody(req);
    if (body.expected_revision !== l.revision) {
      return sendJSON(res, 409, errorBody('revision_conflict', 'The lesson changed; reload it before trying again.', false, lid));
    }
    l.accepted = false;
    l.accepted_at = null;
    l.accepted_revision = null;
    bumpLesson(l);
    if (l.lesson) l.lesson.accepted = false;
    return sendJSON(res, 200, lessonResource(l));
  }

  // 404 for unknown
  sendJSON(res, 404, { code: 'not_found', message: 'Not found', retryable: false });
});

server.listen(PORT, () => {
  console.log(`[hramatka-stub] listening on http://localhost:${PORT}`);
  console.log(`[hramatka-stub] golden types: ${PILOT_TYPES.join(', ')}`);
});
