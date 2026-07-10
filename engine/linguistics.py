"""Private linguistics adapter — the engine's ONLY door to VESUM.

Loads the vendored, digest-pinned `learn_ukrainian_linguistics` module (the
adapted `scripts/verification/vesum.py`) through the integrity-checking loader
and re-exports its query API. Nothing under `hramatka/` imports `scripts.*` or
bootstraps `sys.path` any more — this module is the seam.

Every lookup takes an explicit `db_path` resolved from the digest-pinned data
manifest (see `engine.data`); there is no checkout-relative default.
"""

from __future__ import annotations

from . import vendoring

_vesum = vendoring.load_module(
    vendoring.LINGUISTICS, "vesum.py", "hramatka.vendor.learn_ukrainian_linguistics.vesum"
)

verify_word = _vesum.verify_word
verify_words = _vesum.verify_words
verify_lemma = _vesum.verify_lemma

__all__ = ["verify_word", "verify_words", "verify_lemma"]
