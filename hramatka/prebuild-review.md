PRE-BUILD REVIEW (mandatory gate before we dispatch the build). You reviewed the earlier packet-contract
question; this is the RESHAPED + FINALIZED v1 plan (attached design doc). Poke holes BEFORE we build.

PLAN IN ONE BREATH: "Hramatka" = a Ukrainian teacher tool. Teacher supplies an ANCHOR text (paste URL/text)
→ Gemma 4 (`google-ais/gemma-4-31b-it`, TOOLLESS, $0 via AI Studio) generates VARIED activities derived
FROM the anchor's content → DETERMINISTIC gates verify each item (VESUM/sources; the numeral case-government
gate is the moat — model proposes a target tuple, Python+VESUM decides, never LLM-judged) → teacher
assembles/reorders/edits in a PRIVATE HF SPACE UI → STRUCTURED edit-capture (field-ops + reason tags,
reordering-as-signal) is the training-data loop. v1 = TEXT LOOP ONLY, ALL CEFR levels; multimodal
(photo-anchor OCR/handwriting + generated voice/image) = PHASE 2. Activity registry is OPEN/extensible.

Probe already done: Gemma 4 generated correct evidence-cited T/F + correct SIMPLE numeral government
(близько+gen→одного, п'яте) on a Львів anchor; all 9 key forms VESUM-valid, no russicism, $0. Hard numerals
(204337, 4.79%, fractions) untested — that's the gate's job.

ANSWER TERSELY, NUMBERED:
1. SMALLEST correct build order — what's the FIRST end-to-end vertical slice, and what comes after?
2. Biggest risk in THIS finalized plan I'm underweighting?
3. NUMERAL case-government renderer: build our own deterministic UA number-grammar (cardinals/ordinals/
   fractions/percents × 7 cases × 3 genders) or is there an existing UA lib to verify FIRST? Name it.
4. HF Space + Gemma-via-AIS-key + scale-to-zero: gotchas — secret handling inside a Space, cold-start
   latency for a teacher waiting, per-teacher invite auth?
5. Anything in the attached design that is WRONG or won't survive real teachers?
