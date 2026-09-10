PRE-BUILD REVIEW (mandatory gate before we write code). Attached is the finalized v1 design for
"Hramatka" — a Ukrainian TEACHER APP. Poke holes BEFORE we build. Terse, numbered.

PLAN IN ONE BREATH: A teacher app built GROUND-UP on our existing "learn-ukrainian" (LU) project (NOT on
an old prototype). Teacher logs in → adds students + sets per-student level → picks a PEDAGOGY (our own
immersion-first, level-mapped methods: ULP/Ohoiko presentation A1-A2, TTT+metalanguage-bridge B1-B2,
CLIL/content-based B2-C2 — NOT generic PPP/GPPC) → supplies an ANCHOR text → engine generates VARIED
activities DERIVED FROM the anchor, laid into the pedagogy's phase skeleton → teacher reorders/edits/
prunes freely → renders the lesson using LU's EXISTING V7 React activity components (site/src/components/
*.tsx: Quiz, MatchUp, TrueFalse, ReadingActivity…). ENGINE: Gemma 4 (google-ais/gemma-4-31b-it, TOOLLESS,
$0) generates; OUR Python+VESUM gates verify (model proposes, gate DECIDES). Grounding is two-sided: our
harness does retrieval-IN (verified forms/vocab into the prompt) + gate-OUT (VESUM/numeral-government/
evidence-span/russicism). MOAT = deterministic numeral case-government gate. HOSTING: Hugging Face Space
(PRO $9/mo verified), corpus+tools+app co-located, persistence via mounted Storage Buckets (Space disk is
ephemeral), own invite-token auth, Gemma key as Space secret. SCOPE v1 = TEXT LOOP ONLY, ALL CEFR levels;
photo-anchor + generated image-out (confirmed teacher want) = PHASE 2. SUCCESS BAR: teachers get CORRECT
content they ACCEPT AS-IS most of the time; grammar is gate-able but STYLE/RICHNESS is not → we bet on
anchor-EXTRACTIVE activities (richness inherited from the teacher's text) + escalate the per-type
generator if style falls short; verify quality internally BEFORE any teacher sees output; edit-rate is
the KPI we drive down (edit-capture = invisible instrumentation, NOT the product's purpose).

ANSWER TERSELY, NUMBERED:
1. SMALLEST correct FIRST vertical slice + the build order after it?
2. APP BOUNDARY: extend LU's `site/` (Astro + React islands) with a teacher area reusing the `.tsx`
   activities, VS a SEPARATE app that imports the shared activity components? Pick one + why.
3. NUMERAL case-government renderer: build our own deterministic UA number-grammar (cardinals/ordinals/
   fractions/percents × 7 cases × 3 genders) or is there an existing UA lib to verify FIRST? NAME it.
4. HF Space + mounted Storage Buckets + Gemma-AIS-key-as-secret + own invite auth: biggest ops/persistence
   risk you'd flag before we build?
5. Anything in the attached design that is WRONG, won't survive real teachers, or conflicts with reusing
   LU's pipeline/components?
