# Step-7 anchor slate — PRE-REGISTRATION (IMMUTABLE)

> Plan §2 artifact. Committed BEFORE any bake. Any post-commit engine/prompt tuning VOIDS
> this slate → re-pre-register (both slates stay in history; void recorded in results).
> No swapping after results are seen. A defective anchor is marked VOID with reason and
> replaced by a NEW pre-registered entry; both stay recorded.
> Leak list applies (Sol item 1): anchors, IR, prompts, reports stay in this private repo.
> Public reporting = aggregates only (plan §7).
> Supersedes the working draft `step7-slate-draft.md` (kept in history for provenance).

## Version pins (environment MUST match at bake time; report header asserts equality)

| pin | value |
| --- | --- |
| engine SHA (this repo, main) | `4d1943082656c582a89be340e0555f2126b4769b` (post-#62: gates final per #57/#61, measure.py §1-complete per #62) |
| generator A | `google-ais/gemma-4-31b-it` (AIS route, toolless) |
| generator B | `deepseek-v4-pro` (first-party API; bake-off r3 winner — flash dropped, r3 judgment 2026-07-11) |
| gen prompt | `hramatka/engine/prompts/extractive.md` sha256 `9b0c30a97f64b7f769053592b5ac976262e9803c18edeea29d1d3e2b9fa9dfb9` |
| vendor packages | `learn_ukrainian_linguistics@1.0.0` · `lu.activity.v1@1.0.0` · `lu.lesson.v1@1.0.0` (MANIFEST digests enforced by the step-3 drift guard) |
| data release | `2026-07-10` content_sha256 `ed779c7eabc6d8b59ea63e492a3569fa5ada1ddd0223db18f9e0d98d4781cfbf` |
| — vesum.db | `3ed0fda490c576046c67c65b1b463ab9c7d2948749cc28768f4e83559b541462` |
| — atlas.db | `213f076d20c767377fae64e6c3cfa3c2cb8701f21df80e813434efcb2e3fe83b` |
| — sources.db | `e22c4d90d9d7f0e6a013418be1f07b66f29dc09d734b3d9f9494977a0bdfbfca` |
| level | B1; types `true-false, cloze, match-up` (shipped 3 — apples-to-apples with bake-offs) |

Gate state at pin: all 6 bake-discovered FP shapes fixed and regression-locked (#52/#53/#57,
#58/#61). Known accepted residue: none open in the gates as of the pin SHA.
Run-time enforcement: every measurement report MUST carry `header.slate_pin_assertion` with
`pins_match=true` against THIS table (mechanism landed in #62 and was verified to flip on
mismatch); each run asserts its generator equals one of the two pinned generators exactly.

## R-anchor freshness declaration (plan §2 row 01–02, REQUIRED)

Anchors 01–02 are **`tuning-familiar` = YES**, evidence-based: documented throughout the
slice-1 artifacts (plans, recon, results) and exercised via the test scaffolding
(`load_anchor("anchor01")` fixtures — synthetic stand-ins for privacy — plus slice-1 bakes
and CI history across steps 3–4 and PRs #57/#61). Per plan:
they still run, reported SEPARATELY, and a fresh independent 13th regression anchor (R′,
row 13) is added below. (Declared by orchestrator from repo evidence, 2026-07-11.)

## T-swap record (curation note 1)

Alona-corpus swap NOT possible at pre-registration: #4850 (teacher-lesson master ingest)
is OPEN with zero teacher-lesson rows in sources.db (deterministic check quoted in the
issue, 2026-07-10). T anchors stay ULP/Antonenko; ecological-validity caveat carries into
the report (§5b).

## The slate — 13 anchors (2R[tuning-familiar] + 1R′ + 4T + 2P + A1 + A2 + A3 + A4)

| # | class | anchor | source (all probed) | chars | domain | expected landmines (frozen) |
|---|---|---|---|---|---|---|
| 01 | R (tuning-familiar) | «10 причин читати!» | slice-1 fixture (this repo) | as fixture | reading motivation | continuity vs slice baseline; reported separately from R′ |
| 02 | R (tuning-familiar) | «Жінка та чоловік» | slice-2 fixture (this repo) | as fixture | narrative | same |
| 03 | T | ULP podcast intro (Анна), lesson №81 | `docs/references/private/ULP 3-00 Lesson Notes (all in one file).txt` (public checkout; quote-probed ✓×14 at draft) | ~650 | spoken intro | stress-marked vowels; spoken particles; 6-item minimum + shortfall honesty on SHORT anchor |
| 04 | T | Діалог «Ви летите додому?», lesson №81 | same file (probe ✓) | ~1300 | dialogue | dialogue turns inside T; colloquialisms; toponyms |
| 05 | T | «Моє життя в Києві» (голосове), lesson №95 | same file (probe ✓×4) | ~2100 | first-person narrative | diminutive density (матусю, базарчик); idioms (очі розбігаються, не по кишені) — lexical-gate stress |
| 06 | T | Антоненко-Давидович «Заступник і замісник» | `docs/references/private/antonenko.txt` ~L1010 (probe ✓) | ~900 | language column | NATURALLY russianism-bearing: quotes wrong usages verbatim — gates must NOT badge quoted errors as verified (defect #7) |
| 07 | P | «Реальна історія: Розумовський у Краматорську» | sources.db `external_articles` `ext-realna_istoria-4`, **excerpt chars 0–3192**, START «Коли хтось каже, що схід України проросійський…» END «…принести його у своє життя?» | 3192 | history/journalism (ASR transcript) | proper-name-heavy (Краматорськ, Розумовський, Холодний Яр, Ізюм, 93 бригада) → `not-verifiable-from-text` stress; numerals (80 років, 4:50, 2017, 10 годин); mild ASR noise incl. «пов'язаня пов'язані» stutter — authentic P texture. Internal use only (YouTube transcript, Akim Galimov) |
| 08 | P | ULP «Український інтернет-сленг» (еп. 166) | `ext-ulp_youtube-124`, **excerpt chars 0–3200**, START «ви слухаєте ini Lens podcast…» END «…розшар будь ласка екран у Зумі» | 3200 | internet culture (ASR transcript) | anglicisms/slang mostly NOT in VESUM (забанити, зафрендити, хз, вподобайка, мемас, гівка) → lexical-gate stress; ASR noise verbatim («ini Lens podcast», «fфіча», «L lol») = semi-A3 bonus; unpunctuated flow. Internal use only (Anna Ohoiko) |
| 09 | A1 | «Встановлення кордонів УРСР» | `docs/references/textbooks-txt/11-klas-history.txt` (quote probe ✓ at draft) | ~1700 | history textbook | numeral-dense: years, ranges (1939–1940), full dates (16 серпня 1945 року), ordinals — defect-#2 regression classes incl. the NOW-FIXED shapes (#57/#61) |
| 10 | A2 | Коцюбинський, «Тіні забутих предків» — дорога Івана на полонину | **PRIMARY**: sources.db `literary_texts` `25ea8a3b_c0010→c0012` (ukrlib-kotsyubynsky, the novel itself; probes ✓ 2026-07-11; †1913 public domain). Corroboration: the same passage abridged (1,857 ch) is quoted in Крип'якевич `48346587_c0609→c0610` (wave7 provenance labels from data prep) | 2625 measured in primary, QUOTE-BOUNDED (normalize whitespace before extraction; offsets not stable across chunkings): START «Теплим весняним ранком Іван ішов в полонину.» END «…важким своїм тілом давила землю.» | classic literary prose | ABOVE-B1 by design → «grade the task, not the text»: gates must not reject the anchor for difficulty; tasks stay B1. VESUM pre-check 11/12 FOUND (плай, вориння, оседок, оборіг, жереп, бартка, білиця, крайнебо, Чорногора); **«гутори» NOT in VESUM + anchor-verbatim → must stay un-badged, not failed; a task that INTRODUCES it must fail**; «Чорногора» exercises #57 proper-noun fix |
| 11 | A3 | NATURAL imperfect paste: Voron 9-klas grammar page (OCR-damaged) | `docs/references/textbooks-txt/9-klas-ukrajinska-mova-voron-2017.txt` **L1098–1116** («Частини складних речень…» → «…(Леся Українка).») | ~900 | textbook (scanned/OCR) | NATURAL example (plan-preferred over constructed). Frozen error inventory: «виде»(→вийде) · «Я к»(→Як) · «бидеиі»(→будеш) · «поаиювати»(→працювати) · «. то»(→«, то») · «до хат и»(→до хати) · «биде»(→буде) · «П леш е»(→Плеще) · «скачать»(→скачуть; russism-shaped) · «Л бажаю»(→Я бажаю) · «било»(→було) · spaced-apostrophe «з ’єднуватися». VESUM evidence: бидеиі/поаиювати/биде/скачать NOT-FOUND → introduced use must fail; **«виде» and «било» ARE VESUM forms of unrelated lemmas (вид, било) — homograph traps: lexical gates CANNOT catch them; protection = defect-#7 no-verified-badge on anchor-verbatim + baseline diagnostics present** |
| 12 | A4 | Q&A «Нам треба серйозно поговорити» (Реальна історія) | `ext-realna_istoria-58`, **excerpt chars 1461–3950**, START «Давайте почнемо з запитань.» END «…і закінчив її у 2002 році.» | 2489 | Q&A interview (ASR) | structured non-paragraph: question prompts + spoken answers (2 full Q&A blocks) — evidence-span repair stress; biographical proper names + dates (Приморський край, Узбекистан, Сімферополь, 1994, 2002) |
| 13 | R′ (fresh independent regression) | ULP «День вчителя в Україні» — «Вільний вступ» (Анна), lesson №87 | `ULP 3-00 Lesson Notes (all in one file).txt`, section «Вільний вступ» of Episode 87: START «До́брий день! Мене́ зва́ти А́нна…» END «…нови́х розмо́в Христи́ни в Украї́ні?» (UA column only, EN column stripped per the anchor-03 extraction recipe) | ~700 | spoken intro | NEVER used in tuning (repo grep: zero hits in hramatka/). Plain B1, stress-marked — restores the independent regression pair that 01–02 no longer provide |

**Excerpt boundary convention:** anchors 07/08/12 offsets are measured on the RAW stored
`external_articles.text` (no normalization; verified by direct SQL probes 2026-07-11).
Anchor 10 is QUOTE-BOUNDED only (its literary_texts rows carry CRLF hard-wraps). START/END
quotes are authoritative everywhere; offsets are a convenience and must be re-derived if any
ingest re-normalizes text.

## Alternates (pre-registered bench; promotion only via the VOID rule)

- P-alt: «Депортація 1944» `ext-imtgsh-61` (probe ✓ at draft) — heavy acronyms (СРСР, УПА).
- A1-alt: 11-klas-history ≈L12121–12141 — «35,6 %», ranges «від 15 до 28 %», «5—7 %», mixed
  spacing; same-file-as-09 caveat if promoted.
- A2-alt: Антоненко «Застінок, катівня» ~L1003 (probe ✓, ~600).

## Rejected at curation (recorded, #M-4)

- «Grade 10 Economics (Krupska)» — file does not exist; fabricated candidate (agy round 1).
- «Рід іменників» — attribution mismatch + child register.

## Raters & scoring (plan §5 pointers, unchanged)

Dual-BLIND raters; rater B = independent pedagogy lane (agy primary, cursor alternate);
calibration round + κ per plan §5; evidence-informed adjudication; gold slot reserved for
the 2026-07-12 teacher opportunity. Arming bar (plan §6): hard gates + accept-or-minor-edit
≥80% / substantive ≤15% (accept-as-is 70/85 tracked).

X-Agent: main-claude/orchestrator
