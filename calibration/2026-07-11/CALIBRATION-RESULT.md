# Step-7 calibration — reliability & rubric LOCK (2026-07-11)

Plan §5 deliverable. Calibration is excluded from main KPIs.

## Raters
- **Rater A** — main-claude (orchestrator seat). Sheet `rater-A-scores.{json,md}` (hash-stamped, committed before B saw anything).
- **Rater B** — agy (Gemini family, cross-family), blind, from the same shipped items + anchors + rubric.
- **Adjudicator** — codex `gpt-5.6` (third family), on the 3 disagreements only.

## Reliability
- n = 18 items (6 cells × 3).
- Exact-verdict agreement (A vs B): **15/18 (83%)**.
- Collapsed {accept, edit, reject} agreement: **15/18 (83%)**.
- Cohen's κ (collapsed): **0.557** (moderate).
- **Rejects: 0 · reject-lang: 0 · reject-fact: 0** (both raters) → **honesty architecture validated**: shipped content is language- and fact-clean; every issue is pedagogical polish.

## Disagreements → adjudicated (FINAL)
| item | A | B | adjudicated final | reason |
|---|---|---|---|---|
| anchor01-deepseek cloze | accept | edit-content | **edit-content** | ambiguous-answer: «занять» also fits «завдань» |
| anchor02-deepseek matchup (годувати→харчувати) | accept | edit-content | **edit-content** | sense-mismatch: годувати грудьми = breastfeed; харчувати loses the sense |
| var-deepseek TF s4 | edit-minor | accept | **edit-minor** | precision-ambiguity: anchor says «дві-три хвилини» |

Both raters' misses were caught: A missed the two distractor/sense edits; B missed the TF precision edit.

## Adjudicated final tally
accept 12 · edit-minor 1 · edit-content 5 · reject 0 → accept-or-minor 13/18 · substantive 5/18 · reject 0%.

## Rubric LOCK (clarifications adopted from calibration)
1. **Scoring basis:** score the SHIPPED (assembled) activity; gate-held sub-items (`rejected[]`/review_required) are the honesty surface, not a shipped-item defect.
2. **Cloze:** if ANY distractor plausibly fits the gap → **edit-content** (`ambiguous-answer`). A cloze must have exactly one defensible answer.
3. **Match-up:** pairs must hold in the anchor's SENSE, not merely as dictionary synonyms → `sense-mismatch` = **edit-content**.
4. **TF/numeric:** a claim defensible-but-imprecise vs the anchor's stated range → **edit-minor** (`precision-ambiguity`).
5. Reason classes extended: `ambiguous-answer`, `sense-mismatch`, `precision-ambiguity`.

## Carry-forward
The engine richness expansion (3→12 activity types) will void the current pinned step-7 measurement. This rubric is **engine-agnostic** and carries forward to rate the richer engine. Gold teacher signal (2026-07-11): a real teacher blind-preferred the hand-authored reference on the Львів lesson for richness/variety — recorded as the density+variety driver, not a language/fact defect.
