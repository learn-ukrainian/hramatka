# Bake-off judgment — 2026-07-10 (#41 informal quality preview)

Judge: main-claude (quality seat). Bar: **teacher-accept-as-is** (#41 FINAL rule: quality
decides the route; cost = waste-guard only). Data: `hramatka/bakeoff/2026-07-10/` @ 364fbe2 —
3 anchors (rent/karp/var) × 2 engines (gemma-ais, deepseek) × 3 types (true-false, cloze,
match-up), judged against the hand-authored reference (the 3 demo lessons, SEED_V=7).
Scope caveat: **informal, non-slate anchors, tiny N** — formal measurement = step-7 plan.
Engines received `level=B1, types=[...]` only — no grammar focus. Reference activities target
each lesson's focus (ступені / дієслова руху / наказовий спосіб); that structural gap is
assembler/roadmap territory (#38/#39), NOT charged against the generators here.

All linguistic claims below are VESUM/anchor-backed (verify_words batch, 18/18 FOUND;
anchor grep). Not guessed.

## Per-item verdicts

Verdict scale: ✅ accept-as-is · ✏️ minor edit · 🔧 substantive edit · ❌ reject.

### rent × gemma-ais (delivered: TF 4 · cloze 1 · match 4)
| item | verdict | note |
| --- | --- | --- |
| TF1 «Парі потрібна була двокімнатна…» | ✅ | faithful paraphrase; «двокімнатна» good B1 inference |
| TF2 «найдешевшою з усіх» (Н) | ✅ | |
| TF3 «холодною, тому що вікна…» (П) | ✅ | nit: colon in anchor attributes cold to батареї too; defensible |
| TF4 «є ліфт…» (Н) | ✅ | |
| cloze «виявилася {gap} жінкою» | ✅ | distractors all fem. instr. adj, VESUM-valid; unambiguous |
| match бюджет/власниця/помірна/житло | ✅ ×4 | correct B1 glosses, lemma-form left sides |
| instruction (TF) | ✏️ | EN artifacts «(True)/(False)»; «правдивими»→«правильними» |
GATE COST: items[4] (locator) «Ціна… двадцять тисяч гривень» killed — **false positive**
(see § Gate precision).

### rent × deepseek (delivered: TF 4 · cloze 1 · match 2)
| item | verdict | note |
| --- | --- | --- |
| TF1–TF4 | ✅ ×4 | explanations quote anchor verbatim — excellent evidence habit |
| cloze «Ми {gap} одразу» | ✅ | verbatim sentence; «зателефонували» distractor near-miss but text-decidable |
| match привітною/цегляний | ✏️ ×2 | glosses fine; left sides are inflected forms (привітною), not lemmas |
| match-up ACTIVITY as delivered | 🔧 | only 2 pairs survived salvage → trivially guessable; teacher must add pairs |
| instruction (TF) | ✏️ | EN artifacts «(true)/(false)» |
GATE COST: items[4] (locator) «близько двадцяти тисяч гривень» killed — **false positive**, and it was
deepseek's numeral PROBE done CORRECTLY (близько + Р.в.; its explanation even names the rule).
GATE WIN: 2 match-up pairs (оголошення, помірна) dropped for fabricated evidence quotes —
correct per extractive contract; the glosses themselves were fine, the EVIDENCE was invented.

### karp × gemma-ais (delivered: TF 5 · cloze 1 · match 4)
| item | verdict | note |
| --- | --- | --- |
| TF1–TF4 | ✅ ×4 | incl. good year-round-shepherd transform |
| TF5 «зайняла три години» (П) | ✏️ | anchor: «**майже** три години» — attentive student defensibly answers Н; add «майже» |
| cloze «сиділи біля {gap}» | ✅ | verbatim-decidable |
| match краєвид/вовна/полонина/садиба | ✅ ×4 | «полонина ↔ гірське пасовище» is the textbook definition; шерсть VESUM-valid |
| instruction (TF) | ✏️ | EN artifacts |

### karp × deepseek (delivered: TF 4 · cloze 1 · match 3; 1 whole activity REJECTED)
| item | verdict | note |
| --- | --- | --- |
| TF1–TF4 | ✅ ×4 | «майже» kept — precise; «тепла навіть узимку» = strong transform |
| cloze «Стежка до нього {gap}…» | ✅ | nit: dangling «нього» if printed out of lesson context |
| match вівчар/краєвид/шкарпетки | ✅ ×3 | lemma-form, correct glosses |
| instruction (TF) | ✏️ | EN artifacts |
GATE WIN (demo gold): standalone TF «Близько три годин тривала дорога…» — ungrammatical
(«близько» → Р.в. «трьох годин»; «три годин» invalid in ANY reading) and the model's
explanation *defends* the error as correct. Killed by numeral gate → partition dropped the
activity. The teacher never saw it. This is the harness earning its keep.
GATE COST: match pair «полонина ↔ високогірне пасовище в Карпатах» killed — **false
positive** («Карпатах» IS in VESUM, lemma Карпати; it was the best pair of the four).

### var × gemma-ais (delivered: TF 5 · cloze 1 · match 4)
| item | verdict | note |
| --- | --- | --- |
| TF1–TF3 | ✅ ×3 | |
| TF4 + TF5 | ✏️ | both probe the SAME fact (wait after floating) — replace one |
| cloze «ложку {gap}» | ✅ | |
| match подавати/змішати/родина | ✅ ×3 | родина↔сім'я is a true synonym pair |
| match начинка ↔ «…(фарш)» | ✏️ | «фарш» = м'ясна начинка; misleads at B1 (fillings here are potato/cherry) |
| instruction (TF) | ✏️ | EN artifacts |

### var × deepseek (delivered: TF 4 · cloze 1 · match 4 · standalone TF 1)
| item | verdict | note |
| --- | --- | --- |
| TF1–TF4 | ✅ ×4 | |
| cloze «одна з найвідоміших українських {gap}» | ✏️ | world-knowledge-guessable (напоїв/пісень/книжок); needs real distractors |
| match шкварки/спливуть | ✅ ×2 | «шкварки ↔ смажені шматочки сала» — excellent |
| match начинка «фарш / …» | ✏️ | same фарш issue, worse (фарш listed first) |
| match розкачайте ↔ «розрівняйте качалкою» | ✏️ | розкачати ≠ розрівняти; gloss imprecise; inflected left side |
| standalone 1-item TF «близько двох-трьох хвилин» | ✏️ | item itself ✅ (correct Р.в. — the probe done right, gate-clean); 1-item activity is structurally odd → merge into main TF (#39 assembler) |

## Aggregates (item level, as delivered)

| cell | items | ✅ | ✏️ | 🔧 | accept-or-minor |
| --- | ---: | ---: | ---: | ---: | ---: |
| rent × gemma | 9 | 9 | 0 | 0 | 100% |
| rent × deepseek | 7 | 5 | 2 | (activity-level 🔧) | 100% item / match-up activity 🔧 |
| karp × gemma | 10 | 9 | 1 | 0 | 100% |
| karp × deepseek | 8 | 8 | 0 | 0 | 100% |
| var × gemma | 10 | 7 | 3 | 0 | 100% |
| var × deepseek | 10 | 6 | 4 | 0 | 100% |
| **gemma total** | **29** | **25** | **4** | **0** | **100%** |
| **deepseek total** | **25** | **19** | **6** | **1 activity** | **100% item-level** |

Every delivered item clears accept-or-minor; nothing delivered required rejection — the
rejects were all caught upstream by the gates. Step-7 arming bar (accept-or-minor ≥80%,
substantive ≤15%) would pass for both engines on this informal, tiny-N preview.
Instruction-level EN artifacts are excluded from item counts (one ✏️ per TF activity, both
engines, same root cause — see prompt bug below).

## Gate precision (the pipeline's own scorecard)

| gate event | verdict | detail |
| --- | --- | --- |
| karp-dpsk «три годин» TF rejected | ✅ REAL CATCH | ungrammatical + model defended it; teacher never saw it |
| rent-dpsk 2 match pairs dropped (fabricated evidence) | ✅ REAL CATCH | extractive contract enforced |
| rent-gemma «двадцять тисяч гривень» item killed | ❌ FALSE POSITIVE | the phrase is **verbatim in the teacher's anchor**; двадцять governs Р.в. мн. («тисяч» IS a VESUM form of «тисяча»); nested-magnitude rule misclassifies «двадцять» as ends_1 |
| rent-dpsk «близько двадцяти тисяч гривень» item killed | ❌ FALSE POSITIVE | same rule bug; killed a correctly-built numeral probe |
| karp-dpsk «Карпатах» pair killed | ❌ FALSE POSITIVE | «Карпатах» IS in the pinned vesum.db (`word_form='Карпатах'`, lemma Карпати, `noun:…:v_mis:ns:prop:geo`; direct SQL). Root cause in code: `gates/vesum.py` lowercases every token (`{t.lower() for t in tokens}`) before the byte-exact `WHERE word_form = ?` lookup — 'карпатах' ≠ 'Карпатах'. Any INTRODUCED inflected proper noun dies this way; anchor-verbatim ones survive only via the verbatim bypass |

Numeral-gate kill precision this run: 1/3. vesum_token kill precision: 0/1. Salvage silently
removed 3 teacher-acceptable items (recall loss a teacher never sees — worse than a visible
warn). Filed as engine bugs (see issues). The warn tier (false_statement, matchup_semantics)
behaved exactly as designed — teacher-confirm, never auto-accept.

## Cross-engine read (informal)

- **gemma-ais**: fuller, more consistent activities (4–5 TF items, 4 match pairs everywhere);
  zero hallucinated evidence; zero grammar rejects; 1 faithfulness precision miss («майже»);
  ~70–78 s/cell.
- **deepseek**: verbatim-quoting explanations (best evidence habit), stronger statement
  transforms; but 1 grammar reject + 2 fabricated evidence spans (harness had to intervene
  3× vs 0× for gemma); ~10–12 s/cell.
- **Reference bar** (hand-authored): larger volume (5–7 items, 5–6 pairs), focus-grammar
  alignment, word-bank cloze subtype, productive follow-through («Якщо неправда — виправте»),
  fully-UA instructions. Engines are not close to this as WHOLE LESSONS — but per the #41
  frame the reference brackets the top; the engines' job was items, and their items clear
  the teacher-accept bar.

## Recommendation

1. **Route: stay gemma-ais for the pilot.** Quality-decides rule: gemma needed zero harness
   interventions and delivers fuller activities; deepseek's speed (7×) is not a route
   criterion. Formal confirmation = step-7 on the immutable slate.
2. **Fix before step-7 bakes** (else measured accept-rate is depressed by our own bugs):
   numeral nested-magnitude misclassification; vesum_token proper-noun lookup; gen-prompt
   UA-only instruction rule (both engines emit «(True)/(False)» — schema vocabulary leaks
   into teacher-visible text; #M-13 immersion).
3. Match-up prompt: require lemma-form left sides; min-pairs floor (salvage below 3 pairs
   should downgrade the activity to review_required-with-note or drop it, not deliver a
   2-pair board).
4. Numeral probes: deepseek built one correct and one wrong probe; gemma built statement-level
   numerals only. Keep the probe requirement — it's doing its job in both directions.

X-Agent: main-claude/orchestrator
