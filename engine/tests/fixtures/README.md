# Offline engine-test fixtures

The engine test suite runs fully offline against a tiny SQLite bundle the
`conftest.py` builds in a tmp dir from the JSON here — no real corpus DB
(≈2.8 GB, protected) and no network.

- `anchor01.txt` — a **synthetic** B1 anchor (fabricated; no real teacher text).
  Every `evidence` quote in `engine/fixtures.py` is a verbatim substring of it.
- `vesum_forms.json` — the exact VESUM `forms` rows the suite queries
  (`word_form`, `lemma`, `tags`, `pos`), extracted from the real DB.
- `atlas_rows.json` — the public Word Atlas payloads for the anchor's lemmas,
  trimmed to the fields `retrieval.build_atlas_lookup` reads.
- `_build_fixtures.py` — regenerates the two JSON files from a real corpus
  release: `HRAMATKA_TEST_DATA_DIR=/path .venv/bin/python
  hramatka/engine/tests/fixtures/_build_fixtures.py`. It captures exactly the
  rows the suite reads, so the offline fixtures reproduce real DB behaviour.

All content here is public linguistic reference data — no teacher material, no
private corpus. The DB files themselves are never committed (`.gitignore`).
