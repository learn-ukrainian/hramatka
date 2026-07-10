# Step 3 — engine goes private (pinned imports + data manifests)

Plan-of-record step 3 (#4542) / Sol separation review §Item 1. The recovered
slice-1 engine was authored to live inside the **public** checkout
(`.agent/tmp/hramatka/engine/`), where `get_project_root()` walked up to the
public repo root and found `data/*.db`, `schemas/…`, and `scripts/…` on
`sys.path`, plus a dev-home AIS key. Moved into this private repo, every one of
those references dangles. This step makes the private engine self-contained.

## 5-line plan
1. **Vendor** every public file the engine reads into `hramatka/vendor/<artifact>@<version>/`
   (`lu.activity.v1`, `lu.lesson.v1`, `learn_ukrainian_linguistics`) each with a
   `MANIFEST.json` (source repo/commit + per-file sha256+size); a loader reads
   ONLY from the vendor dir and verifies digests before use.
2. **Data manifests**: resolve `vesum.db`/`atlas.db`/`sources.db` from
   `HRAMATKA_DATA_DIR` through a digest-pinned `data-manifest.json`; refuse on
   mismatch unless `HRAMATKA_ALLOW_DATA_DRIFT=1`. Kill `PROJECT_ROOT/data`,
   `../`-escapes and dev-home paths.
3. **Full-input fingerprint** (Sol defect #4/item 4): anchor + level + pedagogy
   + phase + requested types + prompt digest + package versions + engine version
   + data-bundle **content** digests (no more size/mtime).
4. **GeneratorPort**: private transport reading the AIS key from
   `HRAMATKA_AIS_API_KEY` (never a hardcoded path); no `scripts.*`/opencode
   import. Locked route kept: `google-ais/gemma-4-31b-it`, TOOLLESS.
5. **CI + PIN test**: direction-of-flow check (no public-checkout paths/imports,
   vendor digests verified, no `sources.db`/anchors in any export path) + a test
   proving an unpinned file cannot change engine behaviour and a vendor-digest
   mismatch is refused.

## What the engine consumes from the public repo (now vendored/pinned)
| Consumer | Public source (repo `learn-ukrainian.github.io`) | Pinned form |
|---|---|---|
| `schema.py` b1 validation | `schemas/activities-b1.schema.json` | `vendor/lu.activity.v1@1.0.0/` |
| API lesson validation | `lesson-document-v1.md` @ `f77ccf76fd` (interim schema) | `vendor/lu.lesson.v1@1.0.0/` |
| `retrieval.py`, `gates/vesum*.py` | `scripts/verification/vesum.py` | `vendor/learn_ukrainian_linguistics@1.0.0/vesum.py` |
| `retrieval.py` grounding | `data/atlas.db` (`article_payloads`) | data-manifest input `atlas.db` |
| VESUM lookups | `data/vesum.db` (`forms`) | data-manifest input `vesum.db` |
| protected corpus | `data/sources.db` | data-manifest input `sources.db` (never a file in git) |

Public artifacts pinned at commit `28c257d8` (schema + vesum.py); lesson
contract at `f77ccf76fd`. Digests recorded in each `MANIFEST.json`.

## Package / import model
The engine becomes a real submodule of the `hramatka` wheel package
(`hramatka.engine`). Test imports move `engine.* → hramatka.engine.*`. No
`sys.path` bootstrapping and no `scripts.*` imports remain anywhere under
`hramatka/`.

## Offline testability
The real corpus DBs (≈2.8 GB, protected) are never committed. Tests build a tiny
SQLite bundle in a tmp dir from committed JSON fixtures (`vesum_forms.json`,
`atlas_rows.json`) extracted from the real DBs for exactly the forms/lemmas the
suite touches, plus a synthetic B1 `anchor01.txt` (no teacher text). The engine
runs identically against the fixture bundle and the production bundle — the only
difference is which digests the manifest pins.

## Out of scope (step 4 / later)
- Sol engine-integration defects other than fingerprint identity (warning
  semantics, Russianism propagation, MatchUp semantics, anchor diagnostics).
- The mock→real bake adapter swap: `create_app` still defaults to the mock; the
  real `EngineLessonBaker` lands wired to the seam but is not the default.
- A real Google-AIS HTTP client: the `GeneratorPort` reads the env key and keeps
  the locked route, but the concrete network transport remains a stub until the
  swap step.
