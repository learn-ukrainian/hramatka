# Hramatka — ground-up teacher app on LU (spec sketch for user reaction, 2026-07-07)
> ⚠️ **2026-07-10 SUPERSESSION NOTES (see docs/decisions/hramatka-hosting-decision.md + epic #4542):**
> 1. The HOSTING section below (HF PRO) is SUPERSEDED — pinned decision = **Scaleway dev1-m**, one
>    server shared with the atlas/practice backend. HF = documented fallback.
> 2. Band question RESOLVED = **B1 first** (user 2026-07-08 + 2026-07-10); B2 later, treated separately.
> 3. "Student data — v1 stores NONE" STANDS (supersedes the earlier roster/per-student-difficulty flow
>    for v1; rosters return only with the progress-tracking loop, later phase).


> Built FRESH on learn-ukrainian (our activities, gates, corpus, engine). Vibe = requirements
> reference only, NO code reuse. Engine internals (anchor→activities→gates, Gemma, success-bar,
> edit-capture) are in `design-v0.md`; this is the APP layer on top.

## 🔬 FLEET PRE-BUILD REVIEW — findings APPLIED (codex + agy-gemini-pro + cursor, 2026-07-08)
Strongly convergent. Real catches that CHANGE the plan:
1. **HOME = the PRIVATE infra repo** `learn-ukrainian-infra-private` (user 2026-07-08; THIS repo is PUBLIC).
   Fleet independently: **SEPARATE APP** (not inside LU's static Astro `site/`) — import V7 components via a
   shared package/ADAPTER, do NOT hard-import `site/src` internals. → app-boundary fork RESOLVED.
2. **⚠️ BIGGEST GAP (unanimous): V7 activity components are READ-ONLY LEARNER widgets rendered via STATIC
   MDX bake (`yaml_activities.py`) — NOT runtime JSON→component, and NOT editors.** Must build (a) a
   **runtime ActivityPlayer** (lesson JSON → existing `.tsx`) + (b) an **editor UI** + fixture tests.
   I under-specified "just reuse the components" — real, first-order work.
3. **❌ MY ERROR, corrected: `sources.db` ≈ 1.6 GB, NOT "MB-scale"** (cursor, per `docs/corpus-inventory.md`).
   → corpus baked READ-ONLY into the image / lazy subset; the mounted Bucket holds ONLY the lessons/edits DB.
4. **SQLite-on-mounted-Bucket = top ops risk (unanimous):** object-mount ≠ transactional FS → single-writer,
   WAL, prove locking/fsync BEFORE build; Space restart/sleep kills in-flight writes → generation jobs
   **idempotent/checkpointed**; async bake queue + explicit "generating…" UX.
5. **NUMERAL gate: no off-the-shelf lib (unanimous).** pymorphy3/VESUM inflect tokens but don't COMPOSE
   numeral+noun government; num2words = weak baseline. Verify, then **BUILD our own deterministic government
   rules** (1 / 2-4 / 5+ classes, ordinals, fractions, %). `grammar.py check_case_government` = regex toy.
6. **SCOPE tension → USER CALL:** fleet unanimously says ship **ONE level band first**, not all CEFR at once
   (A1/A2 ULP extractive ≠ C1 CLIL synthesis; TTT/CLIL need GENERATED scaffolding beyond spans → more style
   risk). Does NOT contradict "don't lock to B1-B2": ARCHITECTURE stays all-levels; the BUILD/pilot proves
   ONE band first. NEEDS USER: which band (fleet leans A1/A2 extractive = lowest style risk).
7. Smaller: allow **phase-BYPASSING** from day one (enable-not-bound ✓); badges show **actionable** gate
   failures; invite auth = hashed tokens + revocation + no secrets in logs; instrument **edits-per-type** d1.
### First vertical slice (fleet consensus)
1 invited teacher → 1 level band + 1 pedagogy → paste anchor → engine emits 2-3 ANCHOR-EXTRACTIVE activities
(TrueFalse, MatchUp, Cloze/mark-the-words) w/ evidence spans → VESUM + numeral gate → render + edit →
persist lesson + edit events. **Build order:** shared component schema/renderer ADAPTER → engine pipeline
(retrieval-IN → Gemma → gate-OUT) → persistence/auth → review UI → async jobs/telemetry → THEN more levels/
pedagogies/types. Prove edit-rate on ~10 real teacher anchors BEFORE UI polish.

## What it is
A teacher app for Ukrainian teachers: bring a text, choose how you want to teach it, get a correct,
ready-to-run lesson — built from OUR activities and verified by OUR tools. The teacher mostly doesn't
have to edit it.

## The teacher flow (the spine)
1. **Log in.**
2. **Roster** — add students; set each student's level / difficulty.
3. **New lesson:**
   a. pick a student (or just a level),
   b. pick a **pedagogy (methodology)** — the guardrail,
   c. supply the **anchor** (paste text/URL now; photo or one of our modules later),
   d. the pedagogy decides any extra inputs we ask for,
   e. **Generate.**
4. **Review & assemble** — generated activities appear, grouped into the pedagogy's phases; teacher
   reorders / edits / prunes freely; correctness badges come from the gates; **Accept.**
5. **Deliver** — render the lesson page: print, assign to a student, or run it.

## Pedagogy as guardrail (the heart of it) — REGROUNDED on OUR method (not vibe's ELT grab-bag)
Vibe's four (PPP/GPPC/TTT/TBL) were a generic list — GPPC has ZERO presence in our project, TBL is
marginal. Our real pedagogy is **immersion-first** (#M-13) and **level-mapped**. The teacher picks a
method → it defines a phase skeleton → each phase requests inputs + gets anchor-derived, gated activities:
| Our method | Shape | Where it fits |
|---|---|---|
| **ULP / Ohoiko immersion presentation** | UK-first, English scaffold recedes (encounter-in-context → practice → produce) | A1–A2 |
| **TTT + Metalanguage Bridge** | diagnose → teach the gap (grammar terms in UK from B1) → re-test | B1–B2 grammar |
| **CLIL / content-based immersion** | language THROUGH content: comprehension → analysis → production, 100% UK | B2–C2 + seminars |
| **Custom** | teacher-defined phases | any |
HARD RULE: immersion-ADAPTED — NO English-first rule presentation; English scaffold ONLY where the
per-level immersion policy allows (A1/A2), never raised (#M-13). Even "PPP" for us is the ULP variant,
not textbook English-first PPP.
The pedagogy choice = a principled, immersion-correct lesson shape; the engine fills each phase with
gated, anchor-derived activities (respecting the level→activity-type matrix in `activity-pedagogy.md`).
⚠️ Pedagogy is the CONTENT lane's authority — validate the final taxonomy against our docs + a pedagogy
reviewer before it hardens; I am not the authority on what's pedagogically right for Ukrainian.

## Scope of "tracks" (user 2026-07-07)
Current structure = **core levels A1→C2 + seminars** (FOLK/HIST/BIO/ISTORIO/LIT/OES/RUTH). There are
**NO "PRO tracks"** (stale vibe language). **STEM / specialization tracks = FUTURE, explicitly OUT of
scope now** — adding them now would just distract. The app targets core + seminar teaching.

## How the engine plugs in (LU side, per phase)
`anchor + level + methodology + phase` →
1. **retrieval-IN:** our harness pulls verified data (VESUM forms, CEFR-appropriate vocab, correctly
   declined numerals) into the prompt.
2. **Gemma generates** activity content for that phase, in our V7 activity schema (toolless).
3. **gate-OUT:** deterministic checks (VESUM, numeral case-government, evidence-span, russicism);
   model proposes, gate decides.
4. gated activities returned.
Bar: correct content accepted as-is; quality verified internally BEFORE any teacher sees output;
escalate the generator per-type if style falls short. Edit-rate is the KPI we drive down.

## 🧩 REUSE MAP — practice + atlas (VERIFIED in code 2026-07-08, Explore sweep)
CRUCIAL disambiguation: there are TWO activity systems, near-zero shared code —
(a) **runtime SRS player** `site/src/components/LexiconPractice.tsx` (fetches JSON decks, renders, SCORES,
schedules) + (b) **~40 self-scoring MDX widgets** `site/src/components/*.tsx` (plain props, grade
themselves; today bound to component type at MDX-authoring time — no runtime dispatcher for THEM).
| Hramatka need | Verdict | Path |
|---|---|---|
| Interactive activity WIDGETS | **REUSE-DIRECTLY** | `site/src/components/*.tsx` + `schemas/activity*.schema.json` — take plain props, self-grade |
| Runtime activity PLAYER (data→interactive) | **ADAPT** | `LexiconPractice.tsx` exists but hardwired to the SRS-deck schema + 10 modes → generalize its dispatcher to teacher-authored lesson JSON |
| SRS scheduling | **REUSE-DIRECTLY** | `site/src/lib/lexicon/srs.ts` = FSRS-6 (ts-fsrs), storage-agnostic (injectable `StorageLike`), `CardState` shape |
| Per-student identity/progress/backend | **BUILD-FRESH** | practice today = anonymous **localStorage, NO auth/user/backend** → this is Hramatka's genuinely-new part (auth + multi-student + server store; GDPR-pseudonymized; reuse `CardState` as the row shape) |
| Lexical grounding (word→level/forms/russianism) | **REUSE-DIRECTLY via Python** | `data/atlas.db` (CEFR + heritage/russianism + forms/stress + synonyms/etymology) — Python precedent `scripts/audit/generate_practice_deck.py` already reads it; + live `sources` MCP (VESUM/CEFR/heritage) |
Refines fleet gap #2: NOT "build a runtime player from scratch" → **reuse the widgets + adapt the existing
player's dispatcher**. Still build-fresh: the EDITOR UI + the multi-student backend.
CAVEATS: (1) widgets ↔ runtime player are separate codebases — marrying widget-lib to a generalized
dispatcher is the main net-new render integration; (2) paronyms (`data/lexicon/paronym_pairs.yaml`) +
collocations (live GRaC only) are NOT in the atlas.db payload → separate grounding sources.
THE LOOP (user "practice/atlas can help hramatka"): engine emits activities as JSON → conform to a
generalized practice-deck schema → a teacher's lesson flows straight into the student's SRS practice →
progress feeds back to per-student difficulty/targeting.

## Activities — REUSE LU's V7 set
Render with our existing React components (`site/src/components/*.tsx`: Quiz, MatchUp, TrueFalse,
Anagram, ReadingActivity, …) — the SAME activities learners see. Open/extensible registry: each type =
`(generator, deterministic gate)`. Bias toward anchor-EXTRACTIVE types (richness inherited from the
teacher's own text → lowest edit risk); reserve generative slices for where they add value.

## Hosting / backend = HUGGING FACE (user 2026-07-08; NO Supabase, NO Scaleway) — PRICING VERIFIED
Everything co-located on HF: **corpus (`sources.db`) + our tools/gates + the app**, one deployable; engine
calls VESUM/sources LOCALLY (no network hop). VERIFIED against huggingface.co/pricing + spaces-storage docs
(2026-07-08):
### PRO vs Team — FULL contents (verified 2026-07-08: pricing page + storage-limits + inference-providers docs)
| Benefit | Free | **PRO $9/mo** | Team $20/seat/mo (org) |
|---|---|---|---|
| Private storage | 100 GB | **1 TB** + $18/TB PAYG | 1 TB/seat + PAYG |
| Public storage | best-effort | up to 10 TB | 12 TB + 1 TB/seat |
| HF-routed inference credits | $0.10/mo | **$2.00/mo** | $2.00/seat/mo |
| ZeroGPU quota | base | **8×** + priority + Spaces hosting | 8× for all members |
| Spaces Dev Mode (SSH/VS Code) | ✗ | **✓** | ✓ |
| Org governance (SSO SAML/OIDC · Audit Logs · Resource Groups · Storage Regions · Repo Analytics · token control) | ✗ | ✗ | **✓** |
| Dataset Viewer (private) · blog · PRO badge | ✗ | ✓ | ✓ |
**VERDICT (facts): PRO $9/mo is right; Team $20/seat NOT needed for the pilot.** Team only adds org
governance (SSO/audit/resource-groups) a small teacher pilot doesn't use. CRUCIAL nuance: most PRO
marquee perks **don't even apply to us** — the $2 inference credits are for HF-ROUTED inference, but we
call Gemma on Google AI Studio DIRECTLY ($0), bypassing HF routing → credits irrelevant; ZeroGPU only
matters if we SELF-HOST a model on HF (we don't). Our data is small → free 100 GB private likely covers
it. So the real PRO value for US = **Spaces Dev Mode** (SSH/VS Code to build on HF) + headroom. Even
FREE could technically run the pilot; PRO $9 is worth it for Dev Mode. Go Team ONLY if we later need org
SSO/audit.
| Component | HF fact | Our cost |
|---|---|---|
| Account | **PRO = $9/mo** (right choice; Team not needed) — see full table above | $9/mo |
| App/orchestration compute | CPU Basic = **FREE** (2 vCPU/16 GB); CPU Upgrade $0.03/hr (8 vCPU/32 GB) | $0 (free CPU) |
| **Model inference (Gemma)** | via Google AI Studio, external → **NO HF GPU needed at all** | **$0** |
| Persistence | Space disk is **EPHEMERAL** (lost on restart — confirmed). Persist via **Storage Buckets** (mounted volume), Hub rate **$18/TB/mo private** | ~$0 (MB-scale data = pennies) |
| **ALL-IN pilot** | | **≈ $9/mo** |
vs Scaleway VPS (~€8–40/mo) + Supabase (free-tier limited / $25/mo Pro), split across 2 providers. HF wins
on cost AND consolidation. (This is the cost record that was missing — now captured.)
### Hard constraints (my lane, from the verification)
- **NEVER write persistent data to Space disk** — it's ephemeral. ALL data (teachers/students/lessons/
  edits + `sources.db`) lives on a mounted **Storage Bucket**. Design the app to read/write only the bucket.
- **Gemma key** = HF Space **secret** (egress to Google AIS). Spaces support secrets — fine.
- **Free CPU Basic pauses on inactivity** → cold-start on wake. Acceptable for a low-traffic pilot; if we
  want always-warm, CPU Upgrade ~$0.03/hr (PRO does NOT buy always-on compute — that's separate).
- **AUTH**: no Supabase auth → own **invite-token auth** (fits limited-teacher pilot) or HF OAuth.

## Student data — v1 stores NONE (user 2026-07-08: non-commercial, GDPR already followed)
DECISION: v1 stores NO student records → no student PII → the student-data concern is DESIGNED OUT, not
"managed". App is TEACHER-centric: teacher account (invite-token) + lessons + pick a **level** per lesson.
Students practice ANONYMOUSLY in their own browser via the existing localStorage SRS (already how practice
works — no server-side student state). LLM path = anchor + level → activities, student-free (confirmed).
CONSEQUENCE (neutral): defers the server-side "assign→track student progress→feed teacher" loop; add stored
(pseudonymous) students only if/when that loop is wanted. SHRINKS the build — "per-student backend" reduces
to teacher-accounts + lessons; per-student progress stays browser-local (reuse `srs.ts` as-is).
(EU residency note still favors Scaleway for the small teacher-account/lesson store; no further legal flagging.)

## Data model (fresh, our stack — concepts sketched from vibe, written new)
`teachers` · `students` (+ per-student level/difficulty) · `teacher_students` · `lessons`
(methodology, phases, activity refs, target_level) · `assignments` (assign + progress) · `anchors`
(snapshot + hash) · `edit_events` (telemetry). Lives in the persistent DATA layer above; we own auth.

## Phasing
- **v1 — text loop:** anchor → text activities laid into the pedagogy's phases → gates → assemble →
  render. ALL levels. Gemma 4.
- **Phase 2 — multimodal:** photo-anchor (Gemma OCR/handwriting), generated **image-out** (confirmed
  teacher requirement) + voice via Imagen/TTS asset layer, "pick from our modules" as anchor.

## Open decisions (for you + a fleet pre-build review)
1. **App boundary / stack:** extend LU's `site/` (Astro + React islands) with a teacher area that reuses
   the activity components, VS a separate React app that imports the shared activity components. (Either
   way we reuse the `.tsx` activities; either way it deploys to HF as one Space.)
2. ~~Auth + DB~~ **RESOLVED: HF backend, NO Supabase** (see Hosting section). Remaining sub-question =
   exact persistence tier (HF persistent-storage volume + embedded DB vs HF Datasets store) + own
   invite-token auth.
3. **Pedagogy depth:** how much of the phase-skeleton + per-phase activity mapping to encode in v1 vs
   iterate with real teachers.
4. **Engine transport:** in-Space local call (co-located — no network hop) for fast activities; async job
   for a full lesson bake. Confirm the Space type (Docker Space w/ persistent storage) supports both.
