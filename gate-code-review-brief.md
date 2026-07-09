# CODE REVIEW — Hramatka numeral case-government gate (the "moat"). Cross-family adversarial pass.

Read the ACTUAL CODE (local files, this machine):
- `/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/gates/numeral.py`  ← primary
- `/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/gates/vesum_tags.py`
- SPEC it must satisfy: `.agent/tmp/hramatka/slice-1-build-plan.md` §6c (the numeral rules, VESUM-verified).

Context: this is a deterministic Ukrainian numeral+noun case-government verifier — model proposes a phrase,
this gate DECIDES pass/warn/fail against VESUM (no LLM). It already passes 49 unit tests + my independent
11-case oracle (acc-animate, oblique-×1, collective, півтори, nested тисяча, понад-transparent,
genitive-override, з-ambiguous all correct). I need you to find what the tests DON'T cover.

## KNOWN GAP (already found — tell me the BEST fix, and whether your fix is complete)
`два/дві` gender agreement is NOT verified: «дві столи» (стіл=masc, needs `два`) and «два книги»
(книга=fem, needs `дві`) both wrongly PASS. VESUM tags both `два` and `дві` as lemma `два` with identical
tags (no gender), so tag-matching can't catch it. Proposed fix: use the SURFACE string (`два` vs `дві`,
incl. compound last-word `двадцять два/дві`, and `обидва/обидві`) + the noun's VESUM gender (m/n→два, f→дві).
Confirm this is right and complete (three/чотири do NOT inflect for gender — exclude them).

## FIND (ranked, concrete, with the specific line/case)
1. **Correctness bugs the 49 tests + my oracle would miss** — tokenization of multi-word phrases, compound
   numeral parsing (which token is "last"? hyphenated? "сто двадцять один"), noun-not-adjacent-to-numeral,
   punctuation, mixed digits+words, numeral as digits ("17 ділянок") vs words.
2. **Any numeral-government case still MISSED** beyond the два/дві gap (e.g. `нуль`, `обидва`, paucal stress,
   `тисяча` as bare noun, ordinal-in-compound, animate in nom, syncretic noun forms resolving wrong).
3. **VESUM-tag parsing bugs** in vesum_tags.py (case-code map, plural detection, animacy, multi-parse words).
4. **Robustness**: does it crash on empty/garbage input, a phrase with no numeral, an unknown noun (not in
   VESUM)? Should be warn/fail, never an exception.
5. **Code quality** worth fixing now (only if it affects correctness/maintainability of the moat).

## Reply format
Terse. Ranked findings: `SEVERITY | file:concept | the failing input → wrong output | the fix`. Then a
one-line verdict: is the moat CORRECT ENOUGH to build the Phase-2 pipeline on top of (after the два/дві fix),
or are there blockers first? No preamble.
