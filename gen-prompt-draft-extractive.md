# DRAFT — Gemma extractive-generation prompt (Phase 2 folds into engine/prompts/extractive.md)

> v1 for MEASUREMENT. The whole point is to drive edit-rate DOWN by iterating THIS prompt on real results,
> so treat it as a starting bet, not final. ⚠️ Pedagogy = CONTENT-lane authority — the "what makes a good
> B1 item" choices here are provisional and must be validated by a pedagogy reviewer before hardening.
> Immersion policy (#M-13): B1 = Ukrainian teaching voice; English only as an optional gloss on MatchUp
> right-sides, never as the instruction language, never raised.

## System / instruction block (UA-first)
```
Ти — генератор навчальних завдань з української мови для вчителя. Вчитель дає ТЕКСТ-ОПОРУ (anchor) рівня B1.
Твоє завдання — створити завдання, ЦІЛКОМ похідні від цього тексту (extractive): кожен пункт спирається на
конкретний фрагмент опори. Мова інструкцій і завдань — УКРАЇНСЬКА. Не вигадуй фактів, яких немає в опорі.

Створи РІВНО такі завдання (по одному кожного типу):
1) true-false — 4 твердження про зміст опори (частина правдиві, частина хибні). Кожне твердження мусить
   спиратися на дослівний фрагмент опори (evidence). Хибні твердження — це СПОТВОРЕННЯ сенсу опори, а не
   граматично зіпсовані речення.
2) cloze — одне речення, ДОСЛІВНО взяте з опори, з одним пропуском ({gap}) на змістовому слові; відповідь =
   вилучене слово; 3 правдоподібні дистрактори тієї ж частини мови.
3) match-up — 4 пари: ліворуч слово/термін з опори, праворуч його значення українською (синонім/тлумачення).

Для КОЖНОГО пункту додай поле "evidence": дослівну цитату з опори, на якій він тримається (рядок тексту).

ФОРМАТ ВІДПОВІДІ — рівно один JSON-об'єкт, без пояснень до чи після:
{
  "activities": [
    {"type":"true-false","instruction":"…","items":[
       {"statement":"…","correct":true,"explanation":"…","evidence":"<дослівна цитата з опори>"}, …]},
    {"type":"cloze","instruction":"…","text":"… {gap} …","blanks":[
       {"id":1,"answer":"…","options":["…","…","…","…"]}],"evidence":"<речення-опора без пропуску>"},
    {"type":"match-up","instruction":"…","pairs":[
       {"left":"…","right":"…","evidence":"<цитата, де left трапляється в опорі>"}, …]}
  ]
}
```

## Numeral positive probe (added as a 5th true-false item OR a separate block — per §8)
```
Додай ОДНЕ додаткове твердження true-false, у якому ти ПЕРЕКАЗУЄШ число з опори у НОВОМУ реченні, зберігаючи
ПРАВИЛЬНЕ керування числівника (відмінок іменника після числа). Уживай лише називний відмінок або зворот із
прийменником (близько/понад/до) — не будуй складних відмінкових контекстів. evidence = фрагмент опори з тим числом.
```
(Negative/wrong-government numeral cases are NEVER generated for the learner — they live only in the eval
fixtures. The gate REJECTS wrong government by design.)

## Grounding-IN injection (retrieval.py fills this before the anchor)
Prepend a compact, VERIFIED grounding pack the model may rely on:
- `Рівень: B1.`
- `Перевірена лексика опори (лема — рівень CEFR — синоніми):` … (from atlas_lookup on anchor lemmas)
- `Числа в опорі (для звірки керування):` … (numeral inventory: raw span + following noun)
Do NOT dump the whole atlas; only lemmas that actually occur in this anchor. Keep the pack < ~1500 tokens.

## Output-contract notes for the parser (generate.py)
- Gemma is toolless + may wrap a leading `<thought>…</thought>` — `_invoke_opencode` already strips it; still
  guard: extract the first balanced `{…}` JSON object; retry once on parse failure; on 2nd failure → that
  run = gate-fail "unparseable" (do not crash).
- The `evidence` keys are the ENGINE-IR superset — parser lifts them into `ir.evidence`, then STRIPS them so
  `ir.activity` is a clean activities-b1 object (evidence must NOT remain on the b1 item — schema is
  additionalProperties:false). Validate the stripped object with jsonschema before persisting.

## Few-shot (anchor #1 "10 причин читати!") — ONE worked example per type, to include verbatim
(Build Phase 2 will paste a real, hand-checked example set generated from lines 4681-4693 so the model has a
correct pattern to imitate — TrueFalse citing "Третина українців за рік не прочитує жодної книжки",
Cloze on a real sentence, MatchUp on анкор terms like "насолода"→"велике задоволення". Hand-verify the
example's own gates pass before embedding it — a wrong few-shot poisons every generation.)
```
```
