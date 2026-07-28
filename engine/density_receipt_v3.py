"""Content-free, per-block v3 density receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from .teacher_ready_density_v3 import floor_for
from .unit_plan_v3 import UnitPlan

ReceiptDisposition = Literal[
    "ready",
    "tray",
    "unavailable",
    "density_shortfall",
    "dropped",
]
_RECEIPT_DISPOSITIONS: Final = frozenset(
    {"ready", "tray", "unavailable", "density_shortfall", "dropped"}
)


@dataclass(frozen=True)
class BlockDensityReceipt:
    """The locked content-free receipt shape from TeacherReadyDensity.v3 §3."""

    phase: int
    activity_type: str
    disposition: ReceiptDisposition
    units: int
    floor_met: bool

    def __post_init__(self) -> None:
        if self.phase < 1 or self.units < 0:
            raise ValueError("Density receipt phase and units must be non-negative/positive.")
        floor = floor_for(self.activity_type)
        if self.disposition not in _RECEIPT_DISPOSITIONS:
            raise ValueError(f"Unknown v3 receipt disposition: {self.disposition!r}")
        if self.floor_met != (self.units >= floor.minimum_units):
            raise ValueError("floor_met must be derived from the central v3 floor table.")
        if self.disposition in {"ready", "tray"} and not self.floor_met:
            raise ValueError("Ready and tray receipts must independently meet their v3 floor.")
        if self.disposition == "density_shortfall" and self.floor_met:
            raise ValueError("A density shortfall receipt must be below its v3 floor.")

    @classmethod
    def from_unit_plan(
        cls, plan: UnitPlan, *, disposition: ReceiptDisposition
    ) -> BlockDensityReceipt:
        """Create a receipt without exposing plan text, anchors, forms, or keys."""
        if plan.disposition == "unavailable" and disposition != "unavailable":
            raise ValueError("An unavailable unit plan can only emit an unavailable receipt.")
        return cls(
            phase=plan.phase,
            activity_type=plan.activity_type,
            disposition=disposition,
            units=len(plan.units),
            floor_met=plan.floor_met,
        )

    def to_dict(self) -> dict[str, int | str | bool]:
        """Return exactly the contract shape; never leak substrate content."""
        return {
            "phase": self.phase,
            "type": self.activity_type,
            "disposition": self.disposition,
            "units": self.units,
            "floor_met": self.floor_met,
        }
