# Mock lesson fixture

`canonical-lesson.v1.json` is the synthetic six-block, 45-minute B1 TTT fixture
used by the Step 5 mock adapter. It is structurally validated against the
private contract-derived validator under `docs/vendor/` and contains no teacher
anchor, private corpus text, or public-repository runtime import.

The public POC lesson fixture referenced by the contract was not available as a
merged, pin-able `lu.lesson.v1` fixture at public commit `f77ccf76fd`, so this
minimal fixture is deliberately local and clearly labelled as mock data.

Because its activity content is generic rather than derived from the submitted
teacher anchor, every block carries `mark: "warn"`, generated provenance, and
`external_options: true`. The acceptance gate therefore requires an explicit
per-block acknowledgement even in mock mode.
