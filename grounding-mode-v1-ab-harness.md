# grounding-mode v1 A/B harness record

This upcoming exploratory A/B records the frozen lesson-floor oracle from
`content_density.floor_oracle_record()` (grounding-mode v1 §5.1 / E6). The
measurement report writes the same object to `header.floor_oracle_v1`; do not
retune it during the run.

| duration | min blocks | phase minimums | min types | productive |
| --- | ---: | --- | ---: | --- |
| 45 | 6 | `1:2, 2:3, 3:1` | 4 | yes |
| 60 | 9 | `1:2, 2:5, 3:2` | 4 | yes |
| 90 | 12 | `1:4, 2:5, 3:3` | 4 | yes |

The primary A/B run keeps repair off. It records the active flag vector and
bake IDs alongside this oracle, and is exploratory rather than confirmatory.

## Historical Baseline Context
The 2026-07-17 flag-off 18-anchor run (5 ready / 13 floor_unmet / 0 error; short 0/6, med 3/6, long 2/6) predates slices 1-4. A fresh control arm on current main is the honest control; the old run is context only.

## Quick-Read Stratified Subset (`--quick` mode)
For the fast directional read (`--quick` mode), the harness selects a 6-anchor stratified subset (2 short / 2 medium / 2 long) chosen deterministically:
* **Short**: `a01` (427 chars) and `a02` (354 chars)
* **Medium**: `a07` (607 chars) and `a08` (634 chars)
* **Long**: `a13` (1468 chars) and `a14` (1080 chars)

**Rationale**: These are the first two anchors within each length bucket from the full 18-anchor set. This selection provides a balanced, deterministic representative subset covering all three duration/length classes without introduce arbitrary selection bias.
