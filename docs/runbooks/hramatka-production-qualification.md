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

No real-provider mode is provided by this change. If one is added later, it
must be an explicit operator command with a separate spend acknowledgement,
must never run in tests, and must never auto-fallback to another provider or
route. Preserve expected/observed route binding across generation and every
repair trace entry. Do not populate shipped defaults from a no-cost run.
