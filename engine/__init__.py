"""Hramatka engine — the private slice-1 B1 lesson-generation engine.

Anchor → grounding-IN (VESUM + Atlas) → toolless Gemma generation → deterministic
gate chain (evidence-span, VESUM token, numeral moat) → projected lu.activity.v1
items + full IR. Public inputs are consumed ONLY as digest-pinned vendored
artifacts (`hramatka/vendor/`, via `engine.vendoring`) and digest-pinned data
inputs (`engine.data`); nothing here reads a public checkout. See
`MIGRATION-NOTES.md` and `hramatka/slice-1-build-plan.md`.
"""

from __future__ import annotations

# Bumped whenever engine behaviour that could change bake output changes; it is
# a component of the bake fingerprint (Sol defect #4).
ENGINE_VERSION = "slice1-b1.2026.07.10"

