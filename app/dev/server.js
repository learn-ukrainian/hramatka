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
    blocks.push({
      id: blockId,
      phase: ((i % 3) + 1),
      type: t,
      mode: ['усно', 'письмово', 'вдома'][i % 3],
      activity: {
        id: act.id || blockId,
        type: t,
        title: act.title || t,
        level: 'b1',
        payload: act.payload || { type: t, instruction: `Інструкція для ${t}` },
        answer_key: act.answer_key || {},
        provenance: act.provenance || { source: 'generated', generator: 'stub', gates: ['schema'] }
      },
      answer_key: typeof act.answer_key === 'string' ? act.answer_key : (act.answer_key ? JSON.stringify(act.answer_key).slice(0, 80) : 'Ключ відповіді'),
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
      source: 'teacher-paste',
      chars: anchorText.length,
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
    if (!id || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
      return sendJSON(res, 422, errorBody('invalid_input', 'The request is invalid.'));
    }
    if (!anchor || !anchor.text || anchor.text.trim().length < 1 || anchor.text.length > 100000) {
      return sendJSON(res, 422, errorBody('invalid_input', 'The request is invalid.'));
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

  // Get lesson (ready only)
  const lessonMatch = pathname.match(/^\/api\/lessons\/([^/]+)$/);
  if (lessonMatch && method === 'GET') {
    if (!state.session) return sendJSON(res, 401, errorBody('session_required', 'A valid teacher session is required.'));
    const lid = lessonMatch[1];
    const l = state.lessons[lid];
    if (!l) return sendJSON(res, 404, errorBody('lesson_not_found', 'Lesson not found.'));
    if (l.status !== 'ready' || !l.lesson) {
      return sendJSON(res, 409, errorBody('lesson_not_ready', 'The lesson is not ready.'));
    }
    return sendJSON(res, 200, {
      lesson_id: lid,
      revision: l.revision,
      accepted_at: l.accepted_at,
      accepted_revision: l.accepted_revision,
      warning_acknowledgements: l.acks || [],
      lesson: l.lesson,
    });
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
    const block = l.lesson.blocks.find(b => b.id === bid);
    if (!block || block.mark !== 'warn' || l.acks.includes(bid)) {
      return sendJSON(res, 404, errorBody('warning_block_not_found', 'Warning block not found.'));
    }
    l.acks.push(bid);
    l.revision += 1;
    l.updated_at = new Date().toISOString();
    return sendJSON(res, 200, {
      lesson_id: lid,
      revision: l.revision,
      warning_acknowledgements: l.acks,
    });
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
    const warns = l.lesson.blocks.filter(b => b.mark === 'warn').map(b => b.id);
    const unacked = warns.filter(w => !l.acks.includes(w));
    if (unacked.length > 0) {
      return sendJSON(res, 409, errorBody('warning_acknowledgements_required', 'Acknowledge every visible warning before accepting this lesson.', false, lid));
    }
    l.accepted = true;
    l.accepted_at = new Date().toISOString();
    l.accepted_revision = l.revision;
    l.revision += 1;
    l.updated_at = new Date().toISOString();
    // also stamp the inner lesson doc so client UI sees .lesson.accepted
    if (l.lesson) l.lesson.accepted = true;
    return sendJSON(res, 200, {
      lesson_id: lid,
      revision: l.revision,
      accepted_at: l.accepted_at,
      accepted_revision: l.accepted_revision,
      warning_acknowledgements: l.acks,
      lesson: l.lesson,
    });
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
    l.revision += 1;
    l.updated_at = new Date().toISOString();
    if (l.lesson) l.lesson.accepted = false;
    return sendJSON(res, 200, {
      lesson_id: lid,
      revision: l.revision,
      accepted_at: null,
      accepted_revision: null,
      warning_acknowledgements: l.acks,
      lesson: l.lesson,
    });
  }

  // 404 for unknown
  sendJSON(res, 404, { code: 'not_found', message: 'Not found', retryable: false });
});

server.listen(PORT, () => {
  console.log(`[hramatka-stub] listening on http://localhost:${PORT}`);
  console.log(`[hramatka-stub] golden types: ${PILOT_TYPES.join(', ')}`);
});
