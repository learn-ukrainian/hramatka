"""Versioned, fail-closed teacher model qualification registry.

Logical model identities are the only values accepted from the teacher UI.
Provider routes remain private and must each have a current qualification
receipt before their logical model is exposed.  Historical bake-off evidence
is intentionally not loaded here: production starts with no qualified choices
until the production-path qualification run lands receipts for this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from hramatka.engine.content_density import (
    TEACHER_READY_DENSITY_VERSION,
    teacher_ready_density_digest,
)
from hramatka.engine.prompt_pack import PROMPT_PACK_VERSION

QUALIFIED_MODEL_REGISTRY_VERSION: Final = "QualifiedLogicalModels.v1"
DENSITY_CONTRACT_VERSION: Final = TEACHER_READY_DENSITY_VERSION
DENSITY_CONTRACT_DIGEST: Final = teacher_ready_density_digest()
QUALIFICATION_ANCHORS: Final = frozenset({"b1-narrative", "b1-dialogue", "b1-morphology"})


class LogicalModelUnavailable(ValueError):
    """The requested logical model has no complete current qualification."""


@dataclass(frozen=True)
class QualifiedProviderRoute:
    id: str
    host: str
    model_id: str


@dataclass(frozen=True)
class LogicalModelSpec:
    id: str
    label: str
    description_uk: str
    provider_routes: tuple[QualifiedProviderRoute, ...]
    retired: bool = False


@dataclass(frozen=True)
class QualificationReceipt:
    """Content-free result of one production-path route qualification."""

    logical_model_id: str
    provider_route: str
    provider_host: str
    provider_model_id: str
    registry_version: str
    prompt_pack_version: str
    density_contract_version: str
    density_contract_digest: str
    passed_anchors: frozenset[str]
    passed: bool


# This table is the teacher-routing allowlist.  Provider credentials, wire
# model IDs, endpoints, and fallback order deliberately live in providers.py.
LOGICAL_MODELS: Final = (
    LogicalModelSpec(
        id="gemini-3.5-flash",
        label="Gemini 3.5 Flash",
        description_uk="Швидке складання уроку.",
        provider_routes=(
            QualifiedProviderRoute("gemini-flash-ais", "google-ais", "google-ais/gemini-3.5-flash"),
        ),
    ),
    LogicalModelSpec(
        id="gemini-3.1-pro",
        label="Gemini 3.1 Pro",
        description_uk="Ретельніше складання; зазвичай потребує більше часу.",
        provider_routes=(
            QualifiedProviderRoute(
                "gemini-pro-ais", "google-ais", "google-ais/gemini-3.1-pro-preview"
            ),
        ),
    ),
    LogicalModelSpec(
        id="gemma-4-31b",
        label="Gemma 4 31B",
        description_uk="Відкрита модель із внутрішнім резервним маршрутом.",
        provider_routes=(
            QualifiedProviderRoute("gemma-ais", "google-ais", "google-ais/gemma-4-31b-it"),
            QualifiedProviderRoute("gemma-openrouter", "openrouter", "google/gemma-4-31b-it"),
        ),
    ),
)

# Transcribed from the 12 receipt files for the 2026-07-28 12/12 matrix run
# (3 anchors x 4 routes, all outcome=passed, ProductionQualificationCellReceipt.v2)
# located at: /Users/krisztiankoos/hramatka-qual/matrix-receipts-20260727/receipts/
#
# AUTHORIZED RULING (driver decision, Sol concurrence bridge msg 5544):
# - `semantic_gate: not_run` does NOT block qualification (shadow-tier advisory per
#   LLM-QG contract; keeping `semantic_gate: not_run` EXPLICIT here as future cutover seam).
# - `prompt_pack_version='PromptPackInput.v3'` derived from pinned commit
#   2e621ef8add554065f11092ed0b241dee54e9b97.
# - All 12 cell receipts passed across anchors ('b1-narrative', 'b1-dialogue', 'b1-morphology')
#   with density contract TeacherReadyDensity.v2 (digest
#   07081556724c1818c0df2461e679aa03c3d2268607b56152590ef57478e3288b).
PRODUCTION_QUALIFICATION_RECEIPTS: Final[tuple[QualificationReceipt, ...]] = (
    QualificationReceipt(
        logical_model_id="gemini-3.5-flash",
        provider_route="gemini-flash-ais",
        provider_host="google-ais",
        provider_model_id="google-ais/gemini-3.5-flash",
        registry_version="QualifiedLogicalModels.v1",
        prompt_pack_version="PromptPackInput.v3",
        density_contract_version="TeacherReadyDensity.v2",
        density_contract_digest="07081556724c1818c0df2461e679aa03c3d2268607b56152590ef57478e3288b",
        passed_anchors=frozenset({"b1-narrative", "b1-dialogue", "b1-morphology"}),
        passed=True,
    ),
    QualificationReceipt(
        logical_model_id="gemini-3.1-pro",
        provider_route="gemini-pro-ais",
        provider_host="google-ais",
        provider_model_id="google-ais/gemini-3.1-pro-preview",
        registry_version="QualifiedLogicalModels.v1",
        prompt_pack_version="PromptPackInput.v3",
        density_contract_version="TeacherReadyDensity.v2",
        density_contract_digest="07081556724c1818c0df2461e679aa03c3d2268607b56152590ef57478e3288b",
        passed_anchors=frozenset({"b1-narrative", "b1-dialogue", "b1-morphology"}),
        passed=True,
    ),
    QualificationReceipt(
        logical_model_id="gemma-4-31b",
        provider_route="gemma-ais",
        provider_host="google-ais",
        provider_model_id="google-ais/gemma-4-31b-it",
        registry_version="QualifiedLogicalModels.v1",
        prompt_pack_version="PromptPackInput.v3",
        density_contract_version="TeacherReadyDensity.v2",
        density_contract_digest="07081556724c1818c0df2461e679aa03c3d2268607b56152590ef57478e3288b",
        passed_anchors=frozenset({"b1-narrative", "b1-dialogue", "b1-morphology"}),
        passed=True,
    ),
    QualificationReceipt(
        logical_model_id="gemma-4-31b",
        provider_route="gemma-openrouter",
        provider_host="openrouter",
        provider_model_id="google/gemma-4-31b-it",
        registry_version="QualifiedLogicalModels.v1",
        prompt_pack_version="PromptPackInput.v3",
        density_contract_version="TeacherReadyDensity.v2",
        density_contract_digest="07081556724c1818c0df2461e679aa03c3d2268607b56152590ef57478e3288b",
        passed_anchors=frozenset({"b1-narrative", "b1-dialogue", "b1-morphology"}),
        passed=True,
    ),
)


class QualificationCandidateRegistry:
    """Authorize one configured matrix cell while it is being qualified.

    This is deliberately distinct from :class:`QualifiedModelRegistry`:
    candidate authorization proves only that a requested harness cell belongs
    to the immutable routing matrix.  It neither reads nor constructs
    production qualification receipts.  The returned model contains exactly
    the requested configured route, preventing a qualification cell from
    silently exercising a sibling fallback route.
    """

    def __init__(
        self,
        *,
        logical_model_id: str,
        provider_route: str,
        provider_host: str,
        provider_model_id: str,
    ) -> None:
        model = next((item for item in LOGICAL_MODELS if item.id == logical_model_id), None)
        if model is None or model.retired:
            raise LogicalModelUnavailable("Logical model is not a qualification candidate.")
        route = next((item for item in model.provider_routes if item.id == provider_route), None)
        if route is None or (route.host, route.model_id) != (
            provider_host,
            provider_model_id,
        ):
            raise LogicalModelUnavailable("Provider route is not a qualification candidate.")
        self._model = LogicalModelSpec(
            id=model.id,
            label=model.label,
            description_uk=model.description_uk,
            provider_routes=(route,),
        )

    def qualified_models(self) -> tuple[LogicalModelSpec, ...]:
        """Return the sole configured model and route for this harness cell."""
        return (self._model,)

    def require_qualified(self, logical_model_id: str) -> LogicalModelSpec:
        """Return only this cell's configured logical model."""
        if logical_model_id != self._model.id:
            raise LogicalModelUnavailable("Logical model is not a qualification candidate.")
        return self._model

    def public_payload(
        self, *, operational_model_ids: frozenset[str] | None = None
    ) -> dict[str, object]:
        """Implement the small registry surface required by ``create_app``."""
        available = operational_model_ids is None or self._model.id in operational_model_ids
        return {
            "registry_version": QUALIFIED_MODEL_REGISTRY_VERSION,
            "models": (
                [
                    {
                        "id": self._model.id,
                        "label": self._model.label,
                        "description": self._model.description_uk,
                    }
                ]
                if available
                else []
            ),
            "unavailable_message": None if available else "Модель тимчасово недоступна.",
        }


class QualifiedModelRegistry:
    """Resolve teacher choices only from complete, current route receipts."""

    def __init__(
        self,
        *,
        models: tuple[LogicalModelSpec, ...] = LOGICAL_MODELS,
        receipts: tuple[QualificationReceipt, ...] = PRODUCTION_QUALIFICATION_RECEIPTS,
        prompt_pack_version: str = PROMPT_PACK_VERSION,
        density_contract_version: str = DENSITY_CONTRACT_VERSION,
        density_contract_digest: str = DENSITY_CONTRACT_DIGEST,
    ) -> None:
        ids = [model.id for model in models]
        if len(ids) != len(set(ids)):
            raise ValueError("Logical model IDs must be unique.")
        self._models = {model.id: model for model in models}
        self._receipts = receipts
        self.prompt_pack_version = prompt_pack_version
        self.density_contract_version = density_contract_version
        self.density_contract_digest = density_contract_digest

    def qualified_models(self) -> tuple[LogicalModelSpec, ...]:
        return tuple(
            model
            for model in self._models.values()
            if not model.retired and self._has_current_receipts(model)
        )

    def require_qualified(self, logical_model_id: str) -> LogicalModelSpec:
        model = self._models.get(logical_model_id)
        if model is None or model.retired or not self._has_current_receipts(model):
            raise LogicalModelUnavailable("Logical model is not currently qualified.")
        return model

    def public_payload(
        self, *, operational_model_ids: frozenset[str] | None = None
    ) -> dict[str, object]:
        qualified = tuple(
            model
            for model in self.qualified_models()
            if operational_model_ids is None or model.id in operational_model_ids
        )
        active_count = sum(not model.retired for model in self._models.values())
        if not qualified:
            unavailable_message = (
                "Моделі тимчасово недоступні: кваліфікація для поточних "
                "правил уроку ще не завершена."
            )
        elif len(qualified) < active_count:
            unavailable_message = (
                "Показано лише моделі з повною кваліфікацією для поточних правил уроку."
            )
        else:
            unavailable_message = None
        return {
            "registry_version": QUALIFIED_MODEL_REGISTRY_VERSION,
            "models": [
                {
                    "id": model.id,
                    "label": model.label,
                    "description": model.description_uk,
                }
                for model in qualified
            ],
            "unavailable_message": unavailable_message,
        }

    def _has_current_receipts(self, model: LogicalModelSpec) -> bool:
        return all(
            self._has_one_current_route_aggregate(model, route) for route in model.provider_routes
        )

    def _has_one_current_route_aggregate(
        self, model: LogicalModelSpec, route: QualifiedProviderRoute
    ) -> bool:
        """Require exactly one current aggregate per configured route.

        The production receipt loader must reject duplicate/missing/stale cell
        evidence before constructing this aggregate.  Retaining more than one
        aggregate would make route eligibility ambiguous, so this selector
        also fails closed if that invariant is ever bypassed.
        """
        aggregates = tuple(
            receipt
            for receipt in self._receipts
            if receipt.logical_model_id == model.id and receipt.provider_route == route.id
        )
        return len(aggregates) == 1 and self._is_current(aggregates[0], route)

    def _is_current(self, receipt: QualificationReceipt, route: QualifiedProviderRoute) -> bool:
        return (
            receipt.provider_host == route.host
            and receipt.provider_model_id == route.model_id
            and receipt.passed
            and receipt.registry_version == QUALIFIED_MODEL_REGISTRY_VERSION
            and receipt.prompt_pack_version == self.prompt_pack_version
            and receipt.density_contract_version == self.density_contract_version
            and receipt.density_contract_digest == self.density_contract_digest
            and receipt.passed_anchors == QUALIFICATION_ANCHORS
        )


def default_model_registry() -> QualifiedModelRegistry:
    return QualifiedModelRegistry()
