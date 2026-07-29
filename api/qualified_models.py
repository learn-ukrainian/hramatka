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

QUALIFIED_MODEL_REGISTRY_VERSION: Final = "QualifiedLogicalModels.v1"
# These literals are the output of qualification tooling's v3 authorities.
# The live baker now imports the v3 path; transcription still validates these
# fail-closed selector literals against its template and density authorities
# before it may copy a route aggregate here.
PROMPT_PACK_VERSION: Final = "PromptPackInput.v3"
# This is the canonical three-anchor aggregate of the currently pinned v3
# qualification prompts.  It remains a selector literal so an empty registry
# fails closed until the orchestrator transcribes a new matrix.
PROMPT_SHA256: Final = "868f55d166de53e7607fcdeb349a3bb16e62fa23f8e30dd42e1e7931cedee9c3"
TEMPLATE_VERSION: Final = "gemma-phase-pack.v3.2"
TEMPLATE_SHA256: Final = "071351bc6d609610f019205094985e46dc1d2a9347c733def06996a448aaf238"
DENSITY_CONTRACT_VERSION: Final = "TeacherReadyDensity.v3"
DENSITY_CONTRACT_DIGEST: Final = "1114645f2b2015e453ddb44542347b9af1de8aba6f6c64f875b5768550b946bf"
TYPE_KIT_IDENTITY: Final = "TeacherReadyDensity.v3.unit-plan-kit.v1"
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
    prompt_sha256: str
    template_version: str
    template_sha256: str
    density_contract_version: str
    density_contract_digest: str
    type_kit_identity: str
    passed_anchors: frozenset[str]
    passed: bool


# This table is the teacher-routing allowlist.  Provider credentials, wire
# model IDs, endpoints, and fallback order deliberately live in providers.py.
LOGICAL_MODELS: Final = (
    LogicalModelSpec(
        id="gemini-3.6-flash",
        label="Gemini 3.6 Flash",
        description_uk="Швидке складання уроку.",
        provider_routes=(
            QualifiedProviderRoute("gemini-flash-ais", "google-ais", "google-ais/gemini-3.6-flash"),
            QualifiedProviderRoute("gemini-flash-vertex", "google-vertex", "gemini-3.6-flash"),
        ),
    ),
)

# The prior TeacherReadyDensity.v2 transcription is intentionally absent: until
# a full current v3 aggregate is transcribed, the teacher UI must remain
# fail-closed.
PRODUCTION_QUALIFICATION_RECEIPTS: Final[tuple[QualificationReceipt, ...]] = ()


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
        prompt_sha256: str = PROMPT_SHA256,
        template_version: str = TEMPLATE_VERSION,
        template_sha256: str = TEMPLATE_SHA256,
        density_contract_version: str = DENSITY_CONTRACT_VERSION,
        density_contract_digest: str = DENSITY_CONTRACT_DIGEST,
        type_kit_identity: str = TYPE_KIT_IDENTITY,
    ) -> None:
        ids = [model.id for model in models]
        if len(ids) != len(set(ids)):
            raise ValueError("Logical model IDs must be unique.")
        self._models = {model.id: model for model in models}
        self._receipts = receipts
        self.prompt_pack_version = prompt_pack_version
        self.prompt_sha256 = prompt_sha256
        self.template_version = template_version
        self.template_sha256 = template_sha256
        self.density_contract_version = density_contract_version
        self.density_contract_digest = density_contract_digest
        self.type_kit_identity = type_kit_identity

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
            and receipt.prompt_sha256 == self.prompt_sha256
            and receipt.template_version == self.template_version
            and receipt.template_sha256 == self.template_sha256
            and receipt.density_contract_version == self.density_contract_version
            and receipt.density_contract_digest == self.density_contract_digest
            and receipt.type_kit_identity == self.type_kit_identity
            and receipt.passed_anchors == QUALIFICATION_ANCHORS
        )


def default_model_registry() -> QualifiedModelRegistry:
    return QualifiedModelRegistry()
