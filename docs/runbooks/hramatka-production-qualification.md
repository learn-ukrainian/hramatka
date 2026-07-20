# Hramatka production qualification

## Purpose

This runbook defines the fail-closed evidence required before a logical teacher
model can be enabled. It is intentionally separate from deployment, provider
provisioning, and semantic language adjudication.

The shipped selector starts with `PRODUCTION_QUALIFICATION_RECEIPTS = ()`.
The deterministic harness in this repository proves the production path and
receipt aggregation only; it does not qualify a real provider and does not
spend provider credits.

## Immutable input contract

`hramatka/qualification/assets/b1-45m.manifest.json` is versioned and contains
only B1/45-minute metadata for exactly these IDs:

- `b1-narrative`
- `b1-dialogue`
- `b1-morphology`

For each ID it stores source identity and SHA-256 only. Do not commit the raw
teacher anchor, a generated lesson, a database, provider credentials, or
telemetry output. Obtain anchor bodies outside the repository, build
`RuntimeAnchor` inputs in memory, and hash-check them against the manifest
before a cell starts. Any identity, hash, extra-anchor, or missing-anchor
mismatch is a hard failure.

## No-cost path proof

The source-blind test uses the normal authenticated `POST /api/lessons` path,
durable `JobStore`, `BakeRunner`, and a real `EngineLessonBaker`. It injects
only a digest-verified local data bundle and deterministic provider boundary;
it never swaps in a fixture baker or directly calls the engine from the HTTP
driver.

Run the proof locally with:

```bash
.venv/bin/python -m pytest -q tests/test_production_qualification.py tests/test_qualified_model_selector.py
```

The harness executes these twelve exact cells:

| Logical model | Route ID |
| --- | --- |
| Gemini 3.5 Flash | `gemini-flash-ais` |
| Gemini 3.1 Pro | `gemini-pro-ais` |
| Gemma 4 31B | `gemma-ais` |
| Gemma 4 31B | `gemma-openrouter` |

For each of the three anchors, the content-free receipt binds source commit,
harness/manifest/anchor hashes, logical model, expected and observed route,
prompt/density/registry/engine/flag digests, density counts, route-bound
generation/repair trace, and outcome. The deterministic run intentionally
creates one shortfall so the ordinary generic slot-repair loop must run.

The path gate requires a durable ready job, eight visible blocks in the 3/4/1
phase distribution, at least 28 learner-response units, activity diversity,
and phase-three productive transfer. It also verifies route continuity for
each generation and repair call.

## Receipt validation and aggregation

The harness persists each strictly validated, content-free cell receipt under
its operator-provided runtime root; that directory is never committed. Keep
real-run receipt storage outside the repository too. Parse receipts through
`CellReceipt.from_dict()` and aggregate through `aggregate_receipts()` with the
current source commit, manifest hash, runtime anchor hashes, canonical
per-cell prompt hashes, and engine/flag digests.

Aggregation fails closed for a missing, stale, duplicate, mismatched, or failed
cell. Each configured route requires all three anchors; a logical model is
eligible only when every one of its configured routes has exactly one current
three-anchor aggregate. The selector independently rejects duplicate route
aggregates.

The deterministic path result marks `semantic_gate: not_run`. It does not make
claims about Ukrainian language quality, answer keys, or instructional
semantics. A separate deterministic analyzer/adjudication gate must produce
`semantic_gate: passed` for every real cell before a route aggregate may become
a `QualificationReceipt` and be considered for the production registry.

## Real-provider runs

The real mode is an operator-only command and is never invoked by pytest or by
the teacher API. It runs the exact fixed 3-anchor × 4-route matrix, not a
selected subset. Before it constructs a provider it requires a clean worktree,
the current source commit, the immutable manifest digest, all three external
anchor hashes, both production-path feature flags, every route's credential
source, separate scratch and receipt directories outside the repository, and a
spend acknowledgement bound to the current commit and manifest digest.

First obtain the exact acknowledgement string without running the command's
provider mode:

```text
HRAMATKA-QUALIFICATION-SPEND:<current-head>:<manifest-sha256>:B1-45M-3x4
```

The operator then supplies that exact value together with
`--execute-real-provider` to
`.venv/bin/python -m hramatka.qualification.live`. The anchor JSON is an
operator-local object containing only `id`, `source_identity`, and `text` for
the three manifest anchors. It must not be stored in the repository. Receipt
and scratch roots must also be outside the repository.

Each cell constructs one pinned provider port. Gemma's normal production
failover and all round-robin selection are deliberately bypassed: a provider
failure is a failure of that same cell, never evidence for a sibling route.
The ordinary `BakeRunner` may repeat a failed whole bake, but every initial and
repair call remains pinned to the same route and is recorded as content-free
route telemetry.

The run deletes its per-cell SQLite/cache/generated-content scratch directory
and persists only validated content-free receipts. It leaves
`semantic_gate: not_run`, invokes no semantic reviewer, and cannot populate
the shipped production receipt defaults. Semantic adjudication is a separately
authorized later gate that must repeat provenance validation before any call.

Real cells use the production 1,800-second bake hard timeout and keep the
authenticated API lifecycle open for 1,830 seconds, rather than the no-cost
test harness's 600-second/20-second bounds. A cell that has not reached a
terminal durable state by that deadline is refused; a terminal failed job is
also refused. The command exits nonzero unless all twelve cells are present,
passed, and remain `semantic_gate: not_run`; it reports only anchor/route IDs,
never lesson or provider content.

After each TestClient lifecycle stops its runner, live qualification waits up
to one provider timeout plus a 30-second margin before deleting that cell's
scratch directory. Pinned qualification transports make one HTTP attempt only;
the ordinary whole-bake retry remains route-pinned. If a worker still has not
stopped, the command fails without naming a filesystem path and preserves that
one external scratch directory. Do not delete preserved scratch while the
process is alive. After the qualification process has exited and an operator
has confirmed no Hramatka worker remains, remove the preserved external
scratch directory manually before starting a fresh run.
