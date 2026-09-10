"""Direction-of-flow: the private repo has zero public-checkout coupling."""

from __future__ import annotations

from hramatka.engine.tools import check_boundary


def test_no_public_checkout_coupling():
    violations = check_boundary.find_violations()
    assert violations == [], "direction-of-flow violations:\n" + "\n".join(violations)
