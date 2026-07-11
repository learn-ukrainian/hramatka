# Step-7 calibration — Rater A sheet (blind, hash-stamped)

**Rater A** — main-claude (orchestrator seat) · **Date** 2026-07-11
**Rubric** — `hramatka/step7-measurement-plan.md` §5 · **Framing** — *"Would a teacher run this item as-is in a paid 1-1 B1 lesson?"*
**Pins** — engine `4d194308` · data `2026-07-10-ed779c7eabc6`

> **Hash-stamp (pre-registration proof):** the machine-readable verdicts in
> `rater-A-scores.json` have **sha256 = `50e16a19119b33b11fb15f78779bb2905a7dd318a8d8c4fdeac90429011ccf5f`**.
> This sheet is committed BEFORE rater B is dispatched or sees anything (plan §5 blinding).
> Calibration is excluded from the main KPIs.

## Method
Scored the **assembled (shipped)** activity in each of the 6 cells (anchor01, anchor02, вареники × gemma-ais + deepseek-v4-pro). Sub-items held back by gates (`rejected[]`, `review_required`) are the **honesty surface**, not counted as shipped-item defects. TF/fact claims cross-checked against the anchor texts; shipped Ukrainian spot-verified against VESUM; `matchup_semantics` warns treated as known-empty-Atlas false positives (#4949).

## Verdicts (18 items)

| cell | item | type | verdict | class |
|---|---|---|---|---|
| anchor01 · gemma | 0 | true-false | accept | — |
| anchor01 · gemma | 1 | cloze | accept | — |
| anchor01 · gemma | 2 | match-up | accept | — |
| anchor01 · deepseek | 0 | true-false | accept | — |
| anchor01 · deepseek | 1 | cloze | accept | — |
| anchor01 · deepseek | 2 | match-up | accept | — |
| anchor02 · gemma | 0 | true-false | accept | — |
| anchor02 · gemma | 1 | cloze | accept | — |
| anchor02 · gemma | 2 | match-up | **edit-content** | edit-content |
| anchor02 · deepseek | 0 | true-false | accept | — |
| anchor02 · deepseek | 1 | cloze | **edit-content** | edit-content |
| anchor02 · deepseek | 2 | match-up | accept | — |
| вареники · gemma | 0 | true-false | accept | — |
| вареники · gemma | 1 | cloze | accept | — |
| вареники · gemma | 2 | match-up | accept | — |
| вареники · deepseek | 0 | true-false | **edit-minor** | edit-minor |
| вареники · deepseek | 1 | cloze | accept | — |
| вареники · deepseek | 2 | match-up | **edit-content** | edit-content |

**Tally:** accept 14 · edit-minor 1 · edit-content 3 · reject 0 → accept-or-minor 15/18 (83.3%), substantive 3/18 (16.7%), reject 0%.
Per-generator (calibration only, **not** a KPI): gemma 8 accept + 1 edit-content (8/9 accept-or-minor); deepseek 6 accept + 1 edit-minor + 2 edit-content (7/9). Both language- and fact-clean; all differences are pedagogical polish. n=9 each — too small to rank engines.

Per-item rationale is in `rater-A-scores.json`.

## Findings
1. **Honesty architecture validated** — 0 hard-gate defects reached shipped content. Both hallucinated extractive items and all three numeral-government issues were held back. Shipped UA is VESUM-clean and anchor-grounded → **0 reject-lang, 0 reject-fact**.
2. **Numeral-gate false positive** (candidate issue) — «близько + двох-трьох + gen.pl noun» wrongly fails the `ends_2_4` nominative-plural expectation (вареники·gemma held a *correct* statement). Over-holds correct content; conservative, not unsafe.
3. **Rater-only defects** (the value of §5 human rating, invisible to gates) — cloze distractor discrimination, matchup gloss precision, wrong-sense synonyms, B1-appropriateness of abstract terms, borderline-TF phrasing.
4. **matchup_semantics is non-informative today** — warns on ~100% of pairs due to empty Atlas `related_entries` (#4949); raters judge matchup pairs manually until the backfill lands.

## Rubric ambiguities to resolve at lock (vs rater B / adjudication)
- **(a)** Scoring basis = shipped activity; held-back sub-items = honesty surface, not a defect. Rater B must use the same basis.
- **(b)** Cloze with multiple plausible options → edit-content vs edit-minor? (Used edit-content.)
- **(c)** Wrong-sense synonym in matchup → edit-content vs reject-pedagogy? (Used edit-content.)
- **(d)** Thin activity (3 TF items after salvage) → accept vs edit? (Used accept.)
