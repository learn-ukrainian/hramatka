# Independent advisory review

The recovered archive is authoritative. Where it conflicts with my earlier advisory packet, the archive’s locked and fleet-reviewed decisions win.

Two internal archive conflicts also need chronological resolution:

- The private app belongs in `learn-ukrainian-infra-private`, not the public Astro repository. [app-spec-v1.md:9](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:9)
- The later Scaleway DEV1-M decision supersedes the older HF Space sections. [scaleway-setup.md:51](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/scaleway-setup.md:51)
- The product model retains students, difficulty and assignment as future capabilities, but v1 explicitly stores no student records or server-side progress. [app-spec-v1.md:155](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:155)

This was read-only. No files changed. Current checkout: `main`, clean, one commit behind `origin/main`. I could not freshly rerun pytest/ruff because the read-only environment could not create temporary/cache files; the “123 tests green” statement below is archive evidence, not a new test run. [slice-1-measurement-result.md:6](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:6)

## Item 1 — Public/private separation

### Coupling inventory

| Coupling point | Severity and classification | Required boundary |
|---|---|---|
| V7 React activity widgets | **[CRITICAL] Dangerous today.** The archive explicitly forbids private imports from `site/src`, while the widgets themselves import site-local CSS, helpers and UI components. [app-spec-v1.md:10](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:10) [TrueFalse.tsx:1](/Users/krisztiankoos/projects/learn-ukrainian/site/src/components/TrueFalse.tsx:1) | Publish a self-contained `@learn-ukrainian/activity-kit` package containing selected widgets, CSS, assets, public props and the runtime dispatcher. The private frontend depends only on its exact package version. |
| Schema-to-widget conversion | **[HIGH] Dangerous if bypassed.** Activity JSON is not widget props: the current static adapter converts `correct → isTrue`, `text → passage`, and blank IDs to indices. [yaml_activities.py:1810](/Users/krisztiankoos/projects/learn-ukrainian/scripts/yaml_activities.py:1810) [yaml_activities.py:1820](/Users/krisztiankoos/projects/learn-ukrainian/scripts/yaml_activities.py:1820) | Put those mappings in the exported `ActivityPlayer`, with fixture tests. Do not duplicate them in Hramatka or feed schema objects directly to React components. |
| Activity schemas | **[HIGH] Dangerous today.** Slice 1 opens a mutable checkout path directly. [schema.py:25](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/schema.py:25) | Export a bundled, immutable `lu.activity.v1` JSON Schema plus generated TypeScript/Python types. Give the schema a versioned `$id`; do not consume mutable `schemas/activities-b1.schema.json` from `main`. |
| `LexiconPractice` runtime player | **[HIGH] Dangerous to generalize in place.** It is tightly coupled to SRS deck types, weak-area utilities, level storage and ten modes. [LexiconPractice.tsx:1](/Users/krisztiankoos/projects/learn-ukrainian/site/src/components/LexiconPractice.tsx:1) [app-spec-v1.md:89](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:89) | Extract the small dispatcher/event interface; do not import the whole component into the teacher app. Hramatka’s editor remains private and separate from the learner player. |
| FSRS/SRS core | **[MEDIUM] Clean as a versioned adapter.** It is storage-oriented but currently lives under site internals and carries its own data versions. [srs.ts:12](/Users/krisztiankoos/projects/learn-ukrainian/site/src/lib/lexicon/srs.ts:12) [srs.ts:24](/Users/krisztiankoos/projects/learn-ukrainian/site/src/lib/lexicon/srs.ts:24) | Export `@learn-ukrainian/practice-core`; keep v1 progress browser-local. Do not add a Hramatka student-progress backend in slice 2. |
| Pedagogy, immersion and level/type mappings | **[HIGH] Dangerous if loaded from repo docs or internal Python tables.** Pedagogy drives phases and permissible activity types. [app-spec-v1.md:55](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:55) [app-spec-v1.md:69](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:69) | Export a reviewed `lu-pedagogy-policy.v1.json`. Private prompts may consume it; private code must not scrape public documentation at runtime. |
| `atlas.db` | **[HIGH] Clean only as a read-only, versioned data snapshot.** The prototype opens the public-repo path and scans `is_public_route=1`. [retrieval.py:152](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/retrieval.py:152) | Build `atlas-public.sqlite` from admitted public rows, attach a SHA-256 manifest, and mount it read-only. Never mount a developer’s mutable `data/atlas.db`. |
| `vesum.db` and verification API | **[HIGH] Dangerous today.** The engine imports `scripts.verification.vesum` and discovers the DB through the public checkout. [retrieval.py:20](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/retrieval.py:20) [vesum.py:19](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/gates/vesum.py:19) | Export a stable `learn_ukrainian_linguistics` wheel plus a content-addressed, read-only VESUM snapshot. No `sys.path` bootstrapping in production. |
| `sources.db` | **[CRITICAL] Never a public artifact.** It is approximately 1.8 GB locally and includes eight non-redistributable private reference sources. [corpus-inventory.md:15](/Users/krisztiankoos/projects/learn-ukrainian/docs/corpus-inventory.md:15) [corpus-inventory.md:102](/Users/krisztiankoos/projects/learn-ukrainian/docs/corpus-inventory.md:102) | Build it only in protected infrastructure and publish it only to a private artifact store. Mount it read-only on Scaleway. A public release may contain only a schema/license manifest and checksum—not the DB. |
| Sources MCP | **[HIGH] Clean as a versioned service; dangerous as a checkout import.** The server exposes a local MCP interface but also resolves `data/sources.db` from its source tree. [server.py:1612](/Users/krisztiankoos/projects/learn-ukrainian/.mcp/servers/sources/server.py:1612) [server.py:2268](/Users/krisztiankoos/projects/learn-ukrainian/.mcp/servers/sources/server.py:2268) | Publish a pinned `sources-mcp` image/wheel with an explicit `LU_SOURCES_DB_PATH`. Bind it to loopback only and mount the protected snapshot. Slice 2 need not make it a hard dependency until an activity actually uses it. |
| Hramatka prompt templates | **[HIGH] Private ownership.** Runtime prompts include the grounding pack and full teacher anchor. [generate.py:145](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/generate.py:145) | Keep prompts in the private backend, version them independently, and include the prompt digest in every bake fingerprint. Never emit prompt dumps to public CI, issues or telemetry. |
| Hramatka gates and IR | **[HIGH] Private ownership, consuming stable public foundations.** The engine’s richer IR intentionally contains evidence, provenance and gate details. [schema.py:91](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/schema.py:91) | Move the Hramatka gate registry, pipeline and IR to the private repository. Only generic linguistic primitives need a public versioned wheel. |
| Generator transport | **[HIGH] Dangerous production coupling.** The proof imports private `_invoke_opencode` and expects a key in a developer-home path. [generate.py:36](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/generate.py:36) [paths.py:47](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/paths.py:47) | Preserve the locked Google-AIS/toolless behavior, but implement a private `GeneratorPort` adapter. The production worker receives its AIS secret from Scaleway Secret Manager. |
| Future module anchors and Practice/Atlas content | **[MEDIUM] Clean only as a published snapshot/API.** Module-as-anchor is explicitly deferred. [design-v0.md:72](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/design-v0.md:72) | Later consume a versioned content export or authenticated API. Never mount the public repository and traverse curriculum paths at runtime. |

### Concrete boundary contract

Proposed public releases:

- `@learn-ukrainian/activity-kit@1.x`
  - `ActivityPlayer`
  - selected V7 widgets, CSS and assets
  - `lu.activity.v1.schema.json`
  - generated TypeScript types
  - canonical fixtures and completion/edit events

- `@learn-ukrainian/practice-core@1.x`
  - FSRS scheduling
  - browser-local storage adapter
  - versioned card/progress types

- `learn_ukrainian_linguistics==1.x`
  - stable VESUM query API
  - generic normalization and morphology primitives
  - no Hramatka prompts, teacher data or provider transport

- `lu-pedagogy-policy.v1.json`
  - level/immersion policy
  - pedagogy phase definitions
  - permitted activity types by band

- `sources-mcp:1.x@sha256:<digest>`
  - code only
  - explicit DB-path configuration
  - no database or secrets embedded

Protected—not public—data release:

```text
lu-runtime-data-2026.07.10-<sha256>.tar.zst
├── manifest.v1.json
├── sources.db
├── vesum.db
└── atlas-public.db
```

The manifest must include per-file digest, size, schema version, source-code commit, license classification, allowed route, build timestamp and integrity-check result.

Private repository ownership:

- `hramatka-web`
- `hramatka-api`
- bake worker and durable queue
- Hramatka prompts, gates, IR and edit model
- teacher accounts, lessons, anchors, edit events and exports
- provider/auth/TLS/backup secrets
- server configuration, domains, IPs, firewall and backups
- future student/assignment/progress data

Direction-of-flow rules:

1. Public → private only through reviewed, immutable, digest-pinned artifacts.
2. The server never follows public `main`, clones the public checkout at runtime or accepts floating package ranges.
3. Public CI receives no Scaleway credentials, deployment keys or protected-data credentials.
4. Private CI promotes an allow-listed digest after contract tests; a new public release does not deploy automatically.
5. Private runtime data never flows back to GitHub. Any upstream contribution is manually curated, de-identified and reviewed.
6. The browser receives the minimum activity/render data. Raw grounding packs, evidence archives and model diagnostics remain backend-only.
7. Corpus DBs are read-only; mutable Hramatka state is a separate database and backup stream.

Public release CI should enforce schema compatibility, fixture validation, schema→adapter→widget rendering, Playwright grading tests, forbidden `site/src` imports, SBOM generation and signed provenance. Private CI should pin artifact digests, replay canonical fixtures, test frontend against API N and N−1, run leak/secret scans, validate DB digests and execute the Hramatka canary before promotion.

### Private app/backend deployment

Yes—the frontend and backend can deploy and evolve independently on one DEV1-M, provided “one server” does not become “one process.”

| Process | Bind | Responsibility |
|---|---:|---|
| Caddy/nginx | `:80`, `:443` | TLS, host/path routing, static Hramatka frontend |
| `hramatka-api.service` | `127.0.0.1:8080` | Auth, lessons, jobs, editor state, history |
| `hramatka-worker.service` | no listening port | One concurrent Gemma bake, gates, persistence |
| `lu-sources.service` | `127.0.0.1:8766` | Read-only Sources MCP |
| `atlas-practice-api.service` | `127.0.0.1:8081` | Public/read-only Atlas and Practice data if needed |
| backup/health timers | no port | Online SQLite backup, integrity checks, readiness |

Recommended storage:

```text
/srv/lu-data/releases/<data-digest>/   read-only corpus bundle
/var/lib/hramatka/hramatka.db          mutable teacher/job/lesson state
/var/lib/hramatka/releases/            private backend releases
/var/www/hramatka/releases/            independently deployed frontend bundles
```

Use same-origin `/api/v1` routing, not permissive CORS. The API contract should be versioned and support the current and previous frontend release. Only the worker receives the AIS key; the frontend and sources service do not. SQLite WAL belongs on the local block filesystem, not an object mount. That removes the archived HF bucket/fsync risk. [app-spec-v1.md:18](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:18)

DEV1-M remains the correct cheap start. [scaleway-setup.md:53](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/scaleway-setup.md:53) It should, however, launch with one bake worker, bounded SQLite caches and systemd memory limits; the three current databases total roughly 2.8 GB on disk, so “comfortable” memory headroom still needs a concurrent-service load test before teacher access.

### Leak audit

**[CRITICAL] The present exclusion list protects the recovered prototype but is not a complete production control.**

The current public repository correctly ignores databases, `.agent/`, `batch_state/`, private references and common secret files. [.gitignore:94](/Users/krisztiankoos/projects/learn-ukrainian/.gitignore:94) [.gitignore:109](/Users/krisztiankoos/projects/learn-ukrainian/.gitignore:109) [.gitignore:130](/Users/krisztiankoos/projects/learn-ukrainian/.gitignore:130) [.gitignore:208](/Users/krisztiankoos/projects/learn-ukrainian/.gitignore:208)

That is why the current anchors, raw Gemma cache, IR files and HTML reports remain ignored. But `.gitignore` is not a security boundary, and these additional categories must be explicitly denied in the private repo’s public-export and CI policies:

- teacher identities, token hashes, sessions and auth-throttle records
- student identities, pseudonyms, difficulty and progress—even when introduced later
- anchors, URLs, snapshots, hashes and evidence spans
- generated lessons, teacher edits, reason tags and acceptance telemetry
- prompt/grounding dumps, raw model responses, IR and gate diagnostics
- `sources.db`, private/unlicensed excerpts and private-source locators
- AIS keys, invite/access tokens, session/CSRF secrets, TLS keys and backup credentials
- exact server IPs, DNS inventory, SSH users, instance IDs, Secret Manager identifiers and firewall state
- logs, crash traces and provider errors that may echo requests
- local and off-box backups, SQLite WAL/SHM files and restore rehearsals

Generic architecture, service names and loopback ports are safe to document publicly; deployed addresses and credentials are not.

Before any private-to-public export, CI must run secret scanning plus a content sentinel for teacher names, local paths, anchor text, `lesson.ir.json`, prompt dumps and private-source identifiers. The existing private-source rule already provides the right standard: no raw files, paths, transcripts, prompts, screenshots or teacher-identifying labels. [word-atlas-private-teacher-source-scope.md:34](/Users/krisztiankoos/projects/learn-ukrainian/docs/runbooks/word-atlas-private-teacher-source-scope.md:34)

## Item 2 — Slice-2 build readiness

### What slice 1 genuinely de-risked

- **[LOW risk] Google-AIS Gemma transport and parseability:** real, toolless Gemma produced clean JSON on the first recorded call. [slice-1-measurement-result.md:8](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:8)
- **[LOW risk] The bounded B1 extractive mechanism:** two dissimilar anchors produced 6/6 shippable activities after per-item salvage. [slice-1-measurement-result.md:41](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:41)
- **[LOW risk] Evidence reconstruction:** offsets are recomputed from literal anchor spans rather than trusted from the model. [evidence_span.py:33](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/gates/evidence_span.py:33)
- **[LOW risk] IR → public activity projection:** evidence is kept out-of-band and the learner object is schema-validated. [schema.py:133](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/schema.py:133) [schema.py:168](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/schema.py:168)
- **[MEDIUM risk] Numeral classification/verification:** the offline bank passed four correct cases and rejected five incorrect cases. It does not prove a generative all-case numeral renderer, which remains deferred. [slice-1-measurement-result.md:46](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:46) [slice-1-build-plan.md:156](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-build-plan.md:156)
- **[LOW risk] Per-item salvage:** a bad statement/pair can be flagged while good siblings remain usable. [pipeline.py:275](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/pipeline.py:275)

### Genuine unknowns

- Runtime `ActivityPlayer`, editor, drag/reorder/prune flow and fixture compatibility—the archive calls this the largest app gap. [app-spec-v1.md:12](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:12)
- Pedagogy phase construction and phase bypassing; slice 1 had no pedagogy or phase layer. [slice-1-build-plan.md:17](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-build-plan.md:17)
- Real teacher acceptance. The recorded 3/3 judgment is an internal sample judgment, and pedagogy review remains owed. [slice-1-measurement-result.md:33](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:33)
- The planned 8–10 anchor measurement; only two anchors are recorded.
- Authentication, durable jobs, SQLite lease recovery, editor persistence, backups and restart behavior.
- URL ingestion, SSRF/content sanitization and snapshot licensing.
- A1/A2, B2+, seminars, TTT and CLIL behavior. The proven slice is B1 only.
- Full Sources MCP integration; the proven engine uses Atlas and VESUM directly, not model tool calls.
- DEV1-M memory and latency under API + sources + Atlas + one bake worker.
- Practice-deck conversion and browser-local SRS handoff.

### App assumptions invalidated by measurement or code

1. **[HIGH] “Gate passed” cannot mean “accept as-is.”** False T/F statements intentionally warn but still ship, while the reviewed plan says teacher confirmation must block accept-as-is. [pipeline.py:140](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/pipeline.py:140) [slice-1-build-plan.md:261](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-build-plan.md:261)  
   **Recommendation:** expose `clean | review_required | failed`, not one `passed` boolean. Warnings must be actionable and prevent automatic acceptance.

2. **[HIGH] Numeral-gate coverage was not “finished.”** Real output exposed a mixed-fraction false positive and then a date false positive. [slice-1-measurement-result.md:25](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:25) [slice-1-measurement-result.md:53](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:53)  
   **Recommendation:** retain the current special-case fixes, then add a regression corpus for dates, ranges, fractions, percentages, ordinals and ambiguous prepositions before expanding activity types.

3. **[HIGH] Activity-level rejection was too coarse.** Real runs required per-item salvage, now implemented. [slice-1-measurement-result.md:49](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:49)  
   **Recommendation:** the editor must display salvaged counts and flagged items; it must not silently hide rejected material.

4. **[HIGH] Cache/idempotency is not ready for the app.** The measurement observed different output at the same fingerprint. [slice-1-measurement-result.md:56](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:56) Current fingerprints omit requested activity types, pedagogy and phase and identify DBs by size/mtime rather than content digest. [pipeline.py:71](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/pipeline.py:71)  
   **Recommendation:** fingerprint anchor + level + pedagogy + phase + requested types + exact prompt/package/engine/model versions + data-bundle digests. HTTP idempotency remains a separate job-store concern.

5. **[HIGH] Russianism checking is promised but not wired end-to-end.** Retrieval returns `atlas_lookup`, and the VESUM gate can use it, but pipeline calls omit it. [retrieval.py:286](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/retrieval.py:286) [vesum.py:82](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/gates/vesum.py:82) [pipeline.py:131](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/pipeline.py:131)  
   **Recommendation:** pass the grounding context into every lexical gate and add an integration fixture that proves a known heritage warning reaches the IR/UI.

6. **[HIGH] MatchUp semantic grounding is incomplete.** The reviewed plan requires Atlas-sourced synonyms or a teacher-confirm warning; current code merely rejects non-VESUM words. [slice-1-build-plan.md:237](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-build-plan.md:237) [pipeline.py:208](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/pipeline.py:208)  
   **Recommendation:** verify right-hand meaning against Atlas/source evidence or mark it `review_required`. Word validity is not semantic correctness.

7. **[HIGH] Teacher-pasted anchors cannot be assumed to be published/correct prose.** The app accepts pasted text, while the token gate automatically trusts anchor-verbatim content. [app-spec-v1.md:48](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:48) [vesum.py:51](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/engine/gates/vesum.py:51)  
   **Recommendation:** add anchor provenance and baseline diagnostics. Quoted source errors may remain quoted, but the product must not badge them as linguistically verified.

8. **[MEDIUM] “All levels in v1” is not a build-ready assumption.** The fleet recommended one-band proof, and slice 1 locked B1. [app-spec-v1.md:24](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/app-spec-v1.md:24) [slice-1-build-plan.md:20](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-build-plan.md:20)  
   **Recommendation:** slice 2 should be a B1 app pilot while retaining level-neutral API/schema seams.

### Slice-2 entry sequence

1. Create the private repository/project structure and record the reconciled decisions: Scaleway, B1 pilot, mandatory pedagogy, no v1 student storage, AIS toolless.
2. Establish the activity-kit/schema package contract and canonical three-type fixtures.
3. Move the proven engine into the private backend and replace checkout imports with pinned adapters/data manifests.
4. Fix warning semantics, Russianism propagation, MatchUp semantics, anchor diagnostics and fingerprint identity.
5. Build durable SQLite jobs and the async API.
6. Build the editor/player frontend against the versioned API.
7. Run 8–10 anchors, independent pedagogy review and teacher-style acceptance scoring.
8. Only then arm access for a real teacher.

## Item 3 — Earlier packet reconciliation

### Superseded

- **[CRITICAL] OpenRouter tooled Gemma is superseded.** The packet required `openrouter/google/gemma-4-31b-it`; the archive locks `google-ais/gemma-4-31b-it`, toolless, with Python gates, and the measured run proved it. [advisory packet:8](/Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/advisory-hramatka-design.result:8) [design-v0.md:31](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/design-v0.md:31) [slice-1-measurement-result.md:8](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/slice-1-measurement-result.md:8)
- The proposed grounded-B1 QG adapter, tool-call budget and model-generated factual review are not the Hramatka engine.
- The packet request made `source_text` optional and centered topic/goals; the archive requires a teacher-supplied anchor. [advisory packet:40](/Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/advisory-hramatka-design.result:40) [design-v0.md:31](/Users/krisztiankoos/projects/learn-ukrainian/.agent/tmp/hramatka/design-v0.md:31)
- The four-file Markdown packet and universal “facts/culture” warning are superseded by interactive activity JSON, evidence-bearing IR, gates and teacher assembly.
- “Atlas is not a hard dependency” is superseded for the proven slice; Atlas is part of grounding-IN.
- The thin “B1 homework form” without pedagogy is superseded. The product must retain pedagogy phases and the future student/difficulty/assignment domain.
- “No students” survives only as a v1 storage/privacy decision. It does not remove student/difficulty concepts from the product architecture.
- B1-only survives as the pilot slice, not as the long-term architecture.

### Still useful

- The separate frontend is consistent with the archive’s public/private boundary.
- The API patterns survive: signed session, `/me`, async bake submission, job status, history, artifact, retry and delete. [advisory packet:20](/Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/advisory-hramatka-design.result:20)
- Required `Idempotency-Key`, request-hash conflict handling and separate bake provenance remain good design. [advisory packet:167](/Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/advisory-hramatka-design.result:167)
- The durable SQLite lease queue, one worker, WAL, retry limits and expired-lease recovery remain appropriate for DEV1-M. [advisory packet:320](/Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/advisory-hramatka-design.result:320)
- Session cookies, CSRF protection, no wildcard CORS, login rate limits and generic auth failures survive. Replace the plaintext environment access code with hashed, revocable invite credentials. [advisory packet:408](/Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/advisory-hramatka-design.result:408)
- Safe failure codes, non-fabricated progress, health/readiness and history during engine disarm survive.
- Separate API/worker/sources systemd services and a TLS reverse proxy survive, now on Scaleway.
- Online SQLite backups, integrity hashes, off-box encrypted copies and restore rehearsals survive. [advisory packet:392](/Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/advisory-hramatka-design.result:392)
- The no-phone-home rule survives: no teacher request, anchor, artifact or auth data goes to GitHub or public telemetry. [advisory packet:380](/Users/krisztiankoos/projects/learn-ukrainian/batch_state/tasks/advisory-hramatka-design.result:380)

## Executive summary

- The archive wins: private Hramatka app, AIS-toolless Gemma, Python gates and Scaleway DEV1-M.
- The engine is proven for three B1 extractive activity types on two anchors, not for the full app.
- The current prototype is path/import-coupled to the public checkout and must not be deployed that way.
- Public integration should use pinned activity, practice, linguistic, policy and service artifacts.
- `sources.db` must remain a protected read-only runtime snapshot; it contains private/non-redistributable material.
- Frontend, API, worker, Sources MCP and Atlas/Practice can deploy independently on one server.
- V1 should implement pedagogy and B1 level selection but store no student records or server-side progress.
- Russianism propagation, MatchUp semantics, warning states, anchor trust and cache identity need correction.
- Existing async API, SQLite queue, auth and operations advice from my earlier packet remains valuable.
- Provisioning may begin after explicit user go; teacher exposure remains gated on integration and acceptance evidence.

## Verdict

**GO-WITH-CHANGES** for starting slice 2 and provisioning the Scaleway DEV1-M once the user gives the explicit go. The first slice-2 milestone must establish the private repository and versioned public-artifact boundary. It is **not yet a go for teacher access** until the five engine integration defects above, pedagogy validation, 8–10-anchor measurement, leak controls, restart/backup rehearsal and DEV1-M load test are complete.