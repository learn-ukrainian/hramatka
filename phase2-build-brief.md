# Phase 2 build brief — Hramatka engine pipeline (on the VERIFIED moat)

Build the rest of the slice-1 engine ON TOP of the already-built, independently-verified numeral gate.
Same governance as Phase 1: build ONLY under `.agent/tmp/hramatka/engine/` (gitignored — NO git add/commit/
push, NO worktree, NO redirects into the checkout; Write tool for files; `.venv/bin/python -m pytest` to test).

## READ FIRST (your spec)
- `.agent/tmp/hramatka/slice-1-build-plan.md` — esp. §2 (IR), §3 (pipeline), §4 (retrieval), §5 (generate),
  §6a (evidence-span REPAIR), §6b (VESUM token gate), §7 (per-type chains), §10/§10b (measure), **§R (HARDENING — all mandatory)**.
- `.agent/tmp/hramatka/engine-integration-recon.md` — exact signatures/keys/paths.
- `.agent/tmp/hramatka/gen-prompt-draft-extractive.md` — fold into `prompts/extractive.md`.
- ALREADY BUILT (reuse, do not rewrite): `engine/paths.py`, `engine/gates/vesum_tags.py`,
  `engine/gates/numeral.py` (`check_numeral_government(phrase, context_case=None) -> Result{status,rule,expected,detail}`).

## BUILD
```
engine/
  schema.py          # HramatkaActivity IR dataclass (activity, evidence[], provenance, gate_result);
                     #   project_to_b1(ir) -> dict (pure activities-b1 item, evidence STRIPPED);
                     #   validate_b1(obj) via jsonschema against schemas/activities-b1.schema.json (§R).
  retrieval.py       # atlas lemma dict (ONE pass, {lemma→payload}), lemmatize anchor (pymorphy3/VESUM),
                     #   numeral-inventory extraction, build the grounding pack (<~1500 tok). §4 + §R B1/C1.
  generate.py        # generate(anchor, level, types, *, generator=call_gemma) -> list[raw activity dict].
                     #   call_gemma wraps _invoke_opencode(agent="chat", model=GEMMA_MODEL, output_format="json",
                     #   default_timeout_s=900); catch SystemExit -> raise GeneratorUnavailable; parse first
                     #   balanced {..}; retry once; 2nd fail -> mark unparseable. **generator is INJECTABLE**
                     #   so tests use a MOCK returning fixture JSON (NO real Gemma in unit tests).
  gates/evidence_span.py  # §6a REPAIR: locate quote in anchor (NFC + ws-collapse), RECOMPUTE offsets;
                          #   absent -> FAIL for extractive types (not warn). Returns per-item verdict.
  gates/vesum.py     # §6b: verify model-INTRODUCED tokens (non-anchor-verbatim) via verify_words; russianism=warn.
  pipeline.py        # §3: snapshot anchor -> grounding-IN -> generate -> gate chain per §7 -> project+validate
                     #   -> write lesson.b1.json + lesson.ir.json. Idempotency key = full inputs fingerprint (§R).
  measure.py         # §10: run pipeline over anchors, emit measure-report.html (rendered item + evidence +
                     #   gate verdicts for human accept-scoring); track gate-pass, russianism-warn rate,
                     #   B1-B2 tags, numeral positive-probe pass + offline NEGATIVE-fixture recall bank.
  prompts/extractive.md   # from the draft; UA-first (#M-13); strict JSON contract; evidence spans required.
  tests/             # unit tests with MOCK generator (deterministic fixture activities incl. one numeral
                     #   positive probe + the offline negative bank); evidence-span repair cases (good offset,
                     #   bad offset but quote present -> repaired, quote absent -> fail); projection+jsonschema
                     #   validation (correct->isTrue etc.); cloze pre-gap-span rule. NO network in tests.
```

## HARD REQUIREMENTS
- Everything in §R applies (path/preflight, lemmatization, {lemma→payload} once, SystemExit guard, jsonschema
  validate before write, cloze pre-gap span, fingerprint cache, MatchUp UA synonyms preferred).
- #M-4: any UA form in a fixture = VESUM-verified. NO real Gemma call in pytest (inject a mock). The pipeline
  MUST run correctly invoked from repo root AND from the engine dir.
- Do NOT modify the built moat files except to IMPORT them.

## REPORT
Files created; raw `pytest -v`; how to run one real end-to-end generation (the exact command I'll use with
real Gemma); anything underspecified + the choice made. Don't claim done for anything pytest didn't prove green.
