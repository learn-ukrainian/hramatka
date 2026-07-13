# Frozen pilot activity registry

`pilot_activity_types.schema.json` is the single checked-in
`PILOT_ACTIVITY_TYPES` source of truth for the pasted-text pilot. Its enum order is:

1. `true-false`
2. `cloze`
3. `match-up`
4. `quiz`
5. `mark-the-words`
6. `fill-in`
7. `error-correction`
8. `text-questions`
9. `short-writing`

The list is pilot product policy, so it lives in the private Hramatka package rather
than broadening or narrowing the public activity-kit contract. The private baker
imports `PILOT_ACTIVITY_TYPES` from `hramatka.contracts`; the OpenAPI contract
references the JSON Schema enum directly; and private browser/acceptance tests must
read that same enum when exercising the public activity-kit viewer. The public viewer
remains generic and does not need a private import or a second hard-coded pilot list.

The public source evidence is vendored verbatim and digest-pinned at commit
`ffd54054d37e40a46dfed3f0a4714df20a3028be`:

- `hramatka/vendor/lu.activity.v1@1.0.0-ffd54054/lu.activity.v1.schema.json`
- `hramatka/vendor/lu.activity.v1@1.0.0-ffd54054/lu.activity.v1.fixtures.json`

The landed schema permits 15 canonical activity shapes; the landed golden fixture is
the exact nine-type player-backed pilot subset. `test_pilot_contract.py` requires the
registry to equal the fixture types in order, verifies all nine are schema-permitted,
validates every golden fixture against the landed schema, and requires the baker
registry to derive from the same tuple.
