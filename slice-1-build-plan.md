# Hramatka — Slice 1 CONCRETE build plan (B1 engine, local-first)

> Turns the fleet-reviewed design (`app-spec-v1.md`, `design-v0.md`) into a buildable spec for ONE
> vertical slice. Integration facts are recon-verified (2026-07-08); UA example forms are VESUM-verified.
> This doc WAS the FLEET-REVIEW target for the mandatory pre-build gate.

> **✅ FLEET REVIEW APPLIED (2026-07-08): codex + cursor + grok-build — all GO-WITH-CHANGES.**
> Unanimous on the 5 open questions (see §12). They caught ~5 real linguistic bugs in my solo §6c numeral
> design — ALL re-verified against VESUM by me (#M-4a) and corrected below (§6c rewritten). Infra fixes
> (path robustness, span-repair, lemmatization, fingerprint cache, schema-validate, SystemExit) folded into
> §R. Consensus: architecture is right; the corrections below are the pre-dispatch conditions. Findings are
> now IN this doc — the build brief references §6c + §R as hard requirements. Bridge msg ids 2207/2209/2211.

## 0. Scope (what slice 1 IS — and is NOT)
**IS:** one real B1 anchor + level → grounding-IN → Gemma → 3 gated activities → activity JSON → measure
on ~8-10 real B1 anchors. Activity set: **TrueFalse, Cloze, MatchUp** + **one numeral-probe item** (see §8).
Pure Python engine, run from CLI; NO UI, NO auth, NO persistence, NO server (those are later slices).
**IS NOT:** the full app (teacher UI/roster/assembler), the generative numeral *renderer* (digit→words in
all 7 cases — deferred to the numeral-rewrite slice), multimodal, other CEFR bands, other pedagogies.
**Band = B1** (locked). **Model = Gemma 4 AIS $0 toolless** (locked). **Pattern = model proposes, Python
gate decides** (locked). **KPI = would-a-teacher-accept-as-is rate**, instrumented by gate-pass + edit proxy.

## 1. Placement & governance (no provisioning, private-app posture)
- Build as a **local prototype** at `.agent/tmp/hramatka/engine/` (gitignored → satisfies "Hramatka app is
  a SEPARATE PRIVATE app, not in the public repo tree" + gate #12 "local prototyping allowed, no
  provisioning"). NO `gh repo create` yet (needs user nod); formalize into the private repo once proven.
- Engine READS existing repo assets read-only: `data/atlas.db`, `data/vesum.db`, `scripts/verification/
  vesum.py`, `scripts/ai_agent_bridge/_opencode.py`. It does NOT modify any committed file.
- **Do NOT edit the public `schemas/activities-*.schema.json`** to add engine-only fields (see §4).

## 2. Architecture — engine IR projects to a b1-conformant activity
Recon finding: no `*-b1` activity type has an evidence-span/citation field, and all use
`additionalProperties:false`. So the engine keeps a **richer intermediate representation (IR)** that
carries grounding metadata, and **projects** a pure `activities-b1`-conformant object for rendering.
(This also resolves design-v0 open-Q#1: yes, a richer IR with pre-tagged spans/numbers/lemmas.)

```
HramatkaActivity (engine IR, internal):
  activity:    <one activities-b1 item — the ONLY thing the renderer sees>   # projection
  evidence:    [ {char_start, char_end, quote, kind: literal|inference} ]     # per item/statement/blank
  provenance:  { anchor_id, anchor_hash, level: "B1", type, generator: "gemma-4-31b" }
  gate_result: { passed: bool, checks: [ {gate, status: pass|warn|fail, detail} ] }
```
`render_payload = ir.activity` (strict b1 subset); `gate(ir)` consumes `ir.evidence` + `ir.activity`.
Emit two files per run: `lesson.b1.json` (array of `ir.activity`, validates against activities-b1) and
`lesson.ir.json` (full IR with evidence + gate results, for the measurement harness + teacher review UI later).

## 3. Pipeline stages (`pipeline.py`)
`run(anchor, level="B1", types=[...]) -> list[HramatkaActivity]`
1. **snapshot anchor** → `{anchor_id, body_uk, hash, char_len}` (paste text now; URL/module later).
2. **grounding-IN** (`retrieval.py`) → build a grounding pack (verified vocab + forms + numeral inventory).
3. **generate** (`generate.py`) → Gemma emits candidate activities (JSON) per requested type, each item
   REQUIRED to carry `evidence` spans (char offsets into `body_uk`).
4. **gate-OUT** (`gates/`) → run per-type gate chain; annotate `gate_result`; drop/flag failures.
5. **project + persist** → write `lesson.b1.json` + `lesson.ir.json`.
Idempotent + checkpointed per (anchor_hash, type) so a crash/re-run doesn't double-call Gemma
(fleet ops-risk #4). Cache Gemma raw output keyed by prompt hash under `engine/.cache/`.

## 4. Grounding-IN (`retrieval.py`) — REUSE, don't reinvent
- **atlas.db** (recon Q1): `sqlite3.connect("data/atlas.db")`; `SELECT payload_json FROM article_payloads
  WHERE is_public_route=1`. Per-lemma metadata lives INSIDE the `payload_json` dict — mirror
  `generate_practice_deck.py` helpers: `_cefr_level`, `_heritage`/`_severity` (russianism), `_paradigm`
  (`entry["enrichment"]["morphology"]["paradigm"]["cases"][case]["singular"|"plural"]`), `_section_items`
  (synonyms/antonyms). Build a small local `atlas_lookup(lemma) -> {cefr, heritage, forms, synonyms}`.
- **VESUM** (recon Q1): `from scripts.verification.vesum import verify_words, verify_word`. Copy the
  `_verify_paradigm()` pattern: any form that fails VESUM is dropped/blanked BEFORE it reaches the prompt.
- **Numeral inventory:** regex-extract every numeral+noun candidate from the anchor (digits and
  spelled-out) → tag each `{raw_span, char_offset, numeral, following_noun, class}` for the numeral gate.
- Grounding pack fed to the prompt = { CEFR-appropriate anchor lemmas + verified forms; the numeral
  inventory }. This is retrieval-IN: verified data goes INTO the prompt; the gate checks OUTPUT.

## 5. Generation (`generate.py`) — Gemma, toolless
- `from scripts.ai_agent_bridge._opencode import _invoke_opencode` (recon Q3 — reusable text-in/text-out,
  no bridge-log side effects). Call: `_invoke_opencode(prompt, model="google-ais/gemma-4-31b-it",
  agent="chat", output_format="json", default_timeout_s=900)`. Toolless is MANDATORY (`agent="chat"`).
- Prompt (`prompts/extractive.md`): give Gemma the anchor + grounding pack + STRICT output contract:
  emit `activities-b1`-shaped items, and for each item/statement/blank a REQUIRED `evidence` char-span
  into the anchor. Few-shot 1 example per type. Instruct UA-only content (immersion, #M-13); English only
  where B1 policy allows (glosses) — B1 = teaching voice in UA.
- Retry once on unparseable JSON; log raw to `.cache/`. If still bad → that type = gate-fail "unparseable".

## 6. Gate-OUT (`gates/`) — deterministic, model-proposes/gate-decides
### 6a. evidence_span gate (`evidence_span.py`) — the backbone of "anchor-derived" (span-REPAIR, fleet P0)
Model-emitted char offsets are UNRELIABLE (fleet cursor/grok P0 — trusting them mass-fails 6a). Algorithm:
1. take `evidence.quote`; search for it in `body_uk` (NFC-normalized; try exact, then whitespace-collapsed).
2. **found → RECOMPUTE `char_start/end`** from the located position (ignore the model's offsets); `kind=literal`.
   Store BOTH original char offsets and the normalized-match mapping so downstream render highlights are correct.
3. **quote genuinely ABSENT from the anchor** → for EXTRACTIVE types (TrueFalse/Cloze/MatchUp/MarkTheWords)
   this is a **`fail`** (hallucinated item — must not reach teacher review), NOT a warn (fleet codex MED).
   `kind=inference`+`warn` is allowed ONLY for activity types explicitly declared inferential (none in slice 1).
This makes items authentic AND keeps hallucinations out of the review sheet.
### 6b. VESUM token gate (`vesum.py`)
Every content token the model INTRODUCES that is NOT a verbatim anchor substring (e.g. a Cloze distractor,
a MatchUp gloss) → `verify_words`. Not found → `fail` (fabricated/misspelled form). Anchor-verbatim tokens
are trusted (published text). Russianism = `warn` (atlas `_heritage`), never a hard block in MVP.
### 6c. numeral case-government gate (`numeral.py`) — THE MOAT (REWRITTEN post-fleet-review, VESUM-verified)
Extractive scope = **CLASSIFY + VERIFY**, not generate. Runs on numeral+noun phrases in MODEL-INTRODUCED
strings (a T/F statement, a numeral-probe answer — NOT anchor-verbatim spans, correct by construction).
**Verification uses VESUM tags DIRECTLY** (source of truth; reuse `morphological_validator._parse_case` if
present). Tag map: `v_naz`=nom `v_rod`=gen `v_dav`=dat `v_zna`=acc `v_oru`=instr `v_mis`=loc `v_kly`=voc;
`:p:`=plural (else singular); `anim`/`inanim` on nouns. **pymorphy3 is used ONLY for lemmatizing raw anchor
text, NEVER for final verification** (it can disagree with VESUM — fleet A2).

**Step 1 — required CASE CONTEXT** (fleet A1/A3: this is LIMITED; be honest, WARN when unsure):
- default **nominative**. If a preposition/trigger precedes the numeral, set context via a curated lexicon,
  split into two kinds (fleet BLOCKER — не всі "тригери" = genitive):
  - **case-OVERRIDING triggers** (force the whole numeral phrase into ONE case, numeral class irrelevant):
    `близько/коло/до/від/більш(е) ніж/менш(е) ніж` → **genitive** (`близько ста людей`, `до п'яти років`);
    `завдяки` → **dative**.
  - **TRANSPARENT triggers** (do NOT override — the numeral's own class still decides): **`понад`/`під`**
    (approx) → accusative-inanim = base government (`понад два роки` = nom-pl `роки`, `понад п'ять років` =
    gen-pl). ⚠️ REMOVED the old `понад→genitive` mapping — it was WRONG (VESUM: `роки` is `p:v_naz`).
  - **AMBIGUOUS `з/із`** = genitive (ablative "from") OR instrumental (comitative "with") — SAME surface,
    different case → **WARN, never force** (fleet A/HIGH). Same for other syncretic preps.
- Verb-governed accusative/oblique inside free prose is NOT auto-detectable in slice 1 → if context can't be
  determined from nominative-default or an overriding trigger, mark the item **WARN** and document the limit.
  Do NOT claim full sentence-case coverage. Probe items (§8) are RESTRICTED to nom or explicit-trigger context.

**Step 2 — government CLASS from the numeral's LAST word** (compound → last token decides), producing a
required {case, number} for the noun:
- ends in **1** (один/одна/одне, not 11) → noun in **context case, SINGULAR**, and один agrees in gender+case.
  ⚠️ This holds in OBLIQUE too: `на двадцяти одному поверсі` = loc **sg** `поверсі`; `з двадцятьма одним
  студентом` = instr sg. (Old "oblique → all plural" was WRONG for ×1 — fleet BLOCKER/P0.)
  ⚠️ **COMPOUND-PREFIX DECLENSION (build-verified 2026-07-08):** standard UA declines EVERY component of a
  compound cardinal — `на ДВАДЦЯТИ одному`, not `на двадцять одному` (my earlier example used the colloquial
  un-declined prefix — WRONG, caught by the builder's re-verification). The gate verifies each numeral
  component has a VESUM parse in the required case; an un-declined prefix (e.g. loc `сто двадцять`, should be
  `ста двадцяти`) → **WARN `compound-prefix-unverified`** (never a false PASS; WARN not FAIL because the
  un-declined form is widespread colloquially → teacher decides).
- ends in **2/3/4** (not 12-14):
  - context nom / **acc-INANIMATE** → noun **nominative plural** (`два столи`).
  - context **acc-ANIMATE** → noun **genitive-plural form** (`бачу двох студентів`, VESUM `студентів` =
    `anim:p:v_zna`≡`anim:p:v_rod`) — NOT nom pl. (Missing acc-animate branch was a fleet P0 — use VESUM `anim`.)
  - context oblique (gen/dat/instr/loc) → noun in **context case, plural** (numeral also declines).
- ends in **5-9 / 0 / 11-19** → noun **genitive plural** in nom/acc; in oblique → context case plural.
- **collective numerals** (двоє/троє/четверо/п'ятеро/…, обоє) → SEPARATE class: noun **genitive plural**
  ALWAYS; never the 2-4 nom-pl rule (fleet P1).
- **півтора(m/n)/півтори(f)** → noun **genitive SINGULAR** (`півтора року`) (fleet P2).
- **тисяча/мільйон/мільярд/тисячі…** = NESTED phrase, NOT "always gen pl" (fleet P1, VESUM-confirmed
  `дві тисячі`=`p:v_naz` vs `п'ять тисяч`=gen pl): (a) classify the OUTER numeral to fix the magnitude word's
  form (`дві тисячі`/`п'ять тисяч`); (b) the magnitude word then governs the counted noun as **genitive
  plural** (`дві тисячі людей`). Verify BOTH the magnitude-word form and the noun form.
- **ordinals** (перший/сімнадцятий…) → adjectival agreement (gender/number/case) with the noun; for slice 1
  either check agreement or **WARN/skip** (out of the cardinal moat) — decide in build, default WARN.
- **fractions / DECIMALS** (`2,5 рази`) → **WARN, never hard-fail** — government is genuinely variable
  (VESUM: both `рази` and `раза` valid). Defer to teacher. (Confirmed §10b.)

**Step 3 — VERIFY** the noun form present has VESUM tags matching the required {case, number} for its lemma
(and animacy where it matters); the numeral's own form is VESUM-valid and gender-agrees (×1, ×2 дв-а/-і).
**Step 4** — violation → `fail` (specific rule + expected form); genuinely ambiguous/undetectable context,
syncretic prep, decimal, or ordinal → `warn`. **No LLM judge anywhere.**

VESUM-verified acceptance examples (2026-07-08): `два столи`→`п'ять столів`; `дві третини`; `17 ділянок`;
`близько ста людей`; `бачу двох студентів`(acc-anim); `на двадцяти одному поверсі`(loc sg, ALL components
declined); `дві тисячі людей`; gender: `два столи`/`дві книги` pass, `дві столи`/`два книги` FAIL.
**Deferred to the numeral-rewrite slice:** the GENERATIVE digit→words renderer (compose a numeral
in words in an arbitrary case+gender). Not needed to CLASSIFY+VERIFY existing phrases.

## 7. Per-type gate chains
| Type | Gates applied |
|---|---|
| TrueFalse | 6a on each statement's evidence; 6b on introduced tokens; 6c on any numeral phrase in a statement. `correct` label for FALSE statements = `warn` (can't fully verify falsity deterministically → teacher confirms). |
| Cloze | 6a: carrier sentence must be an exact anchor span; the blanked token = the removed anchor token (answer correct-by-construction). 6b on distractor `options`. 6c if the blank sits in a numeral phrase. |
| MatchUp | 6a: left items = anchor lemmas/spans; 6b: right items (glosses/synonyms) VESUM-valid or atlas-`_section_items`-sourced. |
| numeral-probe (§8) | 6c is the PRIMARY gate; 6a on the anchor number cited. |

## 8. Numeral moat exercise — POSITIVE probe (pipeline) + NEGATIVE fixtures (eval-only) — RESOLVED by fleet
The moat is the differentiator, but a purely extractive slice barely fires 6c (extracted phrases are
correct-by-construction). Fleet was UNANIMOUS on the resolution — split acceptance vs rejection testing:
- **POSITIVE probe (learner-facing, in the pipeline):** one anchor-grounded item per anchor where Gemma
  RESTATES a number from the anchor in a new sentence that must **PASS** 6c (e.g. anchor «дві третини» →
  a correct restatement). Contexts RESTRICTED to nominative or explicit-trigger (per §6c step-1 limit).
  These measure that 6c ACCEPTS good model-constructed forms.
- **NEGATIVE fixtures (NEVER learner-facing):** deliberately wrong-government strings live ONLY in
  `measure.py`/pytest as a hand-labeled bank, to measure 6c's REJECTION recall. Shipping a morphologically
  wrong T/F item to a teacher would itself be a reject-trigger (fleet D) — wrong-government items are
  gate-fail BY DESIGN, so they belong in the eval harness, not the lesson. (If we later want a "spot the
  error" activity, that's an explicit error-CORRECTION type, gated the opposite way — slice 2+.)
Net: slice 1 measures BOTH sides of the moat (accept via the positive probe + anchor #1's real phrases;
reject via the offline negative fixtures) without ever showing a broken item to a human.

## 9. Anchors (`anchors/`)
Seed from `alona-text.txt` (recon Q4): lines 4681-4693 ("10 причин читати!"), 12619-12631 ("Жінка та
чоловік…"), 4640-4680 (myth-busting drills, closer to clean B1). Extract 8-10 short prose passages
(150-400 words). **Level-check step:** run each candidate's lemmas through `query_cefr_level`/atlas CEFR
before treating as verified B1 (both flagged B1-B2 by recon). Do NOT use lines ~12690-12730 (C1/C2). These
are PRIVATE student material — run OUR local engine on them, never send raw student text to an external API
beyond the Gemma generation call the pipeline already makes on the anchor (which the design accepts).

## 10. Measurement harness (`measure.py`) — the point of the slice
For each anchor × type: run pipeline → record gate-pass rate, gate-fail reasons, and produce a review
sheet (`measure-report.html`, ai→human = HTML #M-2) with the rendered activity + evidence + gate verdicts
for HUMAN acceptance scoring. KPIs:
- **gate-pass rate** (deterministic): % items passing 6a/6b/6c clean.
- **would-accept rate** (human, the real KPI): I + fleet sample-judge each item accept-as-is / edit / reject.
- **numeral-differentiator score:** on the §8 probe, 6c's precision/recall vs a hand-labeled key
  (does the gate catch the wrong-government cases and pass the correct ones?).
- **edit proxy:** count/kind of edits a reviewer would make (drives the edit-rate-down loop).
Success = high would-accept on extractive types AND 6c correctly separates good/bad numerals. A high style-
edit rate = FAILURE signal → escalate the generator, not "great, more data" (design-v0 success bar).

## 10b. VERIFIED anchor + numeral test cases (2026-07-08, VESUM-confirmed)
Anchor #1 = `alona-text.txt` lines 4681-4693 ("10 причин читати!") is CONFIRMED ideal for slice 1: real
popular-science prose (B1-B2) that NATURALLY contains numeral-government cases, giving the moat gate real
acceptance fixtures for free (a purely extractive slice already exercises 6c on real phrases; the §8 probe
adds the REJECTION test). Confirmed forms (all VESUM-found):
- «10 причин» — 10 → gen pl → `причин` (причина) ✓ ; «дві третини» — 2(fem) → nom pl → `третини` (третина) ✓
- «17 ділянок» — 17 → gen pl → `ділянок` (ділянка) ✓ ; «активізуються 17 ділянок» (nominative context).
- «в 2,5 рази» — DECIMAL: both `рази` AND `раза` are valid VESUM forms of `раз` → decimal government is
  genuinely AMBIGUOUS → **gate 6c must WARN (not hard-fail) on decimals/fractions**, deferring to teacher.
  (Bake this as a gate rule + a fixture: decimal+noun → warn, never fail.)
These become the numeral-gate unit-test fixtures (acceptance side). The §8 probe supplies the rejection side.

## R. HARDENING — fleet B/C/D fixes (HARD build requirements, apply all)
Infra/pipeline/quality fixes from the review. The build brief lists these as acceptance criteria.
**Path & preflight (fleet P0 — critical for the `.agent/tmp/` placement):**
- Central `get_project_root()` (walk up to the repo marker) OR a `--root` CLI arg; the CLI entry `chdir`s to
  root or aborts with a clear message. `vesum`/`atlas` callers take an **explicit `db_path`** (don't rely on
  cwd-relative `data/atlas.db`). **Preflight** asserts: `data/atlas.db` + `data/vesum.db` exist, `opencode`
  on PATH, `~/.secret/google-ais.key` present, `chat` agent resolvable — fail fast with actionable errors.
- Test the engine RUN from its real directory (`.agent/tmp/hramatka/engine/`), not just from repo root.
**Grounding-IN (fleet B1/C1):**
- Add an explicit **lemmatization stage**: tokenize anchor → lemmatize (pymorphy3 or VESUM reverse-lookup) →
  atlas lookup + numeral-inventory extraction. atlas/VESUM need LEMMAS, not surface forms.
- Build a `{lemma→payload}` dict ONCE per run (extract anchor lemmas first) — no full-table-scan per lemma.
**Generation (fleet P1/C2):**
- Wrap `_invoke_opencode` to catch `SystemExit` → `gate_result.fail("generator-unavailable")` (don't crash
  the pipeline). Pin the private import + add a narrow regression test that the raw invoke still returns text.
- Gemma emits an evidence-carrying SUPERSET; parse it, then STRIP to the pure b1 subset for `activity`.
**Gate/output integrity (fleet P1):**
- **`jsonschema.validate`** each projected item against `schemas/activities-b1.schema.json` BEFORE writing
  `lesson.b1.json` (catch projection drift: `correct`→`isTrue`, `text`→`passage`, `id`→`index`, etc.).
- **Cloze evidence** = the PRE-gap source-sentence span (the gapped `text` can't be an exact substring);
  separately assert the answer token was removed from that exact span. Unit-test with a real cloze carrier.
- **Idempotency/cache key** = full inputs fingerprint: `anchor_hash + grounding_pack_hash +
  prompt_template_hash + model_id + db_fingerprint(mtime/hash)` — not just `(anchor_hash, type)` (stale hits).
**MatchUp semantics (fleet MED/P2):** right-sides prefer **UA synonyms from atlas `_section_items`**; a
VESUM-valid token is NOT proof of correct meaning → non-atlas right-sides are `warn` (teacher-confirm).
Keep MatchUp UA-first (#M-13); an English gloss is a fallback `warn`, never the default.
**Measurement honesty (fleet D):** the harness tracks **russianism-warn rate** and **B1-B2 lexical-stretch
tags** as NON-green signals (gate-pass ≠ teacher-accept); it scores SEMANTIC accept separately (a literal
span proves SOURCE, not TRUTH — a T/F item can misread the anchor). `would-accept-as-is` is the only real
bar; a high style-edit rate ⇒ escalate the generator, per the success bar — never "great, more data".

## 11. Build division (dispatch plan)
- **Numeral gate + pipeline + gates (novel, cross-file, hard linguistics) → codex** (primary).
- **retrieval.py + schema projection + fixtures (mechanical reuse of known patterns) → agy** (or fold into codex if small).
- **Anchors extraction + measurement harness → can be codex or inline.**
- Prototype lives local (no worktree needed for gitignored `.agent/tmp/`), BUT the builder must still: run
  pytest on the gate unit tests, ruff, and commit NOTHING to the public tree (engine stays gitignored).

## 12. The 5 questions — RESOLVED by unanimous fleet call (2026-07-08)
1. **Numeral-probe (§8):** ✅ INCLUDE — positive probe in the pipeline; negative wrong-government cases as
   eval-only fixtures (never learner-facing). All three agreed.
2. **Engine IR vs schema edit (§2):** ✅ IR — carry evidence out-of-band, project to the pure b1 subset;
   public schema untouched. All three agreed.
3. **Numeral gate scope (§6c):** ✅ CLASSIFY+VERIFY, defer the generative renderer — BUT only after the §6c
   linguistic patches (all three made this a hard pre-dispatch condition).
4. **Gemma transport (§5):** ✅ `_invoke_opencode` subprocess now, with preflight + `SystemExit` wrap
   (§R); direct AIS client only if it bottlenecks. All three agreed.
5. **FALSE-statement verification (§7):** ✅ WARN + teacher-confirm, and teacher-confirm MUST BLOCK
   "accept-as-is" (NLI out of scope). All three agreed.
