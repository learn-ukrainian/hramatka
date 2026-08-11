# hramatka-teacher-frontend

Static teacher frontend for the Hramatka pasted-text pilot (private repo only).

**Stack choice (lightest that reuses the public activity-kit): Vite + React + TypeScript.**

- Vite chosen over Astro because this is a pure client-side SPA (routes, in-memory session, polling, two interactive modes, print). Astro adds unnecessary routing/islands/SSG machinery for a bundle that is 100% hydrated client React with no static pages or content islands at build time.
- Reuses `@learn-ukrainian/activity-kit` (pinned at ffd54054 via in-repo tarball under `../../vendor/activity-kit/`) **as a package/adapter only**. Normal package resolution + subpath exports (no custom aliases or react-symlink postinstall hack needed). Never imports `site/src` internals (per fleet review finding and app-spec-v1.md).
- All 9 pilot activity types render through the real `ActivityPlayer` with zero placeholders or fallbacks (launch gate).

## Frozen contracts (LAW — do not reinterpret)
- `../../api/openapi.yaml`
- `../../api/teacher-access-contract.md`
- `../../contracts/pilot_activity_types.schema.json` (derive PILOT_ACTIVITY_TYPES; no second list)
- `../../vendor/lu.activity.v1@1.0.0-ffd54054/` (golden fixtures + schema)
- Sol design msg 2629 § Teacher frontend
- `../../app-spec-v1.md` fleet findings 1-2: separate bundle in PRIVATE, public package only.

**OUT OF SCOPE (do not touch):** `../../api/**`, `../../engine/**`, `../../contracts/**` (except reading the schema for test derivation), public repo, deploy/Caddy.

## Local development
```bash
cd hramatka/app

# No sibling checkout needed. The @learn-ukrainian/activity-kit is vendored as
# a pinned npm tarball under ../../vendor/activity-kit/ (ffd54054).
# `npm ci` (or `npm install`) unpacks it; Vite serves the SPA.
npm run dev          # Vite on :5173 (serves /teacher/* paths; /api proxied to stub)

# In another shell: stub API (dev only, never committed to prod)
npm run stub         # Node server on :8787 (or configured)

# Then open http://localhost:5173/teacher/#invite=TESTTOKEN (or use UI)
```

The stub serves golden fixture lesson containing one of each of the 9 pilot types. It implements redeem (any 43-char token), session, create/status/get/ack/accept/draft with scripted happy + error paths per openapi.

## Tests (must be green in CI)
```bash
npm test          # component tests (vitest) — all 9 types render from golden via contracts schema
npm run test:e2e  # playwright against stub (full redeem→paste→bake→lesson→ack→accept→draft roundtrips + scrub + storage + 401/410)
```

## Build
```bash
npm run build     # tsc + vite build → dist/ (static bundle)
```

## Key behaviors (per contract)
- Invite token only in fragment → POST /api/session/redeem → `history.replaceState` scrub in finally (never in URL after, never in storage/logs).
- CSRF: GET /api/session yields csrf_token; every mutation sends `X-CSRF-Token` + exact Origin.
- 401 → show invite screen. 403/409/410/422/503 → teacher-facing messages from envelope.
- Paste disclosure: "Вставлений текст буде надіслано зовнішньому провайдеру Gemma. Не використовуйте чутливі або персональні дані."
- Level fixed to B1. Durations 45/60/90. Focus optional.
- Lesson view groups by phase (1,2,3). REVIEW shows answer keys + warnings + provenance + ack UI (accept blocked until all visible warns acked). RUN uses learner player view.
- Every block: `<ActivityPlayer activity={block.activity} ... />`
- Accept / draft use revision check (409 on stale).
- Print stylesheet + "Завантажити прийнятий JSON".
- No token in localStorage/sessionStorage ever.

## CI
Defined in `../../.github/workflows/private-boundary.yml`. Draft PRs run the
changed Python tests and only the frontend or real-backend lane whose owned
paths changed. Ready PRs and `main` run the complete Python, frontend, stub
browser, and real-backend browser gates. The vendored tarball means no public
checkout or `ACTIVITY_KIT_DIR` override is required.

## Production note
The bundle is served by Caddy at `/teacher/*` (SPA fallback to index.html) + `/api/*` proxy to backend on same origin. Cookie `__Host-hramatka_session`.

**PR evidence required:** test + e2e output + DOM/screenshot proving one golden lesson renders all 9 blocks with zero fallbacks.
