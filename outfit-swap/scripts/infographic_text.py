"""Two-reading coordination for literal infographic inventories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypeVar


class InventoryError(ValueError):
    """Raised when an infographic inventory cannot be safely settled."""


def _literal_tuple(value: object, name: str) -> tuple[str, ...]:
    if (not isinstance(value, tuple) or not value
            or not all(isinstance(item, str) and item for item in value)):
        raise InventoryError(f"{name} must be a non-empty tuple of literal strings")
    return value


@dataclass(frozen=True)
class InventoryReading:
    target_token: str
    visible_text: tuple[str, ...]
    panels: tuple[str, ...]
    garment_instances: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target_token, str) or not self.target_token:
            raise InventoryError("inventory target token must not be empty")
        _literal_tuple(self.visible_text, "visible text")
        _literal_tuple(self.panels, "panels")
        _literal_tuple(self.garment_instances, "garment instances")


@dataclass(frozen=True)
class InfographicInventory:
    target_token: str
    visible_text: tuple[str, ...]
    panels: tuple[str, ...]
    garment_instances: tuple[str, ...]
    reading_count: int
    adjudicated: bool
    settled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.target_token, str) or not self.target_token:
            raise InventoryError("inventory target token must not be empty")
        _literal_tuple(self.visible_text, "visible text")
        _literal_tuple(self.panels, "panels")
        _literal_tuple(self.garment_instances, "garment instances")
        if (not isinstance(self.reading_count, int)
                or isinstance(self.reading_count, bool)
                or self.reading_count != 2
                or not isinstance(self.adjudicated, bool)
                or self.settled is not True):
            raise InventoryError("inventory must be settled from exactly two readings")

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "visible_text": list(self.visible_text),
            "panels": list(self.panels),
            "garment_instances": list(self.garment_instances),
        }

    def plan_dict(self) -> dict[str, object]:
        """Return every field required to reconstruct the settled artifact."""
        return {
            "target_token": self.target_token,
            **self.to_dict(),
            "reading_count": self.reading_count,
            "adjudicated": self.adjudicated,
            "settled": self.settled,
        }


ReadInventory = Callable[[str], InventoryReading]
AdjudicateInventory = Callable[
    [str, InventoryReading, InventoryReading], InventoryReading,
]
GenerationResult = TypeVar("GenerationResult")


def _checked_reading(value: object, target_token: str) -> InventoryReading:
    if not isinstance(value, InventoryReading):
        raise InventoryError("inventory reader returned an invalid reading")
    if value.target_token != target_token:
        raise InventoryError("inventory reading belongs to a different target")
    return value


def settle_inventory(
        target_token: str, *, read: ReadInventory,
        adjudicate: AdjudicateInventory,
) -> InfographicInventory:
    """Settle exact literals and panel instances from two same-target readings."""
    if not isinstance(target_token, str) or not target_token:
        raise InventoryError("inventory target token must not be empty")
    if not callable(read) or not callable(adjudicate):
        raise InventoryError("inventory callbacks must be callable")

    first = _checked_reading(read(target_token), target_token)
    second = _checked_reading(read(target_token), target_token)
    adjudicated = first != second
    settled_reading = (
        _checked_reading(adjudicate(target_token, first, second), target_token)
        if adjudicated else first
    )
    return InfographicInventory(
        target_token=target_token,
        visible_text=settled_reading.visible_text,
        panels=settled_reading.panels,
        garment_instances=settled_reading.garment_instances,
        reading_count=2,
        adjudicated=adjudicated,
    )


def settle_then_generate(
        target_token: str, *, read: ReadInventory,
        adjudicate: AdjudicateInventory,
        paid_generate: Callable[[InfographicInventory], GenerationResult],
) -> GenerationResult:
    """Gate a paid generation callback on a successfully settled inventory."""
    if not callable(paid_generate):
        raise InventoryError("paid generation callback must be callable")
    inventory = settle_inventory(
        target_token, read=read, adjudicate=adjudicate,
    )
    if not inventory.settled:  # Defensive even though construction enforces it.
        raise InventoryError("paid generation requires a settled inventory")
    return paid_generate(inventory)
