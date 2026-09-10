# Hramatka engine — integration facts (recon-verified 2026-07-08). Build against these exact signatures.

## Grounding-IN — atlas.db (data/atlas.db)
- No reusable class; raw `sqlite3`. Query: `SELECT payload_json FROM article_payloads WHERE is_public_route=1`.
- Per-lemma metadata lives INSIDE the `payload_json` dict (not SQL columns). Mirror these helper reads from
  `scripts/audit/generate_practice_deck.py` (do NOT import it — it's 3205 lines; copy the shapes):
  - lemma/gloss/pos: `entry["lemma"]`, `entry["gloss"]`, `entry["pos"]`.
  - CEFR: `entry["enrichment"]["cefr"]` → else `entry["cefr"]`; normalize regex `\b([ABC][12])\b`.
  - russianism/heritage: `entry["enrichment"]["heritage"]["classification"|"status"]` → else
    `entry["heritage_status"][...]`; severity `entry["enrichment"]["severity"]["level"|"label"]`.
  - paradigm/forms: `entry["enrichment"]["morphology"]["paradigm"]["cases"][case]["singular"|"plural"]`.
  - synonyms/antonyms: `entry["sections"]["synonyms"|"antonyms"]["items"][]` (string list).
- Build `{lemma → payload}` dict ONCE per run (no full scan per lemma).
- Preflight: atlas.db may need `npm --prefix site run atlas:build-db` if absent → clear error.

## Grounding-IN — VESUM (data/vesum.db) — the verification source of truth
- `from scripts.verification.vesum import verify_word, verify_words, verify_lemma, get_vesum_conn`
  - `verify_word(word, pos_filter=None, db_path=None) -> list[dict]`  # each {lemma, pos, tags}
  - `verify_words(words, pos_filter=None, db_path=None) -> dict[str, list[dict]]`
  - Pass an explicit `db_path` (don't rely on cwd). Table `forms(word_form, lemma, pos, tags)`.
- VESUM tag format (confirmed live): `noun:anim:p:v_rod` etc. Case codes: v_naz=nom v_rod=gen v_dav=dat
  v_zna=acc v_oru=instr v_mis=loc v_kly=voc. `:p:`=plural (else sg). anim/inanim on nouns. numr POS = numeral.
  For ANIMATE nouns, acc form == gen form (v_zna≡v_rod). Numerals: `numr:m:v_dav` etc.
- Reuse the case-tag parser: check for `scripts/.../morphological_validator.py::_parse_case` (cursor ref);
  if absent, parse tags directly. **pymorphy3 = lemmatization ONLY, never final verification.**

## Generation — Gemma 4 (toolless, $0 AIS)
- `from scripts.ai_agent_bridge._opencode import _invoke_opencode`
  `_invoke_opencode(content, model, *, variant=None, output_format="default", data=None, no_timeout=False,
   default_timeout_s=OPENCODE_DEFAULT_TIMEOUT_S, agent=None) -> str`  (returns response text; no bridge side effects)
- Call: `_invoke_opencode(prompt, model="google-ais/gemma-4-31b-it", agent="chat", output_format="json",
  default_timeout_s=900)`. `agent="chat"` is MANDATORY (Gemma is toolless; the full tool bundle breaks it).
- Constants in that module: `GEMMA_MODEL="google-ais/gemma-4-31b-it"`, `GEMMA_DEFAULT_TIMEOUT_S=900`.
- Requires `opencode` on PATH (`shutil.which`), key at `~/.secret/google-ais.key` (opencode global config).
- routing_guard fail-closed allows only `google-ais/gemma*`. WRAP the call: catch `SystemExit` → gate-fail
  "generator-unavailable" (don't crash pipeline). Add a regression test that the raw invoke returns text.

## Target activity schema (project to this; validate before writing)
- `schemas/activities-b1.schema.json` — each `*-b1` def is SELF-CONTAINED (does NOT $ref base). Target b1, not base.
  All defs set `additionalProperties:false` → evidence field CANNOT be added inline (use the engine IR + projection).
- TrueFalse (`true-false-b1`): `{type, instruction, items:[{statement, correct:bool, explanation?}]}`.
- MatchUp (`match-up-b1`): `{type, instruction, pairs:[{left, right}]}` (≥2 pairs).
- Cloze (`cloze-b1`): `{type, instruction, text (with {gap}/{{N}} markers), options?:[], blanks?:[{id≥1, answer, options[2..6]}]}`.
- MarkTheWords (`mark-the-words-b1`): `{type, instruction, text, target_words:[…≥1], criteria?}`.
- Quiz (`quiz-b1`): `{type, instruction, items:[{question, options[2..6], correct:int(0-based), explanation?}], vesum_exempt?}`.
- Renderer prop transforms (for reference; slice 1 only emits JSON, no runtime render):
  b1 `correct`→React `isTrue`; Cloze `text`→`passage`, blank `id`→`index`; Quiz `correct:index`→`options:[{text,correct:bool}]`.
- Validate each projected item with `jsonschema.validate` against activities-b1.schema.json before writing lesson.b1.json.

## Anchors (PRIVATE — local only, do not send raw to external APIs beyond the Gemma generation call)
- `.agent/tmp/hramatka/alona-text.txt` (mixed homework transcript). Real B1(-B2) prose:
  - lines 4681-4693 "10 причин читати!" (has real numeral government: 10 причин, дві третини, 17 ділянок, 2,5 рази) — PRIMARY anchor.
  - lines 12619-12631 "Жінка та чоловік…" (abstract, B1-B2). Lines 4640-4680 myth-busting drills (closer to clean B1).
  - Do NOT use lines ~12690-12730 (C1/C2). Level-check lemmas (query_cefr_level/atlas) before trusting as B1.
