# TASK: <one-line goal>

Repo: learn-ukrainian-infra-private (you are IN a dispatch worktree; never touch the
primary checkout). Parent issue: <#N>.

## Evidence discipline (#M-4 — binding)
Every claim: deterministic tool + quoted raw output (command + cwd + output). Test
results, counts, receipt fields, SHAs — quoted, never asserted. No theory without
receipts: if you cannot show the number, say so and measure first.

## Known state (verify, do not trust)
<bullet facts with their receipts/refs>

## Steps
1. <read context: issue, files, receipts>
2. <measure before fixing — quote the numbers>
3. <implement at the evidence-named layer, with a RED-on-old regression test; prove RED once, quoted>
4. Focused tests + full private pytest suite + ruff; quote counts.
5. Conventional commit + X-Agent trailers, push branch, open PR to private main.
   PR body: evidence-quoted summary. NO auto-merge — private merges are manual after
   cross-family review.

## Hard constraints
- Never lower/relax any floor, gate, or threshold.
- No generated lessons, secrets, teacher anchors, or telemetry DBs committed.
- Do not touch paths owned by parallel tasks: <list>.
- `.venv/bin/python` only. Work only inside this worktree.

<!-- Dispatcher fills: agent per model-assignment fit; --research-role/-task-family/-track/
-owned-path flags; --mode danger requires this worktree; arm a Monitor settle-watch on
batch_state/tasks/<task-id>.json (terminal: anything outside {spawning,running,""};
done = success). -->
