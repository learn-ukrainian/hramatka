"""Placeholder for derived-mode gate modules (grounding-mode v1).

Slice 1 ships this module empty so ``_gate_impl_digest`` already covers the
path; slice 3 fills in kit-closure, RULE_REGISTRY, and diversity gates.
No active derived gates yet — do not import behavior from here until then.
"""

from __future__ import annotations

# Reserved for slice 3: kit lemma closure, numeral MOAT rule_id registry,
# entity/numeral bounds, and per-batch diversity (spec §6.2 / §5.4).
DERIVED_GATES_VERSION = "derived.gates.stub.v0"
