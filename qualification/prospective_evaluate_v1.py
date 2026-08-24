"""Fail-closed prospective population aggregate for the frozen #552 sample."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hramatka.engine import data

from .prospective_manifest_v1 import (
    ELIGIBLE_COUNT,
    SELECTED_COUNT,
    UNSELECTED_ELIGIBLE_COUNT,
    ProspectiveManifest,
)
from .prospective_proof_schema_v1 import COMPOSITE_RULE_DIGEST, sha256
from .prospective_replay_v1 import replay_source


class ProspectiveEvaluationError(ValueError):
    pass


CLAIM_CEILING = (
    "Passes/94 of the selected 94-of-917 captured Wikipedia rows with char_count>=2600 "
    "as an encyclopedic teacher URL-import structural proxy. The 823 unselected eligible rows "
    "and all <2600 rows are unevaluated; this makes no arbitrary-web, shorter-source, CEFR-B1, "
    "pedagogical-semantic, or general-teacher-corpus claim."
)


@dataclass(frozen=True)
class ProspectiveAggregate:
    manifest_digest: str
    composite_rule_digest: str
    passed: int
    failed: int
    selected: int
    eligible: int
    unselected_eligible: int
    claim_ceiling: str
    certificate_digests: tuple[tuple[int, str], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "HramatkaProspectiveAggregate.v1",
            "manifest_digest": self.manifest_digest,
            "composite_rule_digest": self.composite_rule_digest,
            "passed": self.passed,
            "failed": self.failed,
            "selected": self.selected,
            "eligible": self.eligible,
            "unselected_eligible": self.unselected_eligible,
            "claim_ceiling": self.claim_ceiling,
            "certificate_digests": [
                {"sqlite_id": item, "digest": digest} for item, digest in self.certificate_digests
            ],
        }

    @property
    def digest(self) -> str:
        return sha256(self.to_dict())


def evaluate_population(
    manifest: ProspectiveManifest,
    certificate_bytes: Mapping[int, bytes],
    source_texts: Mapping[int, str],
    *,
    bundle: data.DataBundle,
    repository: Path,
) -> ProspectiveAggregate:
    """Replay every selected source and aggregate only the exact preregistered set."""
    expected = {source.sqlite_id for source in manifest.sources}
    if set(certificate_bytes) != expected or set(source_texts) != expected:
        raise ProspectiveEvaluationError(
            "Certificates and source texts must match the exact 94-source manifest set."
        )
    results = []
    for source in manifest.sources:
        certificate = replay_source(
            certificate_bytes[source.sqlite_id],
            manifest,
            source,
            source_texts[source.sqlite_id],
            bundle=bundle,
            repository=repository,
        )
        results.append((source.sqlite_id, certificate))
    passed = sum(certificate.payload["status"] == "passed" for _source_id, certificate in results)
    failed = SELECTED_COUNT - passed
    if passed + failed != SELECTED_COUNT or len(results) != SELECTED_COUNT:
        raise ProspectiveEvaluationError("Prospective aggregate denominator is not exact.")
    return ProspectiveAggregate(
        manifest.digest,
        COMPOSITE_RULE_DIGEST,
        passed,
        failed,
        SELECTED_COUNT,
        ELIGIBLE_COUNT,
        UNSELECTED_ELIGIBLE_COUNT,
        CLAIM_CEILING,
        tuple((source_id, certificate.digest) for source_id, certificate in results),
    )
