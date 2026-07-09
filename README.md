# Hramatka — design + B1 engine archive (recovered 2026-07-10)

The complete Hramatka design and prototype state, moved here from gitignored
`.agent/tmp/hramatka/` in the public repo checkout (its designated home is THIS
private repo — fleet review + user decision 2026-07-08). Recovered 2026-07-10 after
the plan was nearly lost to local-only storage.

## What's here
- `app-spec-v1.md` — the app layer (teacher login, students, per-student difficulty,
  pedagogy-as-guardrail PPP/TTT/GPPC/TBL mined from the vibe requirements).
  Fleet-reviewed 2026-07-08 (codex + agy + cursor): SEPARATE app, adapter to V7
  components, home = this repo.
- `design-v0.md` — engine design (anchor → grounded activity bank → assembler).
  Vibe = requirements reference ONLY, no code reuse (user-confirmed 2026-07-07).
- `slice-1-build-plan.md` — fleet-reviewed build plan (codex + cursor + grok-build,
  GO-WITH-CHANGES, findings folded in).
- `slice-1-measurement-result.md` — PROOF: real Gemma 4 (google-ais $0 toolless),
  anchor → 3/3 gated activity types ship, 123 tests green, evidence-span repair,
  0 hallucinated items. This is the "local prototype evidence" issue #12 gates on.
- `engine/` — the working slice-1 Python engine + tests. `anchors/`, `.cache/`,
  `.out/`, and the raw teacher master text are EXCLUDED — machine-local only
  (teacher-privacy rule; regenerable from the local source).
- `scaleway-setup.md` — provisioning plan (pairs with `tasks/001-006`).
- Supporting briefs and review records (gate reviews, recon, prompts, numeral oracle).

## Key locked decisions (from the recovered 2026-07-07/08 conversations + reviews)
- Band = **B1 first** (A1–B2 eventually; B2 treated separately).
- Model = **Gemma 4 via google-ais, $0, toolless**; pattern = model proposes,
  **Python gates decide** (VESUM, numeral moat, evidence-span repair).
- KPI = would-a-teacher-accept-as-is rate.
- **Anchor-first**: teacher supplies a rich text; activities generate from it
  (later: our modules selectable as anchors).
- Quality bar: teachers must NOT need to edit constantly.
- Pedagogy (PPP/TTT/GPPC/TBL) = guardrail driving requested inputs + lesson shape.
- Voice + image generation = flagged interest, later slice.
- One server shared with the practice/atlas backend; infra stays private (security).
