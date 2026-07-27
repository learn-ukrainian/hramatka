# Hramatka Driver Trails

Phase declaration (operator order, 2026-07-27): the hramatka epic (#4542) is driven by
**trails** — exact, verifiable step sequences authored by a frontier seat — so that
non-frontier models can drive the epic mechanically. Quality does not live in the driver's
judgment; it lives in deterministic gates, hash-paired evidence, and the stop rules below.

## Driver tiers

| Tier | Who | May do |
|---|---|---|
| T0 executor | any code lane (codex, agy, grok, glm-local, kimi) | follow trails verbatim: run commands, compare against the written verify, fill dispatch templates, post receipts |
| T1 reviewer | cross-family lane (family ≠ author's) | code review per the trail's checklist; verdict + receipt URL |
| T2 frontier | Fable / Sol only | Ukrainian-content verdicts, any gate/floor/threshold change, architecture, anything teacher-visible, authoring or editing trails, contested calls |

## Iron rules (binding on every tier)

1. **Floors and gates are minimums.** Never lower, soften, special-case, or bypass one.
   A red gate is a STOP, not an obstacle.
2. **No Ukrainian-language judgment below T2.** Word validity, stress, morphology,
   russianisms: `mcp__sources__*` tools only, and any doubt escalates. A weak driver never
   scores lesson content.
3. **Teacher-visible actions** (invite, arming, deploy, catalog changes) happen only on the
   operator's explicit, present-tense go. It is never inferable from a trail.
4. **Verify mismatch = STOP.** If a step's output does not match its written verify,
   stop and escalate. Never improvise around a failed step; never re-run to make it pass.
5. **Private-repo merges are manual**: exact-head green CI + cross-family review receipt
   URL quoted in the PR. Auto-merge is forbidden here.
6. **Every claim tool-backed**: quote command + cwd + raw output. A peer's "verified" is
   not evidence; receipts are.
7. **Reviews launch from a neutral cwd** (`.agent/tmp/review-scratch/`), diff piped;
   reviewers never check out branches in a primary checkout.

## Surfaces — what exists and what is canonical

| Surface | Role | Driver rules |
|---|---|---|
| Public repo (`learn-ukrainian.github.io`) | curriculum + shared tooling; GitHub canonical | only the published boundary artifacts (activity-kit, schemas) touch it; hramatka internals (anchors, prompts, lessons, teacher data, host details) NEVER go public |
| Private repo (`learn-ukrainian-infra-private`) | hramatka engine/api/app/ops — canonical for this epic | manual merges only, cross-family review, worktree discipline |
| Pilot VPS (runbook `hramatka/ops/pilot-vps.md`) | the ONE live host; in-place deploys, no staging/blue-green to switch to | available at the driver's discretion for qualification runs, benchmark bakes/soaks, report serving, and safe maintenance; host ops are dispatched with a brief, never improvised; teacher-visible actions remain bound by the trail-03 stop gates |
| entire.io (Phase 1 pilot — `docs/entire-adoption-dossier.md`, issue #268) | ADDITIVE checkpoint/provenance layer; **GitHub remains canonical; agents do NOT switch fetch/push paths** | never fetch/push via Entire mirrors; never publish transcripts/checkpoints on a public-repo branch; pilot scope is one Claude lane with isolated hooks — other lanes stay off it |

## Escalation

Stop the trail, comment on the trail's issue with the failing step + quoted output, and
ping the frontier seat via the agent bridge. Do not proceed past a stop.

## Trail format

Each trail: PRECONDITIONS (checkable commands) → numbered STEPS, each
`do:` (exact command/template) + `verify:` (expected output) + `on-fail:`
(stop | retry-once | escalate) → STOP GATES → DONE WHEN (deterministic predicate).
Only T2 creates or edits trails; drivers execute them exactly as written.

## Index

| Trail | Goal | State |
|---|---|---|
| [trail-01](trail-01-qualification-matrix.md) | #258 qualification matrix: land the density fix, complete the 12-cell run, feed the #244 selector | active (codex dispatch in flight) |
| [trail-02](trail-02-benchmark-v3.md) | Benchmark v3: land the report layer, run all seats + reference seats, blind cross-family judging, regenerate the report of record | active (agy dispatch in flight) |
| [trail-03](trail-03-teacher-beta.md) | Teacher beta: local HTTPS proof → operator manual gate → deploy | blocked on trails 01–02 |

Templates: [templates/dispatch-brief.md](templates/dispatch-brief.md)
