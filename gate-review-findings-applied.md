# Numeral-gate code review — findings (codex #2213 + grok #2215), ALL confirmed against the gate, fixes dispatched
Cross-family review after the moat passed 49 unit tests + my 11/11 behavioral oracle. The oracle used
clean "numeral+noun" phrases → it MISSED everything below (real model output has punctuation/verbs/compounds).
Every claim re-probed by me against the live gate (2026-07-08) — all 7 real. Consolidated fix sent to builder a65967d7.

| # | sev | confirmed probe → wrong output | fix |
|---|-----|-------------------------------|-----|
| 1 | BLOCK | `17 ділянок.` → noun-not-in-vesum; `Два студенти прийшли.` → head='прийшли.' | strip punct + scan reversed post-tokens for rightmost pos=noun (root fix) |
| 2 | BLOCK | `дві столи`,`два книги`,`обидві столи` → pass/warn | обидва→ENDS_2_4; surface-gender (два/обидва→m,n; дві/обидві→f) nom-case only, on immediate/magnitude noun; excl три/чотири/oblique/digits; lemma gender fallback |
| 3 | HIGH | `нуль студентів` → no-numeral-found | classify нуль (VESUM noun) → ENDS_5_9_0 |
| 4 | MED | `два мої` → PASS (мої not a noun) | pos_filter="noun" for head parse |
| 5 | BLOCK | `до сто двадцять одного року` → PASS (prefix `сто двадцять` should be gen `ста двадцяти`) | verify each compound component's case-form; else WARN compound-prefix-unverified — never false-PASS |
| 6 | MED | `20 два студенти` → PASS (mixed digit+word) | reject malformed-compound (allow all-digit / digit+magnitude) |
| 7 | MED | `17-ділянок`,`два-три`,`2-3 рази` | handle hyphen deliberately (split last segment / reject) |

vesum_tags.py: clean (no verdict-inverting bugs). Robustness holds. 500-line fn refactor = optional, after correctness.
**Moat quality arc: solo design → design fleet-review (5 linguistic bugs) → build (49 tests) → my behavioral
oracle (11/11 + 1 gap) → code fleet-review (7 more bugs) → consolidated fix.** Verdicts: codex NOT-yet (compound
blocker) / grok blockers-first-then-ok → after this fix the moat is Phase-2-ready. Re-verify set after fix =
11 oracle + 4 gender + 7 review probes.
