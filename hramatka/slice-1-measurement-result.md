# Slice-1 measurement — FIRST real-Gemma run on anchor #1 (2026-07-08, overnight)

## ✅ CONFIRMED 3/3 (2026-07-08, post fraction-fix re-run): true-false + cloze + match-up ALL ship.
Re-run with the fixed gate (cached generation, re-gated): true-false `passed=True` (numeral «два з половиною
рази» → `warn fraction-variable-government`, not fail), cloze pass, match-up pass. `lesson_b1` = all 3 types.
Builder: 123 tests green (+7 fraction tests), moat zero regression, fingerprint unchanged.

## Verdict: the vertical slice WORKS. Mechanism proven, quality high; the one narrow moat bug is FIXED.
Real end-to-end: anchor #1 ("10 причин читати!", real B1 prose) → grounding-IN → **real Gemma 4 (google-ais,
$0, toolless)** → 3 anchor-extractive activities → VESUM + numeral moat + evidence-span gates → validated
`lesson.b1.json`. `generation_error: None` — Gemma produced clean parseable JSON on the first real call.

## What Gemma produced (all anchor-derived, B1, UA-immersion-correct #M-13 — no English)
| Activity | Gate | Quality (my judgment) | Ships? |
|---|---|---|---|
| **Cloze** | PASS (evidence-span located) | good carrier from anchor, meaningful gap («підвищує»), 4 plausible same-POS distractors | ✅ yes |
| **MatchUp** | PASS (4/4 spans located) | 4 pairs, UA definitions (дозвілля↔вільний час; синапси↔зв'язки між нейронами; деталь↔подробиця; запобігати↔не дати статись) — immersion-correct | ✅ yes |
| **TrueFalse** | FAIL (1 numeral false-positive) | content EXCELLENT — 5 items, all anchor-derived, correct T/F labels, all evidence spans located; 2 false-stmt WARNs (by design → teacher-confirm) | ⛔ dropped (bug, not content) |

## The gates worked
- **evidence-span REPAIR** worked perfectly — every quote located, offsets recomputed (the model's offsets were ignored, as designed). 0 hallucinated items.
- **false-statement WARN** fired on the 2 false T/F items (teacher-confirm, correct).
- **numeral moat FIRED** on real output (it's live), but this instance was a FALSE POSITIVE (see below).

## The one real bug (only real output surfaces it — #M-4a payoff)
Gemma restated «в 2,5 рази» as «у **два з половиною рази**» (2.5×, CORRECT Ukrainian). Moat →
`no-noun-found on 'два з'`: the tokenizer took «два», saw «з» (prep) next, gave up before the noun «рази».
My oracle + the code review only used the DIGIT form «2,5 рази» → the spelled-out "X з половиною [noun]"
mixed-fraction construction was never tested. **Fix in flight** (builder): recognize «X з половиною/чвертю/
третиною [noun]», locate the noun past the fraction words, return WARN (fractional government is variable,
like decimals). After the fix the true-false should ship too (item[4] → warn, not fail).

## KPIs (this anchor)
- Generation: 1/1 anchors parseable ($0). Activity types: 3/3 requested produced.
- Gate-pass: **2/3 clean** (cloze, match-up); 1/3 (true-false) failed on 1 numeral false-positive → **expected 3/3 after the fix**.
- would-accept (my sample-judgment): **3/3 content teacher-quality** (the true-false "fail" is a gate bug, not a content problem). Pedagogy-lane validation still owed (content-lane authority) before real teachers.
- Numeral differentiator: the moat is ACTIVE on real output; Gemma got the anchor's numerals RIGHT
  («17 ділянок», «два з половиною рази») so there was no real wrong-government error to catch here — the
  REJECTION side is proven by the offline negative-fixture bank, not this anchor.

## 2-ANCHOR MEASURE (anchor01 + anchor02, real Gemma, 2026-07-08) — generalization CONFIRMED
- **KPI:** 6/6 activities ship (gate_pass_rate 1.0 after per-item salvage), 0 russianism warns, 1 numeral warn.
- **anchor02 (Жінка та чоловік — gender/sociology, totally different topic):** 3/3 ship, teacher-quality; T/F
  all 5 items pass; «на сім відсотків» (7→gen pl відсотків) correctly passes; «удвічі» (adverb) correctly
  skipped. → mechanism is NOT anchor01-specific.
- **Numeral differentiator PROVEN (offline bank):** positive 4/4 pass (17 ділянок, дві третини, п'ять столів,
  близько ста людей); negative 5/5 reject (п'ять студенти, два студентів, **дві столи** [gender], **двоє
  студенти** [collective], **півтора років** [half]) — the rejection side works across classes.
- **Per-item salvage WORKS on real output:** anchor01's regen produced (a) a DATE false-positive «23 квітня»
  (moat wrongly applied cardinal ends_2_4 → flagged+dropped, 4 T/F items shipped) and (b) a match-up pair
  whose evidence quote wasn't an exact anchor substring «іншими процесами деменції» (evidence_span fail →
  dropped, 3 pairs shipped). Both salvaged correctly — lesson still ships good items + flags the bad ones.
- **NEW BUG → FIX IN FLIGHT:** the «23 квітня» DATE false-positive (2nd special-construction class after
  fractions). Fix dispatched: detect `<number> <genitive-month>` → date (ordinal day + gen month, correct) →
  don't apply cardinal government. Verify via oracle post-fix.
- ⚠️ **Caching note:** the 2-anchor measure produced DIFFERENT anchor01 items than the single run at the SAME
  fingerprint — either .cache was cleared between runs (regen, expected) or a §R idempotency miss (builder checking).
- Report: `.agent/tmp/hramatka/engine/.out/measure-2anchor/measure-report.html`.

## Open refinements (noted, not blocking the slice proof)
1. **Per-item vs per-activity gate granularity:** one item's fail dropped the whole 5-item true-false. For a
   teacher tool, carry per-item verdicts (keep good items, flag the one) — gate_result.checks are already
   per-locator. Asked the builder for a recommendation; my call pending.
2. **Pedagogy validation** of the generated activities by the content lane before any real teacher (the
   plan's success bar). My assessment = teacher-quality, but pedagogy authority isn't mine.
3. Run the full `measure.measure()` for the HTML report + multi-anchor KPIs after the fraction fix.

## Bottom line for the user
The Hramatka B1 engine's first vertical slice is PROVEN end-to-end on a real anchor with real $0 Gemma:
correct, anchor-grounded, immersion-correct UA activities, gated by our deterministic VESUM+numeral moat.
The moat has been through solo-design → design-review(5 bugs) → build → my oracle(11/11) → code-review(7 bugs)
→ real-run(1 bug) — each caught real defects; nothing shipped on one agent's word.
