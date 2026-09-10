# Hramatka — homework-packet DNA + harness gap (from real Alona master doc)

> LOCAL / GITIGNORED. Derived from `data/native-reviewer-lessons/alona-master.docx`
> (113 MB, gitignored, NOT tracked). No student identity / correspondence in this file.
> Extracted text cached at `.agent/tmp/hramatka/alona-text.txt` (gitignored). Do NOT send
> raw student material to external model APIs — abstract to the pattern below.

## Source shape
- 672K chars (~448 pp), 15,013 paragraphs, 145 embedded images, NO Word heading styles
  (structure is implicit → this is why the doc "isn't helpful" structurally; the DATA is).
- Running B1–B2 lesson journal for one advanced student. Dated sessions Mar–Apr 2026.
  Marker counts: B2 ×28, B1 ×16, Level ×16, Завдання ×18, Домашн ×10, Слов ×81, A1 ×2, A2 ×0.
- Source is privacy-conscious: `[REDACTED_ZOOM]` already in the doc.
- User's note: Alona carries unfinished tasks forward across sessions → per-lesson task↔date
  mapping is UNRELIABLE. Harvest the task/content DATA, not the sequencing.

## Packet DNA (the repeatable pattern — THIS is the artifact Hramatka must produce)
1. **Authentic dated current-events text** — a real UA news article (UNIAN / gazeta.ua /
   hromadske / klopotenko), topic-varied (gaming-PC hardware stats, AI adoption in Ukraine,
   university admissions, drone defence, fitness study), WITH source URLs. B1–B2 register.
2. **ДОМАШКА** — ONE productive writing task with explicit, checkable constraints, e.g.:
   "email to a computer store, 10–12 sentences, ≥4 numerals, use 'Чи могли б Ви…', state
   what the computer is for." Constraints are gradable.
3. **ВПРАВА 1…N** — drills bound TIGHTLY to that text:
   - Правда/Брехня (T/F comprehension of the article's facts)
   - "Зробіть речення" / sentence-transformation with starters (Приблизно…, Понад половина…)
   - **Numerals-in-cases spine (dominant):** write %/fractions/counts as WORDS with correct
     government — `завдяки + Dat` (шістнадцяти гігабайтам), `понад/близько/з + Gen`, ordinal
     & fractional forms (чотири цілих сімдесят дев'ять сотих відсотка), case-tagging of raw
     numbers (204 337 → Р., 4 643 → Д.).
   - Cause→Result mapping (Причина/Результат)
   - Помилочки (error-correction — fix agreement/case)

**Signature = authentic-text-anchored · productive · case-government-heavy · gradable.**
This is precisely the "proper UA lesson stock GPT/Claude/Gemini can't make" the service targets;
the numeral case-government is the exact failure mode ungrounded LLMs exhibit.

## HARNESS GAP (load-bearing design finding)
- `scripts/build/pilot_uk_lesson.py` = curriculum-MODULE generator: theory prose 1200–1800w,
  `INJECT_ACTIVITY` markers, A1 immersion voice, Gemini-CLI (now unavailable). Output sample
  `pilot-output/a1/sounds-letters-and-hello.md` is a lovely A1 phonetics *lesson* — NOT a
  teacher homework packet. Wrong genre for Hramatka.
- => Hramatka needs a NEW packet artifact + bake path. Open design questions:
  - **Authentic-text sourcing:** live news (browser fetch) vs corpus (sources MCP: wiki/
    literary/textbook — grounded but not "current news") vs model-generated + grounded.
  - **Grounding/verification:** numerals & forms VESUM-verified (`mcp__sources__verify_words`,
    `query_cefr_level`, `check_russian_shadow`) so the drills are CORRECT — the value prop.
  - **Gradability:** every ВПРАВА needs a machine-checkable answer key (T/F, exact-form,
    case-tag) → this is what lets us auto-score packet quality AND capture teacher edits as data.
  - **Model:** gemma-4-31b-it (AIS $0) vs deepseek-4 — measured, not guessed (per discuss doc).

## Recommended plan (for review, not locked)
1. Fleet-review the packet CONTRACT + bake approach (mandatory gate) with the abstracted DNA.
2. Bake ONE POC packet through a tooled model + sources MCP, VESUM-verified drills.
3. Scale to 2–3 packets across topics → Tetiana/Alona validate → capture edits as data.
4. HF-infra design doc → MANDATORY fleet-review BEFORE any Space build.

## FLEET PANEL (codex / agy-gemini-pro / cursor-auto, 2026-07-07) — convergence
- **MOAT = deterministic numeral case-government gate (UNANIMOUS).** Model PROPOSES a target tuple
  `[value, required_case, gender, trigger, noun_lemma]`; a Python gate DECIDES: digit→words via fixed
  UA number grammar, every token VESUM `verify_words`, governing case from a trigger lexicon
  (понад/близько/з/завдяки/… → case enum), optional ULIF paradigm cross-check. Never LLM-judged.
- **#1 RISK = teacher-edit capture (UNANIMOUS).** Freeform WYSIWYG/Docs edits = noise → the whole
  feedback loop dies. Capture structured field-path ops `{op, path, old, new, reason_tag:
  wrong_case|unnatural|too_hard|factual|other}` + packet-level labels (reject / accept-with-edits).
- **Per-item grounding gates:** T/F evidence_span must be exact substring of reading body (else mark
  `inference` + block till teacher-approved); error-correction keys VESUM-verified; domashka constraint
  checklist; russianism/style = WARN not block in MVP.
- **Text anchor:** codex+cursor → teacher supplies live article (paste URL/text → snapshot+hash+ground);
  agy → model-generate-then-ground (CEFR safety). Corpus = verification/fallback only, NOT reading body.
- Offered next: `schemas/hramatka-packet/0.1.yaml` + `scripts/hramatka/gate_packet.py` stub, mirroring
  `run_b1_writer.py` "bake without full V7 pipeline" pattern.

## USER STEER (2026-07-07, binding) — reshapes the architecture
1. **ENABLE teachers, don't BOUND them.** Teachers reorder/skip/swap/compose freely; the packet is a
   flexible SCAFFOLD they adapt, never a fixed-order deliverable. (Matches "Alona reorders tasks".)
2. **Activity types are OPEN, not the ~5 seen in the doc — "much more, much varied".**
=> Architecture becomes a **grounded ACTIVITY BANK + teacher-driven ASSEMBLER**, NOT a rigid packet
   template. Activity `type` is an EXTENSIBLE REGISTRY; each type ships its own deterministic grounding
   gate (numeral-government is gate #1 of many, not the whole system). Edit-capture must handle free
   reordering/composition, not a frozen sequence. The schema serves the MACHINE (gate + edit-diff), it
   does NOT constrain the teacher's ordering or activity choice.
