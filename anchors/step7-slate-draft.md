# Step-7 anchor slate — WORKING DRAFT (2026-07-10)

> Status: **DRAFT — this is NOT the pre-registration.** The immutable pre-registration commit
> (plan §2) happens when steps 3–4 merge, and must add: engine SHA + prompt/package/gate
> versions + data-bundle digests, final excerpt boundaries, and per-anchor expected-landmine
> lists frozen. Until then this file records curated candidates + open slots.
> Provenance: agy candidate research (bridge ask 2360→2361) → main-claude source-verification
> (every path/chunk probed; **1 of 14 candidates was a phantom** — see §Rejected) → curation.

## Slate per plan §2 composition (2R + 4T + 2P + A1 + A2 + A3 + A4 = 12)

| # | class | anchor | source (verified) | ~chars | why / landmines |
|---|---|---|---|---|---|
| 01 | R | «10 причин читати!» | slice-1 fixture (this repo) | — | regression continuity; freshness declaration owed by engine lane at pre-reg |
| 02 | R | «Жінка та чоловік» | slice-2 fixture (this repo) | — | regression continuity; same declaration |
| 03 | T | ULP podcast intro (Анна) | `docs/references/private/ULP 3-00…txt` (l0081 intro; quote probe ✓×14) | ~650 | SHORT constraint ✓; stress-marked vowels, spoken particles — tests tokenizer + 6-item minimum + shortfall honesty |
| 04 | T | Діалог «Ви летите додому?» | same file, l0081 dialogue (probe ✓) | ~1300 | dialogue turns INSIDE a T anchor; colloquialisms, toponyms |
| 05 | T | «Моє життя в Києві» (голосове) | same file, l0095 (probe ✓×4) | ~2100 | diminutive density (матусю, базарчик), idioms (очі розбігаються, не по кишені) — lexical-gate stress |
| 06 | T | Антоненко-Давидович «Заступник і замісник» | `docs/references/private/antonenko.txt` ~L1010 (probe ✓) | ~900 | NATURALLY russianism-bearing constraint ✓ — quotes wrong usages verbatim; gates must not "verify" quoted errors (ties to defect #7 semantics) |
| 07 | P | «Реальна історія… Розумовський у Краматорську» | sources.db `external_articles` chunk `ext-realna_istoria-4` (probe ✓, 15K — excerpt TBD at pre-reg) | excerpt ~3000 | proper-name-heavy constraint ✓ (Краматорськ, Розумовський, Холодний Яр…) — stresses not-verifiable-from-text; domain: history/journalism. YouTube transcript (Akim Galimov) — internal measurement use only |
| 08 | P | ULP «Український інтернет-сленг» | chunk `ext-ulp_youtube-124` (probe ✓, 9.8K — excerpt TBD) | excerpt ~2800 | anglicisms/slang (забанити, зафрендити, хз…) — most words NOT in VESUM → lexical-gate stress test; domain: internet culture. ASR noise present («ini Lens podcast») — bonus semi-A3 material. Transcript (Anna Ohoiko) — internal use only |
| 09 | A1 | «Встановлення кордонів УРСР» | `docs/references/textbooks-txt/11-klas-history.txt` (quote probe ✓) | ~1700 | numeral-dense: years, ranges (1939–1940), full dates (16 серпня 1945 року), ordinals — defect-#2 regression classes |
| 10 | A2 | Коцюбинський, «Тіні забутих предків» — дорога Івана на полонину («Теплим весняним ранком…» → «…щоб захитати море верхів…») | sources.db chunks `48346587_c0609`→`c0610` (wave7-krypyakevych-istkult; primary quote inside Крип'якевич; both chunks probed ✓ 2026-07-11; †1913 public domain; internal use) | ~2200 full; excerpt ~1500 cut at «…важким своїм тілом давила землю.» | «grade the task, not the text» ✓ — dense Hutsul dialect ABOVE B1; VESUM pre-check 11/12 FOUND (плай/вориння/оседок/оборіг/жереп/бартка/білиця/крайнебо/Чорногора ✓); **«гутори» NOT in VESUM and IS anchor-verbatim → frozen defect-#7 expectation: kept un-badged, not failed; a task that INTRODUCES it must fail**; «Чорногора» exercises the #57 proper-noun fix. Alternate stays: Антоненко «Застінок, катівня» (~L1003, probe ✓, ~600) |
| 11 | A3 | imperfect teacher-paste — **constructed at pre-reg** (plan allows, must be labeled) | base: anchor 03 or 05 + realistic typos/OCR noise + 1–2 non-normative forms | ~900 | defect-#7 evidence: anchor-verbatim errors NOT badged as verified; baseline diagnostics present |
| 12 | A4 | Q&A інтерв'ю «Нам треба серйозно поговорити» | chunk `ext-realna_istoria-58` (probe ✓, 15K — Q&A excerpt TBD) | excerpt ~2500 | structured/non-paragraph: bold question prompts + spoken answers — evidence-span repair stress |

## Alternates (verified, on the bench)
- **P-alt:** «Депортація 1944» chunk `ext-imtgsh-61` (probe ✓) — history/human-rights; heavy acronyms (СРСР, УПА).
- **A4-alt:** numbered disjoint drill sentences, `10-klas-ukrajinska-mova-zabolotnij-2018.txt` (file ✓; agy's label said "8-klas Zabolotnyi" — mismatch, content must be re-verified before promotion).
- **A1-alt wanted:** decimals/percentages-dense text (economics-style). agy's candidate was a
  phantom (below); find a real one at pre-reg (grep textbooks for `%`-dense chunks).

## Rejected at verification (#M-4)
- **A1_1 «Grade 10 Economics (Krupska)»** — `docs/references/textbooks-txt/10-klas-ekonomika-krupska-2018.txt` **does not exist**; no economics textbook in the corpus at all. agy's ⭐-recommended candidate with detailed "quoted" percentages — fabricated. (13/14 other candidates verified real; the detailed landmine lists made this one indistinguishable without a probe.)
- **A4_2 «Рід іменників»** — file exists but attribution mismatched (Ponomarova vs vashulenko path) and content is Grade-3 child-register; below the band even for «grade the task» purposes. Not promoted.

## Curation notes
1. **T-class ecological validity:** all four T picks are ULP/Antonenko (private refs), not the
   teacher-lesson master (Alona corpus, #4850 channel). At pre-reg, prefer swapping ≤2 of
   03/04/05 for actual Alona-lesson texts if the channel is consultable by then — that corpus
   is the calibration standard the plan names.
2. Excerpt boundaries for the three sources.db chunks (07/08/12) are set at pre-reg (chunks
   are 9–15K; anchors need 2.5–3K coherent excerpts) — recorded as start/end offsets.
3. All source probes this draft: file existence + quoted-string grep + sqlite chunk_id lookup
   (2026-07-10, main-claude). Nothing here rests on agent assertion alone.

X-Agent: main-claude/orchestrator
