"""Private production-qualification protocol with lazy compatibility exports."""

from __future__ import annotations

from importlib import import_module

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

_EXPORTS = {
    "CellReceipt": (".receipts", "CellReceipt"),
    "ProductionQualificationHarness": (".harness", "ProductionQualificationHarness"),
    "QualificationError": (".receipts", "QualificationError"),
    "QualificationManifest": (".manifest", "QualificationManifest"),
    "RuntimeAnchor": (".manifest", "RuntimeAnchor"),
    "aggregate_receipts": (".receipts", "aggregate_receipts"),
    "deterministic_runtime_anchors": (".harness", "deterministic_runtime_anchors"),
    "load_manifest": (".manifest", "load_manifest"),
}


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
