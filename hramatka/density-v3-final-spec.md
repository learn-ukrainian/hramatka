<!-- LOCKED BUILD CONTRACT 2026-07-28. Author: Sol (gpt-5.6-sol, xhigh), bridge msg 5596. Panel: APPROVE-WITH-CHANGES ×2 (agy msg 5560, grok msg 5563); all 7 consolidated changes absorbed/resolved in §0. Locked by the hramatka driver (claude fable 5). Tracking: issue #305. Amendment #442 approved on re-review 2026-08-12 by Gemini 3.1 Pro (High): teacher-runnable generic density, contextual morphology cloze, controlled cross-operation source overlays, semantic source-comprehension, and preservation of composed degree kits. -->

# FINAL BUILD CONTRACT — TeacherReadyDensity.v3 + gemma-phase-pack.v3.15

Amended 2026-07-28: release-gate matrix 12→18 cells (Gemini Flash 3.6 rotation + Vertex routes, operator order).

Core invariant: no list activity reaches a model unless deterministic preflight has already certified its complete type-specific floor under the lesson-wide capacity budget. Density failures never reject the teacher’s text. Generic lessons use teacher-runnable blocks; composed degree kits retain their complete eight-unit progressions.

## §0. Panel change disposition

| # | Disposition | Resolution |
|---|---|---|
| 1 | **ABSORB** | Each type has a deterministic unit builder returning either a certified plan meeting its floor or `unavailable`. Legacy prompts, soft preflight, and tray credit cannot schedule types. |
| 2 | **ABSORB** | A plan below its registered type floor at preflight causes substitution at t=0 with zero model calls. The initial call plus bounded repairs may only serialize the same immutable certified plan. An exhausted text-question slot gets one clean same-plan regeneration before any allocator-certified substitution; it never enters an unbounded item-repair loop. |
| 3 | **ABSORB** | A lesson-wide exact-cover allocator consumes shared evidence/kit resources across all slots. It also certifies conditional replacement plans. On failure, deterministically widen to adjacent lesson paragraphs; if still impossible, emit recoverable `insufficient_anchor_capacity` before generation. |
| 4 | **ABSORB** | False true-false items require a versioned closed mutation rule with deterministic constructor, verifier, and literal evidence binding. Unknown or free-form mutations fail closed. |
| 5 | **ABSORB** | Short-writing constraints come only from a closed deterministic registry, including `contains_lemma_set`, `min_verb_count`, `target_case_usage`, and `word_count_range`, implemented through regex/VESUM. LLM judgment is prohibited. |
| 6 | **ABSORB** | Mark-the-words preflight records token IDs/offsets and surfaces in `certified_target_tokens`. Grading compares exact closed lists and never re-runs an abstract criterion over generated text. |
| 7 | **RESOLVE** | Production is a hard v2→v3 cutover with no runtime coexistence. Amendment #442 makes a five-question source-comprehension block mandatory for generic 45-minute lessons: three literal questions use undrilled propositions; two open interpretive/application questions may overlay a drill carrier, and one must request text-supported interpretation. Quiz, cloze, and fill-in never share a source proposition. Error-correction alone may reuse one under its separately certified identify-and-repair operation. |

## §1. Per-activity contract

| Type | V3 floor |
|---|---|
| `true-false` | ≥5 distinct statements |
| `quiz` | ≥5 distinct questions |
| `cloze` | ≥5 distinct gap positions |
| `match-up` | ≥6 unique Atlas-pass pairs |
| `fill-in` | ≥5 distinct sentence items |
| `error-correction` | ≥5 distinct sentences, exactly one certified error each |
| `text-questions` | ≥5 questions with ≥3 fresh source-comprehension/fact-recovery items; carrier reuse is allowed only for anchored application |
| `mark-the-words` | ≥5 unique pre-certified target tokens in a verbatim multi-sentence span |
| `short-writing` | One productive task with its configured word minimum and ≥2 registered deterministic constraints; at most one exemption per lesson |

These are minimums, never maxima. Normalized duplicate stems, gaps, targets, pairs, or paraphrase padding do not count.

Every ready or tray block independently meets its floor. Lesson aggregates are secondary assertions:

- 45 minutes: ≥27 requested units; ≥26 after the certified match-up fallback
- 60 minutes: ≥48 units
- 90 minutes: ≥57 units

Those bounds assume one writing exemption and the current 6/10/12-block schedules. Specialized composed kits may exceed them.

## §2. Deterministic preflight algorithm

1. Canonicalize the anchor into stable sentence, token, Atlas-pair, form, mutation, and kit-stem inventories.
2. Run each requested type’s pure builder. Every certified unit records its ID, resource claims, evidence/kit anchor, allowed forms, expected key/rule, and citation plan.
3. Reject incomplete builders: list plans below their registered type floor become `unavailable`; writing plans lacking deterministic constraints do likewise.
4. Run a deterministic exact-cover allocation over the complete lesson. Source evidence obeys the existing ≤2 reuse rule and must differ by phase and operation; stems, target tokens, gaps, and pairs remain distinctly accounted.
5. Pre-certify conditional replacement plans against the rest of the chosen lesson allocation.
6. If no complete allocation exists, widen the anchor symmetrically by adjacent lesson paragraphs in stable document order and retry up to the configured lesson boundary.
7. If still unsatisfied, emit `insufficient_anchor_capacity` and return a recoverable draft/retry state before any model call.

Generic cloze is contextual morphology, not cross-lemma vocabulary guessing:
each row offers certified forms of the same lemma, and a closed visible
dependency must exclude every wrong form. Its five gaps span at least four
source sentences, retain three visible tokens between gaps and two at each
edge, and never use semantic absurdity as a distractor. Generic match-up uses
six B1+/unknown source lemmas with complete Ukrainian Atlas definitions; A1/A2
or malformed/truncated gloss rows do not count.

## §3. Generation, repair, and grading

The model may serialize only the scheduled unit IDs; extra, missing, duplicated, or altered substrate is a serialization failure.

The two repair rounds receive the same immutable plan. They cannot change type, invent units, relax floors, or request new evidence. After exhaustion, use a pre-certified replacement; otherwise drop the slot and return a recoverable draft.

Every ready and tray block is graded independently after always-on deterministic gates. Any below-floor output becomes hidden `density_shortfall`, never ready and never tray credit. Receipts remain content-free:

```text
{phase, type, disposition, units, floor_met}
```

Prompt-pack requirements remain:

- `PromptPackInput.v3.4`
- template `gemma-phase-pack.v3.15`
- new type-kit identity
- full-density exemplars only for requested types plus a four-item below-floor negative exemplar
- exact-count validation before raw-contract validation and after gates
- `(True)/(False)` banned from all learner-facing fields
- certified `…, ніж …` and `зі вікон`-class benchmark surfaces
- unchanged VESUM and always-on gates

## §4. Qualification and cutover

Qualification aggregates pin both the pack-input version and exact v3.15 template digest; density and kit identities remain fingerprint inputs.

Old v2 receipts are invalidated. Run the full 18-cell matrix on one new source pin, transcribe the registry only from passing current aggregates, and keep the UI fail-closed until then. This is straight re-qualification, not an A/B authorization decision.

There is no production v2/v3 selector: pre-cutover v3 code remains unreachable; the cutover removes v2 authority and exposes v3 atomically.

## §5. Explicit rejects

- Adaptive floors by anchor, model, or failure rate.
- Aggregate-only density that lets sparse blocks borrow units.
- Below-floor tray laundering.
- Duplicate or paraphrase padding.
- Eight artificial requirements or eight separate prompts for short-writing.
- Forcing every anchor to support all nine types.
- Treating v2 as a production qualification control rather than stale evidence.
- Any legacy-prompt, soft-preflight, free-form mutation, LLM-judged constraint, or post-hoc capacity bypass.

## Build-slice order

1. **Authority and contracts:** central floor table, formal `unit_plan`, receipts, distinctness normalization, and revised 45-minute phase-shape rule.
2. **Per-type certification:** deterministic builders, closed true-false catalog, short-writing validator registry, certified MTW tokens, and regression fixtures.
3. **Lesson-wide capacity:** shared inventory allocator, reuse accounting, conditional replacements, deterministic window widening, and zero-token failure event.
4. **Prompt pack v3.2:** new kit identity, full-density exemplars, immutable-plan serialization, inclusion/count gates, and benchmark fixes.
5. **Evaluator and repair:** per-block ready/tray grading, `density_shortfall`, same-plan two-round repair, slot-specific errors, and recoverable draft behavior.
6. **Qualification surfaces:** harness totals, receipts/digests, telemetry, benchmark rendering, fixtures, UI fail-closed behavior, and removal of every bypass.
7. **Release gate:** qualify the pinned candidate across all 18 cells, transcribe passing receipts, then atomically remove v2 authority and enable v3.
