# FLEET PRE-BUILD REVIEW — Hramatka Slice 1 concrete build plan (infra panel)

You reviewed the OVERALL Hramatka design earlier; this is the mandatory pre-build review of the CONCRETE
first-vertical-slice build plan BEFORE we dispatch implementation. Be adversarial — catch what a solo
designer (me) missed. This is infra/engine work: a local-first Python engine that turns a B1 anchor text
into gated, anchor-derived learning activities.

## Read these (local files, this machine, main checkout — you can read them directly)
- `.agent/tmp/hramatka/slice-1-build-plan.md`  ← THE plan under review (READ FIRST, ~200 lines)
- `.agent/tmp/hramatka/design-v0.md` + `app-spec-v1.md`  ← the already-reviewed design context

## One-paragraph recap
Slice 1 = one real B1 anchor → grounding-IN (reuse `data/atlas.db` payloads + `scripts/verification/
vesum.py`) → Gemma 4 (AIS $0, toolless, via `_invoke_opencode`) proposes `activities-b1`-shaped items with
evidence char-spans → deterministic Python gates DECIDE (evidence-span exact-substring; VESUM token
validity; numeral case-government) → project to renderable b1 JSON + measure would-a-teacher-accept rate on
~8-10 real B1 anchors. Model proposes, gate decides. The moat = our own deterministic numeral
case-government gate (no off-the-shelf UA lib composes numeral+noun government).

## ANSWER THESE 5 OPEN QUESTIONS (state a clear pick + one-line why for each)
1. **Numeral-probe:** include one CONSTRUCTED-numeral item in slice 1 so the numeral moat is actually
   exercised (extracted phrases are correct-by-construction → the gate barely fires otherwise), or keep
   slice 1 purely extractive and defer the moat test to slice 2? (my lean: include it)
2. **Engine IR vs schema edit:** carry evidence spans in the engine's own richer IR that PROJECTS to a
   pure `activities-b1` subset for rendering (public schema untouched), vs adding an optional evidence
   field to the public b1 schema? (my lean: IR — Hramatka is a separate private app)
3. **Numeral gate MVP scope:** CLASSIFY government class + VERIFY the existing noun form via VESUM (defer
   the generative digit→words-in-any-case renderer to the numeral-rewrite slice), vs build the full
   renderer now? (my lean: classify+verify now)
4. **Gemma transport:** shell out to the `opencode` CLI via `_invoke_opencode` for the prototype's
   generation call, vs write a thin direct Google-AIS client now? (my lean: subprocess now)
5. **FALSE-statement verification:** WARN + teacher-confirm for TrueFalse "false" statements (can't
   deterministically prove falsity), vs require an NLI/entailment check? (my lean: WARN now)

## ALSO SURFACE (this is where you earn your keep)
A. **Any LINGUISTIC error** in the numeral case-government rules in §6c (the compound-by-last-word logic,
   the nom-pl-vs-gen-pl-only-in-nominative-context rule, oblique-case override, тисяча/мільйон as nouns,
   trigger→case lexicon). This is the moat — a wrong rule here is the worst possible bug.
B. **Any missed FAILURE MODE** in the pipeline (grounding-IN, Gemma output contract, the gates, idempotency).
C. **Any INTEGRATION RISK** in the reuse plan (atlas.db payload access, VESUM Python path, `_invoke_opencode`
   coupling, projecting to activities-b1).
D. **Anything that would make a real Ukrainian teacher REJECT the output** that a gate won't catch.

## Reply format
Return: (1) the 5 answers, (2) A/B/C/D findings ranked by severity with the specific fix, (3) a one-line
GO / GO-WITH-CHANGES / NO-GO verdict on dispatching the build. Terse and concrete. No preamble.
