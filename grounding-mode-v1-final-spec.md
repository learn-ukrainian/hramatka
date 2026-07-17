# Hramatka grounding-mode v1 — FINAL build contract

> **Status:** LOCKED for dispatch (panel 2026-07-17: 3 seats, UNANIMOUS APPROVE-WITH-CHANGES).  
> **Supersedes:** prior Sol design sketches (addendum-2 / prompt-v2 architecture) and any “floors unchanged” wording that implied 60/90 floors already exist in code.  
> **Rejects (still):** adaptive-sizing · global reuse-relax · soft quoting `evidence_span` · ungrounded “derived” · pedagogy chrome before mode+gate split.  
> **Acceptance DNA:** full Alona packet signature (authentic text + tightly bound ВПРАВИ mix + productive constrained ДОМАШКА) — **not** DNA §2 homework checklist alone.

This document is the build contract. Slice PRs must cite it. Disputes vs the panel change-set are called out in §0; everything else is absorbed.

---

## 0. Disputes / non-silent notes (vs panel change-set)

| ID | Stance | Note |
|---|---|---|
| **E1 ↔ E7 tension** | **Flagged, resolved below** | E1: IR + raw validators + gate bifurcation in the **same PR**. E7: validator dual-path in step (1), gate bifurcation in step (3), kit between. **Resolution:** keep E7 commit order for substrate safety; treat “same PR” as **same release train for production-derived** — `grounding_mode_v1` publish path stays OFF until slice-3 gates land. Slice 1 may land dual-path validators under the flag, but derived items must not publish/select as survivors until slice 3. Do **not** ship prompt-v2 (slice 5) against quoting-only validators. |
| **Floor numbers 60/90** | **Absorb + justify** | Live `LESSON_FLOORS` only defines `45 → 6`; `meets_lesson_floor` returns **True** for 60/90 today (no floor). Prior sketch’s “floors unchanged: 60→9 / 90→12” was aspirational, not code. **ADD** those entries (§5). Numbers sit **at or below** `sizing_policy` plans (45: floor 6 vs plan 8; 60: 9 vs 10; 90: 12 vs 12) so floors are survivability mins, not plan targets. |
| **match-up mode** | **Absorb (table change)** | Prior sketch listed `match-up` as derived; panel (and Sol match-up caution) wins: **quoting in v1**, derived synonym rewrite later, **last** regardless. |
| All other P*/G*/E* | **Absorb** | No further disputes. |

---

## 1. Verdict (architecture stands)

Short anchors starve because the engine over-applies **quoting** grounding (verbatim `evidence` restore + lesson-wide sentence-reuse ≤2) to types that are pedagogically **derived**.

**Fix:** mandatory per-type `grounding_mode` ∈ {`quoting`, `derived`}.

- Floors stay (and 60/90 floors are **added** — see §5).
- Lessons stay full-length (do not shrink plans for short text).
- Teachers’ short texts become usable because **derived** types stop consuming the sentence inventory.
- Addendum-1 frames (**adaptive-sizing / reuse-relax**) remain **superseded — do not build**.

---

## 2. Root cause (locked)

| Engine assumption today | Real lesson DNA (Alona + ULP) |
|---|---|
| One extractive contract for all types (`evidence` = verbatim restore) | Only comprehension **quotes**; drills **derive** |
| `error-correction` / `fill-in` / `short-writing` must restore literal evidence | Teachers compose new stems from vocab/grammar |
| `sentence_reuse_allowed` ≤2 lesson-wide | Quoting is scarce; derived should not burn quote inventory |
| Prompt says «похідні» then orders дослівне відновлення | Self-contradiction; gates enforce the lie |

Live proof points (engine seat):

- `_validate_fill_in` / `_validate_error_correction` / `_validate_match_up` / `_validate_short_writing` hardcode non-empty `evidence` quote strings (`registry.py`).
- `_gate_impl_digest` hashes gate modules + `retrieval` / `generate` / `pipeline` / `registry` / `selector` / `prompt_pack` — **not** `content_density.py` (reuse-only edits there can miss bake-id).
- `LESSON_FLOORS` = `{45: min_blocks=6, …}` only; 60/90 unconstrained.
- Sizing plans (`sizing_policy.B1`): 45→8, 60→10, 90→12.

---

## 3. Clarifications (upheld by panel)

1. **DNA-full-signature.** Packet DNA §2 is ДОМАШКА only. Acceptance shape = authentic text + productive constrained homework + tightly bound ВПРАВИ (quoting comprehension + derived drills). Do not shrink the gate to homework alone.
2. **`cloze` = quoting in v1.** Authentic comprehension gap in a reading span. Controlled **new-sentence** gaps are `fill-in` (**derived**). Dual-mode cloze later if needed — not this cycle.
3. **Derived ≠ free generation.** Kit lemma closure + VESUM + decidable entity/numeral bounds. Softening quoting `evidence_span` remains forbidden.
4. **Error-correction expansion is gated.** Decidable `rule_id`s only (numeral MOAT first; agreement/case only with a Python oracle). No “any grammar typo the model invents.”
5. **match-up caution.** Heaviest reclass (synonym-semantics). Keep **quoting** in v1; derived later; sequence last.

### Pedagogy clarifications (P2 / P3) — binding wording

- **Cloze (quoting):** authored explanatory / instructional text around the gap may use **kit vocabulary**. Only the **gap span** (the withheld/restored token region and its `evidence` quote that locates it in the reading) needs quote provenance. Do **not** read “quoting” as whole-item quote-sourcing of every instructional string.
- **Fill-in (derived):** stems are **kit-derived sentence stems**, never quote restoration. A fill-in that merely blanks a verbatim anchor sentence is a **spec violation**, not a clever quoting hybrid.

---

## 4. Registry property + mode tables

### 4.1 Mandatory registry field

```text
grounding_mode: "quoting" | "derived"   # required on every ActivityRegistryEntry
```

- Included in `ActivityRegistryEntry.fingerprint()` and therefore in bake fingerprint / `gate_chain` identity.
- Active flag vector (§7) is also part of `fingerprint_inputs`.

### 4.2 Quoting types (v1)

| Type | Contract |
|---|---|
| `true-false` | `evidence_span` exact/whitespace-tolerant substring of reading; false = sense-distortion |
| `quiz` | same evidence hardness |
| `text-questions` | same |
| `mark-the-words` | evidence ≡ activity text span from reading |
| `cloze` | gap span quote-provenanced; surrounding authored text may use kit vocab (P2) |
| `match-up` | **quoting in v1** (E2) — pair sides bound to quote evidence; synonym-semantics derived rewrite is **out of v1** |

Sentence-reuse caps + distinct-sentence-per-item apply to **quoting primary sentence IDs only**.

### 4.3 Derived types (v1)

| Type | Contract |
|---|---|
| `fill-in` | kit-derived stems; **never** quote restoration (P3) |
| `error-correction` | novel error stem under `RULE_REGISTRY` `rule_id`; numeral MOAT first |
| `short-writing` | kit-bound prompt + machine-checkable `constraints[]` (DNA §2 shape) |
| `sentence-builder` | **ADD** (P1) — maps to Alona «Зробіть речення»; kit-derived sentence construction from starters / focus forms |

`sentence-builder` may land as registry+mode+schema ahead of full prompt/projector polish, but its mode is **derived** from day one (no quoting half-measure).

### 4.4 Explicitly deferred

- Derived `match-up` (synonym / paraphrase pairing) — after v1 A/B, last among derived expansions.
- Dual-mode `cloze`.
- Adaptive floors / global reuse-relax.

---

## 5. Floors, sizing, thin-source, reuse

### 5.1 Lesson floors (selection survivability) — MUST ADD

Live today: only `45`. Panel E6 is correct: “floors unchanged” currently means **no floor** for 60/90.

| Duration | `min_blocks` | `phase_minimums` | `min_types` | `require_productive` | Plan (`sizing_policy`) |
|---|---|---|---|---|---|
| 45 | **6** (existing) | `{1:2, 2:3, 3:1}` (existing) | 4 | True | 8 (`3/4/1`) |
| 60 | **9** (**ADD**) | `{1:2, 2:5, 3:2}` (**ADD**) | 4 | True | 10 (`3/5/2`) |
| 90 | **12** (**ADD**) | `{1:4, 2:5, 3:3}` (**ADD**) | 4 | True | 12 (`4/5/3`) |

**Freeze the floor oracle for A/B** (E6): pin these entries in env/harness record; do not retune mid-experiment.

Justification: floors are hard mins below (or equal to) plan targets — same relationship as today’s 45 (6 vs 8). Changing 60→10 or 90→10 would need a separate justified ADR; default lock is **9 / 12**.

### 5.2 Thin-source recalibration

`source_lacks_lesson_evidence` (or successor): blame only when **quoting quota** cannot cover required quoting slots **and** derived kit is empty — not merely “few sentences.”

### 5.3 Reuse / density / selector (E5)

Under `grounding_mode_v1`:

- Quoting: keep sentence-reuse + distinct sentence-id rules on quoting primaries.
- Derived: **do not** use `meets_content_density` / `itemized_sentence_ids_are_distinct` as-is — replace with **lemma / novelty floors** (core-lemma coverage, duplicate-stem ban, kit-closure).
- Bump `SELECTOR_POLICY_VERSION` when those predicates change.

### 5.4 Per-batch diversity gate (G5)

For each derived batch of size N:

```text
distinct_core_lemma_clusters >= ceil(N / 3)
```

Fail-closed. Blocks the one-paradigm-N-items dodge.

---

## 6. Gate bifurcation (harden, don’t soften)

### 6.1 Quoting (unchanged hardness)

- `evidence_span` fail-closed; no silent inference in MVP.
- Sentence-reuse + distinct-sentence-per-item on quoting primary IDs only.

### 6.2 Derived fail-closed contract

1. **Kit lemma closure** — tokens ⊆ (anchor lemmas ∪ FF synonyms / aspect-pairs / focus forms). **Empty kit ⇒ fail closed** (E4); never soften to open generation.
2. **VESUM** on novel tokens (`error` may be non-VESUM only under a proved malformation for the active `rule_id`).
3. **Numeral MOAT** — model proposes `[value, case, gender, trigger, noun_lemma]` → Python decides.
4. **`error_correction_vesum`** on correction + valid options.
5. **Decidable “no new facts” subset (G1):** **no new named entities or numerals outside kit**. Drop semantic-facts claims from gate-contract language (not decidable at this layer).
6. **Russianism / style** = WARN, not block — **only safe if** `teacher_review_tray_v1` is operational (G4).
7. **`short-writing`:** machine-checkable `constraints[]`.
8. **Per-batch diversity** (§5.4).

### 6.3 RULE_REGISTRY precondition (G2)

Concrete enumerated registry **before** derived error-correction is allowed to publish:

```text
rule_id → verifier_function
```

- Unit-tested pairs.
- **Numeral MOAT = first entry.**
- No registry entry for the claimed `rule_id` → item **rejected fail-closed**.

### 6.4 Provenance / IR (G3, E1)

| Mode | Provenance field | Semantics |
|---|---|---|
| quoting | `evidence` / `evidence_span` | literal quote from reading |
| derived | `kit_anchors` | `{ lemmas: [...], witness_span: <REQUIRED>, rule_id?: ... }` |

- **`witness_span` is REQUIRED** on every derived `kit_anchors` (G3) — provenance chain for every derived item (anchor substring that justified the lemma/rule choice; **not** a fake “restored sentence”).
- Provenance tag: derived `generated`, quoting `anchor`.
- Never emit a fake verbatim quote to satisfy old validators.

### 6.5 Teacher review tray (G4)

`teacher_review_tray_v1` = **HARD ordering constraint** before any derived-mode content **publishes** to teachers.

- WARN-not-block (russianism/style) is only admissible with an operational tray.
- Slice acceptance for publish paths must check tray readiness; otherwise derived publish stays blocked even if gates are green.

---

## 7. Flags, composition semantics, fingerprint (E4)

Three flags — never one mega-flag:

| Flag | Owns |
|---|---|
| `kit_enrichment_v1` | FF banks (substrate) |
| `writer_prompt_v2` | mode-split authoring + pedagogy elements |
| `grounding_mode_v1` | registry mode + gate bifurcation + quoting-only reuse + derived selector predicates |

**Composition semantics (not just metrics credit):**

| Condition | Behavior |
|---|---|
| `writer_prompt_v2` ON **and** `grounding_mode_v1` ON | mode-split prompt text |
| `writer_prompt_v2` ON **and** `grounding_mode_v1` OFF | **force extractive-v5 (or current extractive) copy** — no mode-split text |
| Derived gates | active **only** under `grounding_mode_v1` |
| Derived lemma-closure with empty kit | **fail closed**, never softens |
| Yield credit for short-anchor | only when `grounding_mode_v1` is on |

**Fingerprint:** active flag vector ∈ `fingerprint_inputs` (alongside existing digests).

**Bake-id coverage (E3):** add `content_density.py` **and** any new derived-gate module(s) to `_gate_impl_digest` file list so reuse/density/derived-gate edits reshuffle bake identity.

---

## 8. Feed-forward kit = derived substrate

Toolless writer cannot invent gate-survivable derived stems without a pre-verified kit:

- synonym banks (disjoint)
- CEFR tags
- aspect + stress
- focus citation-forms
- numeral tuples

Kit FF is **enabling infrastructure for derived mode**, not polish. **Kit before fail-closed derived gates bite** (E7).

---

## 9. Prompt-v2 must-have lines (slice 5; only meaningful under `grounding_mode_v1`)

- Header: two modes; type list carries mode.
- Quoting: inventory-only evidence; T/F = sense distortions; cloze gap-span provenance (P2).
- Derived: NEW sentences from KIT only; fill-in ≠ quote restore (P3); correction-first for error-correction; vocabulary-trap when focus supported; `sentence-builder` starters when type requested (P1).
- Domashka: emit checkable `constraints[]`.
- Anti-meta / persona / cultural-upgrade remain secondary to mode correctness.

---

## 10. A/B protocol (E6) — exploratory, not confirmatory

| Item | Lock |
|---|---|
| Design | Exploratory paired comparison |
| Stats | McNemar; stratified short / med / long anchors |
| n | 18 is **exploratory**, not confirmatory power |
| Repair | **OFF** pinned in env + harness record for **primary** |
| Repair-ON | secondary **shadow** only |
| Floor oracle | **FREEZE** (§5.1 entries) for the run |
| Treatment of interest | `grounding_mode_v1` (with kit substrate live) |

Do not claim confirmatory significance from n=18.

---

## 11. Build sequence + per-slice acceptance (E7)

Commit / dispatch order. Kit before gates. Prompt last before measure.

### Slice 1 — Registry mode + fingerprint/flag-vector + IR/`kit_anchors` + validator dual-path

**Ship:**

- `grounding_mode` on every `ActivityRegistryEntry` (§4 tables, including `match-up=quoting`, `sentence-builder=derived` if registered).
- Flag vector plumbed into `fingerprint_inputs`.
- IR schema: derived items carry `kit_anchors` with **required** `witness_span`.
- Raw validators dual-path: quoting types still require evidence quotes; derived types accept `kit_anchors` (not fake evidence restore) for `fill-in` / `error-correction` / `short-writing` / `sentence-builder`.
- `_gate_impl_digest` includes `content_density.py` (and placeholder path for upcoming derived-gate module if created empty/stub).

**Do not yet:** fail-closed derived semantic gates; prompt-v2 mode-split; publish derived to teachers.

**Acceptance:**

- [ ] Registry fingerprint changes when `grounding_mode` flips.
- [ ] Flag vector present in fingerprint blob; toggling a flag changes bake id.
- [ ] Unit tests: quoting validators still reject missing evidence; derived validators reject missing `kit_anchors.witness_span` and reject evidence-only “quote restore” fill-in shapes when mode=derived.
- [ ] `match-up` remains evidence-quote validated (quoting).
- [ ] No production publish path treats derived survivors as lesson-ready.

### Slice 2 — Kit feed-forward (`kit_enrichment_v1`)

**Ship:**

- Pre-verified kit banks wired into generation context.
- Empty-kit detection API used by later gates.

**Acceptance:**

- [ ] Kit enrichment behind `kit_enrichment_v1`.
- [ ] Deterministic fixture: known anchor → non-empty lemma/synonym/numeral kit (or explicit empty).
- [ ] Empty kit is distinguishable (no silent “soft full lexicon”).

### Slice 3 — Gate bifurcation + RULE_REGISTRY + diversity

**Ship:**

- Derived gate modules active only under `grounding_mode_v1`.
- Quoting gates unchanged hardness; reuse scoped to quoting primaries.
- `RULE_REGISTRY` with numeral MOAT as first unit-tested entry; unknown `rule_id` → reject.
- G1 decidable entity/numeral bound (not semantic-facts language).
- G5 `ceil(N/3)` cluster diversity.
- Derived gate module(s) in `_gate_impl_digest`.
- `teacher_review_tray_v1` hard gate on **publish** (or publish blocked with explicit dependency error).

**Acceptance:**

- [ ] Derived item without kit closure fails closed.
- [ ] Derived error-correction without registry `rule_id` fails closed.
- [ ] Numeral MOAT oracle decides; LLM judgment not authoritative.
- [ ] Diversity gate rejects mono-cluster batches.
- [ ] Quoting `evidence_span` failures still fail closed (no regression).
- [ ] Publish path refuses derived content if tray not operational.
- [ ] E1 satisfied for production: validators + gates agree on derived IR (no prompt-only derived).

### Slice 4 — Selector + density + thin-source recalibration

**Ship:**

- Selector co-changes: lemma/novelty floors for derived; quoting keeps sentence-id rules.
- Bump `SELECTOR_POLICY_VERSION`.
- Thin-source blame only when quoting quota unmet **and** kit empty.
- `LESSON_FLOORS` ADD 60→9 and 90→12 (+ phase mins in §5.1).

**Acceptance:**

- [ ] Derived candidates are not rejected solely for shared sentence ids / old density quote metrics.
- [ ] Quoting reuse caps still bind quoting primaries.
- [ ] `meets_lesson_floor(..., duration=60|90)` can return False on underfilled lessons.
- [ ] Selector policy version bumped; fingerprint reflects it.
- [ ] Floor oracle frozen values documented in harness for upcoming A/B.

### Slice 5 — Prompt-v2 (`writer_prompt_v2`)

**Ship:**

- Mode-split prompt text **only** when `grounding_mode_v1` on; else force extractive-v5/current extractive copy (E4).
- P2/P3 wording in prompt; `sentence-builder` instructions when type requested.

**Acceptance:**

- [ ] Flag matrix tests: prompt-v2 ∧ ¬grounding_mode_v1 → extractive copy (no derived authoring instructions).
- [ ] Prompt-v2 ∧ grounding_mode_v1 → mode tables present; fill-in “never quote restore”; cloze gap-span provenance.
- [ ] Prompt digest changes bake id.

### Slice 6 — A/B (exploratory)

**Ship:**

- Paired 18-anchor run; McNemar; short/med/long strata.
- Repair OFF primary; repair-ON shadow.
- Floor oracle frozen; flags recorded in harness.

**Acceptance:**

- [ ] Harness record shows repair OFF, floor freeze, flag vector, bake ids.
- [ ] Report labeled **exploratory** (not confirmatory).
- [ ] Primary metric attribution requires `grounding_mode_v1` (+ kit live).

---

## 12. Explicit rejects (do not build)

- Adaptive lesson floors / shrinking plans for short anchors  
- Global sentence-reuse relax  
- Soft quoting `evidence_span` / silent inference  
- “Derived” without kit closure  
- Semantic “no new facts” as an undecidable gate claim  
- Shipping pedagogy chrome / prompt-v2 before mode+gate split is production-ready  
- Confirmatory claims from n=18  
- Derived `match-up` in v1  

---

## 13. Panel change-set traceability

| Panel ID | Where folded |
|---|---|
| P1 | §4.3 `sentence-builder` |
| P2 | §3 cloze provenance; §9 |
| P3 | §3 fill-in; §4.3; §9 |
| G1 | §6.2 item 5 |
| G2 | §6.3 |
| G3 | §6.4 `witness_span` REQUIRED |
| G4 | §6.5; slice 3 acceptance |
| G5 | §5.4; slice 3 |
| E1 | §0 resolution; slices 1+3 |
| E2 | §4.2 `match-up` quoting; §4.4 deferred |
| E3 | §7 digest list; slice 1/3 |
| E4 | §7 composition + fingerprint |
| E5 | §5.3; slice 4 |
| E6 | §5.1 floors ADD; §10 A/B |
| E7 | §11 sequence |

---

**Next action:** dispatch Slice 1 against this contract. Tray / publish dependency (G4) should be scheduled so it does not become a silent blocker at slice-3 closeout.
