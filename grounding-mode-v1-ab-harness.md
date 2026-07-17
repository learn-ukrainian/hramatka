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
