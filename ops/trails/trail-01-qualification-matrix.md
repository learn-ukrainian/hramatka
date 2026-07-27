# TRAIL 01 — #258 qualification matrix to a populated #244 selector

Goal: the density root-cause fix is merged, the real 12-cell matrix has current receipts,
and only receipt-backed models reach the teacher selector.
Issue: private #258. In-flight dispatch: `258-density-matrix-v2` (codex, worktree
`.worktrees/dispatch/codex/258-density-matrix`).
History: the v1 dispatch STOPPED correctly — the 07-20 run's receipts were destroyed
(receipt count 0), the no-cost proof was red on main (`require_session` ImportError), and
the flash route rotated to `gemini-3.6-flash` in config while engine routes/tests lagged.
v2 authorizes the integration fix and uses ONE fresh instrumented cell as the density
measurement source.

## PRECONDITIONS
- `jq -r .status /Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/258-density-matrix-v2.json`
  returns `done`. Any other settled status → escalate with the task log tail.
  Known infra bug: `no_deliverable` with reason `commit_count_unknown` can be a FALSE
  negative on private-repo worktrees — always check for an opened PR and unpushed
  worktree commits before treating it as a failure.

## STEPS
1. **Locate the PR.**
   do: `gh pr list -R learn-ukrainian/learn-ukrainian-infra-private --state open --search "258" --json number,title,headRefName`
   verify: exactly one PR whose branch is `codex/258-density-matrix`.
   on-fail: check the worktree for finished-unpushed work (`git -C <worktree> log --oneline -3`, `git status`); if work exists unpushed, escalate; if no work, escalate with dispatch log.
2. **Confirm the floor is untouched.**
   do: `git -C <worktree> diff origin/main --stat -- '*density*' '*floor*'` and read the PR diff for the density-contract constants.
   verify: 8-block / ≥28-unit floor values unchanged. Any change to a floor or gate constant → STOP (T2 only).
3. **Confirm the RED-proof is quoted in the PR body** (fix reverted → regression test fails). Missing → request it from the author lane; do not review without it.
4. **Route T1 review** (reviewer family ≠ codex/OpenAI): gemini-family or grok-4.5 or Claude seat. From `.agent/tmp/review-scratch/`, generate the diff with `git -C <repo> diff origin/main...<branch>` and send via bridge ask. Review must answer: root-cause layer named with receipt evidence; RED-proof present; floors untouched; no secrets/lessons committed; suite counts quoted.
   verify: verdict APPROVE with a receipt URL (PR comment).
5. **Merge** (manual): `gh pr checks <n>` all green on the exact head, review receipt URL in PR → `gh pr merge <n> --squash` → delete remote branch → `git worktree remove` the dispatch worktree → delete local branch.
6. **Verify matrix receipts on main**.
   do: `.venv/bin/python -m hramatka.qualification.receipts aggregate --receipt-dir <operator-local-receipt-root>/receipts`
   verify: it prints `Qualification receipts aggregated: 4 routes` plus one `anchors=3/3` line per configured route only when every cell is current, passed, and teacher-ready; otherwise it refuses with `QualificationError`. Quote the aggregate lines or the refusal on issue #258.
7. **Feed #244**: only routes with current aggregate receipts become selector-eligible. Post the eligible set + receipt refs on #258 and close it if all acceptance boxes hold.

## STOP GATES
- Any cell that passes only via a floor/gate relaxation → STOP.
- google-ais 503 storms: retry policy as implemented, then park the cell and record it; never hammer.
- New paid spend beyond the 12-cell matrix → operator.

## DONE WHEN
#258 acceptance boxes all check with quoted receipts, the eligible-model set is posted, and the selector consumes only current receipts.
