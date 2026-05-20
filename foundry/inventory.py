from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from foundry.grid import Cell, Grid


@dataclass
class SKU:
    sku_id: str
    weight_kg: float = 1.0
    demand_class: str = "B"   # A / B / C


@dataclass
class Slot:
    sku_id: str
    rack_pos: tuple[int, int]   # the rack cell itself (not traversable)
    pick_pos: tuple[int, int]   # adjacent aisle cell where an agent stands to pick
    qty: int = 100              # current stock level


class Inventory:
    """Maps SKUs to rack slots and provides pick-position lookups."""

    def __init__(self, skus: list[SKU], slots: list[Slot]) -> None:
        self._skus: dict[str, SKU] = {s.sku_id: s for s in skus}
        self._slots: dict[str, Slot] = {s.sku_id: s for s in slots}

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, grid: Grid, skus: list[SKU], seed: int = 42) -> Inventory:
        """Randomly assign SKUs to accessible rack cells (default slotter).

        Only rack cells with at least one traversable neighbour are used —
        interior cells deep inside an 8-wide bay have no aisle face.
        """
        rack_cells = grid.cells_of_type(Cell.RACK)
        # Pre-filter to cells that actually have an aisle face
        accessible = [
            (rack, pp)
            for rack in rack_cells
            if (pp := _find_pick_pos(grid, rack)) is not None
        ]
        rng = random.Random(seed)
        rng.shuffle(accessible)

        slots: list[Slot] = [
            Slot(sku.sku_id, rack_pos, pick_pos)
            for i, sku in enumerate(skus)
            if i < len(accessible)
            for rack_pos, pick_pos in [accessible[i]]
        ]

        return cls(skus, slots)

    @classmethod
    def from_csv(cls, path: str | Path, grid: Grid, seed: int = 42) -> Inventory:
        """Load SKUs from CSV (columns: sku_id, weight_kg, demand_class)."""
        skus: list[SKU] = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                skus.append(
                    SKU(
                        sku_id=row["sku_id"],
                        weight_kg=float(row.get("weight_kg", 1.0)),
                        demand_class=row.get("demand_class", "B"),
                    )
                )
        return cls.build(grid, skus, seed=seed)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def sku_ids(self) -> list[str]:
        return list(self._skus.keys())

    def get_pick_pos(self, sku_id: str) -> Optional[tuple[int, int]]:
        slot = self._slots.get(sku_id)
        return slot.pick_pos if slot else None

    def get_sku(self, sku_id: str) -> Optional[SKU]:
        return self._skus.get(sku_id)

    def has_stock(self, sku_id: str, qty: int = 1) -> bool:
        slot = self._slots.get(sku_id)
        return slot is not None and slot.qty >= qty

    def deduct(self, sku_id: str, qty: int) -> None:
        slot = self._slots[sku_id]
        slot.qty = max(0, slot.qty - qty)

    def slot_count(self) -> int:
        return len(self._slots)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _find_pick_pos(grid: Grid, rack_pos: tuple[int, int]) -> Optional[tuple[int, int]]:
    """Return the nearest traversable neighbour of a rack cell."""
    r, c = rack_pos
    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        nr, nc = r + dr, c + dc
        if grid.traversable(nr, nc):
            return (nr, nc)
    return None
