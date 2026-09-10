"""Private linguistics adapter — the engine's ONLY door to VESUM.

Loads the vendored, digest-pinned `learn_ukrainian_linguistics` module (the
adapted `scripts/verification/vesum.py`) through the integrity-checking loader
and re-exports its query API. Nothing under `hramatka/` imports `scripts.*` or
bootstraps `sys.path` any more — this module is the seam.

Every lookup takes an explicit `db_path` resolved from the digest-pinned data
manifest (see `engine.data`); there is no checkout-relative default.
"""

from __future__ import annotations

import threading

from . import vendoring

_vesum = vendoring.load_module(
    vendoring.LINGUISTICS, "vesum.py", "hramatka.vendor.learn_ukrainian_linguistics.vesum"
)

_vesum_lock = threading.RLock()


def verify_word(*args, **kwargs):
    """Serialize the vendored path-keyed SQLite handle across bake threads."""
    with _vesum_lock:
        return _vesum.verify_word(*args, **kwargs)


def verify_words(*args, **kwargs):
    """Serialize the vendored path-keyed SQLite handle across bake threads."""
    with _vesum_lock:
        return _vesum.verify_words(*args, **kwargs)


def verify_lemma(*args, **kwargs):
    """Serialize the vendored path-keyed SQLite handle across bake threads."""
    with _vesum_lock:
        return _vesum.verify_lemma(*args, **kwargs)

__all__ = ["verify_word", "verify_words", "verify_lemma"]
