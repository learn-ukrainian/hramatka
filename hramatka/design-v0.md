# Hramatka — design v0 (anchor → grounded activity bank → teacher-driven assembler)
> ⚠️ **2026-07-10:** the «UI/host: Private HF Space» line below is SUPERSEDED — pinned host =
> **Scaleway dev1-m** (docs/decisions/hramatka-hosting-decision.md). Everything else stands.


## ‼️ DIRECTION (2026-07-07, user-confirmed) — BUILD GROUND-UP ON LU; vibe = REFERENCE ONLY
`~/projects/vibe` is the user's OLD 1-agent learning project (mistakes, weaker stack). It is a
SPEC-BY-EXAMPLE of the vision — NOT code to reuse or fork. "We cannot use vibe, we will run into
conflicts" (divergent+weaker activity model, stale stack, two-codebase merge pain). USE VIBE ONLY to
mine REQUIREMENTS — above all the pedagogy-as-GUARDRAIL idea: `lessons.methodology`
(ppp/ttt/gppc/tbl/custom) + `phases` drive what we ask the teacher and how we structure the lesson.
Also the teacher→students→per-student-difficulty→lesson→assign flow as a data-model sketch.
=> BUILD FRESH, on learn-ukrainian's OWN foundation:
   - ACTIVITIES: LU's V7 set (22 types) — richer/better than vibe's 8. These are our real components.
   - CORRECTNESS: sources MCP + VESUM + deterministic gates.
   - CONTENT + ENGINE: curriculum/corpus + the Gemma anchor→activities generator (designed below).
   - APP: a NEW teacher-facing app (auth/students/difficulty/lesson/pedagogy-guardrail/render) written
     from scratch on our stack — do NOT import vibe code.
The ENGINE design below (anchor→activities→gates, Gemma, edit-capture, success-bar) stays valid.
The old UI/hosting sections (HF Space/FastAPI "packet" framing) are SUPERSEDED — the deliverable is a
ground-up teacher APP on LU, not a packet tool and not vibe.

## ⭐ SUCCESS BAR (user 2026-07-07, overrides the "edit-capture is the moat" framing)
Teachers expect a FINISHED product that generates CORRECT content they DON'T need to edit. Occasional
edits = fine; editing EVERY TIME = FAILURE. The primary KPI is **teacher-accepts-as-is rate**, not
"edits captured". Edit-capture stays but is INVISIBLE INSTRUMENTATION we use to drive the edit-rate
DOWN — it is NOT the product's reason to exist and NOT a license to ship mediocre generation.
COROLLARY: grammar is gate-able (deterministic), but ACCEPTANCE (style/richness/pedagogy) is not →
we must PROVE would-accept quality INTERNALLY (me + gates + fleet + your sample-judgement) BEFORE any
real teacher sees output. Do not burn teacher trust discovering quality gaps in front of them.
COROLLARY: Gemma 4 is the STARTING bet ($0, explore its ceiling); if it can't hit the acceptance bar,
ESCALATE the per-type generator to a stronger model. Product bar > cost here.

## v1 DECISIONS LOCKED (user 2026-07-07)
- Anchor: teacher ALWAYS supplies it (paste URL/text). Future: pick from our modules.
- Activities: OPEN/extensible registry, derived FROM the anchor content. Enable, don't bound.
- Model: Gemma 4 (`google-ais/gemma-4-31b-it`, $0 AIS, toolless) — probe-verified good UA on
  simple cases; deterministic gates are the safety net for hard numerals.
- UI/host: **Private HF Space** (Gemma via AIS, scale-to-zero) — shareable for Tetiana/Alona.
- Scope v1: **TEXT LOOP ONLY** (anchor→text-activities→gate→teacher-edit). All CEFR levels.
  Multimodal (photo-anchor OCR/handwriting + generated voice/image assets) = **PHASE 2**.
- **Generated IMAGE-OUT is a CONFIRMED teacher requirement** (user 2026-07-07: "my teachers want
  image out, we'll figure it out later") — NOT optional. Parked to phase 2 but the activity registry
  keeps a pluggable "asset generator" seam so image-out (Gemma orchestrates → Imagen-class gen) slots
  in without an architecture change. Do not design the v1 activity schema in a way that blocks it.


> DRAFT for fleet-review (mandatory gate before build). Local/gitignored while it references the
> private teacher corpus analysis; a PII-free version promotes to `docs/hramatka/` via worktree PR
> after review. No student identity/correspondence anywhere. Sources: fleet panel 2026-07-07
> (codex/agy-gemini-pro/cursor) + user steers 2026-07-07.

## 1. Problem & goal
Ukrainian teachers say stock ChatGPT/Claude/Gemini can't produce *proper* Ukrainian lessons — the
failure mode is confident wrong morphology (esp. numerals in cases) and unnatural/russified forms.
We have the antidote: `sources` MCP (VESUM/СУМ-20/Грінченко/ЕСУМ/textbooks + russianism/CEFR checks),
deterministic gates, and a bakeoff showing cheap TOOLED models get grounded where frontier models
fabricate. Build a teacher-facing tool that **enables** teachers (never bounds them) to turn a text
they choose into a rich, correct, varied set of activities — and captures their edits as ground-truth
data (the moat).

## 2. Core flow (LOCKED w/ user)
```
teacher supplies ANCHOR ──▶ generate ACTIVITIES from the anchor's content ──▶ teacher ASSEMBLES
   (paste URL→snapshot+hash,     (open, varied activity registry;              (reorder / skip / swap /
    or paste text;                each item derived from + cited to             compose freely — the tool
    LATER: pick our module)       the anchor; gated deterministically)          is a scaffold, not a cage)
                                              │
                                              ▼
                              DETERMINISTIC GATES verify each item (VESUM/sources)
                                              │
                                              ▼
                              teacher edits ──▶ STRUCTURED EDIT-CAPTURE (the data loop)
```
- **Anchor is teacher-supplied, always.** Interface is source-agnostic: `{kind: url|text|module,
  body_uk, url?, published_date?, excerpt_hash}`. URL → snapshot + hash (anchors must not rot).
  Future `kind: module` reuses our curriculum content as an anchor — same interface.
- **Activities are ANCHOR-DERIVED.** Every generated item must cite the anchor (span/number/lemma it
  came from). This is what makes them authentic AND auto-gradable against the source.

## 3. Architecture — activity BANK + assembler (per user steer: open & varied, not a fixed template)
- **Activity registry (extensible).** Each activity `type` = `{generator, grounding_gate,
  answer_key_shape, difficulty_tags}`. Adding a new activity type = registering one more (generator,
  gate) pair — the system is NOT limited to the ~5 types seen in one teacher's doc. Seed set (MVP):
  Правда/Брехня, numeral-rewrite/case-tag, sentence-transform, error-correction, cause→result,
  productive writing task (ДОМАШКА). Backlog is open (gap-fill, matching, ordering, dictation,
  paraphrase, register-shift, синоніми/антоніми, collocations, …).
- **Assembler = teacher-driven.** Generated items land in a pool; the teacher orders/prunes/edits.
  No enforced sequence. Reordering is captured as signal, not suppressed.
- **Gate registry.** Per-type deterministic grounding gate. The model PROPOSES; the gate DECIDES.
- **Generator model:** cheap tooled seat (gemma-4-31b AIS $0 / deepseek-4) + `sources` MCP.
  Choice measured via the existing bakeoff + a lesson-writing probe, not guessed.

## 4. The moat — deterministic numeral case-government gate (gate #1, unanimous fleet)
Model emits, per numeral item, a TARGET TUPLE (not free prose):
```yaml
raw_span: "близько 204337 людей"      # cited from anchor
trigger: "близько"                     # governing word
required_case: genitive                # from trigger→case lexicon (понад/близько/з/завдяки/…)
numeral: 204337
noun_lemma: "людина"
```
Gate (pure Python + VESUM, NO LLM judge):
1. `trigger` → `required_case` via a fixed trigger→case lexicon.
2. digit→words render of `numeral` in `required_case` via a deterministic UA number-grammar module.
3. every rendered token `verify_words` against VESUM; noun phrase agreement checked.
4. the rendered+verified string becomes the strict `expected` answer key.
Fail if any form is missing from VESUM or the government rule is violated. This is the exact class
stock LLMs get wrong — and it's 100% deterministic, so it's a real gate, not a vibe.

## 5. Edit-capture contract (INSTRUMENTATION to drive the edit-rate down — NOT the product's purpose)
Reframed per the Success Bar: the teacher's goal is to NOT edit. Edit-capture is invisible telemetry we
use to improve generation; it never adds teacher friction and never excuses weak output. When it does
happen, capture STRUCTURED ops on a frozen `packet_id` (freeform Docs/WYSIWYG edits = unusable noise):
```yaml
op: replace|remove|add|reorder
path: activities.<id>.items[<i>].expected.lemma   # field-path, not whole-doc diff
old: ...
new: ...
reason_tag: wrong_case|unnatural|too_hard|too_easy|factual|off_topic|other
```
Plus packet-level labels: `reject_packet` / `accept_with_edits` / `accept_as_is`. **Reordering IS a
captured op** (aligns with "Alona carries tasks forward" — that's signal, not something to prevent).
Output: diff-friendly YAML + labeled (model_output → teacher_truth) pairs for eval/SFT.

## 6. Per-activity grounding gates (MVP)
| Activity | Deterministic gate |
|---|---|
| Numeral rewrite / case-tag | §4 — full deterministic government + VESUM |
| Error-correction | `error_token` must fail the target rule; `correct_token` VESUM-verified + passes government |
| Правда/Брехня | `evidence_span` must be exact substring of anchor body; else `inference`+teacher-approve |
| Sentence-transform / cause→result | required lemmas+prepositions VESUM-verified; endpoints cite anchor spans |
| ДОМАШКА (productive) | constraint checklist (min sentences, ≥N numerals, required constructions); numerals in any model draft hit §4. Free-writing quality = teacher rubric, NOT model-judged |
| all keys | russianism/style = WARN (not block) in MVP |

## 6b. Model = Gemma 4 (user pick 2026-07-07) — verified capability surface
- Seat: `google-ais/gemma-4-31b-it` ($0 via AIS, wired #4767). Runs TOOLLESS → fits our
  "model proposes, gate decides" moat perfectly (grounding = OUR Python+VESUM gates, not Gemma tools).
- **Gemma 4 = multimodal IN, text OUT** (model card). It reads images (OCR incl. multilingual,
  HANDWRITING recognition, document/PDF, chart comprehension) + audio (E2B/E4B/12B only) + video.
  It does NOT generate voice or images itself.
- **Input-multimodality is the big teacher unlock** and powers "teacher supplies the anchor":
  - anchor = PHOTO of a textbook page / worksheet / chart → OCR → activities (no retyping);
  - HANDWRITING recognition → read handwritten student work / teacher notes;
  - audio-in (E4B/12B variant) → listening + pronunciation activities from student/teacher speech.
- **Generated voice/images = composition, not Gemma alone:** pair Gemma (text + understanding) with
  Google's image-gen (Imagen-class) + a TTS model for listening clips / visual-vocab pictures. Same
  Google/AIS stack, cheap; just not the Gemma LLM by itself. Design the media layer as a pluggable
  "asset generator" the activity registry can call, independent of the text generator.
- ⚠️ Ukrainian NOT in Gemma's headline 35+ languages → MEASURE its UA generation quality early
  (bakeoff/probe); the numeral-government + VESUM gates are the safety net for its UA morphology.

## 7. MVP cut (2–3 teacher-validated packets) — WITH a UI (user: no YAML torture)
Bake first: `schemas/hramatka-packet/0.1.yaml` + `scripts/hramatka/gate_packet.py` (mirrors the
`run_b1_writer.py` "bake without full V7 pipeline" pattern) → teacher-supplied anchor (paste URL/text/
PHOTO) → generate the seed activity set → run gates → render in a **real teacher UI** (assemble/reorder/
edit + structured edit-capture behind the scenes). **All CEFR levels** — the anchor + generator are
level-parametrized (teacher picks or it's inferred); the B1–B2 sample data does NOT constrain the system.
**Gate BEFORE teachers:** every pilot packet clears an internal would-a-teacher-use-this-untouched bar
(me + deterministic gates + a fleet quality pass + your sample-judgement) BEFORE Tetiana/Alona see it.
Pilot success = teachers accept packets AS-IS most of the time (occasional edits fine); a high style-edit
rate is a FAILURE signal → escalate the generator, not "great, more data".
Defer: auto news-fetch/rotation, auto-grading free writing, module-as-anchor, generated voice/image
assets (compose later), broad activity breadth beyond the seed set (add via registry post-pilot).

## 8. UI (IN the MVP — user order: don't torture teachers without a UI)
Teachers are non-technical → the pilot ships a real UI, not a YAML round-trip. Candidate stack (from the
earlier sketch, to fleet-review): thin FastAPI backend + invite-token auth + streamed generation + the
assembler board (drag/reorder/edit) + inline gate badges + structured edit-capture. Host: private HF
Space (gemma-4 via AIS; scale-to-zero) OR small VPS. The UI's edit surface IS the data-capture surface —
they're the same component. Media (voice/image) is a later pluggable asset layer. ZNO formats: backlog.

## 9. Open questions for the fleet review of THIS doc
1. Is the anchor→activity registry the right seam, or does the assembler need a richer intermediate
   representation (e.g. anchor is pre-annotated: numbers, spans, lemmas tagged once, activities drawn
   from that annotation layer)?
2. digit→words UA number grammar: build ours vs an existing lib? (must cover cardinals, ordinals,
   fractions, percents, large ints, all 7 cases, gender agreement) — verify before choosing.
3. Edit-capture UI vs a structured YAML round-trip for the pilot (no UI yet) — cheapest path that
   still yields clean labels?
4. Trigger→case lexicon coverage: is a curated list sufficient, or do we need corpus-derived
   government patterns (GRAC) to avoid gaps?
