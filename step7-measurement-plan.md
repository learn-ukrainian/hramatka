# Step-7 measurement plan — anchor-set spec + acceptance rubric (v2, 2026-07-10)

> Status: v2 — cross-family review applied (grok-build, ask 2351/2352, GO-WITH-CHANGES,
> all 12 changes accepted; r2 CONFIRM-GO, ask 2353/2354). Author: main-claude (orchestrator seat).
> Plan of record: #4542 build sequence (Sol's 8 steps); step 7 = «8–10 anchors, independent
> pedagogy review and teacher-style acceptance scoring». This is one of FIVE legs of the
> step-8 teacher-access gate (defects fixed · **this measurement** · leak controls ·
> restart/backup rehearsal · dev1-m load test). It arms nobody by itself.
> Written feedback-independent so it FIRES the moment steps 3–4 land.

## 0. What this measures — and what it does not

Measures the **engine at anchor scale**: the private B1 extractive pipeline
(google-ais Gemma toolless → deterministic gates → IR → lu.activity/lesson
projection) across a pre-registered 12-anchor slate, plus e2e evidence for all
8 Sol defect fixes, plus the real KPI — **teacher-style accept-as-is** — scored
by two independent blind raters with third-family adjudication.

**Honest power statement (small-N):** 12 anchors (~100–140 items) is a
QUALIFYING gate, not a statistical estimate — planted fixtures + hard gates
give defect *detection*; acceptance rates carry wide uncertainty and are read
as evidence for a judgment call, never as tight point estimates. The design
compensates with pre-registration, class diversity, blind dual rating, and
zero-tolerance hard gates on the product-killing classes.

Does NOT measure: editor UX (step 6), auth/ops (leak controls, restart, load —
separate gate legs), URL ingestion (post-pilot), non-B1 bands (out of v1
scope, defect #8). **Unit-of-measurement honesty:** if the step-5 lesson
assembler (mock→real swap) is live at run time, lesson-level assembly is
measured too (§3); otherwise the measurement is ITEM-LEVEL ONLY, lesson-level
thresholds in §6 are marked N/A in the results, and no public statement may
claim lesson-level capability from this run.

## 1. Preconditions (fire checklist — all boxes before the run)

- [ ] Steps 3–4 merged: engine private, imports pinned, all 8 defect fixes
      landed with their own unit/integration tests green.
- [ ] Tri-state gate semantics live end-to-end (`clean | review_required |
      failed`) — defect #1. `engine/measure.py` migrated off the `passed`
      boolean: per-item tri-state counts in KPI + report.
- [ ] `measure.py` extended (small, engine-lane): determinism probe (§4.8),
      russianism e2e fixtures (§4.4), expanded numeral regression corpus wired
      as the bank (§4.5), numeral-transfer KPI (§4.5b), wall-clock + cost per
      bake recorded, report fields per §5b.
- [ ] Anchor slate pre-registered, committed, and **version-pinned** (§2)
      BEFORE any bake.
- [ ] Rater A + B calibration round completed and criteria locked (§5).
- [ ] Rater B (independent pedagogy lane, cross-family) booked; blind protocol
      agreed (§5).

## 2. Anchor-set spec — N=12, pre-registered, immutable

Anchors, IR, prompts, and reports are on the leak list (Sol item 1): everything
in this measurement stays in the **private repo**. Public reporting = aggregate
numbers only (§7).

**Pre-registration rule (anti-cherry-picking, hardened):** the full slate is
committed to `hramatka/anchors/step7-slate.md` as a SINGLE IMMUTABLE artifact
BEFORE any bake: exact 12 anchors, full metadata each (id, source class, char
count, domain, why-chosen, *expected landmines* — what the gates must
catch/pass in this text). **The same commit pins the exact engine SHA,
prompt/package/gate versions, and data-bundle digests the run will use. Any
post-curation engine/prompt tuning VOIDS the slate → re-pre-register (both
slates stay in history; the void is recorded in results).** No swapping after
results are seen; a defective anchor (wrong level class etc.) is marked VOID
with reason and replaced by a NEW pre-registered entry, both recorded.

| # | class | spec |
|---|---|---|
| 01–02 | **R — regression** | anchor01 («10 причин читати!») + anchor02 (Жінка та чоловік) unchanged — continuity with the slice baseline; results must not regress. **Freshness declaration required at pre-reg:** engine lane states whether 01–02 were used to tune any prompt/gate/measure code after slice-1. If yes → they are labeled `tuning-familiar` (still run, reported separately) and a fresh 13th anchor is added to restore an independent regression pair. |
| 03–06 | **T — teacher corpus** (4) | Real texts from the private teacher-lesson master (#4850 channel) — highest ecological validity. Constraints: ≥1 SHORT anchor (500–800 chars — stresses the 6-item minimum + shortfall honesty «складено N із M»); ≥1 text naturally bearing surface russianisms/calques or non-normative usage (beyond the planted fixtures); diverse lesson topics. Never leave the private repo. |
| 07–08 | **P — authentic public prose** (2) | Published B1-suitable Ukrainian prose. Domain spread EXPLICIT at pre-reg (two named domains not already covered; slice covered pop-science + sociology). ≥1 proper-name-heavy / current-events-ish text that invites external-knowledge items — stresses `not-verifiable-from-text` rejection. Licensing: internal measurement use, not redistribution; source recorded. |
| 09 | **A1 — numeral-dense adversarial** | Authentic text dense in the defect-#2 regression classes: dates, ranges, fractions, percentages, ordinals, ambiguous prepositions. Expected landmines pre-listed per phrase (e.g. «23 квітня» → date, no cardinal government; «півтора року» → pass). |
| 10 | **A2 — «grade the task, not the text»** | Authentic ABOVE-B1 text (C1-class prose). Contract §2 freeze: gates check TASK language, never reject the teacher's text for difficulty. Expected: bake succeeds, tasks are B1, no rejection-for-difficulty anywhere. |
| 11 | **A3 — imperfect teacher-paste** (UNCONDITIONAL) | Pasted prose with realistic flaws (typos/OCR noise, 1–2 known non-normative forms) — sourced from the corpus if a natural example exists, else constructed and labeled as constructed at pre-reg. Defect #7 evidence: anchor-verbatim errors may remain quoted but must NOT be badged as linguistically verified; baseline diagnostics present. |
| 12 | **A4 — structured/non-paragraph text** | Dialogue turns, list/Q&A/heading-fragmented authentic text — stresses evidence-span repair and «verbatim in anchor» logic, which clean narrative paragraphs never exercise. |

Slate balance: 2 regression + 4 ecological + 2 breadth + 4 adversarial = 12
(exceeds the mandated 8–10 floor deliberately — the added classes each target
a specific gate-gap a reviewer identified).

**Anchor sourcing step (execution step 0, ~1 session):** content-lane assist
(agy) proposes T/P candidates; my curation + pre-registration commit. B1
suitability of T/P anchors sanity-checked against `query_cefr_level`; A2
deliberately fails that check (that's its job).

## 3. Run protocol

- Engine config: locked route — `google-ais/gemma-4-31b-it`, toolless, $0,
  deterministic Python gates. Fingerprint per defect #4: anchor + level +
  pedagogy + phase + requested types + prompt/package/engine/model versions +
  data-bundle digests. Environment MUST match the slate-commit pins (§2).
- Per anchor: request the full shipped type set (≥3: true-false, cloze,
  match-up; plus any type that landed with steps 3–4) toward the 90-min max
  plan. Shortfall honesty recorded verbatim.
- If the lesson assembler is live: assemble the full `lu.lesson.v1` document
  (TTT phases, blocks, reserve) and score at BOTH item and lesson level.
- **Determinism probe (expanded):** one anchor from EACH main class — 01 (R),
  one T, 07 (P), one A — baked twice at identical fingerprint → byte-identical
  IR required; if the assembler is live, byte-identical full lesson JSON, not
  just IR (defect #4). Any diff = hard fail.
- One generation retry allowed per anchor on parse failure; retries recorded.
- Record per bake: wall-clock, cost, generation_error, retry count. P50/P95
  wall-clock feed the latency-honesty claim (async 20s–4min).
- Report header pins: engine SHA, package versions, data-bundle digests, date
  — asserted equal to the slate-commit pins.

## 4. Deterministic KPIs (machine-scored) — with HARD gates

HARD = any miss blocks the step-8 arming input; root-cause, fix, re-run the
affected anchors (re-run recorded, not silently folded).

1. **Evidence integrity (HARD):** 100% of shipped items have their evidence
   span located verbatim in the anchor by the repair pass. 0 hallucinated
   items shipped.
2. **Warn-blocks-accept e2e (HARD — defect #1; critical path for the
   2026-07-12 teacher presentation):** no item in `review_required` can reach
   accepted state without an explicit teacher action — proven by an API-level
   test in the run (attempt auto-accept → refused), not by code reading.
   Tri-state distribution (clean/review_required/failed) reported per anchor,
   including the warn-vs-ok mark distribution among SHIPPED items and spot
   audit of `note` field quality (raters, §5).
3. **No silent drops (HARD — defect #3):** every non-shipped item appears in
   `rejected`/flagged with machine-readable reason; report shows salvage
   counts. Assert: generated == shipped + flagged + rejected. Teacher-facing
   visibility of this is separately validated by raters via §5b report fields.
4. **Russianism gate e2e (HARD — defect #5; critical path for 2026-07-12):**
   ≥2 planted known-heritage-warning fixtures travel retrieval → gate → IR →
   report in the SAME run configuration as the real anchors, AND ≥1 planted
   case where the russianism/calque appears in **generated task language**
   (statement/definition/question text), not the anchor — must be blocked or
   warned. Plus: russianism warn-rate on the 12 real anchors reported
   (anchor T-russianism-bearing and A3 may trigger legitimately).
5. **Numeral moat regression (HARD — defect #2):** expanded corpus covering
   dates, ranges, fractions (incl. «X з половиною/чвертю/третиною»),
   percentages, ordinals, ambiguous prepositions, collectives, gender
   agreement — positive bank 100% pass, negative bank 100% reject. Corpus
   ships with steps 3–4; this run executes it as the bank in `measure()`.
   **5b. Numeral transfer (real-anchor):** count + pass/warn/fail rate of
   numeral-bearing items actually GENERATED from the 12 anchors, reported as
   its own KPI (synthetic bank recall alone does not prove transfer).
6. **MatchUp semantic verification (defect #6):** ≥2 varied planted decoy
   pairs (lexically plausible, semantically wrong), spread across anchors
   that emit match-up. All caught.
7. **Anchor-provenance honesty (defect #7):** on A3, anchor-verbatim
   non-normative forms are NOT badged as verified; baseline diagnostics
   present in IR AND surfaced in the rater-visible report (§5b).
8. **Fingerprint determinism (HARD — defect #4):** 4 anchors (one per class)
   × 2 repeat bakes byte-identical (§3).
9. **Scope enforcement (defect #8):** non-B1 level request refused at the API
   seam (one negative probe).
10. **Parseability:** all 12 anchors (8/8 core + 4/4 adversarial) produce
    clean JSON within the retry budget (retries recorded). Any anchor failing
    after retry ⇒ measurement incomplete: root-cause required before the leg
    can be called satisfied — the hardest anchor is not allowed to silently
    drop out.
11. **Shortfall honesty (KPI):** on any anchor that under-generates (esp. the
    SHORT T anchor), the deficit statement is present, numerically accurate
    («складено N із M»), and shown to raters.
12. **Latency/cost (recorded, explicitly NOT a hard gate — async by design):**
    P50/P95 wall-clock vs the ≤4 min honest-async budget; cost per bake
    (expected $0). Reported; arming judgment weighs it.

## 5. Acceptance scoring — the real KPI (human/agent-scored)

Instrument: `measure-report.html` per-item verdict + reason class. Framing for
every rater: **«Would a teacher run this item as-is in a paid 1-1 B1
lesson?»** (teacher-accept-as-is is the locked KPI).

Per-item verdict: `accept` | `edit-minor` | `edit-content` | `reject`, with
reason class required for non-accepts:

| class | meaning | severity |
|---|---|---|
| `reject-lang` | Ukrainian is wrong: non-word, agreement, russianism/calque, wrong government | **CRITICAL — hard-gate class** |
| `reject-fact` | claim not supported by the anchor | critical |
| `reject-pedagogy` | wrong level, type misuse, meaningless/trivial task | budgeted |
| `edit-content` | item substance needs change | budgeted |
| `edit-minor` | wording/instruction polish | budgeted |

Per-lesson verdict (only when assembly is live; else N/A in results):
`run-as-is` | `run-with-edits` | `would-not-run`, + phase-fit check (does each
block belong in its TTT phase?).

**Raters, blinding, calibration (hardened per review):**
- **Rater A** = main-claude (orchestrator seat). *Known residual bias:* A has
  engine/prompt context. Mitigations: calibration round + locked criteria
  (below), A's sheet committed (hash-stamped in the private repo) BEFORE B's
  sheet is opened, gold outranks (below), severity reporting.
- **Rater B** = independent pedagogy review, **cross-family, content lane**
  (agy/gemini-pro primary; cursor alternate — module-content panel members not
  involved in the engine build). BOTH raters score independently and blind to
  each other; neither sees the other's sheet before both are committed.
- **Calibration round (mandatory, before main scoring):** both raters
  independently score the slice-1 anchors (01–02 items) + ONE new anchor
  against the exact rubric; agreement reviewed, criteria ambiguities resolved,
  rubric text LOCKED. Calibration scores are reported but excluded from the
  main KPIs.
- **Rater B checklist (enumerated, not referenced):** conformance to
  `activity-pedagogy.md`; ≤1 puzzle-type per B1 lesson; mode defaults sensible
  (усно/письмово/вдома); instruction language at B1; #M-13 UA-immersion in all
  task content; answer_key present/correct for auto-checkable types; free
  types carry teacher guidance; external_options flagged where required.
- **Reliability reporting (pre-specified deliverable):** raw agreement % and
  Cohen's κ over {accept, edit, reject}; per-rater severity profile (accept
  rate by rater); disagreement log with rationales.
- **Adjudication:** items where A and B disagree go to a THIRD family
  (codex/gpt-5.5, one-shot). The adjudicator RECEIVES the item, both verdicts,
  AND the evidence spans/IR diagnostics for reject-fact and borderline
  pedagogy cases, and produces a one-sentence rationale per item. Adjudicated
  verdicts are final.
- **Shared-prior caveat (stated honestly):** two LLM raters — even
  cross-family — can converge on a prior that mismatches real 1-1 teacher
  tolerance. The measurement therefore reports rater severity and treats the
  gold slot as the calibration authority when available.
- **Gold-rater slot (opportunistic):** if the 2026-07-12 presentation yields
  Альона scoring even one lesson, her sheet is GOLD — reported separately,
  outranking both raters on the items she scored, and used to calibrate rater
  severity. Not a precondition.

Rater-B dispatch is a review lane, not execution: bridge one-shot with the
HTML artifact path; no commits.

## 5b. Report observability contract (rater-consumable, required)

`measure-report.html` + companion JSON MUST expose, for EVERY block and every
rejected/flagged item — raters must never need raw private IR to validate
grounding or honesty claims:

- tri-state gate outcome + per-gate check rows (gate, status, locator, detail)
- `mark` (ok/warn) + `note` text as the teacher would see it
- provenance at BOTH levels (block: anchor|generated|teacher + gates run;
  activity: generation history; `external_options` flag)
- evidence span(s): quote + located offsets, or the located-failure reason
- rejected items: machine-readable reason class + the content that was dropped
- salvage counts per activity (generated / shipped / flagged / rejected)
- anchor baseline diagnostics (A3: which anchor-verbatim forms are non-normative
  and how the report marks them as NOT verified)
- per-item accept/edit/reject controls (the rater instrument)

If any §4 HARD claim is not rater-visible through these fields, the report is
defective and the run does not count — teacher-facing honesty (defects #3, #7)
is measured through THIS surface, not through code inspection.

## 6. Thresholds → step-8 arming input

**Hard gates (any miss = measurement leg NOT satisfied):**
- All §4 HARD items green (evidence 100%, warn-blocks-accept, no silent
  drops, russianism e2e incl. generated-task case, numeral corpus 100/100,
  determinism 4×2, parseability per §4.10).
- **0 `reject-lang` items among shipped items, post-adjudication.** A language
  error shipped by a language-teaching tool is product-killing; every instance
  is root-caused (gate gap vs generation vs rater error), fixed, and the
  affected anchors re-run. The re-run is part of the record.

**Primary soft bar (post-adjudication, per review r1):**
- **accept OR edit-minor ≥ 80% of shipped items** — the teacher-experience
  bar: at most 1 in 5 items needs substantive work.
- **edit-content + reject-pedagogy + reject-fact ≤ 15% combined.**
- Pure accept-as-is tracked against **70% floor / 85% aspiration** (the locked
  KPI's own trajectory number, reported alongside — precedent was 100% on N=2
  non-blind, so these carry wide uncertainty; see §0 power statement).
- Per-lesson (ONLY if assembly live, else N/A): 0 `would-not-run`;
  ≥ 8/10 `run-as-is` or `run-with-edits`.
- Shortfall honesty KPI green on every under-generating anchor (§4.11).
- P95 wall-clock ≤ 4 min (reported; not hard — §4.12).

**Output of this leg:** `hramatka/step7-results.md` (private) with the KPI
table, both rater sheets + hashes, calibration record, adjudication log,
reliability metrics (agreement %, κ, severity), threshold verdict, and an
explicit recommendation line for the step-8 decision. The user arms teacher
access; the measurement only qualifies or disqualifies.

## 7. Deliverables & reporting boundary

Private (this repo): pre-registered slate + pins (`hramatka/anchors/
step7-slate.md`), `measure-report.{html,json}` per run, rater sheets +
calibration record, adjudication log, `step7-results.md`.

Public (#4542 progress comment): **aggregate counts/rates/verdicts ONLY. No
anchor text, no per-item examples, no IR/gate excerpts, no evidence spans, no
teacher identifiers, no prompts.** (Sol leak list; boundary contract.) If the
assembler was not live, the public comment says explicitly that the run was
item-level.

## 8. Execution estimate & seats

| step | seat | est |
|---|---|---|
| 0. slate curation + pre-registration commit (incl. pins) | agy assist + main-claude curation | ~1 session |
| 1. measure.py extensions (§1, §5b) | engine lane (codex thread, rides steps 3–4) | small |
| 2. calibration round + criteria lock | Rater A + B | ~1 hr |
| 3. bake run + determinism probes | engine, automated | <1 hr wall, ~$0 |
| 4. Rater A scoring (sheet committed before B opens) | main-claude inline | 1–2 hrs |
| 5. Rater B blind scoring | agy (cursor alternate) | 1 dispatch |
| 6. adjudication (with evidence for fact/pedagogy items) | codex one-shot | minutes |
| 7. results doc + reliability metrics + #4542 aggregate comment | main-claude | <1 hr |

## 9. Open items this plan does NOT decide

- Whether lesson assembly is in scope at run time — determined by the step-5
  mock→real swap state on fire day (§0, §3, §6 handle both honestly).
- The glossary gloss-language question and other contract-§7 open questions —
  teacher feedback 2026-07-12; measurement uses shipped types only.
- Numeral corpus contents — engine lane authors it under defect #2; this plan
  consumes it as the bank and holds it to 100/100 + the §4.5b transfer KPI.

## 10. Review record

- r1: grok-build (cross-family, ask 2351 → reply 2352, 2026-07-10):
  **GO-WITH-CHANGES**, 12 changes — ALL applied in v2: immutable slate incl.
  unconditional A3 (+1), structured-text + short-anchor + russianism-bearing +
  proper-name classes (+2), dual-blind + calibration + κ (+3), report
  observability contract §5b (+4), numeral transfer KPI (+5), ≥2 planted
  instances + generated-task russianism (+6), 80%/15% primary bar +
  lesson-N/A fallback + shortfall KPI (+7), 4-class determinism probe +
  lesson-JSON identity (+8), generated-task russianism in gate scope (+9),
  slate-commit version pins + tuning-void rule (+10), evidence-informed
  adjudication + rationale (+11), boundary restated incl. no per-item
  examples (+12). Transport note: r1 ask 2349 died in the bridge
  (rc=-15, stdin_bytes=0) — retried as 2351; bridge bug filed publicly.
