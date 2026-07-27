# TRAIL 02 — Benchmark v3: report layer → full seat run → blind judging → report of record

Goal: replace the invalid 2026-07-23 benchmark page with a v3 report whose every row is
hash-paired, artifact-backed, and generated from judge files.
In-flight dispatch: `benchmark-v3-report` (agy, worktree
`.worktrees/dispatch/agy/benchmark-v3-report`).

## PRECONDITIONS
- `jq -r .status /Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/benchmark-v3-report.json` returns `done`; otherwise escalate with the log tail. Known infra bug: `no_deliverable` with reason `commit_count_unknown` can be a FALSE negative on a private-repo dispatch — check for an opened PR and unpushed worktree commits before treating it as a failure.

## STEPS
1. **Locate PR + confirm RED-proofs** (pairing refusal, link-integrity failure, fidelity property, density fixture) are quoted in the PR body. Missing → back to author lane.
2. **Route T1 review** (reviewer family ≠ Google): codex or grok-4.5 or Claude seat; neutral-cwd diff via bridge. Checklist: pairing gate refuses mismatched anchor-SHA; report renders only from judge JSON; missing artifact fails the render; density numbers come from the #251 analyzer import, not a reimplementation.
3. **Merge** per the standard private manual-merge checklist (trail-01 step 5).
4. **Run the benchmark** (after trail-01 step 5 has merged the density fix, so seats bake against current engine):
   do: `.venv/bin/python -m hramatka.ops.tooled_author --anchor <operator-local-anchor-file> --seats gpt-5.6-ref,opus-5-ref --mode live-tools --out-dir .agent/tmp/benchmark-v3/<round>-tooled` and `.venv/bin/python -m hramatka.ops.tooled_author --anchor <operator-local-anchor-file> --seats gpt-5.6-ref,opus-5-ref --mode bare --out-dir .agent/tmp/benchmark-v3/<round>-bare` (the rep2 runner entrypoint; use the same two commands for each configured product-seat group, replacing only `--seats` and the fresh `--out-dir`).
   - Seats: the four #258 product routes + reference seats gpt-5.6 (OpenAI-compat) and Opus 5 xhigh (Anthropic-compat). Reference-seat model ids are set in config by T2 at run time; key absent → honest skip recorded.
   - Every seat: fresh bake, same frozen anchor manifest, tooled arm (one `vet_vocabulary` prefetch) + bare baseline arm, full transcript + provenance persisted.
   verify: per-seat bundle builds without pairing refusal; refusal → that seat's bake is mispaired, STOP for that seat, record, continue others.
5. **Judge routing (blind, cross-family, no self-family):**
   - Google-authored seats (gemini/gemma) → judges: Claude T2 + gpt-family.
   - OpenAI-authored (gpt-5.6) → judges: Claude T2 + gemini-family or grok-4.5.
   - Anthropic-authored (Opus 5) → judges: gpt-family (Sol) + gemini-family or grok-4.5. **No Claude-family judge on this row.**
   - Judges receive only the blind bundle; verdicts as structured JSON per the report schema; every language claim carries a `mcp__sources__*` receipt per the LLM-QG contract.
6. **Render the report**.
   do: `.venv/bin/python -m hramatka.ops.benchmark_report render --judges-dir <operator-local-judges-dir> --out <operator-local-judges-dir>/index.html`
   verify: the command prints `Benchmark report rendered: <operator-local-judges-dir>/index.html`; link-integrity is green and a density row is present per seat. Serve locally and hand the URL to the operator.
7. **Verdict of record**: T2 (Fable) countersigns PASS rows only — zero learner-facing language errors AND all keys correct AND #251 density floor met, each backed by receipts.

## STOP GATES
- A judge scoring its own family → discard that verdict, re-route.
- Any hand edit to the rendered report → forbidden; fix inputs, re-render.
- Reference-seat spend is one bake per arm per seat; more → operator.

## DONE WHEN
The v3 report is the served page, every row artifact-backed, judge files on disk, and the old 07-23 page is archived with a pointer to v3.
