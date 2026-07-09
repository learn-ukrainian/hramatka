DESIGN REVIEW — "Hramatka" Ukrainian teacher homework-packet generator (packet contract + bake approach)

CONTEXT (learn-ukrainian repo). We're prototyping a teacher-facing service: real Ukrainian teachers
say stock ChatGPT/Claude/Gemini can't produce proper Ukrainian lessons. We have: a `sources` MCP corpus
server (VESUM morphology, СУМ-20, Грінченко, ЕСУМ, textbooks, russianism/CEFR checks), V7 writer prompt
packs, deterministic quality gates, and a bakeoff harness showing cheap TOOLED models (gemma-4-31b +
sources MCP) get grounded/honest where ungrounded frontier models fabricate. Goal: cheap tooled model
generates packets; teachers validate + edit; we capture edits as training/eval data.

REAL TEACHER ARTIFACT (abstracted from a real B1–B2 lesson journal; pattern only, no PII):
A "homework packet" = (1) an AUTHENTIC dated current-events reading text (real UA news, topic-varied,
with source URLs), then (2) ДОМАШКА — ONE productive writing task with explicit checkable constraints
(e.g. "email to a shop, 10–12 sentences, ≥4 numerals, use 'Чи могли б Ви…', say what it's for"), then
(3) ВПРАВА 1..N drills bound tightly to that text: Правда/Брехня comprehension; sentence-transform with
starters; a DOMINANT numerals-in-cases spine (write %/fractions/counts as WORDS with correct government —
завдяки+Dat, понад/близько/з+Gen, ordinals, case-tagging raw numbers like 204337→Genitive); cause→result
mapping; error-correction. Signature = authentic-text-anchored · productive · case-government-heavy ·
gradable. The numeral case-government is exactly what ungrounded LLMs get wrong — that's the value prop.

FINDING: our existing pilot harness generates curriculum MODULES (1200–1800w theory prose + activity
markers, A1 immersion voice) — the WRONG genre. The packet is a new artifact + bake path.

ANSWER TERSELY, NUMBERED:
1. PACKET SCHEMA: what machine-checkable structure would you give a packet so we can (a) auto-score its
   quality and (b) diff a teacher's edits as data? (fields, per-exercise answer-key shape, difficulty tags)
2. AUTHENTIC-TEXT SOURCING: for a teacher service — live news (browser fetch) vs our corpus (wiki/literary/
   textbook, grounded but not current) vs model-generated-then-grounded? Pick one as DEFAULT + why.
3. GROUNDING: which drill types MUST be VESUM/sources-verified before shipping to beat stock LLMs, and
   how would you make the numeral case-government verifiable (not model-judged)?
4. BIGGEST RISK I'm underweighting (latency, gate-in-loop, teacher-edit capture design, model instability)?
5. MVP: for a first 2–3 packet run that real teachers validate, what would YOU bake first vs defer?
