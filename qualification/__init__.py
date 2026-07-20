"""Private production-qualification protocol.

The package contains only contracts, hashes, and deterministic harness code.
It deliberately ships no teacher source text, lesson output, provider secret,
database, or telemetry receipt.
"""

from .harness import ProductionQualificationHarness, deterministic_runtime_anchors
from .manifest import QualificationManifest, RuntimeAnchor, load_manifest
from .receipts import CellReceipt, QualificationError, aggregate_receipts

__all__ = [
    "CellReceipt",
    "ProductionQualificationHarness",
    "QualificationError",
    "QualificationManifest",
    "RuntimeAnchor",
    "aggregate_receipts",
    "deterministic_runtime_anchors",
    "load_manifest",
]
