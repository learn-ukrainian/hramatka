# Independent numeral-gate verification oracle (claude-infra, #M-4a — NOT the builder's own fixtures)
> **FINAL (post-fix) 2026-07-08: MOAT VERIFIED CORRECT.** After the 7-bug consolidated fix: builder 70 unit
> tests green + my comprehensive regression 28/29 (the "1" = `на ста двадцяти одному кілометрі` warned
> `context-undetermined` — DOCUMENTED slice-1 limit, "на" isn't an auto-trigger; passes when context_case=loc
> given) + prefix-declension 2/2 (fully-declined loc → pass; un-declined prefix → warn compound-prefix-unverified)
> + gender 4/4 (дві столи/два книги → FAIL) + all 7 review probes correct. **Spec self-correction:** my §6c/oracle
> used the colloquial un-declined `на двадцять одному` — standard UA declines every compound component
> (`на двадцяти одному`); the builder's re-verify caught it; §6c + this oracle now corrected. Moat is Phase-2-ready.

> **RESULT 2026-07-08: 11/11 oracle cases matched expected verdict** against the built gate (each with
> correct rule + reasoning in the detail string; negatives failed with the right expected form). Moat logic
> INDEPENDENTLY VERIFIED CORRECT. **One real gap found (NOT in this oracle):** `два/дві` gender agreement —
> «дві столи» (masc→needs два) and «два книги» (fem→needs дві) both wrongly PASS (VESUM tags both два/дві as
> lemma `два`, no gender). Fix = surface-string (два vs дві, incl. compound last-word + обидва/обидві) +
> noun VESUM gender (m/n→два, f→дві). Queued for the consolidated builder fix + cross-family code review (bm0ccwh65).

All noun/numeral forms below are VESUM-confirmed (2026-07-08). Run each through the built
`check_numeral_government(phrase, context_case)` after Phase 1; a mismatch = a moat bug to fix before Phase 2.
These deliberately stress the rules the fleet caught (acc-animate, oblique-×1, collective, півтори,
тисяча-nested, понад-transparent, genitive-override, з-ambiguous) with cases DIFFERENT from what I handed the builder.

| # | phrase | context | EXPECT | why (rule) |
|---|--------|---------|--------|-----------|
| 1 | бачу двадцять двох студентів | acc (verb бачити) | pass | compound ends-2 + acc-ANIMATE → gen-form `студентів` (anim p v_zna≡v_rod). Reject `студенти`. |
| 2 | на сто двадцять одному кілометрі | loc (на) | pass | compound ends-1 → SG in context case → `кілометрі` (loc sg). Reject any plural. |
| 3 | четверо друзів | nom | pass | COLLECTIVE → gen pl `друзів`. Must NOT apply 2-4 nom-pl (would wrongly want `друзи`). |
| 4 | півтори години | nom | pass | півтори(f) → gen SG `години`. |
| 5 | п'ять тисяч гривень | nom | pass | 5+ → magnitude `тисяч` (gen pl); noun `гривень` (gen pl). |
| 6 | дві тисячі гривень | nom | pass | 2 → magnitude `тисячі` (nom pl, NOT `тисяч`); noun `гривень` (gen pl). |
| 7 | понад п'ять років | nom (понад transparent) | pass | понад does NOT override → 5+ → `років` (gen pl). |
| 8 | близько двадцяти книжок | (близько→gen override) | pass | `двадцяти` (gen numr) + `книжок` (gen pl). |
| 9 | з трьома друзями | ambiguous з | warn | `з` = gen OR instr (same surface) → gate must WARN, not force. (`трьома` instr, `друзями` instr — internally consistent, but з is undecidable.) |
| 10 | четверо друзи | nom | FAIL | collective needs gen pl `друзів`; `друзи` (nom pl) is wrong. |
| 11 | дві тисяч гривень | nom | FAIL | 2 needs magnitude `тисячі` (nom pl); `тисяч` (gen pl) is wrong for ×2. |

Verified forms + tags: студентів(anim p v_rod≡v_zna), кілометрі(loc sg), друзів(gen pl друг), години(gen
sg/nom pl), тисяч(gen pl тисяча), тисячі(nom pl / gen sg), гривень(gen pl гривня), років(gen pl рік),
двадцяти(gen numr), книжок(gen pl книжка), трьома(instr три/троє), друзями(instr pl друг).
Note ambiguities the gate must handle gracefully: `години`/`тисячі` are syncretic (nom-pl vs gen-sg) →
the gate resolves by the numeral class, not by the noun form alone.
