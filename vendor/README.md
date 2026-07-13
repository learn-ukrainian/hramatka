# Pinned public artifacts (vendored)

Every public file the private Hramatka engine reads lives here as an
**immutable, digest-pinned** copy under `<artifact>@<version>/`, each with a
`MANIFEST.json` recording the source repo/commit/path and a sha256 + size per
file. Nothing under `hramatka/` reads a public checkout at runtime; it reads
only from this directory, and the loader
(`hramatka/engine/vendoring.py`) verifies every file's digest against its
manifest before the bytes are used (`VendorIntegrityError` on mismatch).

| Artifact | Kind | Consumed by |
|---|---|---|
| `lu.activity.v1@1.0.0` | JSON Schema (pinned public base + documented Wave 1B extension) | `engine/schema.py` — projected-activity validation |
| `lu.lesson.v1@1.0.0` | JSON Schema (interim, contract-derived) | `api/validation.py` + engine e2e — lesson-document validation |
| `lu.activity.v1@1.0.0-ffd54054` | Verbatim JSON Schema + golden nine-type fixture | Pilot-registry drift test and frontend acceptance input |
| `lu.lesson.v1@1.0.0-ffd54054` | Verbatim public JSON Schema | Frozen API lesson-resource reference |
| `learn_ukrainian_linguistics@1.0.0` | Python module (adapted) | `engine/linguistics.py` — VESUM `verify_word/words/lemma` |

Direction of flow (Sol separation review §Item 1): public → private ONLY through
these reviewed, immutable, digest-pinned artifacts. A newer public release does
not update anything here automatically — bump `<version>`, re-copy, refresh the
manifest, and review the digest change. Private data (anchors, prompts, IR,
teacher text, `sources.db`) never flows into this directory or back to the
public repo.

The Wave 1B extension in `lu.activity.v1@1.0.0` remains the engine's interim
projection validator. The verbatim `ffd54054` pins are the converged public
envelope/fixture contract; the durable API implementation should migrate its
materializer to them without changing the frozen pilot registry.

## Bumping an artifact
1. Copy the new public file into a new `<artifact>@<newversion>/` dir.
2. Regenerate its `MANIFEST.json` digests.
3. Point the engine/API loader at the new version and review the diff.
4. Delete the old version once nothing references it.
