# TRAIL 03 — Teacher beta: local proof → operator manual gate → deploy

Goal: a teacher-usable app with qualified models, proven locally, gated by the operator.
Blocked until trails 01 and 02 are DONE. Continues the seven-step path recorded on #4542.

## PRECONDITIONS
- Trail-01 DONE (selector fed by current receipts) and trail-02 DONE (report of record).
- #167 (grammar focus never reaches generation) resolved or explicitly waived by the
  operator — a focus-blind lesson fails the teacher-ready promise.

## VPS execution surface
The VPS is available at the driver's discretion for qualification runs, benchmark
bakes/soaks, report serving, and safe maintenance. This execution permission does
not authorize teacher-visible actions: deploys remain subject to the deploy bar,
and invite creation and arming remain subject to the stop gates below.

## STEPS
1. **Fresh local HTTPS teacher proof**: run the merged local teacher launcher (#250) on
   current main; paste → bake → open every activity type → accept → print, source-blind.
   verify: readiness green; lesson meets the #251 density floor; provenance shows a
   selector-eligible model; zero console errors.
   on-fail: file with receipts, escalate; do not retry-loop the app.
2. **Browser UI pass** by the assigned UI seat (T2 or a designated browser lane): every
   activity type exercised in the real UI, screenshots attached to the issue.
3. **Hand the operator the exact steps** for their own manual pass on their Mac
   (verified commands only — verify the local-loop UI path actually serves the teacher UI
   before writing the steps).
4. **WAIT.** The operator runs their manual pass.

## STOP GATES (absolute)
- **No VPS deploy, no invite creation, no arming, no catalog exposure without the
  operator's explicit, present-tense go.** These are never trail-inferable.
- Deploy bar (operator, 2026-07-16) binds: proper B1 lessons from real teacher texts, all
  activity types, no UI bugs, no semantic problems, proper Ukrainian.
- Any semantic doubt in a lesson shown to the operator → T2 first.

## DONE WHEN
Operator has explicitly armed the teacher invite after their own manual pass; deploy
executed per the pilot runbook (`hramatka/ops/pilot-vps.md`); post-deploy smoke green.
