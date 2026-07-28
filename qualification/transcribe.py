"""Print a reviewable production-registry block from a complete v3 matrix.

This is intentionally a read-only operator command.  It validates persisted
cell receipts with the same authority as ``receipts aggregate`` and prints the
literal replacement block for ``api/qualified_models.py``; it never writes the
registry itself.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path

from hramatka.api import qualified_models
from hramatka.api.qualified_models import QualificationReceipt

from .harness import _current_engine_digest, _file_digest, _flag_digest, _source_commit
from .manifest import load_manifest
from .receipts import (
    QualificationError,
    RouteAggregate,
    _load_prompt_hashes,
    _load_receipts,
    aggregate_receipts,
    assert_live_v3_authorities,
)


def _literal(value: str) -> str:
    """Return one stable Python string literal for the committed registry."""
    return json.dumps(value, ensure_ascii=False)


def format_registry_block(aggregates: Iterable[RouteAggregate]) -> str:
    """Return the exact, paste-ready registry declaration in stable route order."""
    receipts = tuple(
        aggregate.as_model_receipt()
        for aggregate in sorted(
            aggregates,
            key=lambda item: (item.logical_model_id, item.route.route_id),
        )
    )
    if not receipts:
        raise QualificationError("A complete matrix must contain at least one route aggregate.")
    lines = ["PRODUCTION_QUALIFICATION_RECEIPTS: Final[tuple[QualificationReceipt, ...]] = ("]
    for receipt in receipts:
        lines.extend(_format_receipt(receipt))
    lines.append(")")
    return "\n".join(lines)


def _format_receipt(receipt: QualificationReceipt) -> tuple[str, ...]:
    fields = (
        ("logical_model_id", receipt.logical_model_id),
        ("provider_route", receipt.provider_route),
        ("provider_host", receipt.provider_host),
        ("provider_model_id", receipt.provider_model_id),
        ("registry_version", receipt.registry_version),
        ("prompt_pack_version", receipt.prompt_pack_version),
        ("prompt_sha256", receipt.prompt_sha256),
        ("template_version", receipt.template_version),
        ("template_sha256", receipt.template_sha256),
        ("density_contract_version", receipt.density_contract_version),
        ("density_contract_digest", receipt.density_contract_digest),
        ("type_kit_identity", receipt.type_kit_identity),
    )
    lines = ["    QualificationReceipt("]
    lines.extend(f"        {name}={_literal(value)}," for name, value in fields)
    anchors = ", ".join(_literal(anchor) for anchor in sorted(receipt.passed_anchors))
    lines.extend(
        (
            f"        passed_anchors=frozenset({{{anchors}}}),",
            "        passed=True,",
            "    ),",
        )
    )
    return tuple(lines)


def _assert_registry_literals(aggregates: Iterable[RouteAggregate]) -> None:
    """Require aggregate fields to bind to the fail-closed registry literals.

    ``assert_live_v3_authorities`` establishes that the template, density, and
    kit literals still name their live v3 authorities.  This second check binds
    the matrix-derived aggregate prompt hash and every receipt-shape identity
    field to the exact literals that the selector will use after transcription.
    """
    expected = {
        "registry_version": qualified_models.QUALIFIED_MODEL_REGISTRY_VERSION,
        "prompt_pack_version": qualified_models.PROMPT_PACK_VERSION,
        "prompt_sha256": qualified_models.PROMPT_SHA256,
        "template_version": qualified_models.TEMPLATE_VERSION,
        "template_sha256": qualified_models.TEMPLATE_SHA256,
        "density_contract_version": qualified_models.DENSITY_CONTRACT_VERSION,
        "density_contract_digest": qualified_models.DENSITY_CONTRACT_DIGEST,
        "type_kit_identity": qualified_models.TYPE_KIT_IDENTITY,
    }
    for aggregate in aggregates:
        receipt = aggregate.as_model_receipt()
        if any(getattr(receipt, name) != value for name, value in expected.items()):
            raise QualificationError(
                "Aggregate receipt literals do not match the live fail-closed selector."
            )


def transcribe(*, receipt_dir: Path, source_commit: str) -> str:
    """Validate one pinned current matrix and return its registry block.

    ``source_commit`` is deliberately mandatory and must be the checkout's
    exact candidate commit.  A matrix produced for a different pin cannot be
    copied into this checkout, even if its receipt files look internally
    complete.
    """
    current_commit = _source_commit()
    if source_commit != current_commit:
        raise QualificationError(
            "Requested source commit does not match the current checkout candidate pin."
        )
    assert_live_v3_authorities()
    manifest = load_manifest()
    aggregates = aggregate_receipts(
        _load_receipts(receipt_dir),
        source_commit=source_commit,
        harness_sha256=_file_digest(Path(__file__).with_name("harness.py")),
        manifest_sha256=manifest.sha256,
        anchor_hashes={anchor.id: anchor.sha256 for anchor in manifest.anchors},
        prompt_hashes=_load_prompt_hashes(receipt_dir),
        engine_sha256=_current_engine_digest(),
        flag_sha256=_flag_digest(),
    )
    _assert_registry_literals(aggregates)
    return format_registry_block(aggregates)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print, but never write, the qualified-model registry block for a v3 matrix."
    )
    parser.add_argument(
        "--receipt-dir",
        type=Path,
        required=True,
        help="operator-local <matrix-run>/receipts directory",
    )
    parser.add_argument(
        "--source-commit",
        required=True,
        help="candidate commit recorded by the matrix; must equal this checkout's HEAD",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; errors refuse before emitting any registry literal."""
    args = _parser().parse_args(argv)
    try:
        print(transcribe(receipt_dir=args.receipt_dir, source_commit=args.source_commit))
    except (OSError, QualificationError, ValueError) as error:
        raise SystemExit(f"Qualification receipt transcription refused: {error}") from error
    return 0


if __name__ == "__main__":  # pragma: no cover - operator command
    raise SystemExit(main())
